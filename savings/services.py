from datetime import datetime, time
from decimal import Decimal, ROUND_HALF_UP

from dateutil.relativedelta import relativedelta
from django.db import transaction
from django.utils import timezone

from .models import SavingsAccount, SavingsProduct, SavingsTransaction

MEMBERSHIP_SAVINGS_PRODUCT_NAME = "Membership/Savings Deposit"
# Older installs used this product name for the same compulsory deposit.
_LEGACY_MEMBERSHIP_SAVINGS_NAMES = ("LOAN SAVINGS", "Loan Savings")


class SavingsError(Exception):
    pass


def generate_account_number():
    from uuid import uuid4

    return f"SV{uuid4().hex[:10].upper()}"


def _opened_at_from_date(opened_on):
    if opened_on is None:
        return timezone.now()
    dt = datetime.combine(opened_on, time.min)
    if timezone.is_naive(dt):
        return timezone.make_aware(dt, timezone.get_current_timezone())
    return dt


def resolve_membership_savings_product():
    """Return the product used for compulsory Membership/Savings Deposit credits."""
    product = SavingsProduct.objects.filter(
        name__iexact=MEMBERSHIP_SAVINGS_PRODUCT_NAME,
        is_active=True,
    ).first()
    if product:
        return product

    for legacy_name in _LEGACY_MEMBERSHIP_SAVINGS_NAMES:
        product = SavingsProduct.objects.filter(name__iexact=legacy_name).first()
        if product:
            if product.name != MEMBERSHIP_SAVINGS_PRODUCT_NAME:
                product.name = MEMBERSHIP_SAVINGS_PRODUCT_NAME
                product.is_active = True
                product.save(update_fields=["name", "is_active"])
            elif not product.is_active:
                product.is_active = True
                product.save(update_fields=["is_active"])
            return product

    product, _ = SavingsProduct.objects.get_or_create(
        name=MEMBERSHIP_SAVINGS_PRODUCT_NAME,
        defaults={
            "description": "Compulsory membership savings credited from loan disbursement.",
            "interest_rate": Decimal("0.00"),
            "interest_term_months": 1,
            "min_balance": Decimal("0.00"),
            "min_deposit": Decimal("500.00"),
            "is_active": True,
        },
    )
    if not product.is_active:
        product.is_active = True
        product.save(update_fields=["is_active"])
    return product


def get_or_open_account(member, product, opened_by=None, opened_on=None):
    """Return the member's active account for the product, opening one if needed."""
    account = (
        SavingsAccount.objects.select_for_update()
        .filter(
            member=member,
            product=product,
            status=SavingsAccount.Status.ACTIVE,
        )
        .first()
    )
    if account:
        return account
    return open_account(member, product, opened_by=opened_by, opened_on=opened_on)


@transaction.atomic
def credit_membership_savings_from_disbursement(
    member,
    amount,
    *,
    loan_reference="",
    created_by=None,
    disbursed_date=None,
):
    """Open/credit Membership/Savings Deposit when a loan is disbursed."""
    amount = Decimal(str(amount))
    if amount <= 0:
        return None

    credited_on = disbursed_date or timezone.localdate()
    product = resolve_membership_savings_product()
    account = get_or_open_account(
        member,
        product,
        opened_by=created_by or member,
        opened_on=credited_on,
    )
    reference = f"DISB-{loan_reference}" if loan_reference else "DISB-MEMBERSHIP"
    notes = "Membership/Savings Deposit deducted from loan disbursement."
    return record_deposit(
        account,
        amount,
        SavingsTransaction.Method.ONLINE,
        reference=reference[:60],
        created_by=created_by,
        notes=notes,
        occurred_on=credited_on,
    )


