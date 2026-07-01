from rest_framework import serializers
from .models import Dispute


class DisputeSerializer(serializers.ModelSerializer):
    raised_by_email = serializers.EmailField(source='raised_by.email', read_only=True)
    agreement_amount = serializers.DecimalField(
        source='agreement.amount', max_digits=12, decimal_places=2, read_only=True
    )

    class Meta:
        model = Dispute
        fields = [
            'id', 'agreement', 'raised_by', 'raised_by_email',
            'agreement_amount', 'buyer_evidence', 'seller_evidence',
            'ai_ruling', 'status', 'resolved_at', 'created_at', 'updated_at',
        ]
        read_only_fields = [
            'raised_by', 'ai_ruling', 'status', 'resolved_at', 'created_at', 'updated_at',
        ]

    def validate_agreement(self, agreement):
        from escrow.models import EscrowAgreement
        request = self.context.get('request')

        if agreement.status == EscrowAgreement.Status.DISPUTED:
            raise serializers.ValidationError("A dispute already exists for this agreement.")

        if agreement.status not in (
            EscrowAgreement.Status.ACTIVE, EscrowAgreement.Status.PENDING_PROOF
        ):
            raise serializers.ValidationError(
                "Disputes can only be raised on active or pending-proof agreements."
            )

        if request and request.user not in (agreement.buyer, agreement.seller):
            raise serializers.ValidationError("You are not a party to this agreement.")

        return agreement


class SubmitEvidenceSerializer(serializers.Serializer):
    evidence = serializers.CharField(min_length=10)
