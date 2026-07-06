"""
Management command: backfill_nomba_wallets
==========================================

Usage:
    python manage.py backfill_nomba_wallets
    python manage.py backfill_nomba_wallets --user-id <UUID>   # target a single user
    python manage.py backfill_nomba_wallets --dry-run          # preview without writing

Purpose:
    Provisions Nomba virtual wallets for any registered users whose
    `nomba_account_number` field is empty, which happens when the
    wallet creation step at registration fails silently (network error,
    Nomba sandbox unreachable, bad credentials, etc.).

When to run:
    - After fixing Nomba credentials or sandbox connectivity issues.
    - After adding new users in bulk who didn't get wallets.
    - As a one-off fix for users reporting "wallet not linked" errors.
"""

import logging

from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model

from payments.services import NombaPaymentService, NombaError

logger = logging.getLogger(__name__)
User = get_user_model()


class Command(BaseCommand):
    help = (
        "Backfill Nomba virtual wallets for users who are missing them. "
        "Targets all users with an empty nomba_account_number by default."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--user-id",
            dest="user_id",
            default=None,
            help="Backfill a specific user by their UUID primary key.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            default=False,
            help="List affected users without calling Nomba or writing to the DB.",
        )

    def handle(self, *args, **options):
        user_id = options.get("user_id")
        dry_run = options.get("dry_run", False)

        # Build queryset
        qs = User.objects.all()
        if user_id:
            qs = qs.filter(pk=user_id)
            if not qs.exists():
                self.stderr.write(self.style.ERROR(f"No user found with id={user_id}"))
                return
        else:
            # Only target users whose wallet provisioning hasn't happened yet
            qs = qs.filter(nomba_account_number="")

        count = qs.count()
        if count == 0:
            self.stdout.write(self.style.SUCCESS("All users already have Nomba wallets. Nothing to do."))
            return

        self.stdout.write(
            self.style.WARNING(
                f"Found {count} user(s) missing a Nomba wallet."
                + (" (DRY RUN — no changes will be made)" if dry_run else "")
            )
        )

        if dry_run:
            for user in qs:
                self.stdout.write(f"  • {user.email} (id={user.pk})")
            return

        # Initialise service once — will raise early if credentials are missing
        try:
            svc = NombaPaymentService()
        except NombaError as exc:
            self.stderr.write(
                self.style.ERROR(
                    f"Cannot initialise NombaPaymentService: {exc}\n"
                    "Fix your Nomba credentials in .env and retry."
                )
            )
            return

        success_count = 0
        failure_count = 0

        for user in qs:
            full_name = f"{user.first_name} {user.last_name}".strip() or user.email
            try:
                wallet = svc.create_virtual_wallet(str(user.pk), account_name=full_name)
                user.nomba_account_ref       = wallet.get("accountRef", "")
                user.nomba_account_number    = wallet.get("bankAccountNumber", "")
                user.nomba_bank_code         = wallet.get("bankCode", "NMB")
                user.nomba_account_holder_id = wallet.get("accountHolderId", "") or wallet.get("id", "")
                user.save(update_fields=[
                    "nomba_account_ref", "nomba_account_number",
                    "nomba_bank_code", "nomba_account_holder_id",
                ])
                self.stdout.write(
                    self.style.SUCCESS(
                        f"  ✓ {user.email} → account {user.nomba_account_number}"
                    )
                )
                success_count += 1
                logger.info(
                    "backfill_nomba_wallets: provisioned wallet for user %s (%s): %s",
                    user.pk, user.email, user.nomba_account_number,
                )
            except NombaError as exc:
                self.stderr.write(
                    self.style.ERROR(f"  ✗ {user.email}: {type(exc).__name__}: {exc}")
                )
                failure_count += 1
                logger.error(
                    "backfill_nomba_wallets: failed for user %s (%s): %s",
                    user.pk, user.email, exc,
                )

        self.stdout.write("")
        self.stdout.write(
            self.style.SUCCESS(f"Done. {success_count} succeeded, {failure_count} failed.")
        )
        if failure_count:
            self.stdout.write(
                self.style.WARNING(
                    "Some wallets could not be provisioned. Check logs and retry after resolving the issue."
                )
            )
