"""
NombaPaymentService — OAuth2-authenticated wrapper around the Nomba Payments API.

Authentication flow (client-credentials):
  1. POST /v1/auth/token/issue with client_id + client_secret in the body,
     and the *parent* account ID in the `accountId` header.
  2. Cache the returned access_token (response is wrapped: { "data": {...} }).
  3. Scope all subsequent calls to the parent account via the `accountId` header.
     Sub-account transfers use the sub-account ID in the *URL path*, not the header.

Error handling:
  - NombaUnavailableError  → Nomba sandbox/prod is unreachable (HTTP 5xx / timeout).
  - NombaInsufficientFundsError → Nomba 400 "insufficient balance".
  - NombaAPIError          → Any other Nomba-side error (credentials bad, etc.).

Docs: https://developer.nomba.com
"""

import logging
import time
import threading
import uuid

import requests
from django.conf import settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Custom exception hierarchy
# ---------------------------------------------------------------------------

class NombaError(Exception):
    """Base class for all Nomba-related errors."""


class NombaUnavailableError(NombaError):
    """Raised when Nomba is unreachable or returns a 5xx response."""


class NombaInsufficientFundsError(NombaError):
    """Raised when Nomba returns HTTP 400 due to insufficient balance."""


class NombaAPIError(NombaError):
    """Raised for other Nomba 4xx errors (bad credentials, bad request, etc.)."""


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

