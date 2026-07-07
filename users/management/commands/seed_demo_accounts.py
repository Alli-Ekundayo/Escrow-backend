"""
Management command: seed_demo_accounts
======================================
Sets known reviewer passwords on the existing production demo accounts so that
reviewers can log in without going through signup.

Targeted accounts (must already exist in the DB):
  - Buyer:  onepeice@gmail.com
  - Seller: ada@yahoo.com
  - Admin:  alliekundayo6@gmail.com

Usage:
    python manage.py seed_demo_accounts

Safe to re-run — only updates passwords, never deletes or creates records.
"""

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

User = get_user_model()

REVIEWER_PASSWORD = "TrustFlow2026!"
ADMIN_PASSWORD = "TrustFlowAdmin2026!"

DEMO_TARGETS = [
    {"email": "onepeice@gmail.com",  "role": "Buyer",  "password": REVIEWER_PASSWORD},
    {"email": "ada@yahoo.com",       "role": "Seller", "password": REVIEWER_PASSWORD},
    {"email": "alliekundayo6@gmail.com", "role": "Admin (superuser)", "password": ADMIN_PASSWORD},
]


class Command(BaseCommand):
    help = "Set known reviewer passwords on existing production demo accounts."

    def handle(self, *args, **options):
        self.stdout.write(self.style.MIGRATE_HEADING("Updating demo account passwords..."))
        self.stdout.write("")

        for target in DEMO_TARGETS:
            try:
                user = User.objects.get(email=target["email"])
                user.set_password(target["password"])
                user.save(update_fields=["password"])
                self.stdout.write(
                    self.style.SUCCESS(
                        f"  ✓ [{target['role']}]  {user.email}  → password set"
                    )
                )
            except User.DoesNotExist:
                self.stdout.write(
                    self.style.ERROR(
                        f"  ✗ [{target['role']}]  {target['email']}  → NOT FOUND in DB"
                    )
                )

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("Reviewer credentials ready:"))
        self.stdout.write("")
        self.stdout.write(f"  Buyer   → onepeice@gmail.com          / {REVIEWER_PASSWORD}")
        self.stdout.write(f"  Seller  → ada@yahoo.com               / {REVIEWER_PASSWORD}")
        self.stdout.write(f"  Admin   → alliekundayo6@gmail.com      / {ADMIN_PASSWORD}")
        self.stdout.write("  Admin panel → https://escrow-backend-production-7b3d.up.railway.app/admin")
