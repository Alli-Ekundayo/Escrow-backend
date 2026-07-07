import logging

from django.db.models import Q
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
        return EscrowAgreement.objects.filter(Q(buyer=user) | Q(seller=user))

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
        If the buyer already has enough balance in their wallet, attempts to debit
        them immediately and transition directly to ACTIVE.
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
            # Attempt on-demand wallet provisioning
            try:
                svc = NombaPaymentService()
                full_name = f"{buyer.first_name} {buyer.last_name}".strip() or buyer.email
                wallet = svc.create_virtual_wallet(str(buyer.id), account_name=full_name)
                buyer.nomba_account_ref       = wallet.get('accountRef', '')
                buyer.nomba_account_number    = wallet.get('bankAccountNumber', '')
                buyer.nomba_bank_code         = wallet.get('bankCode', 'NMB')
                buyer.nomba_account_holder_id = wallet.get('accountHolderId', '') or wallet.get('id', '')
                buyer.save(update_fields=[
                    'nomba_account_ref', 'nomba_account_number',
                    'nomba_bank_code', 'nomba_account_holder_id',
                ])
            except Exception as exc:
                logger.error("On-demand wallet provisioning failed for user %s during lock_funds: %s", buyer.id, exc)
                return Response(
                    {
                        "detail": (
                            f"Virtual wallet provisioning failed: {str(exc)}. "
                            "Please contact support to backfill your wallet or retry later."
                        )
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

        # Generate a unique transaction reference for tracking
        ref = f"tf-{agreement.pk}-{int(timezone.now().timestamp())}"

        # 1. Try to fund directly if buyer has sufficient wallet balance
        available_balance = float(buyer.wallet_balance)

        if available_balance >= float(agreement.amount):
            try:
                from decimal import Decimal
                from django.db import transaction

                with transaction.atomic():
                    # Refresh buyer and lock to avoid race conditions
                    buyer_refreshed = User.objects.select_for_update().get(pk=buyer.pk)
                    if buyer_refreshed.wallet_balance >= agreement.amount:
                        buyer_refreshed.wallet_balance -= agreement.amount
                        buyer_refreshed.save(update_fields=['wallet_balance'])

                        # Successful direct funding!
                        agreement.status = EscrowAgreement.Status.ACTIVE
                        agreement.nomba_transaction_ref = ref
                        agreement.save(update_fields=['status', 'nomba_transaction_ref', 'updated_at'])

                        return Response({
                            "detail": "Agreement successfully funded from your existing wallet balance.",
                            "status": "ACTIVE",
                            "agreement": EscrowAgreementSerializer(agreement, context={'request': request}).data
                        }, status=status.HTTP_200_OK)
            except Exception as exc:
                logger.warning(
                    "Auto-funding from local balance failed for agreement %s: %s — falling back to bank transfer.",
                    pk, exc
                )

        # 2. Otherwise, fall back to normal bank transfer flow
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

        # If all milestones met, advance status to PENDING_PROOF (signals buyer to review & release)
        if agreement.all_milestones_met():
            agreement.status = EscrowAgreement.Status.PENDING_PROOF
            agreement.save(update_fields=['status', 'updated_at'])
        elif agreement.status == EscrowAgreement.Status.ACTIVE:
            # At least one milestone still unmet; keep the agreement ACTIVE
            pass

        return Response(
            MilestoneSerializer(milestone).data,
            status=status.HTTP_200_OK,
        )

    # ------------------------------------------------------------------
    # Action: verify_payment
    # POST /api/escrow/{id}/verify-payment/
    # ------------------------------------------------------------------

    @action(detail=True, methods=['post'], url_path='verify-payment')
    def verify_payment(self, request, pk=None):
        """
        Manually checks whether payment has been received for an AWAITING_PAYMENT
        agreement and activates it if confirmed.

        Use this when a buyer has transferred funds but the Nomba webhook either
        failed to fire or failed to match the agreement (e.g. amount rounding, or
        multiple pending agreements caused the webhook to credit the wallet instead).

        Checks the buyer's wallet balance as a secondary fallback:
          - If the buyer has enough wallet balance, deducts it and activates.

        Returns the updated agreement on success.
        """
        agreement = get_object_or_404(self.get_queryset(), pk=pk)

        if agreement.buyer != request.user:
            return Response(
                {"detail": "Only the buyer can verify payment."},
                status=status.HTTP_403_FORBIDDEN,
            )

        if agreement.status == EscrowAgreement.Status.ACTIVE:
            return Response(
                {
                    "detail": "Agreement is already active.",
                    "agreement": EscrowAgreementSerializer(agreement, context={"request": request}).data,
                },
                status=status.HTTP_200_OK,
            )

        if agreement.status != EscrowAgreement.Status.AWAITING_PAYMENT:
            return Response(
                {"detail": f"Cannot verify payment for an agreement in '{agreement.status}' status."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        buyer = request.user
        from decimal import Decimal
        from django.db import transaction as db_transaction

        # ── Strategy A: buyer's wallet already has enough balance ───────────────
        # This covers the case where the webhook received the payment but credited the
        # wallet instead of the agreement (e.g. Strategy 4 fallback had no match).
        with db_transaction.atomic():
            buyer_refreshed = type(buyer).objects.select_for_update().get(pk=buyer.pk)
            if buyer_refreshed.wallet_balance >= agreement.amount:
                buyer_refreshed.wallet_balance -= agreement.amount
                buyer_refreshed.save(update_fields=["wallet_balance"])
                agreement.status = EscrowAgreement.Status.ACTIVE
                agreement.save(update_fields=["status", "updated_at"])
                logger.info(
                    "verify_payment: Agreement %s activated from wallet balance for user %s. "
                    "Deducted NGN %s, new balance: %s",
                    agreement.id, buyer.email, agreement.amount, buyer_refreshed.wallet_balance
                )
                return Response(
                    {
                        "detail": "Payment verified. Agreement is now active (funded from your wallet).",
                        "agreement": EscrowAgreementSerializer(agreement, context={"request": request}).data,
                    },
                    status=status.HTTP_200_OK,
                )

        # ── Strategy B: query Nomba transaction history ─────────────────────────
        # Checks the last 24h of credits on the buyer's virtual account for a
        # matching amount. Activates the agreement if one is found.
        if not buyer.has_nomba_account:
            return Response(
                {
                    "detail": (
                        "No payment found in your wallet and your Nomba virtual account is "
                        "not yet provisioned. Please complete the bank transfer first."
                    )
                },
                status=status.HTTP_402_PAYMENT_REQUIRED,
            )

        try:
            svc = NombaPaymentService()
            import datetime
            from django.utils import timezone as tz

            # Query recent transactions on the buyer's virtual account
            since = (tz.now() - datetime.timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
            tx_resp = svc._get(
                f"/v1/accounts/transactions",
                params={
                    "accountRef": buyer.nomba_account_ref,
                    "startDate": since,
                    "type": "CREDIT",
                },
            )
            transactions = tx_resp.get("data", {}).get("records", tx_resp.get("data", []))

            expected_kobo = int(round(float(agreement.amount) * 100))
            matched_tx = None
            for tx in (transactions if isinstance(transactions, list) else []):
                tx_amount = tx.get("amount") or tx.get("amountReceived", 0)
                # Allow ±1 kobo tolerance for floating point
                if abs(int(tx_amount) - expected_kobo) <= 1:
                    matched_tx = tx
                    break

            if matched_tx:
                with db_transaction.atomic():
                    agr = EscrowAgreement.objects.select_for_update().get(
                        pk=agreement.pk, status=EscrowAgreement.Status.AWAITING_PAYMENT
                    )
                    agr.status = EscrowAgreement.Status.ACTIVE
                    agr.save(update_fields=["status", "updated_at"])
                logger.info(
                    "verify_payment: Agreement %s activated via Nomba transaction history. "
                    "tx_ref=%s, amount=%s kobo",
                    agreement.id, matched_tx.get("reference"), matched_tx.get("amount")
                )
                agreement.refresh_from_db()
                return Response(
                    {
                        "detail": "Payment confirmed. Agreement is now active.",
                        "agreement": EscrowAgreementSerializer(agreement, context={"request": request}).data,
                    },
                    status=status.HTTP_200_OK,
                )

        except Exception as exc:
            logger.warning(
                "verify_payment: Nomba transaction history lookup failed for agreement %s: %s",
                agreement.id, exc
            )
            # Fall through to the final not-found response

        return Response(
            {
                "detail": (
                    "No matching payment found yet. If you've just transferred, please wait "
                    "1–2 minutes for Nomba to process and try again."
                )
            },
            status=status.HTTP_402_PAYMENT_REQUIRED,
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
