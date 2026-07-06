import logging

from django.utils import timezone
from django.shortcuts import get_object_or_404
from rest_framework import viewsets, permissions, status
from rest_framework.decorators import action
from rest_framework.response import Response

from ai_engine.services import QwenAIService
from payments.services import (
    NombaPaymentService,
    NombaInsufficientFundsError,
    NombaUnavailableError,
    NombaAPIError,
)
from .models import EscrowAgreement, Milestone
from .serializers import (
    EscrowAgreementSerializer,
    DraftWithAISerializer,
    SubmitProofSerializer,
    MilestoneSerializer,
)
from .tasks import verify_proof_task, update_trust_scores_task

logger = logging.getLogger(__name__)


class AgreementViewSet(viewsets.ModelViewSet):
    """
    Full CRUD on EscrowAgreement objects, plus four lifecycle actions:
      - draft_with_ai    → AI parses plain-language conditions
      - lock_funds       → Hold buyer funds via Nomba
      - submit_proof     → Upload proof + trigger AI verification
      - release_funds    → Pay out seller via Nomba
    """

    serializer_class = EscrowAgreementSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        """Return only agreements where the authenticated user is buyer or seller."""
        user = self.request.user
        return EscrowAgreement.objects.filter(
            buyer=user
        ) | EscrowAgreement.objects.filter(seller=user)

    # ------------------------------------------------------------------
    # Action: draft_with_ai
    # POST /api/escrow/draft-with-ai/
    # ------------------------------------------------------------------

    @action(detail=False, methods=['post'], url_path='draft-with-ai')
    def draft_with_ai(self, request):
        """
        Accepts plain-language conditions, calls Qwen to parse them into
        structured milestones, and creates a Draft EscrowAgreement.
        """
        serializer = DraftWithAISerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        # Validate seller exists
        from django.contrib.auth import get_user_model
        User = get_user_model()
        seller = get_object_or_404(User, pk=data['seller_id'])

        if seller == request.user:
            return Response(
                {"detail": "You cannot be both buyer and seller."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Ask Qwen to parse conditions
        try:
            ai_result = QwenAIService().parse_conditions(data['raw_conditions'])
            structured_conditions = ai_result.get('milestones', [])
        except Exception as exc:
            logger.error("AI condition parsing failed: %s", exc)
            return Response(
                {"detail": "AI parsing failed. Please try again or enter conditions manually."},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        # Create the draft agreement
        agreement = EscrowAgreement.objects.create(
            buyer=request.user,
            seller=seller,
            amount=data['amount'],
            currency=data.get('currency', 'NGN'),
            raw_conditions=data['raw_conditions'],
            conditions=structured_conditions,
            deadline=data['deadline'],
            status=EscrowAgreement.Status.DRAFT,
        )

        # Create Milestone objects from AI output
        for m in structured_conditions:
            Milestone.objects.create(
                agreement=agreement,
                description=m.get('description', ''),
            )

        return Response(
            EscrowAgreementSerializer(agreement, context={'request': request}).data,
            status=status.HTTP_201_CREATED,
        )

    # ------------------------------------------------------------------
    @action(detail=True, methods=['post'], url_path='lock-funds')
    def lock_funds(self, request, pk=None):
        """
        Transitions the agreement status to AWAITING_PAYMENT and returns
        bank transfer instructions for the buyer to fund their virtual account.
        """
        agreement = get_object_or_404(self.get_queryset(), pk=pk)

        if agreement.buyer != request.user:
            return Response(
                {"detail": "Only the buyer can lock funds."},
                status=status.HTTP_403_FORBIDDEN,
            )

        if agreement.status != EscrowAgreement.Status.DRAFT:
            return Response(
                {"detail": f"Cannot lock funds for an agreement in '{agreement.status}' status."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        buyer = request.user
        if not buyer.has_nomba_account:
            return Response(
                {
                    "detail": (
                        "Buyer does not have a linked Nomba virtual wallet. "
                        "Wallet provisioning may have failed at registration — "
                        "please contact support to backfill your wallet."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Generate a unique transaction reference for tracking
        ref = f"tf-{agreement.pk}-{int(timezone.now().timestamp())}"

        # Transition status to AWAITING_PAYMENT
        agreement.status = EscrowAgreement.Status.AWAITING_PAYMENT
        agreement.nomba_transaction_ref = ref
        agreement.save(update_fields=['status', 'nomba_transaction_ref', 'updated_at'])

        # Return payment instructions so front-end can display it nicely
        return Response({
            "detail": "Escrow initialized. Please complete bank transfer to fund the escrow.",
            "payment_instructions": {
                "bank_name": "Nomba MFB",
                "bank_code": buyer.nomba_bank_code or "NMB",
                "account_number": buyer.nomba_account_number,
                "amount": str(agreement.amount),
                "currency": agreement.currency,
                "payment_reference": ref,
            },
            "agreement": EscrowAgreementSerializer(agreement, context={'request': request}).data
        }, status=status.HTTP_200_OK)

    # ------------------------------------------------------------------
    # Action: submit_proof
    # POST /api/escrow/{id}/submit-proof/
    # ------------------------------------------------------------------

    @action(detail=True, methods=['post'], url_path='submit-proof')
    def submit_proof(self, request, pk=None):
        """
        Seller submits proof for a specific milestone.
        Uploads the file to Cloudinary (via Django storage), then runs
        AI verification synchronously and marks the milestone.
        """
        agreement = get_object_or_404(self.get_queryset(), pk=pk)

        if agreement.seller != request.user:
            return Response(
                {"detail": "Only the seller can submit proof."},
                status=status.HTTP_403_FORBIDDEN,
            )

        if agreement.status not in (
            EscrowAgreement.Status.ACTIVE, EscrowAgreement.Status.PENDING_PROOF
        ):
            return Response(
                {"detail": "Agreement is not in an active state."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        serializer = SubmitProofSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        milestone = get_object_or_404(
            Milestone, pk=data['milestone_id'], agreement=agreement
        )

        # Save proof data
        update_fields = []
        if data.get('proof_file'):
            from django.core.files.storage import default_storage
            file = data['proof_file']
            path = default_storage.save(f"proofs/{agreement.pk}/{file.name}", file)
            milestone.proof_url = default_storage.url(path)
            update_fields.append('proof_url')

        if data.get('proof_description'):
            milestone.proof_description = data['proof_description']
            update_fields.append('proof_description')

        if update_fields:
            milestone.save(update_fields=update_fields)

        # Run AI verification (inline — no Celery)
        proof_text = milestone.proof_description or f"File uploaded: {milestone.proof_url}"
        verify_proof_task(milestone.pk, proof_text)

        # Refresh from DB after task update
        milestone.refresh_from_db()

        # If all milestones met, advance status
        if agreement.all_milestones_met():
            agreement.status = EscrowAgreement.Status.PENDING_PROOF
            agreement.save(update_fields=['status', 'updated_at'])

        return Response(
            MilestoneSerializer(milestone).data,
            status=status.HTTP_200_OK,
        )

    # ------------------------------------------------------------------
    # Action: release_funds
    # POST /api/escrow/{id}/release-funds/
    # ------------------------------------------------------------------

    @action(detail=True, methods=['post'], url_path='release-funds')
    def release_funds(self, request, pk=None):
        """
        Buyer manually releases held funds to the seller.
        Agreement moves to Completed.
        """
        agreement = get_object_or_404(self.get_queryset(), pk=pk)

        if agreement.buyer != request.user:
            return Response(
                {"detail": "Only the buyer can release funds."},
                status=status.HTTP_403_FORBIDDEN,
            )

        if agreement.status not in (
            EscrowAgreement.Status.ACTIVE, EscrowAgreement.Status.PENDING_PROOF
        ):
            return Response(
                {"detail": "Funds can only be released for active agreements."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        seller = agreement.seller
        if not seller.has_nomba_account:
            return Response(
                {"detail": "Seller does not have a linked Nomba virtual account."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            NombaPaymentService().release_to_seller(
                amount=float(agreement.amount),
                seller_account_number=seller.nomba_account_number,
                seller_bank_code=seller.nomba_bank_code,
                ref=agreement.nomba_transaction_ref,
                source_account_id=agreement.buyer.nomba_account_holder_id,
            )
        except NombaUnavailableError as exc:
            logger.error("Nomba unavailable during release for agreement %s: %s", pk, exc)
            return Response(
                {"detail": "Payment gateway is temporarily unavailable. Please try again shortly."},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        except (NombaAPIError, Exception) as exc:
            logger.error("Nomba release_to_seller failed for agreement %s: %s", pk, exc)
            return Response(
                {"detail": "Payout failed. Please try again."},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        agreement.status = EscrowAgreement.Status.COMPLETED
        agreement.save(update_fields=['status', 'updated_at'])

        # Recalculate trust scores for both parties (inline)
        update_trust_scores_task(agreement.buyer_id)
        update_trust_scores_task(agreement.seller_id)

        return Response(
            EscrowAgreementSerializer(agreement, context={'request': request}).data
        )
