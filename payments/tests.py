"""
TrustFlow — Payment & Escrow test suite
========================================

Covers all five troubleshooting scenarios WITHOUT hitting Nomba or DashScope:

1. "Buyer does not have a linked Nomba wallet"  → 400
2. hold_funds 502 (Nomba unavailable)           → 502
3. hold_funds 400/insufficient funds            → 402
4. create_virtual_wallet fails at registration  → user still created, wallet fields empty
5. AI draft 502 (DASHSCOPE_API_KEY missing)     → 502

Run:
    python manage.py test payments.tests
    python manage.py test escrow.tests
    python manage.py test users.tests

All network calls are intercepted with unittest.mock.patch.
"""

import json
from decimal import Decimal
from unittest.mock import patch, MagicMock

from django.test import TestCase
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from django.contrib.auth import get_user_model

from payments.services import (
    NombaPaymentService,
    NombaInsufficientFundsError,
    NombaUnavailableError,
    NombaAPIError,
)
from escrow.models import EscrowAgreement, Milestone

User = get_user_model()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_user(email="buyer@test.com", phone="+2348011111111", **kwargs):
    """Create a user with a pre-provisioned Nomba wallet for testing."""
    # Extract Nomba fields before passing to create_user (UserManager doesn't forward them)
    nomba_account_number = kwargs.pop("nomba_account_number", "0123456789")
    nomba_bank_code = kwargs.pop("nomba_bank_code", "NMB")
    nomba_account_ref = kwargs.pop("nomba_account_ref", "tf-user-test")

    user = User.objects.create_user(
        email=email,
        username=email.split("@")[0],
        phone_number=phone,
        password="testpass123",
        first_name="Test",
        last_name="User",
        **kwargs,
    )
    # Set Nomba wallet fields directly on the model instance
    user.nomba_account_number = nomba_account_number
    user.nomba_bank_code = nomba_bank_code
    user.nomba_account_ref = nomba_account_ref
    user.save(update_fields=["nomba_account_number", "nomba_bank_code", "nomba_account_ref"])
    return user


def auth_client(user):
    """Return an API client authenticated as `user`."""
    client = APIClient()
    refresh = RefreshToken.for_user(user)
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {refresh.access_token}")
    return client


def make_agreement(buyer, seller, status=EscrowAgreement.Status.DRAFT):
    return EscrowAgreement.objects.create(
        buyer=buyer,
        seller=seller,
        amount=Decimal("5000.00"),
        currency="NGN",
        raw_conditions="Deliver logo design in 3 days.",
        conditions=[],
        deadline=timezone.now() + timezone.timedelta(days=7),
        status=status,
    )


# ===========================================================================
# NombaPaymentService unit tests
# ===========================================================================

class NombaServiceTokenTest(TestCase):
    """Step 1 — Verify token fetch and caching."""

    def setUp(self):
        # Clear the module-level token cache between tests
        NombaPaymentService._token_cache.clear()

    @patch("payments.services.requests.post")
    def test_token_fetch_success(self, mock_post):
        """Token is fetched, cached, and returned correctly."""
        mock_post.return_value = MagicMock(
            status_code=200,
            json=lambda: {"data": {"access_token": "tok_abc", "expires_in": 3600}},
        )
        mock_post.return_value.raise_for_status = lambda: None

        svc = NombaPaymentService()
        token = svc._get_access_token()

        self.assertEqual(token, "tok_abc")
        # Second call should NOT hit the network (cache hit)
        token2 = svc._get_access_token()
        self.assertEqual(mock_post.call_count, 1, "Token should be cached after first fetch")
        self.assertEqual(token2, "tok_abc")

    @patch("payments.services.requests.post")
    def test_token_fetch_5xx_raises_unavailable(self, mock_post):
        """HTTP 5xx from Nomba token endpoint → NombaUnavailableError."""
        import requests as _requests

        mock_resp = MagicMock(status_code=502, text="Bad Gateway")
        mock_post.return_value = mock_resp
        mock_resp.raise_for_status.side_effect = _requests.HTTPError(
            response=mock_resp
        )

        svc = NombaPaymentService()
        with self.assertRaises(NombaUnavailableError):
            svc._get_access_token()

    @patch("payments.services.requests.post")
    def test_token_fetch_timeout_raises_unavailable(self, mock_post):
        """Timeout on token endpoint → NombaUnavailableError."""
        import requests as _requests
        mock_post.side_effect = _requests.Timeout()

        svc = NombaPaymentService()
        with self.assertRaises(NombaUnavailableError):
            svc._get_access_token()

    @patch("payments.services.requests.post")
    def test_token_fetch_bad_credentials_raises_api_error(self, mock_post):
        """HTTP 401 → NombaAPIError (bad credentials, not unavailability)."""
        import requests as _requests

        mock_resp = MagicMock(status_code=401, text="Unauthorized")
        mock_post.return_value = mock_resp
        mock_resp.raise_for_status.side_effect = _requests.HTTPError(
            response=mock_resp
        )

        svc = NombaPaymentService()
        with self.assertRaises(NombaAPIError):
            svc._get_access_token()


