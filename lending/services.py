from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP

from dateutil.relativedelta import relativedelta
from django.db import transaction
from django.utils import timezone

from django.db.models import F

from .models import Installment, Loan, LoanApplication, User

INITIAL_CREDIT_SCORE = Decimal("100.000")
LATE_PAYMENT_CREDIT_PENALTY = Decimal("0.1")
CREDIT_SCORE_PRECISION = Decimal("0.1")
MIN_CREDIT_SCORE_FOR_LOANS = Decimal("50.0")


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


def _is_monthly_frequency(payment_frequency):
    return payment_frequency == LoanApplication.PaymentFrequency.MONTHLY


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


def expected_period_count(term_months, payment_frequency):
    return term_months if _is_monthly_frequency(payment_frequency) else term_months * 2


def calculate_periodic_payment(principal, interest_rate, term_months, payment_frequency):
    """Amortized payment per period for the given loan terms."""
    periods = expected_period_count(term_months, payment_frequency)
    is_monthly = _is_monthly_frequency(payment_frequency)
    periodic_rate = (interest_rate / Decimal("100")) / (Decimal("12") if is_monthly else Decimal("26"))
    if periodic_rate == 0:
        payment = principal / periods
    else:
        factor = (Decimal("1") + periodic_rate) ** periods
        payment = principal * periodic_rate * factor / (factor - Decimal("1"))
    return payment.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), periods


def _first_installment_due_date(loan):
    base = loan.disbursed_date + timedelta(days=loan.grace_period_days)
    if _is_monthly_frequency(loan.application.payment_frequency):
        return base + relativedelta(months=1)
    return base + timedelta(days=14)


def schedule_is_stale(loan):
    """True when stored installments no longer match the loan terms or disbursement date."""
    term = loan_term_months(loan)
    expected = expected_period_count(term, loan.application.payment_frequency)
    if loan.installments.count() != expected:
        return True
    first = loan.installments.order_by("installment_number").first()
    if not first:
        return True
    return first.due_date != _first_installment_due_date(loan)


def generate_schedule(loan):
    Installment.objects.filter(loan=loan).delete()
    frequency = loan.application.payment_frequency
    is_monthly = _is_monthly_frequency(frequency)
    term = loan_term_months(loan)
    payment, periods = calculate_periodic_payment(
        loan.principal, loan.interest_rate, term, frequency
    )
    periodic_rate = (loan.interest_rate / Decimal("100")) / (Decimal("12") if is_monthly else Decimal("26"))
    balance = loan.principal
    due_date = loan.disbursed_date + timedelta(days=loan.grace_period_days)
    installments = []
    for number in range(1, periods + 1):
        due_date = due_date + (relativedelta(months=1) if is_monthly else timedelta(days=14))
        if number == 1 and loan.grace_period_days > 0:
            interest = Decimal("0.00")
        else:
            interest = (balance * periodic_rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        principal = payment - interest
        if number == periods:
            principal = balance
            payment_for_period = principal + interest
        else:
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
        balance = max(Decimal("0.00"), balance - principal)
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
    installment, periods = calculate_periodic_payment(amount, rate, term, application.payment_frequency)
    total_payable = (installment * periods).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    loan = Loan.objects.create(
        application=application,
        principal=amount,
        interest_rate=rate,
        term_months=term,
        grace_period_days=grace_period_days,
        disbursed_date=disbursed_date or timezone.localdate(),
        total_payable=total_payable,
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
    return loan


def _apply_credit_penalty(borrower, penalty):
    borrower.credit_score = normalize_credit_score(borrower.credit_score - penalty)
    borrower.save(update_fields=["credit_score"])


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
    late_payment_count = 0
    for item in installments:
        if remaining <= 0:
            break
        was_overdue = item.status == Installment.Status.OVERDUE
        applied = min(remaining, item.remaining)
        item.amount_paid += applied
        if item.amount_paid >= item.amount_due:
            item.status = Installment.Status.PAID
            item.paid_date = timezone.localdate()
            if not was_overdue and item.paid_date > item.due_date:
                late_payment_count += 1
        item.save(update_fields=["amount_paid", "status", "paid_date"])
        remaining -= applied
    if late_payment_count:
        borrower = User.objects.select_for_update().get(pk=loan.application.borrower_id)
        _apply_credit_penalty(borrower, LATE_PAYMENT_CREDIT_PENALTY * late_payment_count)
    loan.outstanding_balance = max(Decimal("0.00"), loan.outstanding_balance - amount)
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

    borrower_penalties = {}
    for installment in overdue_installments:
        borrower_pk = installment.loan.application.borrower_id
        borrower_penalties[borrower_pk] = borrower_penalties.get(borrower_pk, Decimal("0")) + LATE_PAYMENT_CREDIT_PENALTY
        installment.status = Installment.Status.OVERDUE

    Installment.objects.bulk_update(overdue_installments, ["status"])

    for borrower_pk, penalty in borrower_penalties.items():
        borrower = User.objects.select_for_update().get(pk=borrower_pk)
        _apply_credit_penalty(borrower, penalty)

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
    late_payment_count = installments.filter(
        status=Installment.Status.PAID,
        paid_date__gt=F("due_date"),
    ).count()
    on_time_payment_count = installments.filter(status=Installment.Status.PAID).exclude(
        paid_date__gt=F("due_date")
    ).count()

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
        flags.append(f"{late_payment_count} late payment(s)")
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