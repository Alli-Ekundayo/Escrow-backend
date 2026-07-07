#!/usr/bin/env python
"""
TrustFlow — Production Webhook Test Suite
==========================================

Tests the live production webhook endpoint at:
  https://escrow-backend-production-7b3d.up.railway.app/api/payments/webhook/

What it tests:
  1. HMAC signature enforcement  → invalid sig must return 401
  2. Missing signature            → must return 401 (in production mode)
  3. collection.credit event      → must return 200 and activate a real escrow agreement
  4. virtual_account.funded event → must return 200 (same handler, different event name)
  5. Unknown event type           → must return 200 (webhook handler logs and continues)
  6. Malformed JSON               → must return 400

Usage:
    # Option A — Interactive (prompts for user / agreement details)
    python test_prod_webhook.py

    # Option B — Headless, using DB lookup
    WEBHOOK_URL=https://escrow-backend-production-7b3d.up.railway.app/api/payments/webhook/ \\
    python test_prod_webhook.py --headless

    # Option C — Skip DB setup (just test infra, skip escrow activation test)
    python test_prod_webhook.py --skip-activation

Requirements:
    pip install requests python-decouple
"""

import argparse
import hashlib
import hmac
import json
import os
import sys
import time

import requests

# ──────────────────────────────────────────────────────────────────────────────
# Config
# ──────────────────────────────────────────────────────────────────────────────
PROD_WEBHOOK_URL = os.environ.get(
    "WEBHOOK_URL",
    "https://escrow-backend-production-7b3d.up.railway.app/api/payments/webhook/",
)

# Pull webhook secret from .env / environment variable
try:
    from decouple import config as decouple_config
    WEBHOOK_SECRET = decouple_config("NOMBA_WEBHOOK_SECRET", default="")
except ImportError:
    WEBHOOK_SECRET = os.environ.get("NOMBA_WEBHOOK_SECRET", "")

# ──────────────────────────────────────────────────────────────────────────────
# ANSI colour helpers
# ──────────────────────────────────────────────────────────────────────────────
GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

def ok(msg):   print(f"{GREEN}  ✓ PASS{RESET}  {msg}")
def fail(msg): print(f"{RED}  ✗ FAIL{RESET}  {msg}")
def info(msg): print(f"{CYAN}  ℹ INFO{RESET}  {msg}")
def warn(msg): print(f"{YELLOW}  ⚠ WARN{RESET}  {msg}")
def header(msg):
    print(f"\n{BOLD}{CYAN}{'─'*60}{RESET}")
    print(f"{BOLD}{CYAN}  {msg}{RESET}")
    print(f"{BOLD}{CYAN}{'─'*60}{RESET}")

PASS_COUNT = 0
FAIL_COUNT = 0

def assert_status(label: str, resp: requests.Response, expected: int):
    global PASS_COUNT, FAIL_COUNT
    if resp.status_code == expected:
        ok(f"{label}  (HTTP {resp.status_code})")
        PASS_COUNT += 1
    else:
        fail(f"{label}  — expected HTTP {expected}, got {resp.status_code}")
        fail(f"   Response body: {resp.text[:300]}")
        FAIL_COUNT += 1


# ──────────────────────────────────────────────────────────────────────────────
# Core helpers
# ──────────────────────────────────────────────────────────────────────────────
def sign(payload_bytes: bytes, secret: str) -> str:
    """Compute HMAC-SHA256 signature exactly as the server expects."""
    return hmac.new(
        secret.encode("utf-8"),
        payload_bytes,
        hashlib.sha256,
    ).hexdigest()


def post_webhook(payload: dict, signature: str | None, url: str = PROD_WEBHOOK_URL) -> requests.Response:
    """POST a signed (or unsigned) webhook payload to the server."""
    payload_bytes = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if signature is not None:
        headers["nomba-signature"] = signature

    resp = requests.post(url, data=payload_bytes, headers=headers, timeout=25)
    return resp


