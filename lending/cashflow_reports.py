from collections import OrderedDict
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.db.models import Count, Q, Sum
from django.db.models.functions import TruncDate
from django.utils import timezone

from .models import Loan, LoanApplication, LoanProduct, Payment, User

GRANULARITIES = [
    {"value": "day", "label": "Per day"},
    {"value": "biweekly", "label": "Bi-weekly"},
    {"value": "month", "label": "Monthly"},
]

LOOKBACKS = [
    {"value": "30", "label": "Last 30 days", "days": 30},
    {"value": "90", "label": "Last 90 days", "days": 90},
    {"value": "365", "label": "Last 12 months", "days": 365},
    {"value": "custom", "label": "Custom range"},
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


def _parse_date(value):
    if value is None or value == "":
        return None
    if hasattr(value, "year") and hasattr(value, "month") and hasattr(value, "day"):
        return value
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def resolve_report_period(lookback_key="30", date_from=None, date_to=None):
    """Resolve preset or custom reporting dates. Returns (key, start, end, label)."""
    lookback_map = {item["value"]: item for item in LOOKBACKS}
    if lookback_key not in lookback_map:
        lookback_key = "30"

    if lookback_key == "custom":
        start = _parse_date(date_from)
        end = _parse_date(date_to)
        if start and end and start > end:
            start, end = end, start
        if not start or not end:
            start, end = period_bounds(30)
        label = f"{start.strftime('%b %d, %Y')} – {end.strftime('%b %d, %Y')}"
        return lookback_key, start, end, label

    lookback = lookback_map[lookback_key]
    start, end = period_bounds(lookback["days"])
    return lookback_key, start, end, lookback["label"]


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


def cashflow_report_context(
    lookback_key="30",
    granularity="day",
    staff_user_id=None,
    date_from=None,
    date_to=None,
):
    granularity_map = {item["value"]: item for item in GRANULARITIES}

    if granularity not in granularity_map:
        granularity = "day"

    staff_user = resolve_staff_user(staff_user_id)
    lookback_key, start, end, lookback_label = resolve_report_period(
        lookback_key,
        date_from=date_from,
        date_to=date_to,
    )

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
        "lookback_label": lookback_label,
        "granularity_label": granularity_map[granularity]["label"],
        "date_from": start,
        "date_to": end,
        "custom_from": start.isoformat(),
        "custom_to": end.isoformat(),
        "collection": collection,
        "mutual_aid": mutual_aid,
        "savings": savings,
    }


def resolve_loan_product(product_id):
    if not product_id:
        return None
    try:
        return LoanProduct.objects.get(pk=int(product_id))
    except (TypeError, ValueError, LoanProduct.DoesNotExist):
        return None


def _staff_name(user):
    return user.display_name() if user else ""


def _datetime_parts(value):
    """Return iso date, month label, display date, and local time for audit rows."""
    if value is None:
        return "", "", "", ""
    if isinstance(value, datetime):
        if timezone.is_aware(value):
            value = timezone.localtime(value)
        return (
            value.strftime("%Y-%m-%d"),
            value.strftime("%B %Y"),
            value.strftime("%b %d, %Y"),
            value.strftime("%I:%M:%S %p"),
        )
    return (
        value.strftime("%Y-%m-%d"),
        value.strftime("%B %Y"),
        value.strftime("%b %d, %Y"),
        "",
    )


def _sort_datetime(value):
    if value is None:
        return datetime.min
    if isinstance(value, datetime):
        if timezone.is_aware(value):
            return timezone.localtime(value).replace(tzinfo=None)
        return value
    return datetime.combine(value, time.min)


def _ledger_item(category, when, *, reference, member, detail, type_label, amount, method_or_status, recorded_by, notes=""):
    iso_date, month, display_date, time_label = _datetime_parts(when)
    return {
        "category": category,
        "iso_date": iso_date,
        "month": month,
        "display_date": display_date,
        "time": time_label,
        "reference": reference,
        "member": member,
        "detail": detail,
        "type": type_label,
        "amount": amount,
        "method_or_status": method_or_status,
        "recorded_by": recorded_by,
        "notes": notes,
        "sort_key": _sort_datetime(when),
    }


