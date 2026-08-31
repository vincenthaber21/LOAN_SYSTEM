from collections import OrderedDict
from datetime import datetime
from decimal import Decimal

from django.db.models import Q, Sum
from django.utils import timezone

from .models import SavingsTransaction


def default_export_dates():
    today = timezone.localdate()
    return today.replace(day=1), today


def parse_export_dates(date_from_raw, date_to_raw):
    if not date_from_raw or not date_to_raw:
        raise ValueError("Choose both a start date and an end date.")
    try:
        date_from = datetime.strptime(date_from_raw, "%Y-%m-%d").date()
        date_to = datetime.strptime(date_to_raw, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError("Enter valid dates in YYYY-MM-DD format.") from exc
    if date_from > date_to:
        raise ValueError("Start date must be on or before the end date.")
    if date_to > timezone.localdate():
        raise ValueError("End date cannot be in the future.")
    return date_from, date_to


def total_interest_earned(date_from=None, date_to=None, accounts=None):
    qs = SavingsTransaction.objects.filter(transaction_type=SavingsTransaction.Type.INTEREST)
    if accounts is not None:
        qs = qs.filter(account__in=accounts)
    if date_from:
        qs = qs.filter(created_at__date__gte=date_from)
    if date_to:
        qs = qs.filter(created_at__date__lte=date_to)
    return qs.aggregate(total=Sum("amount"))["total"] or Decimal("0.00")


def interest_transactions_in_range(date_from, date_to):
    return list(
        SavingsTransaction.objects.filter(
            transaction_type=SavingsTransaction.Type.INTEREST,
            created_at__date__gte=date_from,
            created_at__date__lte=date_to,
        )
        .select_related("account", "account__member", "account__product")
        .order_by("account__member__full_name", "account__member__email", "created_at")
    )


def member_interest_summary(transactions):
    grouped = OrderedDict()
    for tx in transactions:
        member = tx.account.member
        if member.pk not in grouped:
            grouped[member.pk] = {
                "member": member,
                "name": member.display_name(),
                "email": member.email,
                "accounts": set(),
                "total": Decimal("0.00"),
                "credits": 0,
            }
        row = grouped[member.pk]
        row["accounts"].add(tx.account.reference)
        row["total"] += tx.amount
        row["credits"] += 1

    summary = []
    for row in grouped.values():
        summary.append({
            "name": row["name"],
            "email": row["email"],
            "accounts": ", ".join(sorted(row["accounts"])),
            "account_count": len(row["accounts"]),
            "credits": row["credits"],
            "total": row["total"],
        })
    return summary


def interest_report_context(date_from, date_to):
    transactions = interest_transactions_in_range(date_from, date_to)
    summary = member_interest_summary(transactions)
    grand_total = sum((row["total"] for row in summary), Decimal("0.00"))
    return {
        "date_from": date_from,
        "date_to": date_to,
        "transactions": transactions,
        "summary": summary,
        "member_count": len(summary),
        "credit_count": len(transactions),
        "grand_total": grand_total,
        "generated_at": timezone.localtime(),
    }
