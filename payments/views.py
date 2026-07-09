import hashlib
import hmac
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
        # Read raw body FIRST before DRF's lazy JSON parser can consume it.
        # Accessing request.body after request.data has been parsed raises an error.
        raw_body = request.body

        # Webhook signature verification
        webhook_secret = getattr(settings, "NOMBA_WEBHOOK_SECRET", "")
        is_test_mode = getattr(settings, "NOMBA_TEST_MODE", True)

        if not webhook_secret:
            if not is_test_mode:
                # Hard-reject in production — a missing secret must never silently
                # allow unsigned requests to activate escrow agreements.
                logger.critical(
                    "NOMBA_WEBHOOK_SECRET is not configured. "
                    "Rejecting all webhook requests in production mode."
                )
                return Response({"detail": "Unauthorized"}, status=status.HTTP_401_UNAUTHORIZED)
            logger.warning(
                "NOMBA_WEBHOOK_SECRET is not configured. "
                "Bypassing signature verification (TEST_MODE only)."
            )
        else:
            # Nomba sends the signature in 'nomba-signature'.
            # It may also appear in 'nomba-sig-value' as a fallback.
            # The value may carry a 'sha256=' or 'hmacsha256=' prefix — strip it.
            signature = (
                request.headers.get("nomba-signature")
                or request.headers.get("nomba-sig-value")
            )
            if not signature:
                if is_test_mode:
                    logger.warning("Webhook signature missing. Bypassing check because NOMBA_TEST_MODE is active.")
                else:
                    logger.error("Webhook signature header 'nomba-signature' missing.")
                    return Response({"detail": "Unauthorized"}, status=status.HTTP_401_UNAUTHORIZED)
            else:
                # Strip any algorithm prefix Nomba may prepend (e.g. "sha256=", "hmacsha256=")
                sig_value = signature
                for prefix in ("sha256=", "hmacsha256=", "hmac-sha256="):
                    if sig_value.lower().startswith(prefix):
                        sig_value = sig_value[len(prefix):]
                        break

                computed = hmac.new(
                    webhook_secret.encode("utf-8"),
                    raw_body,
                    hashlib.sha256
                ).hexdigest()

                logger.debug(
                    "Webhook HMAC check: received_sig=%s computed=%s",
                    sig_value[:16] + "...", computed[:16] + "..."
                )

                if not hmac.compare_digest(sig_value, computed):
                    logger.error(
                        "Webhook signature verification failed. "
                        "received=%s computed=%s (first 16 chars)",
                        sig_value[:16], computed[:16]
                    )
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