def audit_detail_context(start, end, staff_user=None, product=None):
    """Line-level collections, applications, disbursements, and transactions."""
    payments = Payment.objects.filter(
        payment_date__gte=start,
        payment_date__lte=end,
    ).select_related(
        "loan",
        "loan__application",
        "loan__application__borrower",
        "loan__application__loan_product",
        "recorded_by",
        "installment",
    ).order_by("payment_date", "pk")
    if staff_user is not None:
        payments = payments.filter(recorded_by=staff_user)
    if product is not None:
        payments = payments.filter(loan__application__loan_product=product)

    collections = []
    for payment in payments:
        application = payment.loan.application
        collections.append({
            "when": payment.payment_date,
            "reference": payment.reference_number or f"PAY-{payment.pk:05d}",
            "loan": payment.loan.reference,
            "application": application.reference,
            "member": application.borrower_name,
            "product": application.product_name,
            "amount": payment.amount,
            "loan_applied": payment.loan_amount_applied,
            "savings_adjustment": payment.savings_adjustment or Decimal("0.00"),
            "mutual_aid": payment.mutual_aid_contribution or Decimal("0.00"),
            "method": payment.get_method_display(),
            "installment": (
                payment.installment.installment_number if payment.installment_id else ""
            ),
            "recorded_by": _staff_name(payment.recorded_by),
            **dict(zip(
                ("iso_date", "month", "display_date", "time"),
                _datetime_parts(payment.payment_date),
            )),
        })

    loans = Loan.objects.filter(
        disbursed_date__gte=start,
        disbursed_date__lte=end,
    ).select_related(
        "application",
        "application__borrower",
        "application__loan_product",
        "disbursed_by",
    ).order_by("disbursed_date", "pk")
    if staff_user is not None:
        loans = loans.filter(disbursed_by=staff_user)
    if product is not None:
        loans = loans.filter(application__loan_product=product)

    disbursements = []
    for loan in loans:
        application = loan.application
        disbursements.append({
            "when": loan.disbursed_date,
            "reference": loan.reference,
            "receipt": loan.disbursement_receipt_number,
            "application": application.reference,
            "member": application.borrower_name,
            "product": application.product_name,
            "principal": loan.principal,
            "net_release": loan.net_release_amount,
            "outstanding": loan.outstanding_balance,
            "status": loan.get_status_display(),
            "method": loan.disbursement_method,
            "disbursement_reference": loan.disbursement_reference,
            "disbursed_by": _staff_name(loan.disbursed_by),
            **dict(zip(
                ("iso_date", "month", "display_date", "time"),
                _datetime_parts(loan.disbursed_date),
            )),
        })

    applications = LoanApplication.objects.filter(
        Q(applied_on__gte=start, applied_on__lte=end)
        | Q(created_at__date__gte=start, created_at__date__lte=end)
    ).select_related(
        "borrower",
        "loan_product",
        "created_by",
        "reviewed_by",
    ).order_by("created_at", "pk")
    if staff_user is not None:
        applications = applications.filter(
            Q(created_by=staff_user) | Q(reviewed_by=staff_user)
        )
    if product is not None:
        applications = applications.filter(loan_product=product)

    application_rows = []
    for application in applications:
        when = application.created_at
        if application.applied_on:
            local_created = (
                timezone.localtime(application.created_at)
                if timezone.is_aware(application.created_at)
                else application.created_at
            )
            when = datetime.combine(application.applied_on, local_created.time())
        application_rows.append({
            "when": when,
            "reference": application.reference,
            "member": application.borrower_name,
            "email": application.email,
            "product": application.product_name,
            "amount": application.amount_requested,
            "term_months": application.term_months,
            "status": application.status_label,
            "created_by": _staff_name(application.created_by),
            "reviewed_by": _staff_name(application.reviewed_by),
            **dict(zip(
                ("iso_date", "month", "display_date", "time"),
                _datetime_parts(when),
            )),
        })

    from savings.models import SavingsTransaction

    savings_qs = SavingsTransaction.objects.filter(
        created_at__date__gte=start,
        created_at__date__lte=end,
    ).select_related(
        "account",
        "account__member",
        "account__product",
        "created_by",
    ).order_by("created_at", "pk")
    if staff_user is not None:
        savings_qs = savings_qs.filter(created_by=staff_user)

    savings_rows = []
    for tx in savings_qs:
        savings_rows.append({
            "when": tx.created_at,
            "reference": tx.reference_number or f"SVT-{tx.pk:05d}",
            "account": tx.account.reference,
            "member": tx.account.member.display_name(),
            "product": tx.account.product_name,
            "type": tx.get_transaction_type_display(),
            "amount": tx.amount,
            "method": tx.get_method_display(),
            "balance_after": tx.balance_after,
            "recorded_by": _staff_name(tx.created_by),
            "notes": tx.notes,
            **dict(zip(
                ("iso_date", "month", "display_date", "time"),
                _datetime_parts(tx.created_at),
            )),
        })

    from mutual_aid.models import MutualAidContribution

    aid_qs = MutualAidContribution.objects.filter(
        created_at__date__gte=start,
        created_at__date__lte=end,
    ).select_related(
        "membership",
        "membership__member",
        "membership__plan",
        "recorded_by",
        "period",
    ).order_by("created_at", "pk")
    if staff_user is not None:
        aid_qs = aid_qs.filter(recorded_by=staff_user)

    mutual_aid_rows = []
    for contribution in aid_qs:
        membership = contribution.membership
        mutual_aid_rows.append({
            "when": contribution.created_at,
            "reference": contribution.reference_number or f"MAC-{contribution.pk:05d}",
            "membership": membership.reference,
            "member": membership.member.display_name(),
            "plan": membership.plan_name,
            "period": contribution.period.display_label if contribution.period_id else "",
            "amount": contribution.amount,
            "method": contribution.get_method_display(),
            "recorded_by": _staff_name(contribution.recorded_by),
            "notes": contribution.notes,
            **dict(zip(
                ("iso_date", "month", "display_date", "time"),
                _datetime_parts(contribution.created_at),
            )),
        })

    ledger = []
    for row in collections:
        ledger.append(_ledger_item(
            "Collection",
            row["when"],
            reference=row["reference"],
            member=row["member"],
            detail=f"{row['loan']} · {row['product']}",
            type_label="Loan payment",
            amount=row["amount"],
            method_or_status=row["method"],
            recorded_by=row["recorded_by"],
            notes=f"Applied {row['loan_applied']}; savings {row['savings_adjustment']}; mutual aid {row['mutual_aid']}",
        ))
    for row in disbursements:
        ledger.append(_ledger_item(
            "Disbursement",
            row["when"],
            reference=row["reference"],
            member=row["member"],
            detail=f"{row['application']} · {row['product']}",
            type_label="Loan release",
            amount=row["principal"],
            method_or_status=row["status"],
            recorded_by=row["disbursed_by"],
            notes=row["disbursement_reference"] or row["method"],
        ))
    for row in application_rows:
        ledger.append(_ledger_item(
            "Application",
            row["when"],
            reference=row["reference"],
            member=row["member"],
            detail=row["product"],
            type_label=row["status"],
            amount=row["amount"],
            method_or_status=row["status"],
            recorded_by=row["created_by"] or row["reviewed_by"],
            notes=f"Term {row['term_months']} months",
        ))
    for row in savings_rows:
        ledger.append(_ledger_item(
            "Savings",
            row["when"],
            reference=row["reference"],
            member=row["member"],
            detail=f"{row['account']} · {row['product']}",
            type_label=row["type"],
            amount=row["amount"],
            method_or_status=row["method"],
            recorded_by=row["recorded_by"],
            notes=row["notes"],
        ))
    for row in mutual_aid_rows:
        ledger.append(_ledger_item(
            "Mutual aid",
            row["when"],
            reference=row["reference"],
            member=row["member"],
            detail=row["plan"],
            type_label="Contribution",
            amount=row["amount"],
            method_or_status=row["method"],
            recorded_by=row["recorded_by"],
            notes=row["notes"],
        ))
    ledger.sort(key=lambda item: (item["sort_key"], item["category"], item["reference"]))

    return {
        "collections": collections,
        "disbursements": disbursements,
        "applications": application_rows,
        "savings_transactions": savings_rows,
        "mutual_aid_contributions": mutual_aid_rows,
        "ledger": ledger,
        "product_label": product.name if product else "All products",
    }