class NombaServiceHoldFundsTest(TestCase):
    """Step 3 & 4 — hold_funds error classification."""

    def setUp(self):
        NombaPaymentService._token_cache.clear()
        # Pre-fill the token cache so _get_access_token doesn't hit network
        NombaPaymentService._token_cache["706df6c4-b8bb-4130-88c4-d21b052f8631"] = {
            "access_token": "fake-token",
            "expires_at": 9999999999,
        }

    @patch("payments.services.requests.post")
    def test_hold_funds_success(self, mock_post):
        """hold_funds returns data dict on 200 and converts Naira to Kobo."""
        mock_post.return_value = MagicMock(
            status_code=200,
            json=lambda: {"data": {"reference": "ref-123", "status": "successful"}},
        )
        mock_post.return_value.raise_for_status = lambda: None

        svc = NombaPaymentService()
        # Pass 5000.00 Naira
        result = svc.hold_funds(5000.00, "0123456789", "NMB", "tf-1-1234567890")
        self.assertEqual(result["reference"], "ref-123")

        # Verify the API payload received the amount in Kobo (500000)
        args, kwargs = mock_post.call_args
        self.assertEqual(kwargs["json"]["amount"], 500000)

    @patch("payments.services.requests.post")
    def test_hold_funds_insufficient_balance_raises(self, mock_post):
        """Nomba 400 with 'insufficient balance' → NombaInsufficientFundsError."""
        import requests as _requests

        mock_resp = MagicMock(
            status_code=400,
            text='{"message": "Insufficient balance on wallet"}',
        )
        mock_post.return_value = mock_resp
        mock_resp.raise_for_status.side_effect = _requests.HTTPError(
            response=mock_resp
        )

        svc = NombaPaymentService()
        with self.assertRaises(NombaInsufficientFundsError):
            svc.hold_funds(999999, "0123456789", "NMB", "tf-1-xyz")

    @patch("payments.services.requests.post")
    def test_hold_funds_5xx_raises_unavailable(self, mock_post):
        """Nomba 502 → NombaUnavailableError."""
        import requests as _requests

        mock_resp = MagicMock(status_code=502, text="Bad Gateway")
        mock_post.return_value = mock_resp
        mock_resp.raise_for_status.side_effect = _requests.HTTPError(
            response=mock_resp
        )

        svc = NombaPaymentService()
        with self.assertRaises(NombaUnavailableError):
            svc.hold_funds(5000, "0123456789", "NMB", "tf-1-xyz")

    @patch("payments.services.requests.post")
    def test_hold_funds_timeout_raises_unavailable(self, mock_post):
        """Timeout during hold_funds → NombaUnavailableError."""
        import requests as _requests
        mock_post.side_effect = _requests.Timeout()

        svc = NombaPaymentService()
        with self.assertRaises(NombaUnavailableError):
            svc.hold_funds(5000, "0123456789", "NMB", "tf-1-xyz")


# ===========================================================================
# Escrow view integration tests
# ===========================================================================

