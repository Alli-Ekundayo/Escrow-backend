"""
fund_sandbox_wallet.py
======================
Simulate a credit (deposit) to a Nomba sandbox virtual account.

Usage:
    python fund_sandbox_wallet.py --account 7025708740 --amount 50000
    python fund_sandbox_wallet.py --list          # show all wallets + balances

This script only works in NOMBA_TEST_MODE=True (sandbox).
"""

import argparse
import os
import sys

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django

django.setup()

from django.contrib.auth import get_user_model
from payments.services import NombaPaymentService, NombaError

User = get_user_model()


def get_service():
    try:
        return NombaPaymentService()
    except NombaError as exc:
        print(f"[ERROR] Could not initialise Nomba service: {exc}")
        sys.exit(1)


def list_wallets(svc):
    print("\n=== User Wallets ===\n")
    users = User.objects.exclude(nomba_account_number="")
    if not users.exists():
        print("No users have Nomba wallets yet.")
        return

    for user in users:
        # Try to fetch balance
        try:
            bal = svc.get_account_balance(user.nomba_account_holder_id)
            balance = bal.get("balance", bal.get("availableBalance", "N/A"))
        except Exception as exc:
            balance = f"(error: {exc})"

        print(f"  Email   : {user.email}")
        print(f"  Account : {user.nomba_account_number}  (bank: Nomba MFB, code: {user.nomba_bank_code})")
        print(f"  Ref     : {user.nomba_account_ref}")
        print(f"  Balance : NGN {balance}")
        print()


def simulate_credit(svc, account_number: str, amount: float, local_only: bool = False):
    """
    POST to Nomba's sandbox credit simulation endpoint or triggers a local webhook.
    """
    import requests
    import time
    from django.conf import settings

    if not getattr(settings, "NOMBA_TEST_MODE", True):
        print("[ERROR] This script only works in sandbox mode (NOMBA_TEST_MODE=True).")
        sys.exit(1)

    # Find the user by account number
    user = User.objects.filter(nomba_account_number=account_number).first()
    if not user:
        print(f"[ERROR] No user found with account number {account_number}")
        sys.exit(1)

    if local_only:
        print(f"\n[→] Simulating local webhook credit of NGN {amount:,.2f} to account {account_number} ...")
        webhook_payload = {
            "event": "collection.credit",
            "data": {
                "amount": int(round(amount * 100)),  # in kobo
                "currency": "NGN",
                "bankAccountNumber": account_number,
                "accountRef": user.nomba_account_ref,
                "reference": f"simulated-credit-{int(time.time())}"
            }
        }
        try:
            svc.handle_webhook(webhook_payload)
            print("[✓] Success! Local webhook credit handler executed successfully.")
            print("    Any matching AWAITING_PAYMENT agreements for this user have been activated.")
        except Exception as exc:
            print(f"[✗] Local webhook simulation failed: {exc}")
        return

    url = f"{svc.base_url}/v1/accounts/simulate/credit"
    payload = {
        "accountNumber": account_number,
        "amount": amount,
        "currency": "NGN",
        "narration": "Test deposit via fund_sandbox_wallet.py",
    }
    headers = svc._auth_headers()

    print(f"\n[→] Simulating credit of NGN {amount:,.2f} to account {account_number} ...")
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=20)
        if resp.status_code in (200, 201):
            data = resp.json()
            print(f"[✓] Success! Response: {data}")
            return
        else:
            print(f"[✗] Failed (HTTP {resp.status_code}): {resp.text}")
    except Exception as e:
        print(f"[✗] Failed to connect to Nomba API: {e}")

    print("\nFalling back to local webhook credit simulation...")
    webhook_payload = {
        "event": "collection.credit",
        "data": {
            "amount": int(round(amount * 100)),  # in kobo
            "currency": "NGN",
            "bankAccountNumber": account_number,
            "accountRef": user.nomba_account_ref,
            "reference": f"simulated-credit-{int(time.time())}"
        }
    }
    try:
        svc.handle_webhook(webhook_payload)
        print("[✓] Success! Local webhook credit handler executed successfully.")
        print("    Any matching AWAITING_PAYMENT agreements for this user have been activated.")
    except Exception as exc:
        print(f"[✗] Local webhook simulation failed: {exc}")


def main():
    parser = argparse.ArgumentParser(description="Fund a Nomba sandbox virtual account.")
    parser.add_argument("--list", action="store_true", help="List all wallets and balances")
    parser.add_argument("--account", help="Target account number (NUBAN)")
    parser.add_argument("--amount", type=float, default=100_000, help="Amount in NGN (default: 100000)")
    parser.add_argument("--local", action="store_true", help="Simulate webhook locally without contacting Nomba API")
    args = parser.parse_args()

    svc = get_service()

    if args.list or not args.account:
        list_wallets(svc)
        if not args.account:
            print("Tip: pass --account <number> --amount <NGN> to simulate a credit.\n")
        return

    simulate_credit(svc, args.account, args.amount, args.local)


if __name__ == "__main__":
    main()