def write_audit_csv(writer, cashflow, *, generated_at, generated_by, product=None):
    details = audit_detail_context(
        cashflow["date_from"],
        cashflow["date_to"],
        staff_user=cashflow["selected_staff"],
        product=product,
    )
    generated_local = timezone.localtime(generated_at) if timezone.is_aware(generated_at) else generated_at

    writer.writerow(["Detailed audit report"])
    writer.writerow(["Generated month", generated_local.strftime("%B %Y")])
    writer.writerow(["Generated date", generated_local.strftime("%Y-%m-%d")])
    writer.writerow(["Generated time", generated_local.strftime("%I:%M:%S %p")])
    writer.writerow(["Generated at", generated_local.strftime("%B %d, %Y %I:%M:%S %p")])
    writer.writerow(["Generated by", generated_by.display_name() if generated_by else ""])
    writer.writerow(["Period", cashflow["lookback_label"]])
    writer.writerow(["Date from", cashflow["date_from"].isoformat()])
    writer.writerow(["Date to", cashflow["date_to"].isoformat()])
    writer.writerow(["Granularity", cashflow["granularity_label"]])
    writer.writerow(["Staff", cashflow["staff_label"]])
    writer.writerow(["Product", details["product_label"]])
    writer.writerow([])

    writer.writerow(["Summary totals"])
    writer.writerow(["Category", "Count", "Amount"])
    writer.writerow([
        "Collection (loan payments)",
        cashflow["collection"]["total_count"],
        cashflow["collection"]["total_amount"],
    ])
    writer.writerow([
        "Mutual aid contributions",
        cashflow["mutual_aid"]["total_count"],
        cashflow["mutual_aid"]["total_amount"],
    ])
    writer.writerow([
        "Savings deposits",
        cashflow["savings"]["total_count"],
        cashflow["savings"]["total_amount"],
    ])
    writer.writerow(["Applications", len(details["applications"]), ""])
    writer.writerow(["Disbursements", len(details["disbursements"]), ""])
    writer.writerow(["Savings transactions", len(details["savings_transactions"]), ""])
    writer.writerow([])

    writer.writerow(["All activity (chronological)"])
    writer.writerow([
        "Category",
        "Date",
        "Month",
        "Time",
        "Reference",
        "Member",
        "Detail",
        "Type",
        "Amount",
        "Method / status",
        "Recorded by",
        "Notes",
    ])
    for row in details["ledger"]:
        writer.writerow([
            row["category"],
            row["iso_date"],
            row["month"],
            row["time"],
            row["reference"],
            row["member"],
            row["detail"],
            row["type"],
            row["amount"],
            row["method_or_status"],
            row["recorded_by"],
            row["notes"],
        ])
    writer.writerow([])

    writer.writerow(["Detailed collections"])
    writer.writerow([
        "Date",
        "Month",
        "Time",
        "Payment reference",
        "Loan",
        "Application",
        "Member",
        "Product",
        "Amount",
        "Applied to loan",
        "Savings adjustment",
        "Mutual aid",
        "Method",
        "Installment",
        "Recorded by",
    ])
    for row in details["collections"]:
        writer.writerow([
            row["iso_date"],
            row["month"],
            row["time"],
            row["reference"],
            row["loan"],
            row["application"],
            row["member"],
            row["product"],
            row["amount"],
            row["loan_applied"],
            row["savings_adjustment"],
            row["mutual_aid"],
            row["method"],
            row["installment"],
            row["recorded_by"],
        ])
    writer.writerow([])

    writer.writerow(["Detailed applications"])
    writer.writerow([
        "Date",
        "Month",
        "Time",
        "Reference",
        "Member",
        "Email",
        "Product",
        "Amount requested",
        "Term (months)",
        "Status",
        "Created by",
        "Reviewed by",
    ])
    for row in details["applications"]:
        writer.writerow([
            row["iso_date"],
            row["month"],
            row["time"],
            row["reference"],
            row["member"],
            row["email"],
            row["product"],
            row["amount"],
            row["term_months"],
            row["status"],
            row["created_by"],
            row["reviewed_by"],
        ])
    writer.writerow([])

    writer.writerow(["Detailed disbursements"])
    writer.writerow([
        "Date",
        "Month",
        "Time",
        "Loan",
        "Receipt",
        "Application",
        "Member",
        "Product",
        "Principal",
        "Net release",
        "Outstanding",
        "Status",
        "Method",
        "Disbursement reference",
        "Disbursed by",
    ])
    for row in details["disbursements"]:
        writer.writerow([
            row["iso_date"],
            row["month"],
            row["time"],
            row["reference"],
            row["receipt"],
            row["application"],
            row["member"],
            row["product"],
            row["principal"],
            row["net_release"],
            row["outstanding"],
            row["status"],
            row["method"],
            row["disbursement_reference"],
            row["disbursed_by"],
        ])
    writer.writerow([])

    writer.writerow(["Detailed savings transactions"])
    writer.writerow([
        "Date",
        "Month",
        "Time",
        "Reference",
        "Account",
        "Member",
        "Product",
        "Type",
        "Amount",
        "Method",
        "Balance after",
        "Recorded by",
        "Notes",
    ])
    for row in details["savings_transactions"]:
        writer.writerow([
            row["iso_date"],
            row["month"],
            row["time"],
            row["reference"],
            row["account"],
            row["member"],
            row["product"],
            row["type"],
            row["amount"],
            row["method"],
            row["balance_after"],
            row["recorded_by"],
            row["notes"],
        ])
    writer.writerow([])

    writer.writerow(["Detailed mutual aid contributions"])
    writer.writerow([
        "Date",
        "Month",
        "Time",
        "Reference",
        "Membership",
        "Member",
        "Plan",
        "Period",
        "Amount",
        "Method",
        "Recorded by",
        "Notes",
    ])
    for row in details["mutual_aid_contributions"]:
        writer.writerow([
            row["iso_date"],
            row["month"],
            row["time"],
            row["reference"],
            row["membership"],
            row["member"],
            row["plan"],
            row["period"],
            row["amount"],
            row["method"],
            row["recorded_by"],
            row["notes"],
        ])
    writer.writerow([])

    writer.writerow(["Collection totals by period"])
    writer.writerow(["Period", "Payments", "Amount"])
    for row in cashflow["collection"]["rows"]:
        writer.writerow([row["label"], row["count"], row["amount"]])
    writer.writerow([
        "Total",
        cashflow["collection"]["total_count"],
        cashflow["collection"]["total_amount"],
    ])
    writer.writerow([])

    writer.writerow(["Mutual aid totals by period"])
    writer.writerow(["Period", "Contributions", "Amount"])
    for row in cashflow["mutual_aid"]["rows"]:
        writer.writerow([row["label"], row["count"], row["amount"]])
    writer.writerow([
        "Total",
        cashflow["mutual_aid"]["total_count"],
        cashflow["mutual_aid"]["total_amount"],
    ])
    writer.writerow([])

    writer.writerow(["Savings totals by period"])
    writer.writerow(["Period", "Deposits count", "Deposits", "Interest", "Withdrawals", "Net"])
    for row in cashflow["savings"]["rows"]:
        writer.writerow([
            row["label"],
            row["count"],
            row["amount"],
            row["interest"],
            row["withdrawals"],
            row["net"],
        ])
    writer.writerow([
        "Total",
        cashflow["savings"]["total_count"],
        cashflow["savings"]["total_amount"],
        cashflow["savings"]["total_interest"],
        cashflow["savings"]["total_withdrawals"],
        cashflow["savings"]["total_net"],
    ])
