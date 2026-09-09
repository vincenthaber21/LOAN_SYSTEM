from collections import OrderedDict
from datetime import timedelta
from decimal import Decimal

from django.db.models import Count, Sum
from django.db.models.functions import TruncDate
from django.utils import timezone

from .models import Payment, User

GRANULARITIES = [
    {"value": "day", "label": "Per day"},
    {"value": "biweekly", "label": "Bi-weekly"},
    {"value": "month", "label": "Monthly"},
]

LOOKBACKS = [
    {"value": "30", "label": "Last 30 days", "days": 30},
    {"value": "90", "label": "Last 90 days", "days": 90},
    {"value": "365", "label": "Last 12 months", "days": 365},
]

STAFF_ROLES = (User.Role.OFFICER, User.Role.MANAGER, User.Role.ADMIN)


def staff_users_queryset():
    return (
        User.objects.filter(role__in=STAFF_ROLES, is_active=True)
        .order_by("role", "full_name", "username")
    )


def resolve_staff_user(staff_user_id):
    if not staff_user_id:
        return None
    try:
        return staff_users_queryset().get(pk=int(staff_user_id))
    except (TypeError, ValueError, User.DoesNotExist):
        return None


def staff_filter_options():
    options = []
    for user in staff_users_queryset():
        options.append({
            "value": str(user.pk),
            "label": f"{user.display_name()} ({user.role_label})",
            "role": user.role,
        })
    return options


def period_bounds(lookback_days):
    end = timezone.localdate()
    start = end - timedelta(days=lookback_days - 1)
    return start, end


