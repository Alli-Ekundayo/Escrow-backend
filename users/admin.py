from django.contrib import admin
from .models import User


@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    list_display = ['email', 'username', 'phone_number', 'bvn_verified', 'trust_score', 'date_joined']
    list_filter = ['bvn_verified']
    search_fields = ['email', 'username', 'phone_number']
    readonly_fields = ['trust_score', 'nomba_account_ref', 'nomba_account_number', 'nomba_bank_code', 'date_joined']