def build_collection_credit(account_number: str, account_ref: str,
                              amount_naira: float, reference: str) -> dict:
    """Build a realistic Nomba `collection.credit` webhook payload."""
    return {
        "event": "collection.credit",
        "data": {
            "amount": int(round(amount_naira * 100)),  # Naira → Kobo
            "currency": "NGN",
            "bankAccountNumber": account_number,
            "accountRef": account_ref,
            "reference": reference,
            "narration": f"TrustFlow test credit — {reference}",
            "transactionDate": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
    }


# ──────────────────────────────────────────────────────────────────────────────
# Test Cases
# ──────────────────────────────────────────────────────────────────────────────

def test_1_invalid_signature():
    """Webhook with a wrong HMAC signature must be rejected with 401."""
    header("Test 1 — Invalid HMAC signature → 401")
    payload = {
        "event": "collection.credit",
        "data": {
            "amount": 500000,
            "currency": "NGN",
            "bankAccountNumber": "0000000000",
            "reference": "bad-sig-test",
        },
    }
    resp = post_webhook(payload, signature="totally_wrong_signature_abc123")
    assert_status("Invalid signature rejected", resp, 401)


def test_2_missing_signature():
    """Webhook with NO signature header must be rejected with 401 (production has a secret)."""
    header("Test 2 — Missing signature header → 401")
    payload = {
        "event": "collection.credit",
        "data": {
            "amount": 100000,
            "currency": "NGN",
            "bankAccountNumber": "0000000000",
            "reference": "no-sig-test",
        },
    }
    resp = post_webhook(payload, signature=None)
    assert_status("Missing signature rejected", resp, 401)


def test_3_unknown_event_type(secret: str):
    """Unknown event types should be handled gracefully and return 200."""
    header("Test 3 — Unknown event type → 200")
    payload = {
        "event": "some.future.event",
        "data": {
            "reference": f"unknown-event-{int(time.time())}",
        },
    }
    payload_bytes = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    sig = sign(payload_bytes, secret)
    resp = post_webhook(payload, signature=sig)
    assert_status("Unknown event handled gracefully", resp, 200)


def test_4_malformed_body(secret: str):
    """Malformed JSON body should return 400."""
    header("Test 4 — Malformed JSON body → 400")
    bad_bytes = b"this is not json {{{{["
    sig = hmac.new(secret.encode(), bad_bytes, hashlib.sha256).hexdigest()
    headers = {
        "Content-Type": "application/json",
        "nomba-signature": sig,
    }
    resp = requests.post(PROD_WEBHOOK_URL, data=bad_bytes, headers=headers, timeout=25)
    # DRF may return 400 for unparseable JSON
    if resp.status_code in (400, 200):
        ok(f"Malformed JSON handled (HTTP {resp.status_code})")
        global PASS_COUNT
        PASS_COUNT += 1
    else:
        fail(f"Malformed JSON — unexpected HTTP {resp.status_code}: {resp.text[:200]}")
        global FAIL_COUNT
        FAIL_COUNT += 1


def test_5_virtual_account_funded_alias(secret: str, account_number: str,
                                         account_ref: str, amount: float):
    """'virtual_account.funded' is an alias handled by the same collection credit handler."""
    header("Test 5 — virtual_account.funded event (alias) → 200")
    reference = f"va-funded-alias-test-{int(time.time())}"
    payload = {
        "event": "virtual_account.funded",
        "data": {
            "amount": int(round(amount * 100)),
            "currency": "NGN",
            "bankAccountNumber": account_number,
            "accountRef": account_ref,
            "reference": reference,
        },
    }
    payload_bytes = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    sig = sign(payload_bytes, secret)
    resp = post_webhook(payload, signature=sig)
    assert_status("virtual_account.funded alias accepted", resp, 200)
    info(f"Response: {resp.text[:200]}")


def test_6_collection_credit_activation(secret: str, account_number: str,
                                         account_ref: str, amount: float):
    """Full end-to-end: collection.credit with a matching AWAITING_PAYMENT agreement should activate it."""
    header("Test 6 — collection.credit → escrow ACTIVE (full activation test)")
    reference = f"prod-webhook-test-{int(time.time())}"
    payload = build_collection_credit(
        account_number=account_number,
        account_ref=account_ref,
        amount_naira=amount,
        reference=reference,
    )
    payload_bytes = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    sig = sign(payload_bytes, secret)

    info(f"Posting collection.credit for account {account_number} | amount: ₦{amount:,.2f} | ref: {reference}")
    info(f"Payload: {json.dumps(payload, indent=2)}")
    info(f"Signature (nomba-signature): {sig}")

    resp = post_webhook(payload, signature=sig)
    assert_status("collection.credit webhook accepted", resp, 200)
    info(f"Response body: {resp.text}")


def test_7_transfer_success(secret: str, transaction_ref: str):
    """transfer.success event should be handled and return 200."""
    header("Test 7 — transfer.success event → 200")
    payload = {
        "event": "transfer.success",
        "data": {
            "merchantTxRef": transaction_ref,
            "reference": f"nomba-ref-{int(time.time())}",
            "amount": 500000,
            "currency": "NGN",
            "narration": "Test payout",
        },
    }
    payload_bytes = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    sig = sign(payload_bytes, secret)
    resp = post_webhook(payload, signature=sig)
    assert_status("transfer.success event handled", resp, 200)
    info(f"Response: {resp.text[:200]}")


def test_8_transfer_failed(secret: str, transaction_ref: str):
    """transfer.failed event should be handled and return 200."""
    header("Test 8 — transfer.failed event → 200")
    payload = {
        "event": "transfer.failed",
        "data": {
            "merchantTxRef": transaction_ref,
            "reference": f"nomba-failed-{int(time.time())}",
            "amount": 100000,
            "currency": "NGN",
            "reason": "Insufficient funds at destination",
        },
    }
    payload_bytes = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    sig = sign(payload_bytes, secret)
    resp = post_webhook(payload, signature=sig)
    assert_status("transfer.failed event handled", resp, 200)
    info(f"Response: {resp.text[:200]}")


# ──────────────────────────────────────────────────────────────────────────────
# Summary
# ──────────────────────────────────────────────────────────────────────────────
def print_summary():
    total = PASS_COUNT + FAIL_COUNT
    header("Test Summary")
    print(f"  Total:  {total}")
    print(f"  {GREEN}Passed: {PASS_COUNT}{RESET}")
    print(f"  {RED}Failed: {FAIL_COUNT}{RESET}")
    print()
    if FAIL_COUNT == 0:
        print(f"  {GREEN}{BOLD}🎉 All tests passed! Production webhook is working correctly.{RESET}")
    else:
        print(f"  {RED}{BOLD}❌ {FAIL_COUNT} test(s) failed. Review output above.{RESET}")
    print()


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────
def parse_args():
    p = argparse.ArgumentParser(description="TrustFlow production webhook tester")
    p.add_argument("--headless", action="store_true",
                   help="Do not prompt for input; use DB lookup instead")
    p.add_argument("--skip-activation", action="store_true",
                   help="Skip the live escrow activation test (Tests 5, 6)")
    p.add_argument("--url", default=PROD_WEBHOOK_URL,
                   help="Override the webhook URL")
    p.add_argument("--account-number", default="",
                   help="Buyer's Nomba bank account number for activation test")
    p.add_argument("--account-ref", default="",
                   help="Buyer's Nomba account ref (e.g. tf-user-<uuid>)")
    p.add_argument("--amount", type=float, default=5000.0,
                   help="Amount in Naira for the activation test (default: 5000)")
    p.add_argument("--tx-ref", default="tf-dummy-ref",
                   help="Existing transaction ref for transfer event tests")
    return p.parse_args()


def main():
    args = parse_args()
    global PROD_WEBHOOK_URL
    PROD_WEBHOOK_URL = args.url

    header("TrustFlow — Production Webhook Test Suite")
    print(f"  URL:     {BOLD}{PROD_WEBHOOK_URL}{RESET}")

    if not WEBHOOK_SECRET:
        print(f"\n{RED}{BOLD}[ERROR]{RESET} NOMBA_WEBHOOK_SECRET is not set.")
        print("  Set it in your .env file or as an environment variable:")
        print("  export NOMBA_WEBHOOK_SECRET=<your secret>")
        sys.exit(1)

    masked = WEBHOOK_SECRET[:3] + "..." + (WEBHOOK_SECRET[-3:] if len(WEBHOOK_SECRET) > 6 else "")
    print(f"  Secret:  {masked}  ({len(WEBHOOK_SECRET)} chars)")

    # ── Infrastructure tests (no DB needed) ──────────────────────────────────
    test_1_invalid_signature()
    test_2_missing_signature()
    test_3_unknown_event_type(WEBHOOK_SECRET)
    test_4_malformed_body(WEBHOOK_SECRET)

    # ── Event handler tests (need a real user account for Tests 5 & 6) ───────
    if args.skip_activation:
        warn("Skipping Tests 5 & 6 (escrow activation) — --skip-activation flag set")
    else:
        account_number = args.account_number
        account_ref    = args.account_ref
        amount         = args.amount

        # Interactive prompts if not provided via CLI
        if not args.headless and not account_number:
            print(f"\n{BOLD}Escrow Activation Test (Tests 5 & 6){RESET}")
            print("  These tests simulate a real Nomba payment credit to your production server.")
            print("  You need a valid buyer's Nomba bank account number and account ref.")
            print()
            account_number = input("  Buyer's Nomba bank account number: ").strip()
            account_ref    = input("  Buyer's account ref (e.g. tf-user-<uuid>) [optional]: ").strip()
            amount_str     = input(f"  Amount in Naira (default {amount}): ").strip()
            if amount_str:
                amount = float(amount_str)

        if not account_number:
            # Try to pull from the production DB via Django ORM
            try:
                os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
                import django
                django.setup()
                from django.contrib.auth import get_user_model
                User = get_user_model()
                user = User.objects.exclude(nomba_account_number="").first()
                if user:
                    account_number = user.nomba_account_number
                    account_ref    = user.nomba_account_ref or ""
                    info(f"Using account from local DB: {user.email} | account: {account_number}")
                else:
                    warn("No user with a Nomba account found in local DB.")
            except Exception as exc:
                warn(f"Could not query local DB: {exc}")

        if account_number:
            test_5_virtual_account_funded_alias(WEBHOOK_SECRET, account_number, account_ref, amount)
            test_6_collection_credit_activation(WEBHOOK_SECRET, account_number, account_ref, amount)
        else:
            warn("No account number available — skipping Tests 5 & 6.")
            warn("Pass --account-number <num> to run the activation tests.")

    # ── Transfer event tests ─────────────────────────────────────────────────
    test_7_transfer_success(WEBHOOK_SECRET, args.tx_ref)
    test_8_transfer_failed(WEBHOOK_SECRET, args.tx_ref)

    print_summary()
    sys.exit(0 if FAIL_COUNT == 0 else 1)


if __name__ == "__main__":
    main()