def _biweekly_start(day, anchor):
    offset = (day - anchor).days
    return anchor + timedelta(days=(offset // 14) * 14)


def _month_start(day):
    return day.replace(day=1)


def _iter_buckets(start, end, granularity):
    if granularity == "day":
        cursor = start
        while cursor <= end:
            yield cursor
            cursor += timedelta(days=1)
        return

    if granularity == "biweekly":
        cursor = start
        while cursor <= end:
            yield cursor
            cursor += timedelta(days=14)
        return

    cursor = _month_start(start)
    while cursor <= end:
        yield cursor
        if cursor.month == 12:
            cursor = cursor.replace(year=cursor.year + 1, month=1, day=1)
        else:
            cursor = cursor.replace(month=cursor.month + 1, day=1)


def _bucket_for(day, start, granularity):
    if granularity == "day":
        return day
    if granularity == "biweekly":
        return _biweekly_start(day, start)
    return _month_start(day)


def _label_for(bucket_start, granularity, period_end):
    if granularity == "day":
        return bucket_start.strftime("%b %d, %Y")
    if granularity == "month":
        return bucket_start.strftime("%b %Y")
    bucket_end = min(bucket_start + timedelta(days=13), period_end)
    if bucket_start.year == bucket_end.year:
        return f"{bucket_start.strftime('%b %d')} - {bucket_end.strftime('%b %d, %Y')}"
    return f"{bucket_start.strftime('%b %d, %Y')} - {bucket_end.strftime('%b %d, %Y')}"


def build_series(daily_rows, start, end, granularity, include_empty=True):
    """
    daily_rows: mapping date -> {"total": Decimal, "count": int}
    Returns ordered list of period rows including zero buckets when include_empty.
    """
    series = OrderedDict()
    for bucket in _iter_buckets(start, end, granularity):
        series[bucket] = {
            "period_start": bucket,
            "label": _label_for(bucket, granularity, end),
            "amount": Decimal("0.00"),
            "count": 0,
        }

    for day, row in daily_rows.items():
        if day is None or day < start or day > end:
            continue
        bucket = _bucket_for(day, start, granularity)
        if bucket not in series:
            series[bucket] = {
                "period_start": bucket,
                "label": _label_for(bucket, granularity, end),
                "amount": Decimal("0.00"),
                "count": 0,
            }
        series[bucket]["amount"] += row.get("total") or Decimal("0.00")
        series[bucket]["count"] += row.get("count") or 0

    rows = list(series.values())
    if not include_empty:
        rows = [row for row in rows if row["count"]]
    total_amount = sum((row["amount"] for row in series.values()), Decimal("0.00"))
    total_count = sum(row["count"] for row in series.values())
    return {
        "rows": rows,
        "total_amount": total_amount,
        "total_count": total_count,
    }


def _daily_totals(queryset, date_field, *, is_datetime=False):
    if is_datetime:
        aggregated = (
            queryset.annotate(day=TruncDate(date_field))
            .values("day")
            .annotate(total=Sum("amount"), count=Count("id"))
            .order_by("day")
        )
        day_key = "day"
    else:
        aggregated = (
            queryset.values(date_field)
            .annotate(total=Sum("amount"), count=Count("id"))
            .order_by(date_field)
        )
        day_key = date_field
    return {
        row[day_key]: {
            "total": row["total"] or Decimal("0.00"),
            "count": row["count"] or 0,
        }
        for row in aggregated
        if row[day_key] is not None
    }


def collection_report(start, end, granularity, staff_user=None):
    qs = Payment.objects.filter(payment_date__gte=start, payment_date__lte=end)
    if staff_user is not None:
        qs = qs.filter(recorded_by=staff_user)
    return build_series(
        _daily_totals(qs, "payment_date"),
        start,
        end,
        granularity,
        include_empty=granularity != "day",
    )


def mutual_aid_report(start, end, granularity, staff_user=None):
    from mutual_aid.models import MutualAidContribution

    qs = MutualAidContribution.objects.filter(
        created_at__date__gte=start,
        created_at__date__lte=end,
    )
    if staff_user is not None:
        qs = qs.filter(recorded_by=staff_user)
    return build_series(
        _daily_totals(qs, "created_at", is_datetime=True),
        start,
        end,
        granularity,
        include_empty=granularity != "day",
    )


def savings_report(start, end, granularity, staff_user=None):
    from savings.models import SavingsTransaction

    deposits = SavingsTransaction.objects.filter(
        transaction_type=SavingsTransaction.Type.DEPOSIT,
        created_at__date__gte=start,
        created_at__date__lte=end,
    )
    withdrawals = SavingsTransaction.objects.filter(
        transaction_type=SavingsTransaction.Type.WITHDRAWAL,
        created_at__date__gte=start,
        created_at__date__lte=end,
    )
    interest = SavingsTransaction.objects.filter(
        transaction_type=SavingsTransaction.Type.INTEREST,
        created_at__date__gte=start,
        created_at__date__lte=end,
    )
    if staff_user is not None:
        deposits = deposits.filter(created_by=staff_user)
        withdrawals = withdrawals.filter(created_by=staff_user)
        interest = interest.filter(created_by=staff_user)

    deposit_series = build_series(
        _daily_totals(deposits, "created_at", is_datetime=True), start, end, granularity
    )
    withdrawal_series = build_series(
        _daily_totals(withdrawals, "created_at", is_datetime=True), start, end, granularity
    )
    interest_series = build_series(
        _daily_totals(interest, "created_at", is_datetime=True), start, end, granularity
    )

    rows = []
    for index, deposit_row in enumerate(deposit_series["rows"]):
        withdrawal_amount = withdrawal_series["rows"][index]["amount"]
        interest_amount = interest_series["rows"][index]["amount"]
        withdrawal_count = withdrawal_series["rows"][index]["count"]
        interest_count = interest_series["rows"][index]["count"]
        row = {
            "period_start": deposit_row["period_start"],
            "label": deposit_row["label"],
            "amount": deposit_row["amount"],
            "count": deposit_row["count"],
            "withdrawals": withdrawal_amount,
            "interest": interest_amount,
            "net": deposit_row["amount"] + interest_amount - withdrawal_amount,
        }
        if granularity == "day" and not (
            deposit_row["count"] or withdrawal_count or interest_count
        ):
            continue
        rows.append(row)

    return {
        "rows": rows,
        "total_amount": deposit_series["total_amount"],
        "total_count": deposit_series["total_count"],
        "total_withdrawals": withdrawal_series["total_amount"],
        "total_interest": interest_series["total_amount"],
        "total_net": (
            deposit_series["total_amount"]
            + interest_series["total_amount"]
            - withdrawal_series["total_amount"]
        ),
    }


def cashflow_report_context(lookback_key="30", granularity="day", staff_user_id=None):
    lookback_map = {item["value"]: item for item in LOOKBACKS}
    granularity_map = {item["value"]: item for item in GRANULARITIES}

    if lookback_key not in lookback_map:
        lookback_key = "30"
    if granularity not in granularity_map:
        granularity = "day"

    staff_user = resolve_staff_user(staff_user_id)
    lookback = lookback_map[lookback_key]
    start, end = period_bounds(lookback["days"])

    collection = collection_report(start, end, granularity, staff_user=staff_user)
    mutual_aid = mutual_aid_report(start, end, granularity, staff_user=staff_user)
    savings = savings_report(start, end, granularity, staff_user=staff_user)

    return {
        "lookbacks": LOOKBACKS,
        "granularities": GRANULARITIES,
        "staff_options": staff_filter_options(),
        "selected_lookback": lookback_key,
        "selected_granularity": granularity,
        "selected_staff_id": str(staff_user.pk) if staff_user else "",
        "selected_staff": staff_user,
        "staff_label": (
            f"{staff_user.display_name()} ({staff_user.role_label})"
            if staff_user
            else "All staff"
        ),
        "lookback_label": lookback["label"],
        "granularity_label": granularity_map[granularity]["label"],
        "date_from": start,
        "date_to": end,
        "collection": collection,
        "mutual_aid": mutual_aid,
        "savings": savings,
    }
