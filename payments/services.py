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

    # Nomba MFB's own bank code, looked up from /v1/transfers/banks and cached.
    # Confirmed live fallback: "090645" (Nombank MFB's 6-digit code from /v1/transfers/banks).
    # The Nomba transfer API requires bankCode to be exactly 3 or 6 digits.
    _NOMBA_BANK_CODE_FALLBACK = "090645"
    _nomba_bank_code_cache: dict = {}  # keyed by base_url
    _bank_code_lock = threading.Lock()

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
        import re

        # accountRef must be 16–64 chars; prefix guarantees uniqueness.
        # Pad with zeros if the user ID is a short string/integer.
        account_ref = f"tf-user-{user_id}"
        if len(account_ref) < 16:
            account_ref = f"tf-user-{str(user_id).zfill(8)}"
        account_ref = account_ref[:64]

        raw_display_name = account_name.strip() or f"TrustFlow User {str(user_id)[:8]}"
        
        # Sanitize accountName to only allow alphanumeric and space characters
        sanitized_name = re.sub(r'[^a-zA-Z0-9\s]', '', raw_display_name)
        # Normalize multiple spaces to a single space
        sanitized_name = re.sub(r'\s+', ' ', sanitized_name).strip()

        # Enforce minimum 8 chars required by Nomba
        if len(sanitized_name) < 8:
            sanitized_name = f"TrustFlow User {str(user_id)[:8]}".strip()
            if len(sanitized_name) < 8:
                sanitized_name = sanitized_name.ljust(8)

        payload = {
            "accountRef": account_ref,
            "accountName": sanitized_name,
            "currency": "NGN",
        }
        response = self._post("/v1/accounts/virtual", payload)
        # Nomba wraps response in { "data": {...} }
        return response.get("data", response)

    def get_account_balance(self, account_id: str) -> dict:
        """
        Returns the raw balance dict for a given Nomba account ID.
        Use parse_balance() to extract the NGN amount as a float.
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

    @staticmethod
    def parse_balance(balance_data: dict) -> float:
        """
        Extracts the available balance (in NGN) from a Nomba balance response.
        Nomba returns amounts in Kobo; this divides by 100.

        Tries keys in priority order: availableBalance → balance → amount.
        """
        kobo = (
            balance_data.get("availableBalance")
            or balance_data.get("balance")
            or balance_data.get("amount", 0)
        )
        return float(kobo) / 100

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

    def _resolve_bank_code(self, raw_code: str) -> str:
        """
        Translates the internal storage code (e.g. "NMB") to a numeric bank code
        that the Nomba transfer API accepts (must be exactly 3 or 6 digits).

        For Nomba-issued virtual accounts (bank_code stored as "NMB"), we look up
        the real Nomba MFB code from /v1/transfers/banks and cache it for the
        lifetime of the process. Falls back to "100" (Nomba MFB's sort code)
        if the API call fails or Nomba MFB is not found in the list.

        Non-NMB codes are returned as-is (already numeric from the bank list).
        """
        if raw_code not in ("NMB", ""):
            return raw_code

        with self._bank_code_lock:
            cached = self._nomba_bank_code_cache.get(self.base_url)
            if cached:
                return cached

            try:
                resp = requests.get(
                    f"{self.base_url}/v1/transfers/banks",
                    headers=self._auth_headers(),
                    timeout=15,
                )
                resp.raise_for_status()
                banks = resp.json().get("data", [])
                # Widen name match to catch all Nomba MFB variations
                _nomba_keywords = ("nomba", "nombank", "nom mfb", "nom microfinance")
                for bank in banks:
                    name = (bank.get("bankName") or bank.get("name") or "").lower()
                    if any(kw in name for kw in _nomba_keywords):
                        code = str(bank.get("bankCode") or bank.get("code") or "")
                        # Nomba requires exactly 3 or 6 digit numeric codes
                        if code and len(code) in (3, 6) and code.isdigit():
                            logger.info("Resolved Nomba MFB bank code: %s (%s)", code, name)
                            self._nomba_bank_code_cache[self.base_url] = code
                            return code
            except Exception as exc:
                logger.warning(
                    "Could not resolve Nomba MFB bank code from API — using fallback %s: %s",
                    self._NOMBA_BANK_CODE_FALLBACK, exc
                )

            # Fallback: Nomba MFB's sort code (3 digits, always accepted by Nomba transfer API)
            self._nomba_bank_code_cache[self.base_url] = self._NOMBA_BANK_CODE_FALLBACK
            return self._NOMBA_BANK_CODE_FALLBACK

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
            seller_bank_code:      Seller's bank code ("NMB" is resolved to the real
                                   numeric Nomba MFB code via /v1/transfers/banks).
            ref:                   Original escrow agreement reference.
            source_account_id:     Unused (payout always comes from the merchant
                                   sub-account; kept for API compatibility).

        Returns:
            Nomba transfer response data dict.
        """
        # Pay out from the merchant sub-account.
        # NOTE: /v2/transfers/bank/{id} only accepts sub-account IDs.
        # The sub-account must be funded via the Nomba dashboard before payouts work.
        source = self.sub_account_id

        # Resolve the numeric bank code Nomba requires (3 or 6 digits).
        bank_code = self._resolve_bank_code(seller_bank_code)

        account_name = "TrustFlow Recipient"
        try:
            lookup_payload = {
                "accountNumber": seller_account_number,
                "bankCode": bank_code,
            }
            lookup_resp = self._post("/v1/transfers/bank/lookup", lookup_payload)
            resolved_name = lookup_resp.get("data", {}).get("accountName")
            if resolved_name:
                account_name = resolved_name
        except Exception as exc:
            # 404 just means the account isn't on Nomba MFB — proceed with fallback name
            logger.warning("Nomba account lookup failed during payout (non-fatal): %s", exc)

        payload = {
            "amount": int(round(float(amount) * 100)),
            "accountNumber": seller_account_number,
            "bankCode": bank_code,
            "accountName": account_name,
            "merchantTxRef": f"{ref}-release",
            "narration": f"TrustFlow payout — {ref}",
            "senderName": "TrustFlow Escrow",
        }
        logger.info(
            "Initiating payout: amount=%.2f NGN, source=%s, to=%s (bank_code=%s), ref=%s-release",
            amount, source, seller_account_number, bank_code, ref
        )
        response = self._post(f"/v2/transfers/bank/{source}", payload)
        return response.get("data", response)

    def withdraw_to_bank(
        self,
        amount: float,
        account_number: str,
        bank_code: str,
        ref: str,
    ) -> dict:
        """
        Sends funds from the TrustFlow sub-account to a user's external bank account.
        Used for voluntary wallet withdrawals — distinct from escrow releases/refunds.

        Args:
            amount:         Amount in NGN.
            account_number: Destination bank account number.
            bank_code:      Destination bank code.
            ref:            Unique withdrawal reference.

        Returns:
            Nomba transfer response data dict.
        """
        resolved_name = "TrustFlow Withdrawal"
        try:
            lookup_payload = {"accountNumber": account_number, "bankCode": bank_code}
            lookup_resp = self._post("/v1/transfers/bank/lookup", lookup_payload)
            name = lookup_resp.get("data", {}).get("accountName")
            if name:
                resolved_name = name
        except Exception as exc:
            logger.warning("Nomba account lookup failed during withdrawal: %s", exc)

        payload = {
            "amount": int(round(float(amount) * 100)),
            "accountNumber": account_number,
            "bankCode": bank_code,
            "accountName": resolved_name,
            "merchantTxRef": ref,
            "narration": f"TrustFlow wallet withdrawal — {ref}",
            "senderName": "TrustFlow Escrow",
        }
        response = self._post(f"/v2/transfers/bank/{self.sub_account_id}", payload)
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
        # Refund from the merchant sub-account (same constraint as release_to_seller).
        source = self.sub_account_id

        # Resolve the numeric bank code Nomba requires (3 or 6 digits).
        bank_code = self._resolve_bank_code(buyer_bank_code)

        account_name = "TrustFlow Recipient"
        try:
            lookup_payload = {
                "accountNumber": buyer_account_number,
                "bankCode": bank_code,
            }
            lookup_resp = self._post("/v1/transfers/bank/lookup", lookup_payload)
            resolved_name = lookup_resp.get("data", {}).get("accountName")
            if resolved_name:
                account_name = resolved_name
        except Exception as exc:
            logger.warning("Nomba account lookup failed during refund (non-fatal): %s", exc)

        payload = {
            "amount": int(round(float(amount) * 100)),
            "accountNumber": buyer_account_number,
            "bankCode": bank_code,
            "accountName": account_name,
            "merchantTxRef": f"{ref}-refund",
            "narration": f"TrustFlow refund — {ref}",
            "senderName": "TrustFlow Escrow",
        }
        logger.info(
            "Initiating refund: amount=%.2f NGN, to=%s (bank_code=%s), ref=%s-refund",
            amount, buyer_account_number, bank_code, ref
        )
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

    def _get(self, path: str, params: dict = None) -> dict:
        """Authenticated GET request to a Nomba API endpoint."""
        url = f"{self.base_url}{path}"
        try:
            resp = requests.get(
                url,
                params=params or {},
                headers=self._auth_headers(),
                timeout=20,
            )
            resp.raise_for_status()
            return resp.json()
        except requests.Timeout:
            logger.error("Nomba API GET timeout: %s", url)
            raise NombaUnavailableError(
                f"Nomba GET request to {path} timed out."
            )
        except requests.ConnectionError as exc:
            logger.error("Nomba connection error on GET: %s — %s", url, exc)
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

        # 422 means the request body is semantically invalid (e.g. wrong bank code format).
        # Surface it as NombaAPIError with a descriptive message.
        if status_code == 422:
            raise NombaAPIError(
                f"Nomba rejected the request (HTTP 422 — Unprocessable Entity): {body}"
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
        Finds the EscrowAgreement in AWAITING_PAYMENT and transitions it to ACTIVE.

        Matching strategy (in priority order):
          1. merchantTxRef → nomba_transaction_ref (set during lock_funds — most reliable).
             Only present for programmatic payments; absent for manual bank transfers.
          2. Buyer account number/ref + EXACT amount match.
          3. Single-agreement fallback (buyer has exactly 1 pending agreement).
          4. Best-amount fallback for real bank transfers — picks the AWAITING_PAYMENT
             agreement whose amount is closest to the received amount (within 10%).
             This handles real bank-app transfers where Nomba sends no merchantTxRef.

        Uses select_for_update() inside an atomic block to prevent a race
        condition where two concurrent webhook retries activate the same agreement.
        """
        from escrow.models import EscrowAgreement
        from django.contrib.auth import get_user_model
        from django.db import transaction
        from decimal import Decimal
        User = get_user_model()

        account_number  = data.get("bankAccountNumber") or data.get("accountNumber")
        account_ref     = data.get("accountRef")
        amount          = data.get("amount") or data.get("amountReceived")
        # merchantTxRef is the escrow reference set during lock_funds (tf-{pk}-{timestamp}).
        # It is the canonical key for finding the agreement — but it is ONLY present
        # for programmatic (API-initiated) payments, NOT for manual bank transfers.
        merchant_tx_ref = data.get("merchantTxRef", "")
        # paymentRef / reference is the payment provider's own transaction ID — log it
        # but do NOT use it to overwrite the stored escrow reference.
        payment_ref = data.get("paymentRef") or data.get("reference") or merchant_tx_ref

        logger.info(
            "Collection credit received: account=%s, account_ref=%s, amount=%s, "
            "merchant_tx_ref=%s, payment_ref=%s",
            account_number, account_ref, amount, merchant_tx_ref, payment_ref
        )

        if not account_number and not account_ref and not merchant_tx_ref:
            logger.error("Missing account number, account ref, and merchantTxRef in webhook data.")
            return

        # Convert incoming amount (Nomba sends in kobo) to Naira for DB matching.
        try:
            target_amount = Decimal(str(amount)) / 100 if amount is not None else None
        except (TypeError, ValueError):
            logger.error("Invalid amount in webhook data: %s", amount)
            target_amount = None

        # Use an atomic block + select_for_update to prevent two concurrent webhook
        # retries from activating the same agreement simultaneously.
        with transaction.atomic():
            matched_agreement = None
            match_strategy   = None

            # ── Strategy 1: match by merchantTxRef ──────────────────────────────
            # Present only when the payment was initiated programmatically via Nomba API
            # (e.g. from a simulator or wallet-funded flow). Missing for real bank transfers.
            if merchant_tx_ref:
                matched_agreement = EscrowAgreement.objects.select_for_update().filter(
                    nomba_transaction_ref=merchant_tx_ref,
                    status=EscrowAgreement.Status.AWAITING_PAYMENT,
                ).first()
                if matched_agreement:
                    match_strategy = "merchantTxRef"

            # Resolve buyer from virtual account details (needed for strategies 2, 3, 4)
            buyer = None
            if account_number:
                buyer = User.objects.select_for_update().filter(
                    nomba_account_number=account_number
                ).first()
            if not buyer and account_ref:
                buyer = User.objects.select_for_update().filter(
                    nomba_account_ref=account_ref
                ).first()

            if not matched_agreement and buyer:
                agreements_qs = EscrowAgreement.objects.select_for_update().filter(
                    buyer=buyer,
                    status=EscrowAgreement.Status.AWAITING_PAYMENT,
                )

                # ── Strategy 2: buyer account + exact amount ─────────────────
                if target_amount:
                    matched_agreement = agreements_qs.filter(amount=target_amount).first()
                    if matched_agreement:
                        match_strategy = "exact_amount"

                # ── Strategy 3: single-agreement fallback ────────────────────
                if not matched_agreement and agreements_qs.count() == 1:
                    matched_agreement = agreements_qs.first()
                    match_strategy = "single_agreement_fallback"

                # ── Strategy 4: closest-amount fallback (real bank transfers) ─
                # When a buyer manually transfers from their banking app, Nomba sends
                # no merchantTxRef.  We pick the AWAITING_PAYMENT agreement whose
                # amount is within 10% of the received amount (covers rounding and
                # minor fee differences), breaking ties by most recently created.
                if not matched_agreement and target_amount:
                    best = None
                    best_diff = None
                    tolerance = target_amount * Decimal("0.10")  # 10% tolerance
                    for ag in agreements_qs.order_by('-created_at'):
                        diff = abs(ag.amount - target_amount)
                        if diff <= tolerance:
                            if best_diff is None or diff < best_diff:
                                best = ag
                                best_diff = diff
                    if best:
                        matched_agreement = best
                        match_strategy = "closest_amount_fallback"

            if not matched_agreement:
                # Nothing matched — credit the buyer's wallet as an unallocated top-up
                if buyer and target_amount:
                    buyer.wallet_balance += target_amount
                    buyer.save(update_fields=['wallet_balance'])
                    logger.info(
                        "Direct wallet funding: credited user %s with NGN %s. "
                        "New balance: %s  (no matching AWAITING_PAYMENT agreement; "
                        "payment_ref=%s)",
                        buyer.email, target_amount, buyer.wallet_balance, payment_ref
                    )
                else:
                    logger.warning(
                        "No AWAITING_PAYMENT escrow agreement found and no buyer found to "
                        "credit for merchantTxRef=%s, account=%s, amount=%s, payment_ref=%s",
                        merchant_tx_ref, account_number, target_amount, payment_ref
                    )
                return

            # ── Activate the agreement ───────────────────────────────────────────
            # Do NOT overwrite nomba_transaction_ref — it already holds the canonical
            # escrow ref (tf-{pk}-{timestamp}) set during lock_funds.  Overwriting it
            # with the incoming payment_ref would break release/refund lookups.
            matched_agreement.status = EscrowAgreement.Status.ACTIVE
            matched_agreement.save(update_fields=['status', 'updated_at'])

        logger.info(
            "EscrowAgreement %s marked ACTIVE via collection credit (strategy=%s). "
            "Stored ref=%s, incoming merchant_tx_ref=%s, payment_ref=%s",
            matched_agreement.id,
            match_strategy,
            matched_agreement.nomba_transaction_ref,
            merchant_tx_ref,
            payment_ref,
        )
