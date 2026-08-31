from decimal import Decimal

from django.db.models import Sum

from .models import SavingsAccount


def savings_context(request):
    user = request.user
    if not user.is_authenticated or user.is_officer:
        return {}

    accounts = SavingsAccount.objects.filter(
        member=user,
        status=SavingsAccount.Status.ACTIVE,
    ).select_related("product")
    primary_account = accounts.first()
    total_savings = accounts.aggregate(value=Sum("balance"))["value"] or Decimal("0.00")
    return {
        "primary_savings_account": primary_account,
        "total_savings_balance": total_savings,
        "savings_account_count": accounts.count(),
    }
