from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from django.contrib.auth import get_user_model

from payments.services import NombaPaymentService

User = get_user_model()


class RegisterSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, min_length=8)

    class Meta:
        model = User
        fields = ['id', 'email', 'username', 'first_name', 'last_name', 'phone_number', 'password']
        extra_kwargs = {
            'email': {'required': True},
            'phone_number': {'required': True},
        }

    def validate_phone_number(self, value):
        """Basic format guard — extend with proper libphonenumber if needed."""
        cleaned = value.strip()
        if not cleaned.startswith('+'):
            raise serializers.ValidationError("Phone number must include country code, e.g. +2348012345678.")
        return cleaned

    def create(self, validated_data):
        password = validated_data.pop('password')
        user = User(**validated_data)
        user.set_password(password)
        user.save()

        # Provision a Nomba virtual wallet for this user
        try:
            svc = NombaPaymentService()
            wallet = svc.create_virtual_wallet(str(user.id))
            user.nomba_wallet_id = wallet.get('walletId', '')
            user.save(update_fields=['nomba_wallet_id'])
        except Exception:
            # Wallet creation is non-fatal at registration time
            pass

        return user


class UserProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = [
            'id', 'email', 'username', 'first_name', 'last_name',
            'phone_number', 'bvn_verified', 'trust_score', 'nomba_wallet_id',
            'date_joined',
        ]
        read_only_fields = ['id', 'email', 'bvn_verified', 'trust_score', 'nomba_wallet_id', 'date_joined']


class CustomTokenObtainPairSerializer(TokenObtainPairSerializer):
    """Adds user data to the token response."""

    def validate(self, attrs):
        data = super().validate(attrs)
        data['user'] = UserProfileSerializer(self.user).data
        return data
