from django.db import models
from django.conf import settings


class EscrowAgreement(models.Model):
    """
    Represents a full escrow contract between a buyer and a seller.
    Tracks the fund lifecycle from draft → active → completed/disputed/refunded.
    """

    class Status(models.TextChoices):
        DRAFT = 'draft', 'Draft'
        ACTIVE = 'active', 'Active'
        PENDING_PROOF = 'pending_proof', 'Pending Proof'
        COMPLETED = 'completed', 'Completed'
        DISPUTED = 'disputed', 'Disputed'
        REFUNDED = 'refunded', 'Refunded'

    buyer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name='buyer_agreements',
        on_delete=models.PROTECT,
    )
    seller = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name='seller_agreements',
        on_delete=models.PROTECT,
    )
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    currency = models.CharField(max_length=5, default='NGN')

    # Conditions
    raw_conditions = models.TextField(help_text="Original plain-language input from the user.")
    conditions = models.JSONField(
        default=list,
        help_text="AI-parsed structured milestones (list of milestone dicts).",
    )

    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.DRAFT,
        db_index=True,
    )
    deadline = models.DateTimeField()

    # Nomba / AI metadata
    nomba_transaction_ref = models.CharField(max_length=200, blank=True)
    ai_verdict = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Escrow Agreement'
        verbose_name_plural = 'Escrow Agreements'

    def __str__(self):
        return f"Agreement #{self.pk} — {self.buyer} → {self.seller} ({self.status})"

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def is_overdue(self) -> bool:
        from django.utils import timezone
        return self.deadline < timezone.now() and self.status not in (
            self.Status.COMPLETED, self.Status.REFUNDED,
        )

    def all_milestones_met(self) -> bool:
        return self.milestones.exists() and not self.milestones.filter(is_met=False).exists()


class Milestone(models.Model):
    """
    An individual deliverable within an EscrowAgreement.
    The agreement is only releasable when every milestone is met.
    """

    agreement = models.ForeignKey(
        EscrowAgreement,
        related_name='milestones',
        on_delete=models.CASCADE,
    )
    description = models.TextField()
    is_met = models.BooleanField(default=False)
    proof_url = models.URLField(blank=True)
    verified_at = models.DateTimeField(null=True, blank=True)

    # AI verification result
    ai_confidence = models.IntegerField(null=True, blank=True)
    ai_reason = models.TextField(blank=True)

    class Meta:
        ordering = ['id']

    def __str__(self):
        status = "✓" if self.is_met else "✗"
        return f"{status} Milestone #{self.pk}: {self.description[:60]}"
