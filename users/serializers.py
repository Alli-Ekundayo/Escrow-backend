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
            # username is optional — auto-generated from email if not provided
            'username': {'required': False, 'allow_blank': True},
        }

    def validate_phone_number(self, value):
        """Basic format guard — extend with proper libphonenumber if needed."""
        cleaned = value.strip()
        if not cleaned.startswith('+'):
            raise serializers.ValidationError("Phone number must include country code, e.g. +2348012345678.")
        return cleaned

    def validate(self, data):
        # Auto-generate username from the email local-part if not supplied
        if not data.get('username'):
            base = data['email'].split('@')[0][:149]  # AbstractUser.username max_length=150
            candidate = base
            suffix = 1
            while User.objects.filter(username=candidate).exists():
                candidate = f"{base}{suffix}"
                suffix += 1
            data['username'] = candidate
        return data

    def create(self, validated_data):
        password = validated_data.pop('password')
        user = User(**validated_data)
        user.set_password(password)
        user.save()

        # Provision a Nomba virtual wallet for this user
        try:
            svc = NombaPaymentService()
            full_name = f"{user.first_name} {user.last_name}".strip() or user.email
            wallet = svc.create_virtual_wallet(str(user.id), account_name=full_name)
            user.nomba_account_ref       = wallet.get('accountRef', '')
            user.nomba_account_number    = wallet.get('bankAccountNumber', '')
            user.nomba_bank_code         = wallet.get('bankCode', 'NMB')
            # accountHolderId is Nomba's internal UUID for this virtual account.
            # It is used as the source in /v2/transfers/bank/{id} on fund release/refund.
            user.nomba_account_holder_id = wallet.get('accountHolderId', '') or wallet.get('id', '')
            user.save(update_fields=[
                'nomba_account_ref', 'nomba_account_number',
                'nomba_bank_code', 'nomba_account_holder_id',
            ])
        except Exception as exc:
            # Wallet creation is non-fatal at registration time.
            # Run `python manage.py backfill_nomba_wallets` to retry for users
            # whose wallet provisioning failed silently here.
            import logging
            logging.getLogger(__name__).warning(
                "Nomba wallet provisioning failed for user %s (%s). "
                "Error: %s — %s. "
                "Run `manage.py backfill_nomba_wallets` to retry.",
                user.id,
                user.email,
                type(exc).__name__,
                exc,
            )

        return user


class UserProfileSerializer(serializers.ModelSerializer):
    wallet_balance = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            'id', 'email', 'username', 'first_name', 'last_name',
            'phone_number', 'bvn_verified', 'trust_score',
            'nomba_account_ref', 'nomba_account_number', 'nomba_bank_code',
            'wallet_balance', 'date_joined',
        ]
        read_only_fields = [
            'id', 'email', 'bvn_verified', 'trust_score',
            'nomba_account_ref', 'nomba_account_number', 'nomba_bank_code',
            'wallet_balance', 'date_joined',
        ]

    def get_wallet_balance(self, obj) -> float:
        if obj.nomba_account_holder_id:
            try:
                from payments.services import NombaPaymentService
                balance_data = NombaPaymentService().get_account_balance(obj.nomba_account_holder_id)
                return float(balance_data.get("amount", 0.0))
            except Exception:
                return 0.0
        return 0.0


class UserSearchSerializer(serializers.ModelSerializer):
    """Safe public-facing fields only — used by the seller search dropdown."""
    display_name = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = ['id', 'display_name', 'email', 'trust_score']
        read_only_fields = fields

    def get_display_name(self, obj):
        full = f"{obj.first_name} {obj.last_name}".strip()
        return full or obj.username or obj.email.split('@')[0]


class CustomTokenObtainPairSerializer(TokenObtainPairSerializer):
    """Adds user data to the token response."""

    def validate(self, attrs):
        data = super().validate(attrs)
        data['user'] = UserProfileSerializer(self.user).data
        return data
