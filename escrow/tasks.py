"""
Background task functions for the escrow app.

Since we're not using Celery/Redis, these are plain functions called
synchronously in the views. The function signatures mirror what Celery
tasks would look like, so migrating later (just add @shared_task) is trivial.
"""

import logging

from django.utils import timezone

logger = logging.getLogger(__name__)


def verify_proof_task(milestone_id: int, proof_description: str) -> None:
    """
    Runs AI proof verification for a single milestone.
    Updates Milestone.is_met, ai_confidence, and ai_reason.
    """
    from .models import Milestone
    from ai_engine.services import QwenAIService

    try:
        milestone = Milestone.objects.get(pk=milestone_id)
    except Milestone.DoesNotExist:
        logger.error("verify_proof_task: milestone %s not found", milestone_id)
        return

    try:
        result = QwenAIService().verify_proof(
            milestone_description=milestone.description,
            proof_description=proof_description,
        )
        satisfied = result.get('satisfied', False)
        confidence = result.get('confidence', 0)
        reason = result.get('reason', '')
    except Exception as exc:
        logger.error("AI proof verification failed for milestone %s: %s", milestone_id, exc)
        return

    milestone.is_met = satisfied
    milestone.ai_confidence = confidence
    milestone.ai_reason = reason
    if satisfied:
        milestone.verified_at = timezone.now()
    milestone.save(update_fields=['is_met', 'ai_confidence', 'ai_reason', 'verified_at'])

    logger.info(
        "Milestone %s verification: satisfied=%s confidence=%s",
        milestone_id, satisfied, confidence,
    )


def check_agreement_deadlines_task() -> None:
    """
    Escalates all overdue active/pending_proof agreements to 'disputed'.
    Intended to be called by a management command or cron job.
    """
    from .models import EscrowAgreement

    now = timezone.now()
    overdue = EscrowAgreement.objects.filter(
        deadline__lt=now,
        status__in=[EscrowAgreement.Status.ACTIVE, EscrowAgreement.Status.PENDING_PROOF],
    )
    count = overdue.update(status=EscrowAgreement.Status.DISPUTED)
    logger.info("check_agreement_deadlines_task: escalated %d agreement(s) to disputed", count)


def update_trust_scores_task(user_id: int) -> None:
    """
    Recalculates a user's trust score based on their transaction history.

    Formula:
        score = (completed_count / total_count) * 100  capped at 100.0
    """
    from django.contrib.auth import get_user_model
    from .models import EscrowAgreement

    User = get_user_model()

    try:
        user = User.objects.get(pk=user_id)
    except User.DoesNotExist:
        logger.error("update_trust_scores_task: user %s not found", user_id)
        return

    total = EscrowAgreement.objects.filter(
        buyer=user
    ).count() + EscrowAgreement.objects.filter(seller=user).count()

    completed = EscrowAgreement.objects.filter(
        buyer=user, status=EscrowAgreement.Status.COMPLETED
    ).count() + EscrowAgreement.objects.filter(
        seller=user, status=EscrowAgreement.Status.COMPLETED
    ).count()

    score = round((completed / total) * 100, 2) if total > 0 else 0.0
    user.trust_score = min(score, 100.0)
    user.save(update_fields=['trust_score'])

    logger.info("Trust score updated for user %s: %.2f", user_id, user.trust_score)
