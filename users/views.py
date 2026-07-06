from django.contrib.auth import get_user_model
from django.db.models import Q
from rest_framework import generics, permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

from .serializers import RegisterSerializer, UserProfileSerializer, CustomTokenObtainPairSerializer, UserSearchSerializer

User = get_user_model()


class RegisterView(generics.CreateAPIView):
    """
    POST /api/auth/register/
    Creates a new user and provisions a Nomba virtual wallet.
    """
    serializer_class = RegisterSerializer
    permission_classes = [permissions.AllowAny]


class LoginView(TokenObtainPairView):
    """
    POST /api/auth/login/
    Returns JWT access + refresh tokens alongside user profile.
    """
    serializer_class = CustomTokenObtainPairSerializer
    permission_classes = [permissions.AllowAny]


class ProfileView(generics.RetrieveUpdateAPIView):
    """
    GET/PATCH /api/auth/profile/
    Retrieves or partially updates the authenticated user's profile.
    """
    serializer_class = UserProfileSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_object(self):
        return self.request.user


class UserSearchView(APIView):
    """
    GET /api/auth/users/search/?q=<query>
    Returns up to 10 users matching name or email (excludes self).
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        q = request.query_params.get('q', '').strip()
        qs = User.objects.exclude(pk=request.user.pk)
        
        if q:
            qs = qs.filter(
                Q(first_name__icontains=q) |
                Q(last_name__icontains=q) |
                Q(username__icontains=q) |
                Q(email__icontains=q)
            )
            
        qs = qs.order_by('first_name', 'last_name')[:10]
        return Response(UserSearchSerializer(qs, many=True).data)


class WithdrawView(APIView):
    """
    POST /api/auth/withdraw/
    Initiates a payout from the user's virtual wallet to their external bank account.
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        user = request.user
        amount = request.data.get('amount')
        account_number = request.data.get('account_number')
        bank_code = request.data.get('bank_code')

        if not all([amount, account_number, bank_code]):
            return Response(
                {"detail": "Fields amount, account_number, and bank_code are required."},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            amount_val = float(amount)
            if amount_val <= 0:
                raise ValueError()
        except ValueError:
            return Response(
                {"detail": "Amount must be a positive number."},
                status=status.HTTP_400_BAD_REQUEST
            )

        if not user.nomba_account_holder_id:
            return Response(
                {"detail": "No virtual wallet provisioned for this user."},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            from payments.services import NombaPaymentService
            import uuid
            tx_ref = f"tf-wd-{user.id}-{uuid.uuid4().hex[:8]}"
            
            result = NombaPaymentService().release_to_seller(
                amount=amount_val,
                seller_account_number=account_number,
                seller_bank_code=bank_code,
                ref=tx_ref,
                source_account_id=user.nomba_account_holder_id
            )
            return Response(
                {"detail": "Withdrawal successful", "data": result},
                status=status.HTTP_200_OK
            )
        except Exception as exc:
            return Response(
                {"detail": str(exc)},
                status=status.HTTP_502_BAD_GATEWAY
            )


class DebugWalletView(APIView):
    """
    POST /api/auth/debug-wallet/
    Attempts to provision a Nomba virtual wallet for the logged-in user,
    returning the exact success or error response from Nomba.
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        user = request.user
        from payments.services import NombaPaymentService
        try:
            svc = NombaPaymentService()
            full_name = f"{user.first_name} {user.last_name}".strip() or user.email
            wallet = svc.create_virtual_wallet(str(user.id), account_name=full_name)
            
            user.nomba_account_ref       = wallet.get('accountRef', '')
            user.nomba_account_number    = wallet.get('bankAccountNumber', '')
            user.nomba_bank_code         = wallet.get('bankCode', 'NMB')
            user.nomba_account_holder_id = wallet.get('accountHolderId', '') or wallet.get('id', '')
            user.save(update_fields=[
                'nomba_account_ref', 'nomba_account_number',
                'nomba_bank_code', 'nomba_account_holder_id',
            ])
            return Response({
                "status": "success",
                "message": "Wallet provisioned successfully",
                "wallet": wallet
            })
        except Exception as exc:
            import traceback
            return Response({
                "status": "failed",
                "error_type": type(exc).__name__,
                "error_message": str(exc),
                "traceback": traceback.format_exc()
            }, status=status.HTTP_400_BAD_REQUEST)
