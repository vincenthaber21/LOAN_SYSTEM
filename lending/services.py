from datetime import datetime, timedelta
from decimal import Decimal, ROUND_CEILING, ROUND_HALF_UP

from django.db import transaction
from django.utils import timezone

from .models import Installment, Loan, LoanApplication, Notification, User

INITIAL_CREDIT_SCORE = Decimal("100.000")
LATE_PAYMENT_CREDIT_PENALTY = Decimal("0.1")
CREDIT_SCORE_PRECISION = Decimal("0.1")
MIN_CREDIT_SCORE_FOR_LOANS = Decimal("50.0")
BALANCE_EXTENSION_RATE = Decimal("5.00")
MAX_BALANCE_EXTENSION_MONTHS = 3


def adjust_payment(amount):
    """Round a remittance up to the nearest ₱5.

    Exact multiples of ₱5 are left unchanged.

    Examples:
        4533.33 → 4535
        206.06  → 210
        6.00    → 10
        3.00    → 5
    """
    amount = Decimal(str(amount or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if amount <= 0:
        return Decimal("0.00")
    if amount % Decimal("5") == 0:
        return amount.quantize(Decimal("0.01"))
    adjusted = (amount / Decimal("5")).to_integral_value(rounding=ROUND_CEILING) * Decimal("5")
    return adjusted.quantize(Decimal("0.01"))


def application_type_for_member(member):
    """New for first-time applicants; Renew when the member already has loan history."""
    if not member:
        return LoanApplication.ApplicationType.NEW
    has_loan = Loan.objects.filter(application__borrower=member).exists()
    has_prior_application = LoanApplication.objects.filter(borrower=member).exists()
    if has_loan or has_prior_application:
        return LoanApplication.ApplicationType.RENEW
    return LoanApplication.ApplicationType.NEW

# Fixed deductions withheld from every loan release (principal still based on approved amount).
STANDARD_DISBURSEMENT_DEDUCTIONS = (
    {
        "key": "membership_savings",
        "label": "Membership/Savings Deposit",
        "amount": Decimal("500.00"),
        "is_processing_fee": False,
    },
    {
        "key": "processing_fee",
        "label": "Processing fee",
        "amount": Decimal("300.00"),
        "is_processing_fee": True,
    },
    {
        "key": "notarial_fee",
        "label": "Notarial fee",
        "amount": Decimal("200.00"),
        "is_processing_fee": False,
    },
    {
        "key": "kap_mutual_aid",
        "label": "Initial contribution for KAPAMILYA MUTUAL AID PROGRAM",
        "amount": Decimal("200.00"),
        "is_processing_fee": False,
    },
)


def standard_disbursement_deductions():
    """Return the fixed fee breakdown applied on every disbursement."""
    line_items = [
        {
            "key": item["key"],
            "label": item["label"],
            "amount": item["amount"],
            "is_processing_fee": item["is_processing_fee"],
        }
        for item in STANDARD_DISBURSEMENT_DEDUCTIONS
    ]
    processing_fee = sum(
        (item["amount"] for item in line_items if item["is_processing_fee"]),
        Decimal("0.00"),
    )
    other_items = [item for item in line_items if not item["is_processing_fee"]]
    other_fees = sum((item["amount"] for item in other_items), Decimal("0.00"))
    other_fees_description = "; ".join(
        f"{item['label']} ({item['amount']:.0f})" for item in other_items
    )
    # Stored on Loan.other_fees_description (max_length=120).
    if len(other_fees_description) > 120:
        other_fees_description = other_fees_description[:117] + "..."
    total = processing_fee + other_fees
    return {
        "line_items": line_items,
        "processing_fee": processing_fee,
        "other_fees": other_fees,
        "other_fees_description": other_fees_description,
        "total": total,
    }


def normalize_credit_score(score):
    if score is None:
        return INITIAL_CREDIT_SCORE
    normalized = Decimal(str(score)).quantize(CREDIT_SCORE_PRECISION, rounding=ROUND_HALF_UP)
    return max(Decimal("0"), normalized)


def format_credit_score(score):
    return f"{normalize_credit_score(score):.1f}"


def credit_score_blocks_loans(borrower):
    if not borrower:
        return False
    return normalize_credit_score(borrower.credit_score) < MIN_CREDIT_SCORE_FOR_LOANS


def credit_score_loan_block_message(borrower):
    return (
        f"Credit score is {format_credit_score(borrower.credit_score)}. "
        f"A minimum score of {MIN_CREDIT_SCORE_FOR_LOANS:.1f} is required to apply for loans."
    )


def reject_superseded_applications(application, reviewer):
    from .forms import OPEN_APPLICATION_STATUSES  # avoid circular import at module load

    others = LoanApplication.objects.filter(
        borrower=application.borrower,
        loan_product=application.loan_product,
        status__in=OPEN_APPLICATION_STATUSES,
    ).exclude(pk=application.pk)
    for other in others:
        other.status = LoanApplication.Status.REJECTED
        other.reviewed_by = reviewer
        other.decision_date = timezone.now()
        other.review_notes = f"Auto-rejected: superseded by {application.reference}."
        other.save(update_fields=["status", "reviewed_by", "decision_date", "review_notes"])


WORKING_DAYS_PER_MONTH = 22
WORKING_DAYS_PER_WEEK = 5
WORKING_DAYS_PER_BIWEEK = 10


def loan_term_months(loan):
    """Approved term for schedule generation (application term when loan can still be rebuilt)."""
    application_term = loan.application.final_term_months or loan.application.term_months
    if loan.payments.exists():
        return loan.term_months
    return application_term


def sync_loan_term_from_application(loan):
    """Keep loan.term_months aligned with the application before any payments are recorded."""
    if loan.payments.exists():
        return False
    term = loan.application.final_term_months or loan.application.term_months
    if loan.term_months == term:
        return False
    loan.term_months = term
    loan.save(update_fields=["term_months"])
    return True


def working_day_count(term_months):
    """Working days (Mon–Fri) in the loan term; 1 month = 22 working days."""
    return int(term_months) * WORKING_DAYS_PER_MONTH


def calculate_flat_loan_amounts(principal, interest_rate, term_months):
    """Flat interest using working-day collection (Mon–Fri, 22 days/month).

    Example — capital 10000, rate 6%, term 3 months:
        total_interest = 10000 * 0.06 * 3 = 1800
        total_payable  = 1800 + 10000 = 11800
        per_day        = 11800 / (3 * 22) = 178.79
        per_month      = 11800 / 3 = 3933.33
    """
    principal = Decimal(str(principal))
    rate = Decimal(str(interest_rate)) / Decimal("100")
    months = Decimal(int(term_months))
    total_interest = (principal * rate * months).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    total_payable = (principal + total_interest).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    periods = working_day_count(term_months)
    per_day = (total_payable / Decimal(periods)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    per_month = (total_payable / months).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return {
        "principal": principal,
        "total_interest": total_interest,
        "total_payable": total_payable,
        "periods": periods,
        "per_day": per_day,
        "per_month": per_month,
    }


def expected_period_count(term_months, payment_frequency=None):
    """Number of weekday installments for the term (payment_frequency kept for callers)."""
    return working_day_count(term_months)


def calculate_periodic_payment(principal, interest_rate, term_months, payment_frequency=None):
    """Daily (working-day) payment under the flat interest formula."""
    amounts = calculate_flat_loan_amounts(principal, interest_rate, term_months)
    return amounts["per_day"], amounts["periods"]


def _is_working_day(value):
    return value.weekday() < 5  # Monday–Friday


def _next_working_day(value):
    """First working day strictly after `value`."""
    candidate = value + timedelta(days=1)
    while not _is_working_day(candidate):
        candidate += timedelta(days=1)
    return candidate


def _interest_start_monday(value):
    """First Monday on or after `value` — when flat working-day interest begins.

    Example: loan available Friday Sep 4 → interest starts Monday Sep 7.
    If the date is already a Monday, interest starts that same day.
    """
    days_until_monday = (0 - value.weekday()) % 7
    return value + timedelta(days=days_until_monday)


def _first_installment_due_date(loan):
    """Interest and collections start on the Monday on/after schedule start or disbursement."""
    start = loan.schedule_start_date or loan.disbursed_date
    return _interest_start_monday(start)


def loan_maturity_date(loan):
    """Last scheduled due date for the loan, if any."""
    last = loan.installments.order_by("-due_date").values_list("due_date", flat=True).first()
    return last


def loan_term_expired(loan):
    """True when the scheduled term has ended (last due date is before today)."""
    maturity = loan_maturity_date(loan)
    if not maturity:
        return False
    return maturity < timezone.localdate()


def can_extend_loan_balance(loan):
    """Expired-term loans with an outstanding balance can be restructured (1–3 months @ 5%)."""
    if loan.status == Loan.Status.PAID:
        return False
    if (loan.outstanding_balance or Decimal("0.00")) <= 0:
        return False
    return loan_term_expired(loan)


def balance_extension_principal(loan):
    """Cash-adjusted outstanding balance used as principal for remaining-balance extensions."""
    return adjust_payment(loan.outstanding_balance or Decimal("0.00"))


def balance_extension_quote(principal, months):
    """Flat-interest quote for paying a remaining balance over `months` at 5%."""
    months = int(months)
    amounts = calculate_flat_loan_amounts(principal, BALANCE_EXTENSION_RATE, months)
    return {
        "months": months,
        "principal": amounts["principal"],
        "interest_rate": BALANCE_EXTENSION_RATE,
        "total_interest": amounts["total_interest"],
        "total_payable": amounts["total_payable"],
        "per_day": amounts["per_day"],
        "per_month": amounts["per_month"],
        "periods": amounts["periods"],
    }


def balance_extension_previews(loan):
    """Quotes for 1–3 month balance extensions on the adjusted outstanding balance."""
    remaining = balance_extension_principal(loan)
    return [balance_extension_quote(remaining, months) for months in range(1, MAX_BALANCE_EXTENSION_MONTHS + 1)]


class BalanceExtensionError(ValueError):
    """Raised when a remaining-balance extension cannot be applied."""


@transaction.atomic
def extend_loan_balance(loan, months):
    """Restructure an expired loan's remaining balance over 1–3 months at 5% flat interest.

    Uses the cash-adjusted outstanding balance as principal and the same working-day
    flat formula as origination:
        total_interest = adjusted_remaining * 0.05 * months
        total_payable  = adjusted_remaining + total_interest
    """
    months = int(months)
    if months < 1 or months > MAX_BALANCE_EXTENSION_MONTHS:
        raise BalanceExtensionError(f"Choose between 1 and {MAX_BALANCE_EXTENSION_MONTHS} months.")

    loan = Loan.objects.select_for_update().get(pk=loan.pk)
    if not can_extend_loan_balance(loan):
        raise BalanceExtensionError(
            "Balance extension is only available when the loan term has expired and a balance remains."
        )

    remaining = balance_extension_principal(loan)
    if remaining <= 0:
        raise BalanceExtensionError("Adjusted remaining balance must be greater than zero.")
    quote = balance_extension_quote(remaining, months)
    payments_sum = sum((payment.amount for payment in loan.payments.all()), Decimal("0.00"))

    if loan.disbursed_principal is None:
        loan.disbursed_principal = loan.principal
    if loan.original_interest_rate is None:
        loan.original_interest_rate = loan.interest_rate
    if loan.original_term_months is None:
        loan.original_term_months = loan.term_months

    loan.principal = remaining
    loan.interest_rate = BALANCE_EXTENSION_RATE
    loan.term_months = months
    loan.schedule_start_date = timezone.localdate()
    loan.total_payable = (payments_sum + quote["total_payable"]).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    loan.outstanding_balance = quote["total_payable"]
    loan.status = Loan.Status.ACTIVE
    loan.save(
        update_fields=[
            "disbursed_principal",
            "original_interest_rate",
            "original_term_months",
            "principal",
            "interest_rate",
            "term_months",
            "schedule_start_date",
            "total_payable",
            "outstanding_balance",
            "status",
        ]
    )
    generate_schedule(loan)
    return loan, quote


def schedule_is_stale(loan):
    """True when stored installments no longer match the loan terms or disbursement date."""
    term = loan_term_months(loan)
    expected = expected_period_count(term)
    if loan.installments.count() != expected:
        return True
    first = loan.installments.order_by("installment_number").first()
    if not first:
        return True
    return first.due_date != _first_installment_due_date(loan)


def generate_schedule(loan):
    """Build one installment per working day (Mon–Fri) for the loan term.

    Flat interest is charged on the original principal for every month of the term:

        total_interest = principal * (rate/100) * term_months
        total_payable  = principal + total_interest

    Interest begins on the Monday on/after `schedule_start_date` (or disbursement).
    That total is then split evenly across `term_months * 22` working-day payments.
    The final row absorbs rounding so the schedule sums exactly to principal and
    total interest.
    """
    Installment.objects.filter(loan=loan).delete()
    term = loan_term_months(loan)
    amounts = calculate_flat_loan_amounts(loan.principal, loan.interest_rate, term)
    periods = amounts["periods"]
    payment = amounts["per_day"]
    principal_per = (loan.principal / Decimal(periods)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    interest_per = (amounts["total_interest"] / Decimal(periods)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    principal_remaining = loan.principal
    interest_remaining = amounts["total_interest"]
    due_date = _first_installment_due_date(loan)
    installments = []
    for number in range(1, periods + 1):
        if number > 1:
            due_date = _next_working_day(due_date)
        if number == periods:
            principal = principal_remaining
            interest = interest_remaining
            payment_for_period = principal + interest
        else:
            principal = principal_per
            interest = interest_per
            payment_for_period = payment
        installments.append(
            Installment(
                loan=loan,
                installment_number=number,
                due_date=due_date,
                principal_component=principal,
                interest_component=interest,
                amount_due=payment_for_period,
            )
        )
        principal_remaining = max(Decimal("0.00"), principal_remaining - principal)
        interest_remaining = max(Decimal("0.00"), interest_remaining - interest)
    Installment.objects.bulk_create(installments)


def rebuild_loan_schedule(loan):
    """Rebuild installments from the disbursement date and loan terms."""
    if loan.payments.exists():
        return False
    generate_schedule(loan)
    actual_total = sum((item.amount_due for item in loan.installments.all()), Decimal("0.00"))
    loan.total_payable = actual_total
    loan.outstanding_balance = actual_total
    loan.save(update_fields=["total_payable", "outstanding_balance"])
    return True


def ensure_schedule_current(loan):
    """Regenerate the schedule when it is out of sync with loan terms or disbursement date."""
    sync_loan_term_from_application(loan)
    if schedule_is_stale(loan):
        return rebuild_loan_schedule(loan)
    return False


def _bucket_status(chunk):
    statuses = {row.status for row in chunk}
    if statuses == {Installment.Status.PAID}:
        return Installment.Status.PAID, "Paid"
    if Installment.Status.OVERDUE in statuses:
        return Installment.Status.OVERDUE, "Overdue"
    return Installment.Status.PENDING, "Pending"


def _schedule_period_buckets(installments, days_per_period, label_prefix):
    """Group daily installments into fixed working-day periods."""
    items = list(installments)
    buckets = []
    for index in range(0, len(items), days_per_period):
        chunk = items[index : index + days_per_period]
        if not chunk:
            continue
        amount = sum((row.amount_due for row in chunk), Decimal("0.00"))
        principal = sum((row.principal_component for row in chunk), Decimal("0.00"))
        interest = sum((row.interest_component for row in chunk), Decimal("0.00"))
        paid = sum((row.amount_paid for row in chunk), Decimal("0.00"))
        remaining = max(Decimal("0.00"), amount - paid)
        status, status_label = _bucket_status(chunk)
        period_number = (index // days_per_period) + 1
        month_number = (index // WORKING_DAYS_PER_MONTH) + 1
        buckets.append(
            {
                "installment_number": period_number,
                "period_number": period_number,
                "month_number": month_number,
                "label": f"{label_prefix} {period_number}",
                "due_date": chunk[-1].due_date,
                "start_date": chunk[0].due_date,
                "end_date": chunk[-1].due_date,
                "amount": amount,
                "adjusted_amount": adjust_payment(amount),
                "principal": principal,
                "interest": interest,
                "amount_paid": paid,
                "remaining": remaining,
                "status": status,
                "status_label": status_label,
                "day_count": len(chunk),
                "is_next": any(getattr(row, "is_next", False) for row in chunk),
            }
        )
    return buckets


def schedule_month_buckets(installments):
    """Group daily installments into loan months (22 working days each)."""
    return _schedule_period_buckets(installments, WORKING_DAYS_PER_MONTH, "Month")


def schedule_week_buckets(installments):
    """Group daily installments into weeks (5 working days each)."""
    return _schedule_period_buckets(installments, WORKING_DAYS_PER_WEEK, "Week")


def schedule_biweek_buckets(installments):
    """Group daily installments into biweekly periods (10 working days each)."""
    return _schedule_period_buckets(installments, WORKING_DAYS_PER_BIWEEK, "Biweek")


def application_schedule_view_mode(loan):
    """Map the application's Pay frequency to the schedule Display view."""
    frequency = getattr(loan.application, "payment_frequency", None) or "monthly"
    return {
        "daily": "day",
        "weekly": "week",
        "biweekly": "biweek",
        "monthly": "month",
    }.get(frequency, "month")


def view_mode_to_pay_frequency(view_mode):
    """Map schedule Display view to Record payment frequency."""
    return {
        "day": "daily",
        "daily": "daily",
        "week": "weekly",
        "weekly": "weekly",
        "biweek": "biweekly",
        "biweekly": "biweekly",
        "month": "monthly",
        "monthly": "monthly",
    }.get(str(view_mode or "").lower(), "daily")


def next_due_for_display(loan, view_mode="day"):
    """Next remittance for the selected schedule Display (day/week/biweek/month)."""
    view_mode = {
        "day": "day",
        "daily": "day",
        "week": "week",
        "weekly": "week",
        "biweek": "biweek",
        "biweekly": "biweek",
        "month": "month",
        "monthly": "month",
    }.get(str(view_mode or "").lower(), "day")
    frequency = view_mode_to_pay_frequency(view_mode)
    next_item = loan.next_installment
    if not next_item:
        return None

    if view_mode == "day":
        amount = next_item.remaining
        return {
            "due_date": next_item.due_date,
            "label": "Daily",
            "frequency": frequency,
            "amount": amount,
            "adjusted_amount": adjust_payment(amount),
        }

    installments = list(loan.installments.all())
    if view_mode == "week":
        buckets = schedule_week_buckets(installments)
        label = "Weekly"
    elif view_mode == "biweek":
        buckets = schedule_biweek_buckets(installments)
        label = "Biweekly"
    else:
        buckets = schedule_month_buckets(installments)
        label = "Monthly"

    bucket = next(
        (row for row in buckets if row["status"] != Installment.Status.PAID),
        None,
    )
    if not bucket:
        return None

    amount = bucket["remaining"]
    return {
        "due_date": bucket["due_date"],
        "start_date": bucket["start_date"],
        "end_date": bucket["end_date"],
        "label": bucket.get("label") or label,
        "frequency": frequency,
        "amount": amount,
        "adjusted_amount": adjust_payment(amount),
    }


def schedule_display_rows(loan, view_mode="month", month=None):
    """Build schedule rows for day, week, biweek, or month display."""
    return _schedule_display_from_installments(
        list(loan.installments.all()),
        loan_term_months(loan),
        view_mode=view_mode,
        month=month,
    )


class _VirtualInstallment:
    """In-memory installment used to preview an original (pre-reschedule) plan."""

    Status = Installment.Status

    def __init__(self, number, due_date, principal, interest, amount_due):
        self.installment_number = number
        self.due_date = due_date
        self.principal_component = principal
        self.interest_component = interest
        self.amount_due = amount_due
        self.amount_paid = Decimal("0.00")
        self.status = Installment.Status.PENDING
        self.paid_date = None

    @property
    def remaining(self):
        return self.amount_due

    @property
    def amount(self):
        return self.amount_due

    @property
    def adjusted_amount(self):
        return adjust_payment(self.amount_due)

    @property
    def principal(self):
        return self.principal_component

    @property
    def interest(self):
        return self.interest_component

    @property
    def status_label(self):
        return "Original"

    @property
    def is_next(self):
        return False


def build_virtual_installments(principal, interest_rate, term_months, start_date):
    """Generate working-day installments in memory (does not touch the database)."""
    amounts = calculate_flat_loan_amounts(principal, interest_rate, term_months)
    periods = amounts["periods"]
    payment = amounts["per_day"]
    principal_per = (Decimal(str(principal)) / Decimal(periods)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    interest_per = (amounts["total_interest"] / Decimal(periods)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    principal_remaining = Decimal(str(principal))
    interest_remaining = amounts["total_interest"]
    due_date = _interest_start_monday(start_date)
    rows = []
    for number in range(1, periods + 1):
        if number > 1:
            due_date = _next_working_day(due_date)
        if number == periods:
            principal_part = principal_remaining
            interest_part = interest_remaining
            payment_for_period = principal_part + interest_part
        else:
            principal_part = principal_per
            interest_part = interest_per
            payment_for_period = payment
        rows.append(
            _VirtualInstallment(
                number,
                due_date,
                principal_part,
                interest_part,
                payment_for_period,
            )
        )
        principal_remaining = max(Decimal("0.00"), principal_remaining - principal_part)
        interest_remaining = max(Decimal("0.00"), interest_remaining - interest_part)
    return rows


def original_schedule_display_rows(loan, view_mode="month", month=None):
    """Rebuild the pre-reschedule schedule for display only."""
    terms = loan.original_schedule_terms()
    installments = build_virtual_installments(
        terms["principal"],
        terms["interest_rate"],
        terms["term_months"],
        terms["start_date"],
    )
    display = _schedule_display_from_installments(
        installments,
        terms["term_months"],
        view_mode=view_mode,
        month=month,
    )
    display["original_terms"] = terms
    return display


def _schedule_display_from_installments(installments, term_months, view_mode="month", month=None):
    """Build schedule rows for day, week, biweek, or month display from installment rows."""
    aliases = {
        "day": "day",
        "daily": "day",
        "week": "week",
        "weekly": "week",
        "biweek": "biweek",
        "biweekly": "biweek",
        "month": "month",
        "monthly": "month",
    }
    view_mode = aliases.get(str(view_mode or "").lower(), "month")
    term = int(term_months)
    month_options = [{"value": str(number), "label": f"Month {number}"} for number in range(1, term + 1)]

    selected_month = None
    if month not in (None, "", "all"):
        try:
            selected_month = int(month)
        except (TypeError, ValueError):
            selected_month = None
        if selected_month is not None and (selected_month < 1 or selected_month > term):
            selected_month = None

    if view_mode == "month":
        schedule = schedule_month_buckets(installments)
        if selected_month is not None:
            schedule = [row for row in schedule if row["month_number"] == selected_month]
    elif view_mode == "week":
        schedule = schedule_week_buckets(installments)
        if selected_month is not None:
            schedule = [row for row in schedule if row["month_number"] == selected_month]
    elif view_mode == "biweek":
        schedule = schedule_biweek_buckets(installments)
        if selected_month is not None:
            schedule = [row for row in schedule if row["month_number"] == selected_month]
    else:
        if selected_month is not None:
            start = (selected_month - 1) * WORKING_DAYS_PER_MONTH
            schedule = installments[start : start + WORKING_DAYS_PER_MONTH]
        else:
            schedule = installments

    if view_mode in {"month", "week", "biweek"}:
        paid_count = sum(1 for row in schedule if row["status"] == Installment.Status.PAID)
    else:
        paid_count = sum(1 for row in schedule if row.status == Installment.Status.PAID)

    return {
        "view_mode": view_mode,
        "schedule": schedule,
        "payment_count": len(schedule),
        "paid_payment_count": paid_count,
        "month_options": month_options,
        "selected_month": str(selected_month) if selected_month else "",
    }


def _membership_savings_deposit_amount():
    for item in STANDARD_DISBURSEMENT_DEDUCTIONS:
        if item["key"] == "membership_savings":
            return item["amount"]
    return Decimal("0.00")


def _kap_mutual_aid_contribution_amount():
    for item in STANDARD_DISBURSEMENT_DEDUCTIONS:
        if item["key"] == "kap_mutual_aid":
            return item["amount"]
    return Decimal("0.00")


@transaction.atomic
def disburse_application(
    application,
    amount,
    method,
    reference,
    disbursed_date=None,
    processing_fee=Decimal("0.00"),
    other_fees=Decimal("0.00"),
    other_fees_description="",
    disbursed_by=None,
):
    if hasattr(application, "loan"):
        return application.loan
    rate = application.final_interest_rate or application.loan_product.interest_rate
    term = application.final_term_months or application.term_months
    grace_period_days = application.loan_product.grace_period_days if application.loan_product else 0
    # Flat interest: total_payable = principal + (principal * rate% * term_months).
    # The daily schedule splits that total across term_months * 22 working days.
    amounts = calculate_flat_loan_amounts(amount, rate, term)
    total_payable = amounts["total_payable"]
    loan = Loan.objects.create(
        application=application,
        principal=amount,
        disbursed_principal=amount,
        interest_rate=rate,
        term_months=term,
        grace_period_days=grace_period_days,
        disbursed_date=disbursed_date or timezone.localdate(),
        total_payable=total_payable,
        # Nothing has been paid yet, so what's owed (outstanding_balance) equals the
        # full total_payable. From here, record_payment() below subtracts each
        # payment from this field: outstanding_balance = total_payable - sum(payments).
        outstanding_balance=total_payable,
        disbursement_method=method,
        disbursement_reference=reference,
        processing_fee=processing_fee,
        other_fees=other_fees,
        other_fees_description=other_fees_description,
        disbursed_by=disbursed_by,
    )
    rebuild_loan_schedule(loan)
    application.status = LoanApplication.Status.ACTIVE
    application.decision_date = application.decision_date or timezone.now()
    application.save(update_fields=["status", "decision_date"])

    # Credit compulsory deductions into savings / mutual aid for the member.
    membership_deposit = _membership_savings_deposit_amount()
    if membership_deposit > 0:
        from savings.services import credit_membership_savings_from_disbursement

        credit_membership_savings_from_disbursement(
            application.borrower,
            membership_deposit,
            loan_reference=loan.reference,
            created_by=disbursed_by,
            disbursed_date=loan.disbursed_date,
        )

    kap_contribution = _kap_mutual_aid_contribution_amount()
    if kap_contribution > 0:
        from mutual_aid.services import credit_kap_mutual_aid_from_disbursement

        credit_kap_mutual_aid_from_disbursement(
            application.borrower,
            kap_contribution,
            loan_reference=loan.reference,
            recorded_by=disbursed_by,
            disbursed_date=loan.disbursed_date,
        )
    return loan


def create_notification(user, title, message, *, kind=Notification.Kind.GENERAL, link_url=""):
    """Create an in-app notification for a user."""
    if not user:
        return None
    return Notification.objects.create(
        user=user,
        kind=kind,
        title=title,
        message=message,
        link_url=link_url or "",
    )


def _apply_credit_penalty(borrower, penalty):
    previous = normalize_credit_score(borrower.credit_score)
    borrower.credit_score = normalize_credit_score(borrower.credit_score - penalty)
    borrower.save(update_fields=["credit_score"])
    return previous, normalize_credit_score(borrower.credit_score)


def loan_month_number(installment_number):
    """1-based loan month for a daily installment (22 working days per month)."""
    return ((int(installment_number) - 1) // WORKING_DAYS_PER_MONTH) + 1


def loan_month_installment_range(month_number):
    """Inclusive (start, end) installment numbers for a loan month."""
    start = ((int(month_number) - 1) * WORKING_DAYS_PER_MONTH) + 1
    end = int(month_number) * WORKING_DAYS_PER_MONTH
    return start, end


def _month_end_due_date(loan, month_number):
    start, end = loan_month_installment_range(month_number)
    return (
        loan.installments.filter(installment_number__gte=start, installment_number__lte=end)
        .order_by("-installment_number")
        .values_list("due_date", flat=True)
        .first()
    )


def _month_already_penalized(loan, month_number):
    start, end = loan_month_installment_range(month_number)
    return loan.installments.filter(
        installment_number__gte=start,
        installment_number__lte=end,
        credit_penalty_applied=True,
    ).exists()


def _mark_month_penalized(loan, month_number):
    start, end = loan_month_installment_range(month_number)
    loan.installments.filter(installment_number__gte=start, installment_number__lte=end).update(
        credit_penalty_applied=True
    )


def _notify_credit_score_deduction(borrower, loan, month_number, previous_score, new_score):
    from django.urls import reverse

    create_notification(
        borrower,
        title="Credit score deducted",
        message=(
            f"Your credit score was reduced by {format_credit_score(LATE_PAYMENT_CREDIT_PENALTY)} "
            f"because loan {loan.reference} month {month_number} was paid late "
            f"(or left unpaid after the month ended). "
            f"Score: {format_credit_score(previous_score)} to {format_credit_score(new_score)}."
        ),
        kind=Notification.Kind.CREDIT_SCORE,
        link_url=reverse("borrower_dashboard"),
    )


def _apply_late_month_credit_penalty(borrower, loan, month_number):
    """Deduct 0.1 once per late loan month; skip if already applied or month not late."""
    if _month_already_penalized(loan, month_number):
        return False
    month_end = _month_end_due_date(loan, month_number)
    if not month_end or timezone.localdate() <= month_end:
        return False
    previous_score, new_score = _apply_credit_penalty(borrower, LATE_PAYMENT_CREDIT_PENALTY)
    _mark_month_penalized(loan, month_number)
    _notify_credit_score_deduction(borrower, loan, month_number, previous_score, new_score)
    return True


@transaction.atomic
def record_payment(loan, amount, method, reference, user, installment=None):
    loan = Loan.objects.select_for_update().get(pk=loan.pk)
    payment = loan.payments.create(
        amount=amount,
        method=method,
        reference_number=reference or f"PAY-{timezone.now():%Y%m%d%H%M%S}",
        recorded_by=user,
        installment=installment,
    )
    remaining = amount
    installments = (
        [loan.installments.select_for_update().get(pk=installment.pk)]
        if installment
        else list(loan.installments.select_for_update().filter(status__in=["pending", "overdue"]).order_by("due_date"))
    )
    touched_months = set()
    paid_date = timezone.localdate()
    for item in installments:
        if remaining <= 0:
            break
        applied = min(remaining, item.remaining)
        item.amount_paid += applied
        if item.amount_paid >= item.amount_due:
            item.status = Installment.Status.PAID
            item.paid_date = paid_date
            touched_months.add(loan_month_number(item.installment_number))
        item.save(update_fields=["amount_paid", "status", "paid_date"])
        remaining -= applied

    # Credit score: −0.1 only when a loan month is paid after its month-end due date.
    # On-time monthly remittances (paid on/before month end) do not deduct, even if
    # individual working days inside the month were past their daily due dates.
    if touched_months:
        borrower = User.objects.select_for_update().get(pk=loan.application.borrower_id)
        for month_number in sorted(touched_months):
            month_end = _month_end_due_date(loan, month_number)
            if month_end and paid_date > month_end:
                _apply_late_month_credit_penalty(borrower, loan, month_number)

    # outstanding_balance is kept as total_payable minus every payment recorded so
    # far, rather than recomputed from scratch each time — but it's the same number:
    # outstanding_balance == total_payable - sum(loan.payments.values('amount')).
    # clamped at 0 so a stray overpayment can't push the balance negative.
    loan.outstanding_balance = max(Decimal("0.00"), loan.outstanding_balance - amount)
    # Balance hit zero -> the loan is fully repaid, so flip its status to PAID
    # automatically. Any partial payment leaves it ACTIVE (installments still track
    # per-period paid/pending/overdue individually).
    loan.status = Loan.Status.PAID if loan.outstanding_balance == 0 else Loan.Status.ACTIVE
    loan.save(update_fields=["outstanding_balance", "status"])
    loan.application.status = LoanApplication.Status.CLOSED if loan.status == Loan.Status.PAID else LoanApplication.Status.ACTIVE
    loan.application.save(update_fields=["status"])
    return payment


@transaction.atomic
def mark_overdue_installments():
    today = timezone.localdate()
    overdue_installments = list(
        Installment.objects.select_for_update()
        .filter(status=Installment.Status.PENDING, due_date__lt=today)
        .select_related("loan__application__borrower")
    )
    if not overdue_installments:
        return 0

    # Still mark each unpaid working day overdue for schedule display.
    for installment in overdue_installments:
        installment.status = Installment.Status.OVERDUE
    Installment.objects.bulk_update(overdue_installments, ["status"])

    # Credit score: −0.1 once per late loan month (after that month's end due date),
    # not once per overdue working day.
    late_months = {}
    for installment in overdue_installments:
        loan = installment.loan
        month_number = loan_month_number(installment.installment_number)
        month_end = _month_end_due_date(loan, month_number)
        if not month_end or month_end >= today:
            continue
        key = (loan.application.borrower_id, loan.pk, month_number)
        late_months[key] = loan

    for (borrower_pk, _loan_pk, month_number), loan in late_months.items():
        borrower = User.objects.select_for_update().get(pk=borrower_pk)
        _apply_late_month_credit_penalty(borrower, loan, month_number)

    return len(overdue_installments)


def get_borrower_credit_summary(borrower, exclude_application=None):
    loans_qs = Loan.objects.filter(application__borrower=borrower).select_related(
        "application", "application__loan_product"
    )
    if exclude_application:
        loans_qs = loans_qs.exclude(application=exclude_application)

    prior_loans = list(loans_qs)
    prior_loan_count = len(prior_loans)
    loan_ids = [loan.pk for loan in prior_loans]

    if loan_ids:
        installments = Installment.objects.filter(loan_id__in=loan_ids)
    else:
        installments = Installment.objects.none()

    overdue_installment_count = installments.filter(status=Installment.Status.OVERDUE).count()
    # Late months = loan months that incurred the −0.1 credit penalty (not daily rows).
    late_payment_count = len(
        {
            (loan_id, loan_month_number(number))
            for loan_id, number in installments.filter(credit_penalty_applied=True).values_list(
                "loan_id", "installment_number"
            )
        }
    )
    # On-time months: fully paid months with no credit penalty.
    on_time_payment_count = 0
    for loan in prior_loans:
        for bucket in schedule_month_buckets(loan.installments.all()):
            if bucket["status"] != Installment.Status.PAID:
                continue
            start, end = loan_month_installment_range(bucket["month_number"])
            if loan.installments.filter(
                installment_number__gte=start,
                installment_number__lte=end,
                credit_penalty_applied=True,
            ).exists():
                continue
            on_time_payment_count += 1

    defaulted_loan_count = loans_qs.filter(status=Loan.Status.DEFAULTED).count()
    overdue_loan_count = loans_qs.filter(status=Loan.Status.OVERDUE).count()
    paid_loan_count = loans_qs.filter(status=Loan.Status.PAID).count()
    active_loan_count = loans_qs.filter(status__in=[Loan.Status.ACTIVE, Loan.Status.OVERDUE]).count()

    rejected_qs = borrower.loan_applications.filter(status=LoanApplication.Status.REJECTED)
    if exclude_application:
        rejected_qs = rejected_qs.exclude(pk=exclude_application.pk)
    rejected_application_count = rejected_qs.count()

    flags = []
    if defaulted_loan_count:
        flags.append(f"{defaulted_loan_count} defaulted loan(s)")
    if overdue_installment_count:
        flags.append(f"{overdue_installment_count} overdue installment(s)")
    if overdue_loan_count:
        flags.append(f"{overdue_loan_count} overdue loan(s)")
    if late_payment_count:
        flags.append(f"{late_payment_count} late month(s)")
    if rejected_application_count:
        flags.append(f"{rejected_application_count} rejected application(s)")

    if prior_loan_count == 0 and rejected_application_count == 0:
        risk_level = "new"
        risk_label = "No prior loans"
    elif defaulted_loan_count or overdue_installment_count >= 2 or rejected_application_count >= 2:
        risk_level = "poor"
        risk_label = "Payment concerns"
    elif overdue_installment_count or late_payment_count >= 2 or rejected_application_count or overdue_loan_count:
        risk_level = "fair"
        risk_label = "Fair standing"
    elif paid_loan_count > 0 and not flags:
        risk_level = "good"
        risk_label = "Good client"
    elif prior_loan_count > 0 and not flags:
        risk_level = "good"
        risk_label = "Good standing"
    else:
        risk_level = "fair"
        risk_label = "Review carefully"

    if risk_level == "poor":
        recommendation = "Review carefully — payment concerns"
        recommendation_reason = (
            "This borrower has " + ", ".join(flags) + ". Consider additional scrutiny before approving."
        )
    elif risk_level == "fair":
        recommendation = "Proceed with caution"
        recommendation_reason = (
            "Minor issues in payment history: " + ", ".join(flags) + "."
            if flags
            else "Some prior activity warrants a closer look at documents and income."
        )
    elif risk_level == "good":
        recommendation = "Favorable payment history"
        recommendation_reason = (
            f"{paid_loan_count} loan(s) paid successfully with no outstanding issues."
            if paid_loan_count
            else "Active loans are in good standing."
        )
    else:
        recommendation = "First-time borrower"
        recommendation_reason = (
            "No prior loan or rejection history. Base the decision on income, documents, and purpose."
        )

    return {
        "credit_score": normalize_credit_score(borrower.credit_score),
        "risk_level": risk_level,
        "risk_label": risk_label,
        "prior_loan_count": prior_loan_count,
        "paid_loan_count": paid_loan_count,
        "active_loan_count": active_loan_count,
        "overdue_installment_count": overdue_installment_count,
        "late_payment_count": late_payment_count,
        "on_time_payment_count": on_time_payment_count,
        "defaulted_loan_count": defaulted_loan_count,
        "rejected_application_count": rejected_application_count,
        "prior_loans": prior_loans,
        "flags": flags,
        "recommendation": recommendation,
        "recommendation_reason": recommendation_reason,
        "is_good_client": risk_level == "good",
        "has_payment_concerns": risk_level in ("poor", "fair"),
    }


def _activity_sort_key(value):
    if isinstance(value, datetime):
        return value if timezone.is_aware(value) else timezone.make_aware(value)
    return timezone.make_aware(datetime.combine(value, datetime.min.time()))


def get_officer_activity_log(officer, activity_type="all"):
    events = []

    if activity_type in ("all", "application"):
        applications = officer.reviewed_applications.select_related("borrower", "loan_product")
        for application in applications:
            if application.decision_date:
                events.append({
                    "kind": "application",
                    "title": f"{application.reference} {application.status_label.lower()}",
                    "description": application.review_notes or f"{application.product_name} decision recorded.",
                    "member_name": application.borrower_name,
                    "borrower_id": application.borrower_id,
                    "reference": application.reference,
                    "amount": application.amount_requested,
                    "created_at": application.decision_date,
                    "status": application.status,
                    "status_label": application.status_label,
                    "url_name": "application_review",
                    "url_kwargs": {"application_id": application.pk},
                })
            else:
                events.append({
                    "kind": "application",
                    "title": f"{application.reference} created",
                    "description": f"{application.product_name} · submitted for {application.borrower_name}.",
                    "member_name": application.borrower_name,
                    "borrower_id": application.borrower_id,
                    "reference": application.reference,
                    "amount": application.amount_requested,
                    "created_at": application.created_at,
                    "status": application.status,
                    "status_label": application.status_label,
                    "url_name": "application_review",
                    "url_kwargs": {"application_id": application.pk},
                })

    if activity_type in ("all", "payment"):
        payments = officer.recorded_payments.select_related(
            "loan", "loan__application", "loan__application__borrower", "loan__application__loan_product"
        )
        for payment in payments:
            loan = payment.loan
            events.append({
                "kind": "payment",
                "title": f"Payment recorded · {loan.reference}",
                "description": f"{payment.get_method_display()} · ref {payment.reference_number or payment.pk}",
                "member_name": loan.application.borrower_name,
                "borrower_id": loan.application.borrower_id,
                "reference": payment.reference_number or f"PAY-{payment.pk:05d}",
                "amount": payment.amount,
                "created_at": payment.payment_date,
                "status": "paid",
                "status_label": "Payment",
                "url_name": "payment_receipt",
                "url_kwargs": {"payment_id": payment.pk},
            })

    if activity_type in ("all", "disbursement"):
        loans = officer.disbursed_loans.select_related("application", "application__borrower", "application__loan_product")
        for loan in loans:
            events.append({
                "kind": "disbursement",
                "title": f"{loan.reference} disbursed",
                "description": f"{loan.disbursement_method} · net {loan.net_release_amount:,.2f} released",
                "member_name": loan.application.borrower_name,
                "borrower_id": loan.application.borrower_id,
                "reference": loan.reference,
                "amount": loan.net_release_amount,
                "created_at": loan.disbursed_date,
                "status": "disbursed",
                "status_label": "Disbursed",
                "url_name": "disbursement_receipt",
                "url_kwargs": {"disbursement_id": loan.application_id},
            })

    events.sort(key=lambda event: _activity_sort_key(event["created_at"]), reverse=True)
    return events


def format_activity_timestamp(value):
    if isinstance(value, datetime):
        return timezone.localtime(value).strftime("%b %d, %Y · %I:%M %p")
    return value.strftime("%b %d, %Y")