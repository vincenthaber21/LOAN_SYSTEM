from datetime import date, datetime, time, timedelta
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP

from django.db import transaction
from django.utils import timezone

from .models import DisbursementSetting, ExpiredMonthSignature, Installment, Loan, LoanApplication, Notification, User

INITIAL_CREDIT_SCORE = Decimal("100.000")
LATE_PAYMENT_CREDIT_PENALTY = Decimal("0.1")
CREDIT_SCORE_PRECISION = Decimal("0.1")
MIN_CREDIT_SCORE_FOR_LOANS = Decimal("50.0")
BALANCE_EXTENSION_RATE = Decimal("5.00")
MAX_BALANCE_EXTENSION_MONTHS = 3
# Remaining principal at or below this amount is collected with no added interest.
NO_INTEREST_PRINCIPAL_CEILING = Decimal("1000.00")
# Fallback when DisbursementSetting is unavailable (Monday=0 … Sunday=6).
DISBURSEMENT_WEEKDAY = DisbursementSetting.Weekday.FRIDAY


def get_disbursement_setting():
    """Return the singleton disbursement weekday / time / condition settings."""
    return DisbursementSetting.load()


def get_disbursement_weekday():
    """Configured release weekday (0=Monday … 6=Sunday)."""
    return get_disbursement_setting().disbursement_weekday


def get_disbursement_start_time():
    """Configured local time when officers may start releasing funds."""
    return get_disbursement_setting().disbursement_start_time or time(8, 0)


def is_disbursement_condition_enabled():
    """True when the weekday/time disbursement rule is active."""
    return get_disbursement_setting().condition_enabled


def disbursement_weekday_label(weekday=None):
    """Display name for a weekday number (defaults to the configured day)."""
    weekday = get_disbursement_weekday() if weekday is None else weekday
    return dict(DisbursementSetting.Weekday.choices).get(weekday, "Friday")


def disbursement_start_time_label(value=None):
    """Friendly clock time, e.g. '8:00 AM'."""
    value = get_disbursement_start_time() if value is None else value
    formatted = value.strftime("%I:%M %p")
    return formatted.lstrip("0") if formatted.startswith("0") else formatted


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


