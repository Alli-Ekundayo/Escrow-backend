"""
NombaPaymentService — OAuth2-authenticated wrapper around the Nomba Payments API.

Authentication flow (client-credentials):
  1. POST /v1/auth/token/issue with client_id + client_secret in the body,
     and the *parent* account ID in the `accountId` header.
  2. Cache the returned access_token until it expires.
  3. Scope all subsequent calls to the sub-account via the `accountId` header.

Docs: https://developer.nomba.com
"""

import logging
import time
import threading
import uuid

import requests
from django.conf import settings

logger = logging.getLogger(__name__)


class NombaPaymentService:
    # Base URLs (Nomba sandbox vs. production)
    _PRODUCTION_BASE = "https://api.nomba.com/v1"
    _SANDBOX_BASE = "https://sandbox.nomba.com/v1"

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

        # The parent account ID is used to *authenticate* (token endpoint header).
        self.parent_account_id = settings.NOMBA_PARENT_ACCOUNT_ID
        # The sub-account ID scopes all business operations.
        self.sub_account_id = settings.NOMBA_SUB_ACCOUNT_ID

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
            # Nomba tokens typically live for 3600 s; fall back gracefully.
            expires_in = token_data.get("expiresIn", token_data.get("expires_in", 3600))
            self._token_cache[cache_key] = {
                "access_token": token_data["accessToken"],
                "expires_at": time.time() + int(expires_in),
            }
            logger.info("Nomba: obtained new access token (expires_in=%s s)", expires_in)
            return self._token_cache[cache_key]["access_token"]

    def _fetch_token(self) -> dict:
        """POST to Nomba's token-issuance endpoint and return the response body."""
        url = f"{self.base_url}/auth/token/issue"
        headers = {
            "Content-Type": "application/json",
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
            return resp.json()
        except requests.HTTPError as exc:
            logger.error(
                "Nomba token fetch failed: %s — %s",
                exc.response.status_code,
                exc.response.text,
            )
            raise

    def _auth_headers(self) -> dict:
        """Build headers for an authenticated, sub-account-scoped request."""
        return {
            "Authorization": f"Bearer {self._get_access_token()}",
            # Scope all operations to the sub-account
            "accountId": self.sub_account_id,
            "Content-Type": "application/json",
        }

    # -----------------------------------------------------------------------
    # Wallet management
    # -----------------------------------------------------------------------

    def create_virtual_wallet(self, user_id: str) -> dict:
        """
        Creates a dedicated virtual account / wallet for a TrustFlow user.

        Args:
            user_id: Internal UUID of the user (used as a reference).

        Returns:
            Nomba API response dict (contains walletId, accountNumber, etc.)
        """
        payload = {
            "reference": f"trustflow-user-{user_id}",
            "customerId": str(user_id),
        }
        return self._post("/accounts/virtual", payload)

    # -----------------------------------------------------------------------
    # Fund lifecycle
    # -----------------------------------------------------------------------

    def hold_funds(self, amount: float, buyer_wallet_id: str, ref: str) -> dict:
        """
        Transfers buyer funds into the TrustFlow escrow holding account.

        Args:
            amount: Amount in NGN (or configured currency).
            buyer_wallet_id: Nomba wallet ID of the buyer.
            ref: Unique escrow agreement reference.

        Returns:
            Nomba transaction response.
        """
        payload = {
            "amount": amount,
            "sourceAccountId": buyer_wallet_id,
            "destinationAccountId": self.sub_account_id,
            "reference": ref,
            "narration": f"TrustFlow escrow lock — {ref}",
        }
        return self._post("/transfers/initiate", payload)

    def release_to_seller(self, amount: float, seller_wallet_id: str, ref: str) -> dict:
        """
        Releases held funds from the escrow account to the seller.

        Args:
            amount: Amount to release in NGN.
            seller_wallet_id: Nomba wallet ID of the seller.
            ref: Unique escrow agreement reference.

        Returns:
            Nomba transaction response.
        """
        payload = {
            "amount": amount,
            "sourceAccountId": self.sub_account_id,
            "destinationAccountId": seller_wallet_id,
            "reference": f"{ref}-release",
            "narration": f"TrustFlow payout — {ref}",
        }
        return self._post("/transfers/initiate", payload)

    def refund_to_buyer(self, amount: float, buyer_wallet_id: str, ref: str) -> dict:
        """
        Refunds held funds back to the buyer (cancelled or buyer-ruled dispute).

        Args:
            amount: Amount to refund in NGN.
            buyer_wallet_id: Nomba wallet ID of the buyer.
            ref: Unique escrow agreement reference.

        Returns:
            Nomba transaction response.
        """
        payload = {
            "amount": amount,
            "sourceAccountId": self.sub_account_id,
            "destinationAccountId": buyer_wallet_id,
            "reference": f"{ref}-refund",
            "narration": f"TrustFlow refund — {ref}",
        }
        return self._post("/transfers/initiate", payload)

    # -----------------------------------------------------------------------
    # Webhook handler
    # -----------------------------------------------------------------------

    def handle_webhook(self, payload: dict) -> None:
        """
        Processes an incoming payment event from Nomba.

        Dispatches to the appropriate internal handler based on event type.
        Extend this method as Nomba adds new event types.
        """
        event_type = payload.get("event", "")
        reference = payload.get("data", {}).get("reference", "")

        logger.info("Nomba webhook received: event=%s ref=%s", event_type, reference)

        if event_type == "transfer.success":
            self._on_transfer_success(payload["data"])
        elif event_type == "transfer.failed":
            self._on_transfer_failed(payload["data"])
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
        except requests.HTTPError as exc:
            logger.error(
                "Nomba API error: %s %s — %s",
                exc.response.status_code,
                url,
                exc.response.text,
            )
            raise

    def _on_transfer_success(self, data: dict) -> None:
        """Hook: update EscrowAgreement status when Nomba confirms a transfer."""
        from escrow.models import EscrowAgreement

        ref = data.get("reference", "")
        logger.info("Transfer success for ref: %s", ref)

        # Strip suffixes added during release/refund so we can look up the agreement
        base_ref = ref.replace("-release", "").replace("-refund", "")
        EscrowAgreement.objects.filter(nomba_transaction_ref=base_ref).update(
            status="active" if ref == base_ref else None  # release handled by view
        )

    def _on_transfer_failed(self, data: dict) -> None:
        """Hook: log failures; could trigger alerts or retry logic."""
        logger.error("Nomba transfer failed: %s", data)
