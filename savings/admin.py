from django.contrib import admin

from .models import SavingsAccount, SavingsProduct, SavingsTransaction


@admin.register(SavingsProduct)
class SavingsProductAdmin(admin.ModelAdmin):
    list_display = ("name", "interest_rate", "interest_term_months", "min_balance", "min_deposit", "is_active")
    list_filter = ("is_active",)


@admin.register(SavingsAccount)
class SavingsAccountAdmin(admin.ModelAdmin):
    list_display = ("account_number", "member", "product", "balance", "status", "opened_at")
    list_filter = ("status", "product")
    search_fields = ("account_number", "member__email", "member__full_name")
    readonly_fields = ("account_number", "opened_at", "closed_at")


@admin.register(SavingsTransaction)
class SavingsTransactionAdmin(admin.ModelAdmin):
    list_display = ("account", "transaction_type", "amount", "balance_after", "created_at")
    list_filter = ("transaction_type", "method")
    search_fields = ("reference_number", "account__account_number")