@transaction.atomic
def credit_payment_adjustment_from_loan(
    member,
    amount,
    *,
    loan_reference="",
    payment_reference="",
    created_by=None,
):
    """Credit cash-rounding surplus from a loan remittance to Membership/Savings Deposit."""
    amount = Decimal(str(amount))
    if amount <= 0:
        return None

    product = resolve_membership_savings_product()
    account = get_or_open_account(
        member,
        product,
        opened_by=created_by or member,
    )
    reference = f"ADJ-{payment_reference}" if payment_reference else f"ADJ-{loan_reference}"
    notes = (
        f"Cash-rounding adjustment from loan payment on {loan_reference}."
        if loan_reference
        else "Cash-rounding adjustment from loan payment."
    )
    return record_deposit(
        account,
        amount,
        SavingsTransaction.Method.CASH,
        reference=reference[:60],
        created_by=created_by,
        notes=notes,
    )


@transaction.atomic
def open_account(member, product, opened_by=None, initial_deposit=None, opened_on=None):
    if not product.is_active:
        raise SavingsError("This savings product is not available.")

    existing = SavingsAccount.objects.filter(
        member=member,
        product=product,
        status=SavingsAccount.Status.ACTIVE,
    ).exists()
    if existing:
        raise SavingsError(f"You already have an active {product.name} account.")

    deposit_amount = initial_deposit or Decimal("0.00")
    if deposit_amount > 0 and deposit_amount < product.min_deposit:
        raise SavingsError(f"Initial deposit must be at least ₱{product.min_deposit:,.2f}.")

    account = SavingsAccount.objects.create(
        member=member,
        product=product,
        balance=Decimal("0.00"),
        opened_by=opened_by or member,
        opened_at=_opened_at_from_date(opened_on),
    )

    if deposit_amount > 0:
        record_deposit(
            account,
            deposit_amount,
            SavingsTransaction.Method.CASH,
            reference="Initial deposit",
            created_by=opened_by or member,
            notes="Opening deposit",
            occurred_on=opened_on,
        )
        account.refresh_from_db()

    return account


@transaction.atomic
def record_deposit(account, amount, method, reference="", created_by=None, notes="", occurred_on=None):
    if not account.is_operational:
        raise SavingsError("This savings account is not active.")

    amount = Decimal(str(amount))
    if amount <= 0:
        raise SavingsError("Deposit amount must be greater than zero.")
    if occurred_on and occurred_on > timezone.localdate():
        raise SavingsError("Transaction date cannot be in the future.")

    account.balance += amount
    account.save(update_fields=["balance"])

    return SavingsTransaction.objects.create(
        account=account,
        transaction_type=SavingsTransaction.Type.DEPOSIT,
        amount=amount,
        method=method,
        reference_number=reference,
        balance_after=account.balance,
        notes=notes,
        created_at=_datetime_on_date(occurred_on) if occurred_on else timezone.now(),
        created_by=created_by,
    )


@transaction.atomic
def record_withdrawal(account, amount, method, reference="", created_by=None, notes="", occurred_on=None):
    if not account.is_operational:
        raise SavingsError("This savings account is not active.")

    amount = Decimal(str(amount))
    if amount <= 0:
        raise SavingsError("Withdrawal amount must be greater than zero.")
    if amount > account.balance:
        raise SavingsError("Insufficient savings balance.")
    if occurred_on and occurred_on > timezone.localdate():
        raise SavingsError("Transaction date cannot be in the future.")

    remaining = account.balance - amount
    if remaining > 0 and remaining < account.product.min_balance:
        raise SavingsError(
            f"Withdrawal would leave a balance below the minimum of ₱{account.product.min_balance:,.2f}. "
            "Withdraw the full balance or leave at least the minimum."
        )

    account.balance = remaining
    account.save(update_fields=["balance"])

    return SavingsTransaction.objects.create(
        account=account,
        transaction_type=SavingsTransaction.Type.WITHDRAWAL,
        amount=amount,
        method=method,
        reference_number=reference,
        balance_after=account.balance,
        notes=notes,
        created_at=_datetime_on_date(occurred_on) if occurred_on else timezone.now(),
        created_by=created_by,
    )


@transaction.atomic
def close_account(account, closed_by=None):
    if account.balance > 0:
        raise SavingsError("Withdraw the remaining balance before closing this account.")
    account.status = SavingsAccount.Status.CLOSED
    account.closed_at = timezone.now()
    account.save(update_fields=["status", "closed_at"])
    return account


