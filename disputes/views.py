import logging

from django.utils import timezone
from django.shortcuts import get_object_or_404
from rest_framework import viewsets, permissions, status
from rest_framework.decorators import action
from rest_framework.response import Response

from ai_engine.services import QwenAIService
from payments.services import NombaPaymentService
from escrow.models import EscrowAgreement
from .models import Dispute
from .serializers import DisputeSerializer, SubmitEvidenceSerializer

logger = logging.getLogger(__name__)


class DisputeViewSet(viewsets.ModelViewSet):
    """
    Dispute lifecycle:
      POST /api/disputes/                      → Open a dispute
      POST /api/disputes/{id}/submit-evidence/ → Add buyer or seller evidence
      POST /api/disputes/{id}/resolve/         → AI ruling + fund disbursement
    """

    serializer_class = DisputeSerializer
    permission_classes = [permissions.IsAuthenticated]
    http_method_names = ['get', 'post', 'head', 'options']  # no PUT/PATCH/DELETE

    def get_queryset(self):
        user = self.request.user
        return Dispute.objects.filter(
            agreement__buyer=user
        ) | Dispute.objects.filter(agreement__seller=user)

    def perform_create(self, serializer):
        agreement = serializer.validated_data['agreement']

        dispute = serializer.save(raised_by=self.request.user)

        # Escalate agreement to disputed
        agreement.status = EscrowAgreement.Status.DISPUTED
        agreement.save(update_fields=['status', 'updated_at'])

        return dispute

    # ------------------------------------------------------------------
    # Action: submit_evidence
    # POST /api/disputes/{id}/submit-evidence/
    # ------------------------------------------------------------------

    @action(detail=True, methods=['post'], url_path='submit-evidence')
    def submit_evidence(self, request, pk=None):
        """
        Adds the authenticated user's evidence to the dispute.
        Buyers update buyer_evidence; sellers update seller_evidence.
        """
        dispute = get_object_or_404(self.get_queryset(), pk=pk)

        if dispute.status == Dispute.Status.RESOLVED:
            return Response(
                {"detail": "This dispute has already been resolved."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        serializer = SubmitEvidenceSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        evidence_text = serializer.validated_data['evidence']

        agreement = dispute.agreement
        if request.user == agreement.buyer:
            dispute.buyer_evidence = evidence_text
            dispute.status = Dispute.Status.AWAITING_SELLER
        elif request.user == agreement.seller:
            dispute.seller_evidence = evidence_text
            dispute.status = Dispute.Status.AWAITING_BUYER
        else:
            return Response(
                {"detail": "You are not a party to this dispute."},
                status=status.HTTP_403_FORBIDDEN,
            )

        dispute.save(update_fields=['buyer_evidence', 'seller_evidence', 'status', 'updated_at'])

        return Response(DisputeSerializer(dispute, context={'request': request}).data)

    # ------------------------------------------------------------------
    # Action: resolve
    # POST /api/disputes/{id}/resolve/
    # ------------------------------------------------------------------

    @action(detail=True, methods=['post'], url_path='resolve')
    def resolve(self, request, pk=None):
        """
        Triggers AI arbitration and disburses funds according to the ruling.

        Ruling outcomes:
          - "buyer"  → full refund to buyer
          - "seller" → full payout to seller
          - "split"  → proportional transfer based on split_ratio
        """
        dispute = get_object_or_404(self.get_queryset(), pk=pk)

        if dispute.status == Dispute.Status.RESOLVED:
            return Response(
                {"detail": "Already resolved."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        agreement = dispute.agreement
        summary = (
            f"Agreement #{agreement.pk}: {agreement.raw_conditions}. "
            f"Amount: {agreement.amount} {agreement.currency}."
        )

        # --- AI Ruling ---
        try:
            ruling = QwenAIService().resolve_dispute(
                agreement_summary=summary,
                buyer_claim=dispute.buyer_evidence or "(no evidence submitted)",
                seller_claim=dispute.seller_evidence or "(no evidence submitted)",
            )
        except Exception as exc:
            logger.error("AI dispute resolution failed for dispute %s: %s", pk, exc)
            return Response(
                {"detail": "AI arbitration failed. Please try again."},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        dispute.ai_ruling = ruling
        verdict = ruling.get('ruling', 'split')
        amount = float(agreement.amount)
        nomba = NombaPaymentService()
        ref = agreement.nomba_transaction_ref

        # --- Fund Disbursement ---
        try:
            if verdict == 'buyer':
                nomba.refund_to_buyer(
                    amount=amount,
                    buyer_account_number=agreement.buyer.nomba_account_number,
                    buyer_bank_code=agreement.buyer.nomba_bank_code,
                    ref=ref,
                    source_account_id=agreement.buyer.nomba_account_holder_id,
                )
                agreement.status = EscrowAgreement.Status.REFUNDED

            elif verdict == 'seller':
                nomba.release_to_seller(
                    amount=amount,
                    seller_account_number=agreement.seller.nomba_account_number,
                    seller_bank_code=agreement.seller.nomba_bank_code,
                    ref=ref,
                    source_account_id=agreement.buyer.nomba_account_holder_id,
                )
                agreement.status = EscrowAgreement.Status.COMPLETED

            else:  # split
                ratio_str = ruling.get('split_ratio', '50%:50%')
                try:
                    buyer_pct, seller_pct = [
                        float(p.strip('%')) / 100 for p in ratio_str.split(':')
                    ]
                except Exception:
                    buyer_pct, seller_pct = 0.5, 0.5

                if buyer_pct > 0:
                    nomba.refund_to_buyer(
                        amount=round(amount * buyer_pct, 2),
                        buyer_account_number=agreement.buyer.nomba_account_number,
                        buyer_bank_code=agreement.buyer.nomba_bank_code,
                        ref=f"{ref}-split-buyer",
                        source_account_id=agreement.buyer.nomba_account_holder_id,
                    )
                if seller_pct > 0:
                    nomba.release_to_seller(
                        amount=round(amount * seller_pct, 2),
                        seller_account_number=agreement.seller.nomba_account_number,
                        seller_bank_code=agreement.seller.nomba_bank_code,
                        ref=f"{ref}-split-seller",
                        source_account_id=agreement.buyer.nomba_account_holder_id,
                    )
                agreement.status = EscrowAgreement.Status.COMPLETED

        except Exception as exc:
            logger.error("Fund disbursement failed for dispute %s: %s", pk, exc)
            return Response(
                {"detail": "AI ruling recorded but fund disbursement failed. Contact support."},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        agreement.ai_verdict = ruling.get('reasoning', '')
        agreement.save(update_fields=['status', 'ai_verdict', 'updated_at'])

        dispute.status = Dispute.Status.RESOLVED
        dispute.resolved_at = timezone.now()
        dispute.save(update_fields=['ai_ruling', 'status', 'resolved_at', 'updated_at'])

        return Response(DisputeSerializer(dispute, context={'request': request}).data)
