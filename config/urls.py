from django.contrib import admin
from django.urls import path, include

urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/auth/', include('users.urls')),
    path('api/escrow/', include('escrow.urls')),
    path('api/disputes/', include('disputes.urls')),
    path('api/payments/', include('payments.urls')),
]
