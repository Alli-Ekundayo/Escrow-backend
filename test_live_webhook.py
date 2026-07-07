import os
import sys
import hmac
import hashlib
import json
import requests

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django
django.setup()

from django.conf import settings
from django.contrib.auth import get_user_model
from django.utils import timezone

User = get_user_model()

def main():
    webhook_secret = getattr(settings, "NOMBA_WEBHOOK_SECRET", "")
    if not webhook_secret:
        print("[ERROR] NOMBA_WEBHOOK_SECRET is not configured in settings.")
        sys.exit(1)
        
    print(f"Loaded webhook secret: {webhook_secret[:3]}...{webhook_secret[-3:] if len(webhook_secret) > 6 else ''}")
    
    # Get test user details
    email = input("Enter test user email (or press Enter to query local DB): ").strip()
    account_number = ""
    account_ref = ""
    
    if email:
        account_number = input("Enter Nomba bank account number: ").strip()
        account_ref = input("Enter Nomba account reference (optional, e.g. tf-user-<uuid>): ").strip()

    if not email or not account_number:
        print("[INFO] Falling back to local database lookup...")
        if email:
            user = User.objects.filter(email=email).first()
        else:
            user = User.objects.exclude(nomba_account_number="").first()
            
        if not user:
            print("[ERROR] No user found in local database. You must provide email and account number manually.")
            sys.exit(1)
        email = user.email
        account_number = user.nomba_account_number
        account_ref = user.nomba_account_ref
        
    print(f"\nTesting with:")
    print(f"  Email: {email}")
    print(f"  Account Number: {account_number}")
    print(f"  Account Ref: {account_ref}")
    
    amount_str = input("Amount in Naira (default: 5000): ").strip() or "5000"
    amount = float(amount_str)
    
    url = input("Target Webhook URL (default: https://escrow-backend-production-7b3d.up.railway.app/api/payments/webhook/): ").strip() or "https://escrow-backend-production-7b3d.up.railway.app/api/payments/webhook/"
    
    payload = {
        "event": "virtual_account.funded",
        "data": {
            "amount": int(round(amount * 100)), # kobo
            "currency": "NGN",
            "bankAccountNumber": account_number,
            "accountRef": account_ref,
            "reference": f"live-test-ref-{int(timezone.now().timestamp())}"
        }
    }
    
    payload_bytes = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    signature = hmac.new(webhook_secret.encode("utf-8"), payload_bytes, hashlib.sha256).hexdigest()
    
    print(f"\nSending payload to {url}...")
    print(f"Payload: {json.dumps(payload, indent=2)}")
    print(f"Computed Signature: {signature}")
    
    headers = {
        "Content-Type": "application/json",
        "nomba-signature": signature
    }
    
    try:
        resp = requests.post(url, data=payload_bytes, headers=headers, timeout=20)
        print(f"\nResponse Code: {resp.status_code}")
        print(f"Response Body: {resp.text}")
        if resp.status_code == 200:
            print("[✓] Success! The live webhook verified successfully.")
        else:
            print("[✗] Failed to verify.")
    except Exception as exc:
        print(f"[ERROR] Request failed: {exc}")

if __name__ == "__main__":
    main()