class LockFundsViewTest(TestCase):
    """POST /api/escrow/{id}/lock-funds/ — new webhook-based flow."""

    def setUp(self):
        NombaPaymentService._token_cache.clear()
        self.buyer = make_user(email="buyer@trustflow.test", phone="+2348000000001", nomba_account_holder_id="buyer-uuid")
        self.seller = make_user(
            email="seller@trustflow.test",
            phone="+2348000000002",
            nomba_account_number="0987654321",
        )
        self.client = auth_client(self.buyer)

    def test_lock_funds_buyer_no_wallet_returns_400(self):
        """Buyer missing wallet → 400 with helpful message."""
        self.buyer.nomba_account_number = ""
        self.buyer.save()

        agreement = make_agreement(self.buyer, self.seller)
        resp = self.client.post(f"/api/escrow/{agreement.pk}/lock-funds/")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("wallet", resp.data["detail"].lower())
        self.assertIn("backfill", resp.data["detail"].lower())

    def test_lock_funds_success(self):
        """Successful lock-funds transitions to AWAITING_PAYMENT and returns payment instructions."""
        agreement = make_agreement(self.buyer, self.seller)
        resp = self.client.post(f"/api/escrow/{agreement.pk}/lock-funds/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        
        agreement.refresh_from_db()
        self.assertEqual(agreement.status, EscrowAgreement.Status.AWAITING_PAYMENT)
        self.assertTrue(agreement.nomba_transaction_ref.startswith("tf-"))

        self.assertIn("payment_instructions", resp.data)
        instructions = resp.data["payment_instructions"]
        self.assertEqual(instructions["bank_name"], "Nomba MFB")
        self.assertEqual(instructions["account_number"], self.buyer.nomba_account_number)
        self.assertEqual(instructions["amount"], str(agreement.amount))
        self.assertEqual(instructions["payment_reference"], agreement.nomba_transaction_ref)

    def test_lock_funds_only_buyer_can_lock(self):
        """Seller cannot call lock-funds on their own agreement."""
        agreement = make_agreement(self.buyer, self.seller)
        seller_client = auth_client(self.seller)
        resp = seller_client.post(f"/api/escrow/{agreement.pk}/lock-funds/")
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_lock_funds_idempotency_guard(self):
        """Cannot lock an already awaiting or active agreement."""
        agreement = make_agreement(self.buyer, self.seller, status=EscrowAgreement.Status.AWAITING_PAYMENT)
        resp = self.client.post(f"/api/escrow/{agreement.pk}/lock-funds/")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)


from django.test import override_settings

@override_settings(NOMBA_WEBHOOK_SECRET="test_webhook_secret_key")
class WebhookPaymentConfirmationTest(TestCase):
    """Webhook POST /api/payments/webhook/ — verifies collection credit matches and activates escrow."""

    def setUp(self):
        self.buyer = make_user(email="buyer3@trustflow.test", phone="+2348000000003", nomba_account_number="1122334455")
        self.seller = make_user(email="seller3@trustflow.test", phone="+2348000000004", nomba_account_number="5544332211")
        self.agreement = make_agreement(self.buyer, self.seller, status=EscrowAgreement.Status.AWAITING_PAYMENT)
        self.agreement.nomba_transaction_ref = "tf-tx-12345"
        self.agreement.save()
        self.client = APIClient()

    def test_webhook_collection_credit_activates_escrow(self):
        """collection.credit webhook transitions agreement from AWAITING_PAYMENT to ACTIVE."""
        payload = {
            "event": "collection.credit",
            "data": {
                "amount": 500000,  # 500000 kobo = ₦5,000.00
                "currency": "NGN",
                "bankAccountNumber": "1122334455",
                "accountRef": "tf-user-test",
                "reference": "payment-ref-abc-123"
            }
        }

        import hmac
        import hashlib
        payload_str = json.dumps(payload)
        payload_bytes = payload_str.encode("utf-8")
        signature = hmac.new(b"test_webhook_secret_key", payload_bytes, hashlib.sha256).hexdigest()

        resp = self.client.post(
            "/api/payments/webhook/",
            data=payload_str,
            content_type="application/json",
            HTTP_NOMBA_SIGNATURE=signature
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        self.agreement.refresh_from_db()
        self.assertEqual(self.agreement.status, EscrowAgreement.Status.ACTIVE)
        # Verify transaction reference is updated to the actual payment reference
        self.assertEqual(self.agreement.nomba_transaction_ref, "payment-ref-abc-123")

    def test_webhook_invalid_signature_returns_401(self):
        """Webhook with invalid signature must return 401 Unauthorized."""
        payload = {
            "event": "collection.credit",
            "data": {
                "amount": 500000,
                "currency": "NGN",
                "bankAccountNumber": "1122334455",
                "reference": "payment-ref-abc-123"
            }
        }
        resp = self.client.post(
            "/api/payments/webhook/",
            data=payload,
            format="json",
            HTTP_NOMBA_SIGNATURE="invalid_sig"
        )
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)


