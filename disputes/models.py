from django.db import models
from django.conf import settings


class Dispute(models.Model):
    """
    Represents a formal dispute raised against an EscrowAgreement.
    Either party can raise it; both parties submit evidence; Qwen arbitrates.
    """

    class Status(models.TextChoices):
        OPEN = 'open', 'Open'
        AWAITING_SELLER = 'awaiting_seller', 'Awaiting Seller Evidence'
        AWAITING_BUYER = 'awaiting_buyer', 'Awaiting Buyer Evidence'
        RESOLVED = 'resolved', 'Resolved'

    agreement = models.OneToOneField(
        'escrow.EscrowAgreement',
        on_delete=models.CASCADE,
        related_name='dispute',
    )
    raised_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name='raised_disputes',
    )

    buyer_evidence = models.TextField(blank=True)
    seller_evidence = models.TextField(blank=True)

    # AI arbitration result
    ai_ruling = models.JSONField(
        null=True,
        blank=True,
        help_text='{"ruling": "buyer|seller|split", "split_ratio": "...", "reasoning": "..."}',
    )

    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.OPEN,
        db_index=True,
    )
    resolved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Dispute'
        verbose_name_plural = 'Disputes'

    def __str__(self):
        return f"Dispute #{self.pk} — Agreement #{self.agreement_id} ({self.status})"
