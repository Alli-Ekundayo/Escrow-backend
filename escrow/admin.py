from django.contrib import admin
from .models import EscrowAgreement, Milestone


class MilestoneInline(admin.TabularInline):
    model = Milestone
    extra = 0
    readonly_fields = ['is_met', 'proof_url', 'verified_at', 'ai_confidence', 'ai_reason']


@admin.register(EscrowAgreement)
class EscrowAgreementAdmin(admin.ModelAdmin):
    list_display = ['id', 'buyer', 'seller', 'amount', 'currency', 'status', 'deadline', 'created_at']
    list_filter = ['status', 'currency']
    search_fields = ['buyer__email', 'seller__email', 'nomba_transaction_ref']
    readonly_fields = ['nomba_transaction_ref', 'ai_verdict', 'created_at', 'updated_at']
    inlines = [MilestoneInline]


@admin.register(Milestone)
class MilestoneAdmin(admin.ModelAdmin):
    list_display = ['id', 'agreement', 'description', 'is_met', 'verified_at']
    list_filter = ['is_met']
    search_fields = ['description']