class ReleaseFundsViewTest(TestCase):
    """POST /api/escrow/{id}/release-funds/ — key scenarios."""

    def setUp(self):
        NombaPaymentService._token_cache.clear()
        self.buyer = make_user(email="buyer2@trustflow.test", phone="+2348000000003")
        self.seller = make_user(
            email="seller2@trustflow.test",
            phone="+2348000000004",
            nomba_account_number="0987654321",
        )
        self.client = auth_client(self.buyer)

    @patch("payments.services.NombaPaymentService.release_to_seller")
    def test_release_funds_success(self, mock_release):
        """Successful release → agreement moves to COMPLETED."""
        mock_release.return_value = {"status": "successful"}

        agreement = make_agreement(
            self.buyer, self.seller, status=EscrowAgreement.Status.ACTIVE
        )
        agreement.nomba_transaction_ref = "tf-1-ref"
        agreement.save()

        resp = self.client.post(f"/api/escrow/{agreement.pk}/release-funds/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        agreement.refresh_from_db()
        self.assertEqual(agreement.status, EscrowAgreement.Status.COMPLETED)

    @patch("payments.services.NombaPaymentService.release_to_seller")
    def test_release_funds_nomba_unavailable_returns_502(self, mock_release):
        """NombaUnavailableError on release → 502."""
        mock_release.side_effect = NombaUnavailableError("Sandbox down")

        agreement = make_agreement(
            self.buyer, self.seller, status=EscrowAgreement.Status.ACTIVE
        )
        agreement.nomba_transaction_ref = "tf-1-ref"
        agreement.save()

        resp = self.client.post(f"/api/escrow/{agreement.pk}/release-funds/")
        self.assertEqual(resp.status_code, status.HTTP_502_BAD_GATEWAY)
        agreement.refresh_from_db()
        # Agreement must NOT be marked completed when payout fails
        self.assertEqual(agreement.status, EscrowAgreement.Status.ACTIVE)

    def test_release_funds_seller_no_wallet_returns_400(self):
        """Seller without wallet → 400 before hitting Nomba."""
        self.seller.nomba_account_number = ""
        self.seller.save()

        agreement = make_agreement(
            self.buyer, self.seller, status=EscrowAgreement.Status.ACTIVE
        )
        resp = self.client.post(f"/api/escrow/{agreement.pk}/release-funds/")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)


# ===========================================================================
# AI draft view tests
# ===========================================================================