def _localdate(value):
    return timezone.localtime(value).date()


def _datetime_on_date(value):
    dt = datetime.combine(value, time.min)
    if timezone.is_naive(dt):
        return timezone.make_aware(dt, timezone.get_current_timezone())
    return dt


def calculate_interest_for_period(account):
    product = account.product
    balance = account.balance
    if balance < product.min_balance or product.interest_rate <= 0:
        return Decimal("0.00")
    interest = balance * (product.interest_rate / Decimal("100")) * (
        Decimal(product.interest_term_months) / Decimal("12")
    )
    return interest.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def interest_schedule_state(account, as_of=None, last_interest_tx=None):
    as_of = as_of or timezone.localdate()
    term = account.product.interest_term_months
    opened = _localdate(account.opened_at)
    if last_interest_tx is None:
        last_interest_tx = (
            account.transactions.filter(transaction_type=SavingsTransaction.Type.INTEREST)
            .order_by("-created_at")
            .only("created_at")
            .first()
        )
    last_date = _localdate(last_interest_tx.created_at) if last_interest_tx else None

    if last_date:
        due_date = last_date + relativedelta(months=term)
    else:
        due_date = opened + relativedelta(months=term)

    periods_due = 0
    while due_date <= as_of:
        periods_due += 1
        due_date += relativedelta(months=term)

    next_interest_date = due_date
    first_due_date = due_date - relativedelta(months=term * periods_due) if periods_due else due_date
    amount_per_period = calculate_interest_for_period(account)

    return {
        "opened_date": opened,
        "last_interest_date": last_date,
        "next_interest_date": next_interest_date,
        "first_due_date": first_due_date,
        "periods_due": periods_due,
        "is_due": periods_due > 0 and amount_per_period > 0,
        "amount_per_period": amount_per_period,
        "total_due_amount": amount_per_period * periods_due,
        "term_months": term,
        "schedule_label": account.product.interest_credit_label,
        "example_anchor": opened.strftime("%b %d"),
        "example_next": (opened + relativedelta(months=term)).strftime("%b %d"),
    }


@transaction.atomic
def record_interest(account, amount, credited_on=None, created_by=None, notes="", reference=""):
    if not account.is_operational:
        raise SavingsError("This savings account is not active.")

    amount = Decimal(str(amount))
    if amount <= 0:
        raise SavingsError("Interest amount must be greater than zero.")

    account.balance += amount
    account.save(update_fields=["balance"])

    return SavingsTransaction.objects.create(
        account=account,
        transaction_type=SavingsTransaction.Type.INTEREST,
        amount=amount,
        method=SavingsTransaction.Method.ONLINE,
        reference_number=reference or f"INT-{timezone.localtime().strftime('%Y%m%d')}",
        balance_after=account.balance,
        notes=notes,
        created_at=_datetime_on_date(credited_on) if credited_on else timezone.now(),
        created_by=created_by,
    )


@transaction.atomic
def apply_due_interest(account, created_by=None, as_of=None, state=None):
    if not account.is_operational:
        raise SavingsError("This savings account is not active.")
    if account.product.interest_rate <= 0:
        raise SavingsError("This product does not pay interest.")

    state = state or interest_schedule_state(account, as_of)
    if state["periods_due"] == 0:
        raise SavingsError(
            f"No interest is due yet. Next credit date is {state['next_interest_date'].strftime('%b %d, %Y')}."
        )

    applied = []
    term = state["term_months"]
    for index in range(state["periods_due"]):
        amount = calculate_interest_for_period(account)
        if amount <= 0:
            raise SavingsError(
                f"Balance must be at least ₱{account.product.min_balance:,.2f} to earn interest."
            )
        period_date = state["first_due_date"] + relativedelta(months=term * index)
        note = f"Interest credit for period ending {period_date.strftime('%b %d, %Y')}."
        applied.append(
            record_interest(
                account,
                amount,
                credited_on=period_date,
                created_by=created_by,
                notes=note,
                reference=f"INT-{period_date.strftime('%Y%m%d')}-{index + 1}",
            )
        )

    return applied
