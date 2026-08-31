from django.contrib import admin

from .models import MutualAidClaim, MutualAidContribution, MutualAidMembership, MutualAidPeriod, MutualAidPlan


@admin.register(MutualAidPlan)
class MutualAidPlanAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "contribution_amount",
        "contribution_frequency",
        "max_benefit_amount",
        "waiting_period_days",
        "is_active",
    )
    list_filter = ("is_active", "contribution_frequency")


@admin.register(MutualAidMembership)
class MutualAidMembershipAdmin(admin.ModelAdmin):
    list_display = (
        "membership_number",
        "member",
        "plan",
        "status",
        "total_contributed",
        "benefits_claimed",
        "enrolled_at",
    )
    list_filter = ("status", "plan")
    search_fields = ("membership_number", "member__email", "member__full_name")
    readonly_fields = ("membership_number", "enrolled_at", "terminated_at")


@admin.register(MutualAidPeriod)
class MutualAidPeriodAdmin(admin.ModelAdmin):
    list_display = ("plan", "date_from", "date_to", "label", "created_at")
    list_filter = ("plan",)
    date_hierarchy = "date_from"


@admin.register(MutualAidContribution)
class MutualAidContributionAdmin(admin.ModelAdmin):
    list_display = ("membership", "amount", "method", "period", "created_at")
    list_filter = ("method",)
    search_fields = ("reference_number", "membership__membership_number")


@admin.register(MutualAidClaim)
class MutualAidClaimAdmin(admin.ModelAdmin):
    list_display = (
        "reference",
        "membership",
        "claim_type",
        "amount_requested",
        "amount_approved",
        "status",
        "submitted_at",
    )
    list_filter = ("status", "claim_type")
    search_fields = ("membership__membership_number", "membership__member__full_name")
    readonly_fields = ("created_at", "submitted_at", "decision_date", "disbursed_at")