class DraftWithAIViewTest(TestCase):
    """POST /api/escrow/draft-with-ai/ — AI failure scenarios."""

    def setUp(self):
        self.buyer = make_user(email="buyer3@trustflow.test", phone="+2348000000005")
        self.seller = make_user(email="seller3@trustflow.test", phone="+2348000000006")
        self.client = auth_client(self.buyer)

    @patch("escrow.views.QwenAIService")
    def test_draft_with_ai_success(self, MockAI):
        """AI returns structured milestones → agreement created."""
        MockAI.return_value.parse_conditions.return_value = {
            "milestones": [
                {"description": "Deliver logo", "verifiable": True, "deadline_hint": "3 days"},
            ]
        }

        resp = self.client.post(
            "/api/escrow/draft-with-ai/",
            data={
                "seller_id": str(self.seller.pk),
                "amount": "5000.00",
                "currency": "NGN",
                "raw_conditions": "Deliver the logo in 3 days.",
                "deadline": (timezone.now() + timezone.timedelta(days=7)).isoformat(),
            },
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertEqual(EscrowAgreement.objects.count(), 1)
        self.assertEqual(Milestone.objects.count(), 1)

    @patch("escrow.views.QwenAIService")
    def test_draft_with_ai_dashscope_missing_returns_502(self, MockAI):
        """If QwenAIService raises (e.g. missing API key) → 502."""
        MockAI.side_effect = ValueError("DASHSCOPE_API_KEY is not set.")

        resp = self.client.post(
            "/api/escrow/draft-with-ai/",
            data={
                "seller_id": str(self.seller.pk),
                "amount": "5000.00",
                "currency": "NGN",
                "raw_conditions": "Deliver the logo in 3 days.",
                "deadline": (timezone.now() + timezone.timedelta(days=7)).isoformat(),
            },
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_502_BAD_GATEWAY)

    @patch("escrow.views.QwenAIService")
    def test_draft_with_ai_parse_failure_returns_502(self, MockAI):
        """Network error from DashScope → 502."""
        MockAI.return_value.parse_conditions.side_effect = Exception("DashScope timeout")

        resp = self.client.post(
            "/api/escrow/draft-with-ai/",
            data={
                "seller_id": str(self.seller.pk),
                "amount": "5000.00",
                "currency": "NGN",
                "raw_conditions": "Deliver the logo in 3 days.",
                "deadline": (timezone.now() + timezone.timedelta(days=7)).isoformat(),
            },
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_502_BAD_GATEWAY)


# ===========================================================================
# Registration — wallet provisioning failure
# ===========================================================================

class RegistrationWalletTest(TestCase):
    """Step 2 & 3 — Wallet provisioning failure at registration is non-fatal."""

    @patch("users.serializers.NombaPaymentService")
    def test_registration_succeeds_even_if_wallet_fails(self, MockNomba):
        """User is created even when Nomba wallet provisioning fails."""
        MockNomba.return_value.create_virtual_wallet.side_effect = NombaUnavailableError(
            "Sandbox unreachable"
        )

        client = APIClient()
        resp = client.post(
            "/api/auth/register/",
            data={
                "email": "newuser@test.com",
                "phone_number": "+2348099999999",
                "password": "Str0ngPass!",
                "first_name": "New",
                "last_name": "User",
            },
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)

        # User must exist in DB
        user = User.objects.get(email="newuser@test.com")
        # Wallet fields must be empty (not provisioned)
        self.assertEqual(user.nomba_account_number, "")
        self.assertFalse(user.has_nomba_account)

    @patch("users.serializers.NombaPaymentService")
    def test_registration_with_wallet_success(self, MockNomba):
        """When Nomba succeeds, wallet fields are populated on the user."""
        MockNomba.return_value.create_virtual_wallet.return_value = {
            "accountRef": "tf-user-testid",
            "bankAccountNumber": "0123456789",
            "bankCode": "NMB",
        }

        client = APIClient()
        resp = client.post(
            "/api/auth/register/",
            data={
                "email": "walletuser@test.com",
                "phone_number": "+2348077777777",
                "password": "Str0ngPass!",
                "first_name": "Wallet",
                "last_name": "User",
            },
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)

        user = User.objects.get(email="walletuser@test.com")
        self.assertEqual(user.nomba_account_number, "0123456789")
        self.assertTrue(user.has_nomba_account)


# ===========================================================================
# Backfill management command
# ===========================================================================

class BackfillNombaWalletsCommandTest(TestCase):
    """Management command: backfill_nomba_wallets."""

    def setUp(self):
        NombaPaymentService._token_cache.clear()
        # User without wallet
        self.user_no_wallet = User.objects.create_user(
            email="nowallet@test.com",
            username="nowallet",
            phone_number="+2348011111110",
            password="pass",
        )
        self.user_no_wallet.nomba_account_number = ""
        self.user_no_wallet.save(update_fields=["nomba_account_number"])

        # User with wallet (should be skipped)
        self.user_with_wallet = User.objects.create_user(
            email="haswallet@test.com",
            username="haswallet",
            phone_number="+2348011111111",
            password="pass",
        )
        self.user_with_wallet.nomba_account_number = "0123456789"
        self.user_with_wallet.save(update_fields=["nomba_account_number"])

    @patch("users.management.commands.backfill_nomba_wallets.NombaPaymentService")
    def test_backfill_skips_users_with_wallet(self, MockNomba):
        """Only users with empty nomba_account_number are targeted."""
        MockNomba.return_value.create_virtual_wallet.return_value = {
            "accountRef": "tf-user-nowallet",
            "bankAccountNumber": "0000000001",
            "bankCode": "NMB",
        }

        from django.core.management import call_command
        from io import StringIO
        out = StringIO()
        call_command("backfill_nomba_wallets", stdout=out)

        # Only the no-wallet user should have been provisioned
        self.user_no_wallet.refresh_from_db()
        self.assertEqual(self.user_no_wallet.nomba_account_number, "0000000001")

        # User with wallet must be unchanged
        self.user_with_wallet.refresh_from_db()
        self.assertEqual(self.user_with_wallet.nomba_account_number, "0123456789")

        self.assertEqual(MockNomba.return_value.create_virtual_wallet.call_count, 1)

    @patch("users.management.commands.backfill_nomba_wallets.NombaPaymentService")
    def test_backfill_dry_run_does_not_save(self, MockNomba):
        """--dry-run mode lists users but makes no API calls or DB writes."""
        from django.core.management import call_command
        from io import StringIO
        out = StringIO()
        call_command("backfill_nomba_wallets", dry_run=True, stdout=out)

        MockNomba.assert_not_called()
        self.user_no_wallet.refresh_from_db()
        self.assertEqual(self.user_no_wallet.nomba_account_number, "")

    @patch("users.management.commands.backfill_nomba_wallets.NombaPaymentService")
    def test_backfill_nomba_error_does_not_crash_command(self, MockNomba):
        """NombaError during provisioning is logged but command continues."""
        MockNomba.return_value.create_virtual_wallet.side_effect = NombaUnavailableError(
            "Sandbox down"
        )

        from django.core.management import call_command
        from io import StringIO
        err = StringIO()
        # Should not raise
        call_command("backfill_nomba_wallets", stderr=err)

        self.user_no_wallet.refresh_from_db()
        self.assertEqual(self.user_no_wallet.nomba_account_number, "")