class NombaPaymentService:
    # Base URLs (Nomba sandbox vs. production)
    _PRODUCTION_BASE = "https://api.nomba.com"
    _SANDBOX_BASE = "https://sandbox.nomba.com"

    # Module-level token cache (shared across instances within the process)
    _token_cache: dict = {}
    _token_lock = threading.Lock()

    # -----------------------------------------------------------------------
    # Initialisation
    # -----------------------------------------------------------------------

    def __init__(self):
        test_mode = getattr(settings, "NOMBA_TEST_MODE", True)

        if test_mode:
            self.base_url = self._SANDBOX_BASE
            self.client_id = settings.NOMBA_TEST_CLIENT_ID
            self.client_secret = settings.NOMBA_TEST_CLIENT_SECRET
        else:
            self.base_url = self._PRODUCTION_BASE
            self.client_id = settings.NOMBA_LIVE_CLIENT_ID
            self.client_secret = settings.NOMBA_LIVE_CLIENT_SECRET

        # Fail fast if credentials are missing — surface config errors early.
        if not self.client_id or not self.client_secret:
            mode = "TEST" if test_mode else "LIVE"
            raise NombaAPIError(
                f"Nomba {mode} credentials are not configured in settings. "
                f"Set NOMBA_{mode}_CLIENT_ID and NOMBA_{mode}_CLIENT_SECRET in .env."
            )

        # The parent account ID is used to authenticate AND scope all API calls
        # via the `accountId` header.
        self.parent_account_id = settings.NOMBA_PARENT_ACCOUNT_ID
        # The sub-account ID is used in URL paths for sub-account transfers.
        self.sub_account_id = settings.NOMBA_SUB_ACCOUNT_ID

        if not self.parent_account_id:
            raise NombaAPIError(
                "NOMBA_PARENT_ACCOUNT_ID is not configured. Add it to your .env file."
            )

    # -----------------------------------------------------------------------
    # OAuth2 token management
    # -----------------------------------------------------------------------

    def _get_access_token(self) -> str:
        """
        Returns a valid access token, fetching a new one if the cached
        token is absent or within 60 seconds of expiry.
        """
        cache_key = self.client_id

        with self._token_lock:
            cached = self._token_cache.get(cache_key)
            if cached and cached["expires_at"] - time.time() > 60:
                return cached["access_token"]

            token_data = self._fetch_token()
            # Nomba sandbox returns { "data": { "access_token": "...", "expiresAt": "..." } }
            expires_in = token_data.get("expires_in", 3600)
            self._token_cache[cache_key] = {
                "access_token": token_data["access_token"],
                "expires_at": time.time() + int(expires_in),
            }
            logger.info("Nomba: obtained new access token (expires_in=%s s)", expires_in)
            return self._token_cache[cache_key]["access_token"]

    def _fetch_token(self) -> dict:
        """POST to Nomba's token-issuance endpoint and return the inner data dict."""
        url = f"{self.base_url}/v1/auth/token/issue"
        headers = {
            "Content-Type": "application/json",
            # Parent account ID is required for token issuance
            "accountId": self.parent_account_id,
        }
        payload = {
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=20)
            resp.raise_for_status()
            body = resp.json()
            # Nomba wraps the token inside a "data" key
            data = body.get("data", body)
            if not data.get("access_token"):
                raise NombaAPIError(f"Nomba token response missing access_token: {body}")
            return data
        except requests.Timeout:
            logger.error("Nomba token fetch timed out (sandbox unreachable?)")
            raise NombaUnavailableError(
                "Nomba sandbox is unreachable (timeout). Check NOMBA_TEST_MODE and network."
            )
        except requests.ConnectionError as exc:
            logger.error("Nomba token fetch connection error: %s", exc)
            raise NombaUnavailableError(
                "Cannot connect to Nomba. Check network and NOMBA_TEST_MODE setting."
            ) from exc
        except requests.HTTPError as exc:
            status_code = exc.response.status_code
            body = exc.response.text
            logger.error(
                "Nomba token fetch failed: %s — %s", status_code, body
            )
            if status_code >= 500:
                raise NombaUnavailableError(
                    f"Nomba returned HTTP {status_code}. Sandbox may be down."
                ) from exc
            # 4xx → likely bad credentials
            raise NombaAPIError(
                f"Nomba rejected credentials (HTTP {status_code}): {body}"
            ) from exc

    def _auth_headers(self) -> dict:
        """Build headers for an authenticated request (parent account scope)."""
        return {
            "Authorization": f"Bearer {self._get_access_token()}",
            # All API calls are scoped to the parent account via this header.
            # Sub-account operations use the sub-account ID in the URL path instead.
            "accountId": self.parent_account_id,
            "Content-Type": "application/json",
        }

    # -----------------------------------------------------------------------
    # Wallet / virtual account management
    # -----------------------------------------------------------------------

    def create_virtual_wallet(self, user_id: str, account_name: str = "") -> dict:
        """
        Creates a dedicated virtual bank account for a TrustFlow user.

        Nomba assigns a real bank account number (Nombank MFB) that can receive
        transfers from any Nigerian bank.

        Args:
            user_id:      Internal UUID of the user (used as accountRef).
            account_name: Display name on the virtual account (min 8 chars).

        Returns:
            Nomba API `data` dict containing:
              - bankAccountNumber  (10-digit NUBAN)
              - bankAccountName
              - bankName
              - accountRef
              - accountHolderId
        """
        # accountRef must be 16–64 chars; prefix guarantees uniqueness.
        # Pad with zeros if the user ID is a short string/integer.
        account_ref = f"tf-user-{user_id}"
        if len(account_ref) < 16:
            account_ref = f"tf-user-{str(user_id).zfill(8)}"
        account_ref = account_ref[:64]

        display_name = account_name.strip() or f"TrustFlow User {str(user_id)[:8]}"
        # Ensure minimum 8 chars required by Nomba
        if len(display_name) < 8:
            display_name = display_name.ljust(8)

        payload = {
            "accountRef": account_ref,
            "accountName": display_name,
            "currency": "NGN",
        }
        response = self._post("/v1/accounts/virtual", payload)
        # Nomba wraps response in { "data": {...} }
        return response.get("data", response)

    def get_account_balance(self, account_id: str) -> dict:
        """
        Returns the balance for a given Nomba account ID.
        """
        url = f"{self.base_url}/v1/accounts/balance"
        try:
            headers = self._auth_headers()
            headers["accountId"] = account_id
            resp = requests.get(url, headers=headers, timeout=20)
            resp.raise_for_status()
            return resp.json().get("data", resp.json())
        except requests.Timeout:
            raise NombaUnavailableError("Nomba balance check timed out.")
        except requests.HTTPError as exc:
            self._raise_for_nomba_error(exc)

    # -----------------------------------------------------------------------
    # Fund lifecycle  (transfers via sub-account)
    # -----------------------------------------------------------------------

    def hold_funds(
        self,
        amount: float,
        buyer_account_number: str,
        buyer_bank_code: str,
        ref: str,
    ) -> dict:
        """
        Collects buyer funds into the TrustFlow escrow sub-account via bank transfer.

        In the Nomba model, "holding" funds means the buyer transfers to the
        TrustFlow sub-account's bank account number (external bank → sub-account).
        This is typically triggered by a webhook when Nomba confirms receipt.

        For direct sub-account-to-sub-account transfers (same Nomba business),
        use /v2/transfers/bank/{subAccountId}.

        Args:
            amount:               Amount in NGN (minor units — kobo — for some endpoints).
            buyer_account_number: Buyer's bank account number.
            buyer_bank_code:      Buyer's bank code (CBN code, e.g. "058" for GTBank).
            ref:                  Unique escrow agreement reference (idempotency key).

        Returns:
            Nomba transfer response data dict.

        Raises:
            NombaInsufficientFundsError: If buyer's wallet has insufficient balance.
            NombaUnavailableError:       If Nomba is unreachable or returns 5xx.
            NombaAPIError:               For other Nomba errors.
        """
        payload = {
            "amount": int(round(float(amount) * 100)),
            "accountNumber": buyer_account_number,
            "bankCode": buyer_bank_code,
            "merchantTxRef": ref,
            "narration": f"TrustFlow escrow lock — {ref}",
            "senderName": "TrustFlow Escrow",
        }
        response = self._post(f"/v2/transfers/bank/{self.sub_account_id}", payload)
        return response.get("data", response)

    def release_to_seller(
        self,
        amount: float,
        seller_account_number: str,
        seller_bank_code: str,
        ref: str,
        source_account_id: str = None,
    ) -> dict:
        """
        Releases held funds from the virtual account (or sub-account) to the seller.

        Args:
            amount:                Amount in NGN.
            seller_account_number: Seller's bank account number.
            seller_bank_code:      Seller's bank code.
            ref:                   Original escrow agreement reference.
            source_account_id:     Source account UUID (e.g. buyer's accountHolderId).
                                   Defaults to sub_account_id.

        Returns:
            Nomba transfer response data dict.
        """
        source = source_account_id or self.sub_account_id
        bank_code = seller_bank_code
        if bank_code == "NMB":
            bank_code = "101"

        account_name = "TrustFlow Recipient"
        try:
            lookup_payload = {
                "accountNumber": seller_account_number,
                "bankCode": bank_code
            }
            lookup_resp = self._post("/v1/transfers/bank/lookup", lookup_payload)
            resolved_name = lookup_resp.get("data", {}).get("accountName")
            if resolved_name:
                account_name = resolved_name
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning("Nomba account lookup failed: %s", exc)

        payload = {
            "amount": int(round(float(amount) * 100)),
            "accountNumber": seller_account_number,
            "bankCode": bank_code,
            "accountName": account_name,
            "merchantTxRef": f"{ref}-release",
            "narration": f"TrustFlow payout — {ref}",
            "senderName": "TrustFlow Escrow",
        }
        response = self._post(f"/v2/transfers/bank/{source}", payload)
        return response.get("data", response)

    def refund_to_buyer(
        self,
        amount: float,
        buyer_account_number: str,
        buyer_bank_code: str,
        ref: str,
        source_account_id: str = None,
    ) -> dict:
        """
        Refunds held funds back to the buyer (cancelled or buyer-won dispute).

        Args:
            amount:               Amount in NGN.
            buyer_account_number: Buyer's bank account number.
            buyer_bank_code:      Buyer's bank code.
            ref:                  Original escrow agreement reference.
            source_account_id:    Source account UUID (e.g. buyer's accountHolderId).
                                  Defaults to sub_account_id.

        Returns:
            Nomba transfer response data dict.
        """
        source = source_account_id or self.sub_account_id
        bank_code = buyer_bank_code
        if bank_code == "NMB":
            bank_code = "101"

        account_name = "TrustFlow Recipient"
        try:
            lookup_payload = {
                "accountNumber": buyer_account_number,
                "bankCode": bank_code
            }
            lookup_resp = self._post("/v1/transfers/bank/lookup", lookup_payload)
            resolved_name = lookup_resp.get("data", {}).get("accountName")
            if resolved_name:
                account_name = resolved_name
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning("Nomba account lookup failed: %s", exc)

        payload = {
            "amount": int(round(float(amount) * 100)),
            "accountNumber": buyer_account_number,
            "bankCode": bank_code,
            "accountName": account_name,
            "merchantTxRef": f"{ref}-refund",
            "narration": f"TrustFlow refund — {ref}",
            "senderName": "TrustFlow Escrow",
        }
        response = self._post(f"/v2/transfers/bank/{source}", payload)
        return response.get("data", response)

    # -----------------------------------------------------------------------
    # Webhook handler
    # -----------------------------------------------------------------------

    def handle_webhook(self, payload: dict) -> None:
        """
        Processes an incoming payment event from Nomba.

        Dispatches to the appropriate internal handler based on event type.
        Extend this method as Nomba adds new event types.

        Common event types:
          - transfer.success
          - transfer.failed
          - collection.credit  (virtual account received funds)
        """
        event_type = payload.get("event", "")
        reference = payload.get("data", {}).get("reference", "") or \
                    payload.get("data", {}).get("merchantTxRef", "")

        logger.info("Nomba webhook received: event=%s ref=%s", event_type, reference)

        if event_type in ("collection.credit", "virtual_account.funded", "payment_success"):
            self._on_collection_credit(payload.get("data", {}))
        elif event_type == "transfer.success":
            self._on_transfer_success(payload.get("data", {}))
        elif event_type == "transfer.failed":
            self._on_transfer_failed(payload.get("data", {}))
        else:
            logger.warning("Unhandled Nomba event type: %s", event_type)

    # -----------------------------------------------------------------------
    # Private helpers
    # -----------------------------------------------------------------------

    def _post(self, path: str, payload: dict) -> dict:
        url = f"{self.base_url}{path}"
        try:
            resp = requests.post(
                url,
                json=payload,
                headers=self._auth_headers(),
                timeout=20,
            )
            resp.raise_for_status()
            return resp.json()
        except requests.Timeout:
            logger.error("Nomba API timeout: %s", url)
            raise NombaUnavailableError(
                f"Nomba request to {path} timed out. Sandbox may be unreachable."
            )
        except requests.ConnectionError as exc:
            logger.error("Nomba connection error: %s — %s", url, exc)
            raise NombaUnavailableError(
                f"Cannot connect to Nomba ({path}). Check network connectivity."
            ) from exc
        except requests.HTTPError as exc:
            self._raise_for_nomba_error(exc, url=url)

    @staticmethod
    def _raise_for_nomba_error(exc: requests.HTTPError, url: str = "") -> None:
        """
        Converts an HTTPError from Nomba into a typed NombaError subclass.

        - HTTP 400 with "insufficient" in body → NombaInsufficientFundsError
        - HTTP 5xx or timeout                  → NombaUnavailableError
        - Other 4xx                            → NombaAPIError
        """
        status_code = exc.response.status_code
        body = exc.response.text
        logger.error("Nomba API error: %s %s — %s", status_code, url, body)

        if status_code >= 500:
            raise NombaUnavailableError(
                f"Nomba returned HTTP {status_code}. Sandbox may be down or overloaded."
            ) from exc

        # 400 can indicate insufficient funds
        if status_code == 400:
            body_lower = body.lower()
            if any(kw in body_lower for kw in ("insufficient", "balance", "funds")):
                raise NombaInsufficientFundsError(
                    "Insufficient balance on the Nomba virtual account. "
                    "Fund the virtual account via the Nomba dashboard or API before retrying."
                ) from exc

        raise NombaAPIError(
            f"Nomba API error (HTTP {status_code}): {body}"
        ) from exc

    def _on_transfer_success(self, data: dict) -> None:
        """Hook: update EscrowAgreement status when Nomba confirms a transfer."""
        from escrow.models import EscrowAgreement

        ref = data.get("merchantTxRef") or data.get("reference", "")
        logger.info("Transfer success for ref: %s", ref)

        # Strip suffixes added during release/refund/split so we can look up the agreement
        base_ref = ref
        for suffix in ["-release", "-refund", "-split-buyer", "-split-seller"]:
            base_ref = base_ref.replace(suffix, "")

        if base_ref:
            status_target = EscrowAgreement.Status.COMPLETED
            if "-refund" in ref:
                status_target = EscrowAgreement.Status.REFUNDED
            
            EscrowAgreement.objects.filter(
                nomba_transaction_ref=base_ref,
            ).update(status=status_target)

    def _on_transfer_failed(self, data: dict) -> None:
        """Hook: log failures; could trigger alerts or retry logic."""
        logger.error("Nomba transfer failed: %s", data)

    def _on_collection_credit(self, data: dict) -> None:
        """
        Hook: Handles incoming funds into a buyer's virtual account.
        Finds the user matching the account number or reference,
        finds their EscrowAgreement in AWAITING_PAYMENT, and transitions to ACTIVE.
        """
        from escrow.models import EscrowAgreement
        from django.contrib.auth import get_user_model
        from decimal import Decimal
        User = get_user_model()

        account_number = data.get("bankAccountNumber") or data.get("accountNumber")
        account_ref = data.get("accountRef")
        amount = data.get("amount") or data.get("amountReceived")
        payment_ref = data.get("paymentRef") or data.get("reference") or data.get("merchantTxRef", "")

        logger.info(
            "Collection credit received: account=%s, ref=%s, amount=%s, payment_ref=%s",
            account_number, account_ref, amount, payment_ref
        )

        if not account_number and not account_ref:
            logger.error("Missing account number and account ref in webhook data.")
            return

        # Find the buyer user
        buyer = None
        if account_number:
            buyer = User.objects.filter(nomba_account_number=account_number).first()
        if not buyer and account_ref:
            buyer = User.objects.filter(nomba_account_ref=account_ref).first()

        if not buyer:
            logger.error("No user found with Nomba account %s or ref %s", account_number, account_ref)
            return

        try:
            # Convert incoming Kobo to Naira Decimal
            target_amount = Decimal(str(amount)) / 100
        except (TypeError, ValueError):
            logger.error("Invalid amount in webhook data: %s", amount)
            return

        agreements = EscrowAgreement.objects.filter(
            buyer=buyer,
            status=EscrowAgreement.Status.AWAITING_PAYMENT
        )

        matched_agreement = None
        if target_amount:
            # Try to match the amount exactly
            matched_agreement = agreements.filter(amount=target_amount).first()
            if not matched_agreement:
                # If only one awaiting payment, match it anyway (e.g. currency conv minor units etc)
                if agreements.count() == 1:
                    matched_agreement = agreements.first()
        else:
            matched_agreement = agreements.first()

        if not matched_agreement:
            logger.warning(
                "No AWAITING_PAYMENT escrow agreement found for buyer %s with amount %s",
                buyer.email, target_amount
            )
            return

        # Transition the agreement to ACTIVE
        matched_agreement.status = EscrowAgreement.Status.ACTIVE
        matched_agreement.nomba_transaction_ref = payment_ref
        matched_agreement.save()
        logger.info(
            "EscrowAgreement %s marked ACTIVE based on virtual account credit. Ref=%s",
            matched_agreement.id, payment_ref
        )