def payment_adjustment_surplus(exact_amount):
    """Cash-rounding uplift (adjusted − exact) that is credited to member savings."""
    exact_amount = Decimal(str(exact_amount or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if exact_amount <= 0:
        return Decimal("0.00")
    adjusted = adjust_payment(exact_amount)
    return max(Decimal("0.00"), (adjusted - exact_amount).quantize(Decimal("0.01")))


def split_payment_for_savings(loan, amount, installment=None, savings_override=None):
    """Split a collected remittance into loan principal reduction and savings credit.

    Officers collect the cash-adjusted amount. The exact portion reduces the loan;
    the rounding difference (adjusted − exact) is deposited to the member's savings.

    Example: exact ₱1,430.30 → adjusted ₱1,435.00 → loan ₱1,430.30, savings ₱4.70.

    `savings_override` lets officers manually set the savings portion (0 … amount).
    """
    amount = Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if amount <= 0:
        return Decimal("0.00"), Decimal("0.00")

    if savings_override is not None:
        surplus = Decimal(str(savings_override or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if surplus < 0:
            surplus = Decimal("0.00")
        if surplus > amount:
            surplus = amount
        return (amount - surplus).quantize(Decimal("0.01")), surplus

    outstanding = (loan.outstanding_balance or Decimal("0.00")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    candidates = []
    if installment is not None:
        candidates.append(installment.remaining)
    for frequency in ("daily", "weekly", "biweekly", "monthly"):
        candidates.append(loan.suggested_payment_for(frequency, adjust=False))
        # Raw period totals (uncapped) so schedule PAYMENT/EXACT pairs still match.
        mapping = {
            "daily": loan.daily_payment,
            "weekly": loan.weekly_payment,
            "biweekly": loan.biweekly_payment,
            "monthly": loan.monthly_payment,
        }
        candidates.append(mapping[frequency])
    next_item = loan.next_installment
    if next_item is not None:
        candidates.append(next_item.remaining)
    candidates.append(outstanding)

    seen = set()
    for exact in candidates:
        exact = Decimal(str(exact or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if exact <= 0 or exact in seen:
            continue
        seen.add(exact)
        capped_exact = min(exact, outstanding) if outstanding > 0 else exact
        adjusted = adjust_payment(capped_exact)
        if amount == adjusted and adjusted > capped_exact:
            return capped_exact, (adjusted - capped_exact).quantize(Decimal("0.01"))

    # Paying the cash-adjusted outstanding (or any amount between exact and adjusted).
    adjusted_outstanding = adjust_payment(outstanding)
    if outstanding > 0 and amount > outstanding and amount <= adjusted_outstanding:
        return outstanding, (amount - outstanding).quantize(Decimal("0.01"))

    # Entire collection applies to the loan (no identifiable rounding surplus).
    return amount, Decimal("0.00")


def credit_payment_adjustment_to_savings(loan, surplus, *, payment=None, recorded_by=None):
    """Deposit the cash-rounding surplus from a loan payment into membership savings."""
    surplus = Decimal(str(surplus or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if surplus <= 0:
        return None

    from savings.services import credit_payment_adjustment_from_loan

    payment_ref = ""
    if payment is not None:
        payment_ref = payment.reference_number or f"PAY-{payment.pk}"
    return credit_payment_adjustment_from_loan(
        loan.application.borrower,
        surplus,
        loan_reference=loan.reference,
        payment_reference=payment_ref,
        created_by=recorded_by,
    )

def application_type_for_member(member, exclude_pk=None):
    """New for first-time applicants; Renew when the member already has loan history."""
    if not member:
        return LoanApplication.ApplicationType.NEW
    loans = Loan.objects.filter(application__borrower=member)
    prior_applications = LoanApplication.objects.filter(borrower=member)
    if exclude_pk:
        loans = loans.exclude(application_id=exclude_pk)
        prior_applications = prior_applications.exclude(pk=exclude_pk)
    if loans.exists() or prior_applications.exists():
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
        from .audit import application_decision_log

        application_decision_log(reviewer, other)


WORKING_DAYS_PER_MONTH = 22
WORKING_DAYS_PER_WEEK = 5
WORKING_DAYS_PER_BIWEEK = 10

# Display-only term weeks (does not change payment / interest formulas).
# Example: 4 months → 16 weeks total.
CALENDAR_WEEKS_PER_MONTH = 4


def term_weeks_total(term_months):
    """Calendar weeks for a term: months × 4 (display only)."""
    try:
        months = int(term_months)
    except (TypeError, ValueError):
        return 0
    if months <= 0:
        return 0
    return months * CALENDAR_WEEKS_PER_MONTH


def term_weeks_summary(term_months):
    """Display labels for term weeks without changing calculation formulas."""
    months = 0
    try:
        months = int(term_months)
    except (TypeError, ValueError):
        months = 0
    total = term_weeks_total(months)
    return {
        "term_months": months,
        "weeks_total": total,
        "label": f"{total} weeks" if total else "",
        "short_label": f"{total} weeks" if total else "",
    }


def daily_mutual_aid_amount():
    """Compulsory mutual aid per working day from Features (default ₱15)."""
    from .models import Features

    amount = Features.load().daily_mutual_aid_amount
    return Decimal(str(amount or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def mutual_aid_working_days_for_frequency(frequency):
    """Working days covered by a remittance frequency (daily / weekly / …)."""
    return {
        "daily": 1,
        "weekly": WORKING_DAYS_PER_WEEK,
        "biweekly": WORKING_DAYS_PER_BIWEEK,
        "monthly": WORKING_DAYS_PER_MONTH,
    }.get(str(frequency or "daily").lower(), 1)


def mutual_aid_for_pay_frequency(frequency):
    """Mutual aid portion for a pay period: daily rate × working days in that period."""
    per_day = daily_mutual_aid_amount()
    if per_day <= 0:
        return Decimal("0.00")
    days = mutual_aid_working_days_for_frequency(frequency)
    return (per_day * Decimal(days)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def mutual_aid_days_covered_by_amount(loan, amount, *, max_days=None):
    """How many working days a remittance covers (loan daily + ₱15 mutual aid per day)."""
    amount = Decimal(str(amount or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    per_day_ma = daily_mutual_aid_amount()
    if amount <= 0 or per_day_ma <= 0:
        return 0

    daily_loan = Decimal(str(loan.daily_payment or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if daily_loan <= 0:
        return 1

    unit = adjust_payment(daily_loan) + per_day_ma
    if unit <= 0:
        return 1

    days = int((amount / unit).to_integral_value(rounding=ROUND_FLOOR))
    if days < 1:
        days = 1

    if max_days is None:
        max_days = loan.installments.exclude(status=Installment.Status.PAID).count()
    max_days = int(max_days or 0)
    if max_days > 0:
        days = min(days, max_days)
    return days


def mutual_aid_for_remittance_amount(loan, amount, *, max_days=None):
    """Mutual aid for a collected amount: ₱15 × working days covered by that remittance."""
    per_day = daily_mutual_aid_amount()
    if per_day <= 0:
        return Decimal("0.00")
    days = mutual_aid_days_covered_by_amount(loan, amount, max_days=max_days)
    if days <= 0:
        return Decimal("0.00")
    total = (per_day * Decimal(days)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    amount = Decimal(str(amount or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return min(total, amount) if amount > 0 else Decimal("0.00")


def credit_payment_mutual_aid(loan, amount, *, payment=None, recorded_by=None, pay_frequency="daily"):
    """Credit the mutual-aid portion of a loan remittance to KAP mutual aid."""
    amount = Decimal(str(amount or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if amount <= 0:
        return None

    from mutual_aid.services import credit_kap_mutual_aid_from_loan_payment

    payment_ref = ""
    if payment is not None:
        payment_ref = payment.reference_number or f"PAY-{payment.pk}"
    return credit_kap_mutual_aid_from_loan_payment(
        loan.application.borrower,
        amount,
        loan_reference=loan.reference,
        payment_reference=payment_ref,
        recorded_by=recorded_by,
        pay_frequency=pay_frequency,
    )


def loan_term_months(loan):
    """Term used for schedule generation and flat-interest recalculation.

    After a remaining-balance reschedule (`schedule_start_date` set), always use
    `loan.term_months` (the 1–3 month extension). Same after payments have begun —
    unpaid periods shrink and term tracks the rebuilt schedule. Otherwise prefer
    the approved application term so pre-disbursement edits stay in sync.
    """
    if loan.schedule_start_date is not None or loan.payments.exists():
        return loan.term_months
    return loan.application.final_term_months or loan.application.term_months


def sync_loan_term_from_application(loan):
    """Keep loan.term_months aligned with the application before any payments are recorded.

    Never overwrite a remaining-balance reschedule — that term is the extension length
    chosen for the outstanding principal, not the original application term.
    """
    if loan.payments.exists() or loan.schedule_start_date is not None:
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


def principal_waives_interest(principal):
    """True when remaining principal is ₱1,000 or less — no interest is added."""
    amount = Decimal(str(principal or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return amount > 0 and amount <= NO_INTEREST_PRINCIPAL_CEILING


def calculate_flat_loan_amounts(principal, interest_rate, term_months):
    """Flat interest using working-day collection (Mon–Fri, 22 days/month).

    Example — capital 10000, rate 6%, term 3 months:
        total_interest = 10000 * 0.06 * 3 = 1800
        total_payable  = 1800 + 10000 = 11800
        per_day        = 11800 / (3 * 22) = 178.79
        per_month      = 11800 / 3 = 3933.33

    When principal is ₱1,000 or less, interest is waived (payable = principal only).
    """
    principal = Decimal(str(principal)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    months = Decimal(int(term_months))
    periods = working_day_count(term_months)
    if principal_waives_interest(principal) or months <= 0 or periods <= 0:
        per_day = (
            (principal / Decimal(periods)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            if periods > 0
            else Decimal("0.00")
        )
        per_month = (
            (principal / months).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            if months > 0
            else principal
        )
        return {
            "principal": principal,
            "total_interest": Decimal("0.00"),
            "total_payable": principal,
            "periods": periods,
            "per_day": per_day,
            "per_month": per_month,
            "interest_waived": principal_waives_interest(principal),
        }
    rate = Decimal(str(interest_rate)) / Decimal("100")
    total_interest = (principal * rate * months).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    total_payable = (principal + total_interest).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    per_day = (total_payable / Decimal(periods)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    per_month = (total_payable / months).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return {
        "principal": principal,
        "total_interest": total_interest,
        "total_payable": total_payable,
        "periods": periods,
        "per_day": per_day,
        "per_month": per_month,
        "interest_waived": False,
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


def pre_reschedule_maturity_date(loan):
    """Last due date of the original (pre-reschedule) plan.

    Used to enforce the same rule as the first balance extension: the reschedule
    start date must be after that expired maturity day, not on it.
    """
    if loan.schedule_start_date is None:
        return loan_maturity_date(loan)
    terms = loan.original_schedule_terms()
    rows = build_virtual_installments(
        terms["principal"],
        terms["interest_rate"],
        terms["term_months"],
        terms["start_date"],
    )
    if not rows:
        return None
    return rows[-1].due_date


def loan_term_expired(loan):
    """True when the scheduled term has ended (last due date is before today)."""
    maturity = loan_maturity_date(loan)
    if not maturity:
        return False
    return maturity < timezone.localdate()


def can_extend_loan_balance(loan):
    """Expired-term loans with a remaining balance can be restructured (1–3 months @ 5%)."""
    if loan.status == Loan.Status.PAID:
        return False
    if (loan.outstanding_balance or Decimal("0.00")) <= 0:
        return False
    return loan_term_expired(loan)


def balance_extension_principal(loan):
    """Remaining balance used as the base for balance extensions.

    Matches the cash-adjusted Remaining balance on the loan page (not bare principal).
    Flat 5% is then applied on that amount for the chosen months; the total becomes
    the new principal.
    """
    return adjust_payment(loan.outstanding_balance or Decimal("0.00"))


def balance_extension_quote(principal, months):
    """Flat-interest quote for paying a remaining balance over `months` at 5%.

    Returns total_payable as the new principal (remaining balance + interest).
    Amounts of ₱1,000 or less are quoted with no interest.
    """
    months = int(months)
    amounts = calculate_flat_loan_amounts(principal, BALANCE_EXTENSION_RATE, months)
    rate = Decimal("0.00") if amounts.get("interest_waived") else BALANCE_EXTENSION_RATE
    return {
        "months": months,
        "principal": amounts["principal"],
        "capital": amounts["principal"],
        "interest_rate": rate,
        "total_interest": amounts["total_interest"],
        "total_payable": amounts["total_payable"],
        "new_principal": amounts["total_payable"],
        "per_day": amounts["per_day"],
        "per_month": amounts["per_month"],
        "periods": amounts["periods"],
        "interest_waived": bool(amounts.get("interest_waived")),
    }


def balance_extension_previews(loan):
    """Quotes for 1–3 month balance extensions on the remaining balance."""
    remaining = balance_extension_principal(loan)
    return [balance_extension_quote(remaining, months) for months in range(1, MAX_BALANCE_EXTENSION_MONTHS + 1)]


class BalanceExtensionError(ValueError):
    """Raised when a remaining-balance extension cannot be applied."""


def _flat_amounts_for_periods(principal, interest_rate, periods):
    """Flat interest for an exact number of working-day periods (same formula, proportional months).

    Remaining principal of ₱1,000 or less carries no interest.
    """
    principal = Decimal(str(principal)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    periods = int(periods)
    if periods <= 0:
        return {
            "principal": principal,
            "total_interest": Decimal("0.00"),
            "total_payable": principal,
            "periods": 0,
            "per_day": Decimal("0.00"),
            "per_month": Decimal("0.00"),
            "months": Decimal("0"),
            "interest_waived": principal_waives_interest(principal),
        }
    months = Decimal(periods) / Decimal(WORKING_DAYS_PER_MONTH)
    if principal_waives_interest(principal):
        per_day = (principal / Decimal(periods)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        per_month = (per_day * Decimal(WORKING_DAYS_PER_MONTH)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return {
            "principal": principal,
            "total_interest": Decimal("0.00"),
            "total_payable": principal,
            "periods": periods,
            "per_day": per_day,
            "per_month": per_month,
            "months": months,
            "interest_waived": True,
        }
    rate = Decimal(str(interest_rate)) / Decimal("100")
    total_interest = (principal * rate * months).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    total_payable = (principal + total_interest).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    per_day = (total_payable / Decimal(periods)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    per_month = (per_day * Decimal(WORKING_DAYS_PER_MONTH)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return {
        "principal": principal,
        "total_interest": total_interest,
        "total_payable": total_payable,
        "periods": periods,
        "per_day": per_day,
        "per_month": per_month,
        "months": months,
        "interest_waived": False,
    }


def _credit_installments_for_payment(unpaid_installments, loan_amount, paid_date):
    """Mark as many leading installments Paid as this payment's loan portion fully covers.

    Walks the given (already-ordered, unpaid) installments oldest-due-first, consuming
    loan_amount as credit. An installment flips to Paid only once the remaining credit
    fully covers its amount_due — a payment doesn't need to settle the whole loan to
    retire individual periods, but a partial period is left unpaid (its due amount is
    re-priced by the flat-rate recompute on the reduced principal instead).

    Example: 3 unpaid weeks at ₱437.50 each, loan_amount ₱1,000 → weeks 1–2 are marked
    Paid (₱875.00 consumed); ₱125.00 remains, not enough for week 3, so week 3 (and
    anything after it) stays unpaid and gets re-priced on the smaller remaining balance.

    Returns (paid, remaining): installments marked Paid (already saved to the database)
    and the still-unpaid installments, in order, for the caller to rebuild.
    """
    credit = Decimal(str(loan_amount or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    paid = []
    remaining = []
    still_covering = credit > 0
    for item in unpaid_installments:
        if still_covering and credit >= item.amount_due:
            credit = (credit - item.amount_due).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            item.amount_paid = item.amount_due
            item.status = Installment.Status.PAID
            item.paid_date = paid_date
            paid.append(item)
        else:
            still_covering = False
            remaining.append(item)
    if paid:
        Installment.objects.bulk_update(paid, ["amount_paid", "status", "paid_date"])
    return paid, remaining


def _rebuild_schedule_for_remaining_principal(loan, new_principal, start_due_date, periods):
    """Replace unpaid installments with a flat schedule on the reduced principal.

    Each day's amount_due is principal_component + interest_component so the schedule
    always sums exactly to the flat formula total (remaining principal + interest).
    """
    loan.installments.exclude(status=Installment.Status.PAID).delete()
    amounts = _flat_amounts_for_periods(new_principal, loan.interest_rate, periods)
    principal_per = (new_principal / Decimal(periods)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    interest_per = (amounts["total_interest"] / Decimal(periods)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    principal_remaining = new_principal
    interest_remaining = amounts["total_interest"]
    due_date = start_due_date
    paid_count = loan.installments.filter(status=Installment.Status.PAID).count()
    rows = []
    for offset in range(periods):
        if offset > 0:
            due_date = _next_working_day(due_date)
        number = paid_count + offset + 1
        if offset == periods - 1:
            principal = principal_remaining
            interest = interest_remaining
        else:
            principal = principal_per
            interest = interest_per
        payment_for_period = (principal + interest).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        rows.append(
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
    Installment.objects.bulk_create(rows)
    return amounts


@transaction.atomic
def extend_loan_balance(loan, months, start_date=None):
    """Restructure an expired loan's remaining balance over 1–3 months at 5% flat interest.

    Formula on the remaining balance (same cash-adjusted figure shown on the loan page):
        total_interest = remaining_balance * 0.05 * months
        new_principal  = remaining_balance + total_interest

    Example: remaining balance ₱8,945 × 5% × 1 month → new principal ₱9,392.25.
    Interest is rolled into principal once at reschedule; the new schedule collects
    that balance with no further interest added on top.

    `start_date` is when the new term begins (schedule_start_date). It must be after
    the expired maturity date — not on that day. Defaults to today when omitted.
    """
    months = int(months)
    if months < 1 or months > MAX_BALANCE_EXTENSION_MONTHS:
        raise BalanceExtensionError(f"Choose between 1 and {MAX_BALANCE_EXTENSION_MONTHS} months.")

    loan = Loan.objects.select_for_update().get(pk=loan.pk)
    if not can_extend_loan_balance(loan):
        raise BalanceExtensionError(
            "Balance extension is only available when the loan term has expired and a balance remains."
        )

    maturity = loan_maturity_date(loan)
    schedule_start = start_date or timezone.localdate()
    if maturity and schedule_start <= maturity:
        raise BalanceExtensionError(
            "Reschedule start date must be after the loan’s expired maturity date, not on that day."
        )

    remaining = balance_extension_principal(loan)
    if remaining <= 0:
        raise BalanceExtensionError("Adjusted remaining balance must be greater than zero.")
    quote = balance_extension_quote(remaining, months)
    # Remaining balance + 5% flat — this total is the new principal.
    new_principal = quote["total_payable"]
    # Historical loan remittances only (exclude mutual aid / savings top-ups).
    payments_sum = sum(
        (payment.loan_amount_applied for payment in loan.payments.all()),
        Decimal("0.00"),
    )

    if loan.disbursed_principal is None:
        loan.disbursed_principal = loan.principal
    if loan.original_interest_rate is None:
        loan.original_interest_rate = loan.interest_rate
    if loan.original_term_months is None:
        loan.original_term_months = loan.term_months

    loan.principal = new_principal
    # Interest already included in new_principal; later payments only reduce this balance.
    loan.interest_rate = Decimal("0.00")
    loan.term_months = months
    loan.schedule_start_date = schedule_start
    loan.outstanding_balance = new_principal
    loan.total_payable = (payments_sum + new_principal).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
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
    # Schedule collects the new principal (remaining balance + interest) evenly.
    generate_schedule(loan)
    actual_total = sum((item.amount_due for item in loan.installments.all()), Decimal("0.00"))
    if actual_total != loan.outstanding_balance:
        loan.principal = actual_total
        loan.outstanding_balance = actual_total
        loan.total_payable = (payments_sum + actual_total).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        loan.save(update_fields=["principal", "outstanding_balance", "total_payable"])
        quote = dict(quote)
        quote["total_payable"] = actual_total
        quote["total_interest"] = (actual_total - remaining).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    quote = dict(quote)
    quote["new_principal"] = loan.principal
    quote["capital"] = remaining
    return loan, quote


@transaction.atomic
def update_reschedule_start_date(loan, start_date):
    """Correct the reschedule start date and realign installment due dates.

    Used when an officer mistyped the date after a balance-extension reschedule.
    The new date must still be after the original expired maturity date (not that
    day). If the loan has no payments yet, the schedule is rebuilt. Otherwise
    installment due dates are shifted by the change in interest-start Monday.
    """
    if start_date is None:
        raise BalanceExtensionError("Reschedule start date is required.")
    if loan.schedule_start_date is None:
        raise BalanceExtensionError("This loan has not been rescheduled.")

    loan = Loan.objects.select_for_update().get(pk=loan.pk)
    maturity = pre_reschedule_maturity_date(loan)
    if maturity and start_date <= maturity:
        raise BalanceExtensionError(
            "Reschedule start date must be after the loan’s expired maturity date, not on that day."
        )

    old_date = loan.schedule_start_date
    if old_date == start_date:
        return loan, {
            "changed": False,
            "old_date": old_date,
            "new_date": start_date,
            "interest_start": _interest_start_monday(start_date),
            "mode": "unchanged",
            "moved_count": 0,
            "delta_days": 0,
        }

    loan.schedule_start_date = start_date
    loan.save(update_fields=["schedule_start_date"])

    if not loan.payments.exists():
        generate_schedule(loan)
        return loan, {
            "changed": True,
            "old_date": old_date,
            "new_date": start_date,
            "interest_start": _interest_start_monday(start_date),
            "mode": "rebuilt",
            "moved_count": loan.installments.count(),
            "delta_days": (
                _interest_start_monday(start_date) - _interest_start_monday(old_date)
            ).days,
        }

    delta = _interest_start_monday(start_date) - _interest_start_monday(old_date)
    installments = list(loan.installments.all())
    if delta.days != 0 and installments:
        for item in installments:
            item.due_date = item.due_date + delta
        Installment.objects.bulk_update(installments, ["due_date"])

    return loan, {
        "changed": True,
        "old_date": old_date,
        "new_date": start_date,
        "interest_start": _interest_start_monday(start_date),
        "mode": "shifted" if delta.days else "aligned",
        "moved_count": len(installments) if delta.days else 0,
        "delta_days": delta.days,
    }


def schedule_is_stale(loan):
    """True when stored installments no longer match the loan terms or disbursement date."""
    if not loan.installments.exists():
        return True
    # After payments, paid rows are kept for history while unpaid rows (and often
    # term_months) shrink. Total count will no longer equal term×22 — that is
    # expected, and rebuild_loan_schedule refuses to wipe paid history anyway.
    if loan.payments.exists():
        return False
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

    Flat interest is charged on the current principal for every month of the term:

        total_interest = principal * (rate/100) * term_months
        total_payable  = principal + total_interest

    Payments reduce principal (e.g. ₱20,000 − ₱3,000 → ₱17,000) and the same
    formula is reapplied on the new principal for the remaining working days.
    When remaining principal is ₱1,000 or less, interest is waived.

    Interest begins on the Monday on/after `schedule_start_date` (or disbursement).
    That total is then split evenly across `term_months * 22` working-day payments.
    Each day's amount_due equals its principal + interest components so the schedule
    sums exactly to total_payable; the final row absorbs component rounding.
    """
    Installment.objects.filter(loan=loan).delete()
    term = loan_term_months(loan)
    amounts = calculate_flat_loan_amounts(loan.principal, loan.interest_rate, term)
    periods = amounts["periods"]
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
        else:
            principal = principal_per
            interest = interest_per
        payment_for_period = (principal + interest).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
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
    payments_sum = sum(
        (item.loan_amount_applied for item in loan.payments.all()),
        Decimal("0.00"),
    )
    loan.outstanding_balance = actual_total
    loan.total_payable = (payments_sum + actual_total).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    loan.save(update_fields=["total_payable", "outstanding_balance"])
    return True


def _repair_rescheduled_flat_balances(loan):
    """Keep rescheduled loans on the remaining-balance-as-principal model.

    After reschedule:
        new_principal = capital + capital * 0.05 * months   # e.g. ₱11,500
        outstanding   = new_principal
        interest_rate = 0  (interest already rolled into principal)

    Legacy rows that still store capital as principal with 5% on top are migrated
    so principal equals the remaining balance.
    """
    if loan.schedule_start_date is None:
        return False

    payments_sum = sum(
        (item.loan_amount_applied for item in loan.payments.all()),
        Decimal("0.00"),
    )
    principal = Decimal(str(loan.principal or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    outstanding = (loan.outstanding_balance or Decimal("0.00")).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    rate = Decimal(str(loan.interest_rate or 0))

    if loan.payments.exists():
        unpaid = list(
            loan.installments.exclude(status=Installment.Status.PAID).order_by(
                "installment_number", "due_date"
            )
        )
        if not unpaid:
            return False
        periods = len(unpaid)
        # Legacy: interest still separate from principal — roll into principal once.
        if outstanding > principal and rate > 0:
            loan.principal = outstanding
            loan.interest_rate = Decimal("0.00")
            loan.save(update_fields=["principal", "interest_rate"])
            principal = outstanding
            rate = Decimal("0.00")
        amounts = _flat_amounts_for_periods(principal, rate, periods)
        expected = amounts["total_payable"]
        if outstanding == expected and rate == 0:
            # Still rebuild if unpaid rows carry leftover interest components.
            unpaid_interest = sum((row.interest_component for row in unpaid), Decimal("0.00"))
            if unpaid_interest <= 0 and outstanding == sum(
                (row.amount_due for row in unpaid), Decimal("0.00")
            ):
                return False
        start_due = unpaid[0].due_date
        amounts = _rebuild_schedule_for_remaining_principal(loan, principal, start_due, periods)
        loan.outstanding_balance = amounts["total_payable"]
        if loan.principal != amounts["total_payable"] and rate == 0:
            loan.principal = amounts["total_payable"]
        loan.total_payable = (payments_sum + amounts["total_payable"]).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        loan.save(update_fields=["principal", "outstanding_balance", "total_payable"])
        return True

    # No payments yet — migrate capital+interest rows to remaining-balance principal.
    changed = False
    if outstanding > principal and rate > 0:
        for months in range(1, MAX_BALANCE_EXTENSION_MONTHS + 1):
            quote_total = calculate_flat_loan_amounts(
                principal, BALANCE_EXTENSION_RATE, months
            )["total_payable"]
            if quote_total == outstanding:
                if loan.term_months != months:
                    loan.term_months = months
                    changed = True
                break
        loan.principal = outstanding
        loan.interest_rate = Decimal("0.00")
        loan.save(
            update_fields=["principal", "interest_rate"]
            + (["term_months"] if changed else [])
        )
        principal = outstanding
        rate = Decimal("0.00")
        changed = True
    elif rate > 0 and principal == outstanding:
        # Principal already equals remaining balance; stop charging interest again.
        loan.interest_rate = Decimal("0.00")
        loan.save(update_fields=["interest_rate"])
        rate = Decimal("0.00")
        changed = True

    amounts = calculate_flat_loan_amounts(principal, rate, loan.term_months)
    expected = amounts["total_payable"]
    expected_periods = amounts["periods"]
    schedule_total = sum((item.amount_due for item in loan.installments.all()), Decimal("0.00"))
    schedule_count = loan.installments.count()
    needs_rebuild = (
        changed
        or schedule_count != expected_periods
        or schedule_total != expected
        or outstanding != expected
        or schedule_is_stale(loan)
    )
    if not needs_rebuild:
        return False

    generate_schedule(loan)
    actual_total = sum((item.amount_due for item in loan.installments.all()), Decimal("0.00"))
    loan.principal = actual_total
    loan.outstanding_balance = actual_total
    loan.total_payable = (payments_sum + actual_total).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    loan.save(update_fields=["principal", "outstanding_balance", "total_payable"])
    return True


def ensure_schedule_current(loan):
    """Regenerate the schedule when it is out of sync with loan terms or disbursement date.

    Also strips interest from unpaid rows when remaining principal is ₱1,000 or less,
    and keeps rescheduled loans aligned with the remaining-balance flat formula.
    """
    sync_loan_term_from_application(loan)
    if loan.schedule_start_date is not None and _repair_rescheduled_flat_balances(loan):
        return True
    if schedule_is_stale(loan):
        return rebuild_loan_schedule(loan)
    if _waive_interest_on_small_remaining_principal(loan):
        return True
    return False


def _waive_interest_on_small_remaining_principal(loan):
    """If principal is ≤₱1,000, rebuild unpaid schedule with principal only (no interest)."""
    principal = Decimal(str(loan.principal or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if not principal_waives_interest(principal):
        return False
    unpaid = list(
        loan.installments.exclude(status=Installment.Status.PAID).order_by("installment_number", "due_date")
    )
    if not unpaid:
        if (loan.outstanding_balance or Decimal("0.00")) != principal:
            loan.outstanding_balance = principal
            loan.save(update_fields=["outstanding_balance"])
            return True
        return False

    unpaid_interest = sum((row.interest_component for row in unpaid), Decimal("0.00"))
    if unpaid_interest <= 0 and (loan.outstanding_balance or Decimal("0.00")) <= principal:
        return False

    periods = len(unpaid)
    start_due = unpaid[0].due_date
    amounts = _rebuild_schedule_for_remaining_principal(loan, principal, start_due, periods)
    loan_payments_sum = sum(
        (item.loan_amount_applied for item in loan.payments.all()),
        Decimal("0.00"),
    )
    loan.outstanding_balance = amounts["total_payable"]
    loan.total_payable = (loan_payments_sum + amounts["total_payable"]).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    loan.save(update_fields=["outstanding_balance", "total_payable"])
    return True


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


def _mutual_aid_for_day_count(day_count):
    """Mutual aid for a schedule row covering `day_count` working days."""
    per_day = daily_mutual_aid_amount()
    if per_day <= 0:
        return Decimal("0.00")
    days = max(1, int(day_count or 1))
    return (per_day * Decimal(days)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _annotate_schedule_with_mutual_aid(schedule, view_mode):
    """Normalize schedule rows for display.

    The collect amount is the loan remittance the officer collects (rounded up to
    the nearest ₱5). Mutual aid is a separate, manual contribution now, so it is no
    longer folded into the collect total; the per-row mutual aid is left at ₱0.00.
    """
    annotated = []
    for row in schedule:
        if view_mode == "day":
            loan_adjusted = row.adjusted_amount
            annotated.append(
                {
                    "installment_number": row.installment_number,
                    "due_date": row.due_date,
                    "amount": row.amount,
                    "loan_adjusted_amount": loan_adjusted,
                    "mutual_aid": Decimal("0.00"),
                    "adjusted_amount": loan_adjusted,
                    "principal": row.principal,
                    "interest": row.interest,
                    "status": row.status,
                    "status_label": row.status_label,
                    "is_next": row.is_next,
                    "day_count": 1,
                }
            )
        else:
            loan_adjusted = row["adjusted_amount"]
            row = dict(row)
            row["loan_adjusted_amount"] = loan_adjusted
            row["mutual_aid"] = Decimal("0.00")
            row["adjusted_amount"] = loan_adjusted
            annotated.append(row)
    return annotated


def _fold_schedule_buckets(buckets, target_count, label_prefix):
    """Merge overflow buckets into the last allowed period (display only)."""
    items = list(buckets)
    if target_count <= 0 or len(items) <= target_count:
        return items
    kept = [dict(row) for row in items[:target_count]]
    overflow = items[target_count:]
    last = kept[-1]
    for extra in overflow:
        last["amount"] = last["amount"] + extra["amount"]
        last["principal"] = last["principal"] + extra["principal"]
        last["interest"] = last["interest"] + extra["interest"]
        last["amount_paid"] = last.get("amount_paid", Decimal("0.00")) + extra.get(
            "amount_paid", Decimal("0.00")
        )
        last["remaining"] = last["remaining"] + extra["remaining"]
        last["day_count"] = int(last.get("day_count") or 0) + int(extra.get("day_count") or 0)
        last["end_date"] = extra["end_date"]
        last["due_date"] = extra["due_date"]
        last["is_next"] = bool(last.get("is_next")) or bool(extra.get("is_next"))
    last["adjusted_amount"] = adjust_payment(last["amount"])
    statuses = {last["status"]} | {row["status"] for row in overflow}
    if Installment.Status.OVERDUE in statuses:
        last["status"] = Installment.Status.OVERDUE
        last["status_label"] = "Overdue"
    elif Installment.Status.PENDING in statuses:
        last["status"] = Installment.Status.PENDING
        last["status_label"] = "Pending"
    else:
        last["status"] = Installment.Status.PAID
        last["status_label"] = "Paid"
    last["installment_number"] = target_count
    last["period_number"] = target_count
    last["label"] = f"{label_prefix} {target_count}"
    return kept


def schedule_month_buckets(installments):
    """Group daily installments into loan months (22 working days each)."""
    return _schedule_period_buckets(installments, WORKING_DAYS_PER_MONTH, "Month")


def expired_month_rows(loan):
    """Unpaid loan months whose collection period has already ended."""
    today = timezone.localdate()
    signatures = {
        (row.month_number, row.start_date): row
        for row in loan.expired_month_signatures.all()
    }
    rows = []
    for bucket in schedule_month_buckets(list(loan.installments.all())):
        if bucket["status"] == Installment.Status.PAID:
            continue
        if bucket["end_date"] >= today:
            continue
        signature = signatures.get((bucket["month_number"], bucket["start_date"]))
        row = dict(bucket)
        row["signature"] = signature
        row["is_signed"] = bool(signature)
        rows.append(row)
    return rows


class ExpiredMonthSignatureError(ValueError):
    """Raised when an expired-month signature cannot be recorded."""


def record_expired_month_signature(loan, month_number, start_date, signature_file, signed_name, recorded_by=None):
    """Store a borrower signature acknowledging an expired unpaid loan month."""
    month_number = int(month_number)
    match = next(
        (
            row
            for row in expired_month_rows(loan)
            if row["month_number"] == month_number and row["start_date"] == start_date
        ),
        None,
    )
    if not match:
        raise ExpiredMonthSignatureError("That month is not an expired unpaid payment period.")
    if match["is_signed"]:
        raise ExpiredMonthSignatureError("This expired month is already signed.")
    if signature_file is None:
        raise ExpiredMonthSignatureError("Borrower signature is required.")

    record = ExpiredMonthSignature(
        loan=loan,
        month_number=month_number,
        start_date=match["start_date"],
        end_date=match["end_date"],
        remaining_amount=match["remaining"],
        signed_name=(signed_name or "").strip(),
        recorded_by=recorded_by,
    )
    record.signature.save(signature_file.name, signature_file, save=False)
    record.save()
    return record


def _display_term_months(installments, term_months):
    """Months used to cap week/biweek/month schedule views.

    Payments mark leading installments Paid and shrink the unpaid remaining term,
    but paid rows stay on the loan for history. The remaining `term_months` can
    therefore be shorter than the installment list — derive a floor from the
    actual row count so weeks are not crushed into the last remaining period.
    """
    term = max(1, int(term_months or 1))
    count = len(installments)
    if count <= 0:
        return term
    spanned = int(
        (Decimal(count) / Decimal(WORKING_DAYS_PER_MONTH)).to_integral_value(
            rounding=ROUND_CEILING
        )
    )
    return max(term, max(1, spanned))


def schedule_week_buckets(installments, term_months=None):
    """Group daily installments into weeks (5 working days each).

    Display is capped at months × 4 weeks. Leftover working days fold into the
    last week so a 4-month term shows Week 1–16 only. Daily formulas are unchanged.
    """
    items = list(installments)
    display_term = _display_term_months(items, term_months)
    buckets = _schedule_period_buckets(items, WORKING_DAYS_PER_WEEK, "Week")
    return _fold_schedule_buckets(buckets, term_weeks_total(display_term), "Week")


def schedule_biweek_buckets(installments, term_months=None):
    """Group daily installments into biweekly periods (10 working days each).

    Display is capped at months × 2 biweeks so the weekly standard (months × 4)
    stays consistent. Daily formulas are unchanged.
    """
    items = list(installments)
    display_term = _display_term_months(items, term_months)
    buckets = _schedule_period_buckets(items, WORKING_DAYS_PER_BIWEEK, "Biweek")
    target = term_weeks_total(display_term) // 2
    return _fold_schedule_buckets(buckets, target, "Biweek")


def application_schedule_view_mode(loan):
    """Map the application's Pay frequency to the schedule Display view."""
    frequency = getattr(loan.application, "payment_frequency", None) or "monthly"
    return payment_frequency_to_view_mode(frequency)


def payment_frequency_to_view_mode(frequency):
    """Map a payment-frequency choice to the schedule Display view."""
    return {
        "daily": "day",
        "weekly": "week",
        "biweekly": "biweek",
        "monthly": "month",
    }.get(str(frequency or "").lower(), "month")


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
            "mutual_aid": Decimal("0.00"),
            "adjusted_amount": adjust_payment(amount),
        }

    installments = list(loan.installments.all())
    term = loan_term_months(loan)
    if view_mode == "week":
        buckets = schedule_week_buckets(installments, term_months=term)
        label = "Weekly"
    elif view_mode == "biweek":
        buckets = schedule_biweek_buckets(installments, term_months=term)
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
        "mutual_aid": Decimal("0.00"),
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
        else:
            principal_part = principal_per
            interest_part = interest_per
        payment_for_period = (principal_part + interest_part).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
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


def application_payment_preview(application, view_mode=None, month=None, start_date=None):
    """In-memory repayment schedule preview for an application (pre-approval / pre-disbursement).

    Uses requested amount, product (or final) rate/term, and assumed release date.
    Dates shift once the real disbursement date is recorded.
    """
    principal = application.amount_requested
    if principal is None:
        raise ValueError("Application has no requested amount.")
    rate = application.final_interest_rate
    if rate is None and application.loan_product_id:
        rate = application.loan_product.interest_rate
    if rate is None:
        raise ValueError("Application has no interest rate (assign a loan product).")
    term = application.final_term_months or application.term_months
    if not term:
        raise ValueError("Application has no term months.")
    assumed_release = start_date or next_disbursement_weekday()
    amounts = calculate_flat_loan_amounts(principal, rate, term)
    installments = build_virtual_installments(principal, rate, term, assumed_release)
    default_view = payment_frequency_to_view_mode(application.payment_frequency)
    display = _schedule_display_from_installments(
        installments,
        term,
        view_mode=view_mode or default_view,
        month=month,
    )
    per_day = amounts["per_day"]
    per_week = (per_day * Decimal(WORKING_DAYS_PER_WEEK)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    per_biweek = (per_day * Decimal(WORKING_DAYS_PER_BIWEEK)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    per_month = amounts["per_month"]
    interest_start = _interest_start_monday(assumed_release)
    display.update({
        "preview_terms": {
            "principal": amounts["principal"],
            "interest_rate": rate,
            "term_months": int(term),
            "weeks_total": term_weeks_total(term),
            "weeks_label": term_weeks_summary(term)["label"],
            "periods": amounts["periods"],
            "total_interest": amounts["total_interest"],
            "total_payable": amounts["total_payable"],
            "adjusted_total_payable": adjust_payment(amounts["total_payable"]),
            "per_day": per_day,
            "adjusted_per_day": adjust_payment(per_day),
            "per_week": per_week,
            "adjusted_per_week": adjust_payment(per_week),
            "per_biweek": per_biweek,
            "adjusted_per_biweek": adjust_payment(per_biweek),
            "per_month": per_month,
            "adjusted_per_month": adjust_payment(per_month),
            "assumed_release_date": assumed_release,
            "interest_start_date": interest_start,
            "payment_frequency": application.payment_frequency,
            "payment_frequency_label": application.get_payment_frequency_display(),
            "product_name": application.product_name,
        },
        "application_pay_frequency": application.payment_frequency,
        "default_view_mode": default_view,
    })
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
    items = list(installments)
    term = _display_term_months(items, term_months)
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
        schedule = schedule_month_buckets(items)
        if selected_month is not None:
            schedule = [row for row in schedule if row["month_number"] == selected_month]
    elif view_mode == "week":
        schedule = schedule_week_buckets(items, term_months=term)
        if selected_month is not None:
            schedule = [row for row in schedule if row["month_number"] == selected_month]
    elif view_mode == "biweek":
        schedule = schedule_biweek_buckets(items, term_months=term)
        if selected_month is not None:
            schedule = [row for row in schedule if row["month_number"] == selected_month]
    else:
        if selected_month is not None:
            start = (selected_month - 1) * WORKING_DAYS_PER_MONTH
            schedule = items[start : start + WORKING_DAYS_PER_MONTH]
        else:
            schedule = items

    if view_mode in {"month", "week", "biweek"}:
        paid_count = sum(1 for row in schedule if row["status"] == Installment.Status.PAID)
    else:
        paid_count = sum(1 for row in schedule if row.status == Installment.Status.PAID)

    schedule = _annotate_schedule_with_mutual_aid(schedule, view_mode)

    return {
        "view_mode": view_mode,
        "schedule": schedule,
        "payment_count": len(schedule),
        "paid_payment_count": paid_count,
        "month_options": month_options,
        "selected_month": str(selected_month) if selected_month else "",
        "daily_mutual_aid_amount": daily_mutual_aid_amount(),
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


class DisbursementDayError(ValueError):
    """Raised when a loan release is attempted outside the configured weekday/time window."""


def is_disbursement_weekday(value=None):
    """True when the date matches the configured disbursement weekday."""
    value = value or timezone.localdate()
    return value.weekday() == get_disbursement_weekday()


def is_disbursement_time_open(*, now=None):
    """True when the current local time is at or after the configured start time."""
    now = now or timezone.localtime()
    return now.time() >= get_disbursement_start_time()


def next_disbursement_weekday(from_date=None):
    """Return the next configured release day on or after `from_date` (today when omitted)."""
    from_date = from_date or timezone.localdate()
    target = get_disbursement_weekday()
    days_ahead = (target - from_date.weekday()) % 7
    return from_date + timedelta(days=days_ahead)


def disbursement_day_error_message(*, today=None, disbursed_date=None, now=None):
    """Human-readable reason why a disbursement cannot proceed."""
    if not is_disbursement_condition_enabled():
        return ""
    now = now or timezone.localtime()
    today = today or timezone.localdate()
    day_label = disbursement_weekday_label()
    start_label = disbursement_start_time_label()
    next_day = next_disbursement_weekday(today)
    if not is_disbursement_weekday(today):
        return (
            f"Loan disbursements are only allowed on {day_label}s from {start_label}. "
            f"Next release day is {next_day.strftime('%A, %b %d, %Y')}."
        )
    if not is_disbursement_time_open(now=now):
        return (
            f"Loan disbursements start at {start_label} on {day_label}s. "
            f"You can release funds after {start_label}."
        )
    if disbursed_date is not None and not is_disbursement_weekday(disbursed_date):
        return f"Disbursement date must be a {day_label}."
    return ""


def ensure_disbursement_allowed(disbursed_date=None, *, today=None, now=None):
    """Raise DisbursementDayError unless the weekday/time condition allows the release."""
    now = now or timezone.localtime()
    today = today or timezone.localdate()
    release_date = disbursed_date or today
    if release_date > today:
        raise DisbursementDayError("Disbursement date cannot be in the future.")
    if not is_disbursement_condition_enabled():
        return
    message = disbursement_day_error_message(today=today, disbursed_date=release_date, now=now)
    if message:
        raise DisbursementDayError(message)


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
    release_date = disbursed_date or timezone.localdate()
    ensure_disbursement_allowed(release_date)
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
        disbursed_date=release_date,
        total_payable=total_payable,
        # Nothing has been paid yet, so what's owed (outstanding_balance) equals the
        # full total_payable. Payments reduce principal and rebuild interest on the
        # remaining capital with the same flat formula.
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
def record_payment(
    loan,
    amount,
    method,
    reference,
    user,
    installment=None,
    savings_adjustment=None,
    mutual_aid_contribution=None,
    pay_frequency="daily",
    payment_date=None,
):
    """Record a payment that reduces principal, then recalculates with the same flat formula.

    Cash-adjusted remittances are split: the exact portion reduces principal, the
    rounding uplift goes to Membership/Savings, and the mutual-aid portion
    (₱15 × working days by default) is credited to KAP mutual aid.
    """
    loan = Loan.objects.select_for_update().get(pk=loan.pk)
    amount = Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    paid_date = payment_date or timezone.localdate()
    outstanding_before = (loan.outstanding_balance or Decimal("0.00")).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )

    if mutual_aid_contribution is None:
        mutual_aid = mutual_aid_for_remittance_amount(loan, amount)
    else:
        mutual_aid = Decimal(str(mutual_aid_contribution or 0)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
    if mutual_aid < 0:
        mutual_aid = Decimal("0.00")
    if mutual_aid > amount:
        mutual_aid = amount

    remittance_for_loan = (amount - mutual_aid).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    loan_amount, savings_surplus = split_payment_for_savings(
        loan,
        remittance_for_loan,
        installment=installment,
        savings_override=savings_adjustment,
    )
    # Keep savings from eating into the mutual-aid carve-out if an override was too high.
    if savings_surplus > remittance_for_loan:
        savings_surplus = remittance_for_loan
        loan_amount = Decimal("0.00")

    payment = loan.payments.create(
        amount=amount,
        savings_adjustment=savings_surplus,
        mutual_aid_contribution=mutual_aid,
        method=method,
        reference_number=reference or f"PAY-{timezone.now():%Y%m%d%H%M%S}",
        recorded_by=user,
        installment=installment,
        payment_date=paid_date,
        outstanding_before=outstanding_before,
    )

    # Snapshot overdue months before the schedule is rebuilt (for late credit penalties).
    overdue_month_numbers = sorted(
        {
            loan_month_number(number)
            for number in loan.installments.filter(status=Installment.Status.OVERDUE).values_list(
                "installment_number", flat=True
            )
        }
    )

    if loan.disbursed_principal is None:
        loan.disbursed_principal = loan.principal

    unpaid = list(
        loan.installments.exclude(status=Installment.Status.PAID).order_by("installment_number", "due_date")
    )
    # Credit whichever leading installments this payment's loan portion fully covers —
    # they become Paid right away, even when the loan overall isn't settled yet. Only
    # the still-uncovered remainder gets re-priced on the reduced principal below.
    _paid_this_payment, unpaid = _credit_installments_for_payment(unpaid, loan_amount, paid_date)
    periods_remaining = len(unpaid)
    start_due = unpaid[0].due_date if unpaid else _first_installment_due_date(loan)

    # Only the exact loan portion reduces principal; surplus goes to savings / mutual aid.
    new_principal = max(
        Decimal("0.00"),
        (loan.principal - loan_amount).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
    )
    loan_payments_sum = sum(
        (item.loan_amount_applied for item in loan.payments.all()),
        Decimal("0.00"),
    )

    if new_principal <= 0:
        for item in loan.installments.exclude(status=Installment.Status.PAID):
            item.amount_paid = item.amount_due
            item.status = Installment.Status.PAID
            item.paid_date = paid_date
            item.save(update_fields=["amount_paid", "status", "paid_date"])
        loan.principal = Decimal("0.00")
        loan.outstanding_balance = Decimal("0.00")
        loan.status = Loan.Status.PAID
        loan.save(update_fields=["disbursed_principal", "principal", "outstanding_balance", "status"])
        loan.application.status = LoanApplication.Status.CLOSED
        loan.application.save(update_fields=["status"])
    else:
        if periods_remaining <= 0:
            periods_remaining = max(1, working_day_count(loan.term_months))
            start_due = _interest_start_monday(paid_date)
        amounts = _rebuild_schedule_for_remaining_principal(loan, new_principal, start_due, periods_remaining)
        # Paid rows stay for history; term must cover paid + remaining unpaid so
        # week/month views and working-day counts stay aligned with the schedule.
        paid_count = loan.installments.filter(status=Installment.Status.PAID).count()
        schedule_periods = paid_count + periods_remaining
        schedule_months = max(
            1,
            int(
                (Decimal(schedule_periods) / Decimal(WORKING_DAYS_PER_MONTH)).to_integral_value(
                    rounding=ROUND_CEILING
                )
            ),
        )
        loan.principal = new_principal
        loan.term_months = schedule_months
        loan.outstanding_balance = amounts["total_payable"]
        loan.total_payable = (loan_payments_sum + amounts["total_payable"]).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        loan.status = Loan.Status.ACTIVE
        loan.save(
            update_fields=[
                "disbursed_principal",
                "principal",
                "term_months",
                "outstanding_balance",
                "total_payable",
                "status",
            ]
        )
        loan.application.status = LoanApplication.Status.ACTIVE
        loan.application.save(update_fields=["status"])

    if overdue_month_numbers:
        borrower = User.objects.select_for_update().get(pk=loan.application.borrower_id)
        for month_number in overdue_month_numbers:
            month_end = _month_end_due_date(loan, month_number)
            if month_end and paid_date > month_end:
                _apply_late_month_credit_penalty(borrower, loan, month_number)

    if savings_surplus > 0:
        credit_payment_adjustment_to_savings(
            loan,
            savings_surplus,
            payment=payment,
            recorded_by=user,
        )
    if mutual_aid > 0:
        credit_payment_mutual_aid(
            loan,
            mutual_aid,
            payment=payment,
            recorded_by=user,
            pay_frequency=pay_frequency,
        )

    loan.refresh_from_db(fields=["outstanding_balance"])
    # Receipt outstanding moves by the principal applied to the loan (not the
    # interest-rebuilt schedule total, which can drop by more than the remittance).
    payment.outstanding_after = max(
        Decimal("0.00"),
        (outstanding_before - loan_amount).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
    )
    payment.save(update_fields=["outstanding_after"])
    return payment


def payment_receipt_balances(payment):
    """Return (outstanding_before, outstanding_after) for a payment receipt.

    Outstanding after is always before minus the principal applied to the loan.
    """
    applied = payment.loan_amount_applied
    if payment.outstanding_before is not None:
        before = payment.outstanding_before
    else:
        loan = payment.loan
        payments = list(loan.payments.order_by("pk"))
        is_latest = bool(payments) and payments[-1].pk == payment.pk
        if is_latest and len(payments) == 1:
            principal = (
                loan.disbursed_principal
                if loan.disbursed_principal is not None
                else (loan.principal or Decimal("0.00")) + applied
            )
            term = loan.original_term_months or loan.term_months
            before = calculate_flat_loan_amounts(principal, loan.interest_rate, term)[
                "total_payable"
            ]
        elif is_latest:
            after_loan = (loan.outstanding_balance or Decimal("0.00")).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            before = (after_loan + applied).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        else:
            paid_through = sum(
                (item.loan_amount_applied for item in payments if item.pk <= payment.pk),
                Decimal("0.00"),
            )
            principal = loan.disbursed_principal
            if principal is None:
                principal = (loan.principal or Decimal("0.00")) + sum(
                    (item.loan_amount_applied for item in payments),
                    Decimal("0.00"),
                )
            term = loan.original_term_months or loan.term_months
            original_total = calculate_flat_loan_amounts(
                principal, loan.interest_rate, term
            )["total_payable"]
            before = (original_total - paid_through + applied).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )

    after = max(
        Decimal("0.00"),
        (before - applied).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
    )
    return before, after


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


ACTIVITY_PERIOD_FILTERS = [
    {"value": "all", "label": "All time"},
    {"value": "day", "label": "Today"},
    {"value": "week", "label": "This week"},
    {"value": "month", "label": "This month"},
    {"value": "custom", "label": "Custom range"},
]


def _parse_iso_date(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return timezone.localtime(value).date() if timezone.is_aware(value) else value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def resolve_activity_date_range(period="all", date_from=None, date_to=None, today=None):
    today = today or timezone.localdate()
    if period not in {"all", "day", "week", "month", "custom"}:
        period = "all"

    if period == "day":
        return period, today, today
    if period == "week":
        start = today - timedelta(days=today.weekday())
        return period, start, start + timedelta(days=6)
    if period == "month":
        start = today.replace(day=1)
        if start.month == 12:
            end = date(start.year + 1, 1, 1) - timedelta(days=1)
        else:
            end = date(start.year, start.month + 1, 1) - timedelta(days=1)
        return period, start, end
    if period == "custom":
        start = _parse_iso_date(date_from)
        end = _parse_iso_date(date_to)
        if start and end and start > end:
            start, end = end, start
        return period, start, end
    return "all", None, None


def activity_range_label(period, date_from, date_to):
    if period == "all" or (not date_from and not date_to):
        return "All time"
    if date_from and date_to and date_from == date_to:
        day_label = date_from.strftime("%b %d, %Y")
        return f"Today · {day_label}" if period == "day" else day_label
    start_label = date_from.strftime("%b %d, %Y") if date_from else "…"
    end_label = date_to.strftime("%b %d, %Y") if date_to else "…"
    if date_from and date_to and date_from.year == date_to.year:
        start_label = date_from.strftime("%b %d")
    if period == "week":
        return f"This week · {start_label} – {end_label}"
    if period == "month":
        return f"This month · {start_label} – {end_label}"
    return f"{start_label} – {end_label}"


def _activity_sort_key(value):
    if isinstance(value, datetime):
        return value if timezone.is_aware(value) else timezone.make_aware(value)
    return timezone.make_aware(datetime.combine(value, datetime.min.time()))


def _activity_event_date(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        aware = value if timezone.is_aware(value) else timezone.make_aware(value)
        return timezone.localtime(aware).date()
    return value


def get_officer_activity_log(officer, activity_type="all", date_from=None, date_to=None):
    from .audit import KIND_BY_FILTER
    from .models import ActivityLog

    logs = ActivityLog.objects.filter(actor=officer).select_related("member")
    kind = KIND_BY_FILTER.get(activity_type)
    if kind:
        logs = logs.filter(kind=kind)
    if date_from:
        logs = logs.filter(created_at__date__gte=date_from)
    if date_to:
        logs = logs.filter(created_at__date__lte=date_to)
    return [log.as_event() for log in logs]


def format_activity_timestamp(value):
    if isinstance(value, datetime):
        return timezone.localtime(value).strftime("%b %d, %Y · %I:%M %p")
    return value.strftime("%b %d, %Y")