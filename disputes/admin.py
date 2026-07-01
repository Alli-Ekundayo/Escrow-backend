from django.contrib import admin
from .models import Dispute


@admin.register(Dispute)
class DisputeAdmin(admin.ModelAdmin):
    list_display = ['id', 'agreement', 'raised_by', 'status', 'resolved_at', 'created_at']
    list_filter = ['status']
    search_fields = ['agreement__id', 'raised_by__email']
    readonly_fields = ['ai_ruling', 'resolved_at', 'created_at', 'updated_at']
