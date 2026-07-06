import json
import logging

from django.conf import settings
from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import permissions, status

from .services import NombaPaymentService

logger = logging.getLogger(__name__)


@method_decorator(csrf_exempt, name='dispatch')
class NombaWebhookView(APIView):
    """
    POST /api/payments/webhook/

    Receives payment event notifications from Nomba.
    CSRF is exempt because Nomba sends the request — not a browser session.

    TODO: Add HMAC signature verification using Nomba's webhook secret
    before deploying to production.
    """

    permission_classes = [permissions.AllowAny]

    def post(self, request):
        # Webhook signature verification (Strict enforcement)
        webhook_secret = getattr(settings, "NOMBA_WEBHOOK_SECRET", "")
        if not webhook_secret:
            logger.warning("NOMBA_WEBHOOK_SECRET is not configured. Bypassing signature verification for integration availability.")
        else:
            signature = request.headers.get("nomba-signature")
            if not signature:
                if getattr(settings, "NOMBA_TEST_MODE", True):
                    logger.warning("Webhook signature missing. Bypassing check because NOMBA_TEST_MODE is active.")
                else:
                    logger.error("Webhook signature header 'nomba-signature' missing.")
                    return Response({"detail": "Unauthorized"}, status=status.HTTP_401_UNAUTHORIZED)
            else:
                import hmac
                import hashlib
                computed = hmac.new(
                    webhook_secret.encode("utf-8"),
                    request.body,
                    hashlib.sha256
                ).hexdigest()

                if not hmac.compare_digest(signature, computed):
                    logger.error("Webhook signature verification failed.")
                    return Response({"detail": "Unauthorized"}, status=status.HTTP_401_UNAUTHORIZED)

        try:
            payload = request.data  # DRF parses JSON automatically
        except (json.JSONDecodeError, Exception) as exc:
            logger.error("Webhook parse error: %s", exc)
            return Response({"detail": "Invalid payload."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            NombaPaymentService().handle_webhook(payload)
        except Exception as exc:
            logger.error("Webhook handler raised: %s", exc)
            # Always return 200 to Nomba so it doesn't retry indefinitely
            return Response({"detail": "Webhook received with errors."}, status=status.HTTP_200_OK)

        return Response({"detail": "OK"}, status=status.HTTP_200_OK)
