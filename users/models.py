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
    nomba_wallet_id = models.CharField(max_length=100, blank=True)

    USERNAME_FIELD = 'email'
    # username + phone required at registration
    REQUIRED_FIELDS = ['username', 'phone_number']

    class Meta:
        verbose_name = 'User'
        verbose_name_plural = 'Users'
        ordering = ['-date_joined']

    def __str__(self):
        return f"{self.get_full_name() or self.email}"
