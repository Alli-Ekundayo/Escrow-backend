from rest_framework import serializers
from .models import EscrowAgreement, Milestone


class MilestoneSerializer(serializers.ModelSerializer):
    class Meta:
        model = Milestone
        fields = [
            'id', 'description', 'is_met', 'proof_url', 'proof_description',
            'verified_at', 'ai_confidence', 'ai_reason',
        ]
        read_only_fields = ['is_met', 'verified_at', 'ai_confidence', 'ai_reason', 'proof_description']


class EscrowAgreementSerializer(serializers.ModelSerializer):
    milestones = MilestoneSerializer(many=True, read_only=True)
    buyer_email = serializers.EmailField(source='buyer.email', read_only=True)
    seller_email = serializers.EmailField(source='seller.email', read_only=True)

    class Meta:
        model = EscrowAgreement
        fields = [
            'id', 'buyer', 'buyer_email', 'seller', 'seller_email',
            'amount', 'currency', 'raw_conditions', 'conditions',
            'status', 'deadline', 'nomba_transaction_ref',
            'ai_verdict', 'milestones', 'created_at', 'updated_at',
        ]
        read_only_fields = [
            'buyer', 'status', 'nomba_transaction_ref',
            'ai_verdict', 'created_at', 'updated_at',
        ]

    def validate(self, data):
        request = self.context.get('request')
        if request and data.get('seller') == request.user:
            raise serializers.ValidationError("You cannot be both buyer and seller.")
        return data

    def create(self, validated_data):
        validated_data['buyer'] = self.context['request'].user
        return super().create(validated_data)


class DraftWithAISerializer(serializers.Serializer):
    """Input for the draft_with_ai action."""
    raw_conditions = serializers.CharField(min_length=20)
    seller_id = serializers.IntegerField()
    amount = serializers.DecimalField(max_digits=12, decimal_places=2)
    currency = serializers.CharField(max_length=5, default='NGN')
    deadline = serializers.DateTimeField()


class SubmitProofSerializer(serializers.Serializer):
    """Input for the submit_proof action."""
    milestone_id = serializers.IntegerField()
    proof_file = serializers.ImageField(required=False)
    proof_description = serializers.CharField(required=False)

    def validate(self, data):
        if not data.get('proof_file') and not data.get('proof_description'):
            raise serializers.ValidationError(
                "Provide at least a proof_file or proof_description."
            )
        return data
