# Nomba API Integration Guidelines & Rules

You are an automated coding agent integrating the Nomba API into this application. You must strictly adhere to the rules, patterns, and architectural invariants defined in this document. Never deviate from these core rules without explicit human override.

---

## 1. Global Architectural Invariants

* **Amounts are STRICTLY in Kobo:** 
  * ₦1.00 = 100 kobo. 
  * Always multiply Naira amounts by `100` before sending to API endpoints. 
  * Never send raw Naira integers or decimals (e.g., charge `250000` for ₦2,500.00).
* **Idempotency & References:**
  * Always generate and pass a unique `merchantTxRef` (or `orderReference`) for transactions, transfers, and token charges.
  * Join Nomba API data with local database tables using `merchantTxRef` (never internal Nomba IDs, which may rotate during retries).
* **Secret Management:**
  * Never hardcode or commit secrets.
  * Require these environment variables: `NOMBA_CLIENT_ID`, `NOMBA_CLIENT_SECRET`, `NOMBA_ACCOUNT_ID`, `NOMBA_WEBHOOK_SECRET`.

---

## 2. Environments & Test Instruments

When building or testing, isolate sandbox credentials from production.

| Environment | Base URL | Usage |
|---|---|---|
| **Sandbox** | `https://sandbox.api.nomba.com/v1` | All development and hackathon work |
| **Production** | `https://api.nomba.com/v1` | Live deployment post-KYC/certification |

### Sandbox Test Instruments
* **Test Card (Success):** `5060 6666 6666 6666 666` (Any future expiry, any CVV)
* **Test Card (Insufficient Funds):** `5060 6666 6666 6666 674`
* **Test Bank:** Wema Bank, Account Number `0000000000` (Accepts any inbound transfer)

---

## 3. Authentication & Caching Protocol

Nomba uses OAuth 2.0 `client_credentials` for server-to-server calls.

### Token Caching Rule (CRITICAL)
* Tokens expire in **60 minutes**. 
* **Do NOT issue a fresh token per API call.** Cache access tokens in memory or Redis and refresh them at the **55-minute mark**.

### Token Issue Endpoint (`POST /auth/token/issue`)
```python
import os
import requests

response = requests.post(
    "https://api.nomba.com/v1/auth/token/issue",
    headers={
        "Content-Type": "application/json",
        "accountId": os.environ["NOMBA_ACCOUNT_ID"],
    },
    json={
        "grant_type": "client_credentials",
        "client_id": os.environ["NOMBA_CLIENT_ID"],
        "client_secret": os.environ["NOMBA_CLIENT_SECRET"],
    },
)
access_token = response.json()["data"]["access_token"]
```

### Mandatory Request Headers
Every authenticated HTTP request must include:
| Header | Required Value |
|---|---|
| `Authorization` | `Bearer <cached_access_token>` |
| `accountId` | `os.environ["NOMBA_ACCOUNT_ID"]` |
| `Content-Type` | `application/json` |

---

## 4. API Modules & Implementation Rules

### Checkout API (`POST /checkout/order`)
* Generates a hosted payment URL.
* Redirect the client to `data["checkoutUrl"]`.
* Always pass a stable `callbackUrl` and track the order locally before sending.

### Tokenized Cards (`POST /tokenized-card/charge`)
* Nomba does not manage recurring subscription schedules. You must implement a scheduler/cron job that triggers token charges.
* Always pass a unique `merchantTxRef` per charge attempt to ensure safe retries.

### Virtual Accounts (`POST /accounts/virtual`)
* Used for dedicated NUBAN bank-transfer invoicing or wallet funding.
* **Over/Under-Payment Rule:** Bank rails accept any amount regardless of the `amount` lock. Compare `amountReceived` against expected values inside your webhook handler to trigger short-payment alerts or overpayment refunds.

### Transfers (`POST /transfers/bank`)
* **Mandatory Lookup:** Always call `POST /transfers/bank/lookup` to resolve `bankCode` and `accountNumber` before initiating a transfer.
* Store the resolved `accountName` and pass it into the transfer initiation payload along with a unique `merchantTxRef`.

### Direct Debits / Mandates (`POST /mandates/create`)
* Requires customer consent redirection via `data["consentUrl"]`.
* **Ceiling Rule:** Never attempt to debit an amount exceeding `maxAmount`. If pricing increases beyond the ceiling, issue a new mandate request rather than splitting debits.

### Sub-Accounts (`POST /accounts/sub-accounts`)
* Use for multi-tenant, marketplace, or branch ledgering.
* Provide your own deterministic `accountRef` to link sub-accounts directly to primary database records.

---

## 5. Webhook Engineering (Strict Enforcement)

Webhooks alert the system to asynchronous state changes. Implement webhook handlers with strict verification and deduplication.

### Signature Verification Rule
Every incoming request to the webhook endpoint must verify the `nomba-signature` header using HMAC-SHA256 and `os.environ["NOMBA_WEBHOOK_SECRET"]`. Reject mismatches with HTTP `401 Unauthorized`.

```python
# Python pattern reference (Flask/FastAPI/Django style)
import hmac
import hashlib
import os

secret = os.environ["NOMBA_WEBHOOK_SECRET"].encode("utf-8")
expected_signature = hmac.new(secret, raw_body, hashlib.sha256).hexdigest()

if headers.get("nomba-signature") != expected_signature:
    raise PermissionError("Unauthorized webhook signature")
```

### Idempotency Rule
* Network retries may send the exact same webhook multiple times.
* Check `event["requestId"]` against a unique database index or cache. If already processed, immediately return HTTP `200 OK` without altering balances.

### Primary Event Types
| Event Type | Action Required |
|---|---|
| `payment_success` | Mark order/charge as paid, fulfill product/service |
| `virtual_account.funded` | Credit user wallet or settle invoice (check `amountReceived`) |
| `transfer.success` | Mark outgoing payout as settled |
| `transfer.failed` | Reverse local debit, alert operations |
| `mandate.debit_success` | Extend subscription or clear installment balance |

---

## 6. Reconciliation Discipline

If generating background jobs or admin dashboards, implement a daily reconciliation script using `GET /transactions`.
* Fetch Nomba transactions nightly (`dateFrom`, `dateTo`, `status=success`).
* Match against the local ledger using `merchantTxRef`.
* Flag orphan transactions (exist on Nomba, missing locally) and amount drift (local amount != Nomba amount) for automated alerting.

---

## 7. Execution Focus by Feature Track

When generating features for specific use cases, prioritize architectural depth over breadth:

| Target Architecture | Primary APIs to Use | Required Polish / Edge Cases |
|---|---|---|
| **Marketplace / Multi-Vendor** | Sub-accounts, Transfers, Webhooks | Automated reconciliation dashboard |
| **SaaS / Subscription** | Checkout, Tokenized Cards, Mandates | Idempotent cron retries, dunning flow |
| **Treasury / Payouts** | Transfers, Virtual Accounts, Transactions | Mandatory account lookups, audit logs |
| **Bank-Transfer Checkout** | Virtual Accounts, Webhooks | Real-time UI updates via WebSocket/polling |
| **BNPL / Lending** | Mandates, Direct Debits, Transactions | Mandate lifecycle & consent state tracking |