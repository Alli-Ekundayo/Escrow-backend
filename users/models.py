from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    """
    TrustFlow custom user.
    Uses email as the primary login identifier.
    """

    email = models.EmailField(unique=True)
    phone_number = models.CharField(max_length=20, unique=True)
    bvn_verified = models.BooleanField(default=False)
    trust_score = models.FloatField(default=0.0)

    # Nomba virtual account (provisioned at registration)
    # accountRef returned by POST /v1/accounts/virtual
    nomba_account_ref = models.CharField(max_length=100, blank=True)
    # 10-digit NUBAN assigned by Nomba (bankAccountNumber) — buyers share this for incoming transfers
    nomba_account_number = models.CharField(max_length=20, blank=True)
    # Bank code for this virtual account (always "NMB" or Nomba's code)
    nomba_bank_code = models.CharField(max_length=10, blank=True)
    # Nomba internal account UUID (accountHolderId) — used as the source ID in
    # /v2/transfers/bank/{id} to send money OUT of the virtual account (e.g. on release/refund)
    nomba_account_holder_id = models.CharField(max_length=100, blank=True)

    # Local wallet ledger balance (since Nomba virtual accounts do not hold balances themselves)
    wallet_balance = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)

    USERNAME_FIELD = 'email'
    # username + phone required at registration
    REQUIRED_FIELDS = ['username', 'phone_number']

    class Meta:
        verbose_name = 'User'
        verbose_name_plural = 'Users'
        ordering = ['-date_joined']

    def __str__(self):
        return f"{self.get_full_name() or self.email}"

    @property
    def has_nomba_account(self) -> bool:
        """True when a Nomba virtual account has been successfully provisioned."""
        return bool(self.nomba_account_number)
