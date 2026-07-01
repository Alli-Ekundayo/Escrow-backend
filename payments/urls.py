from django.urls import path
from .views import NombaWebhookView

urlpatterns = [
    path('webhook/', NombaWebhookView.as_view(), name='payments-webhook'),
]
