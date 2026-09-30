"""Add-on rate converted to a declining-balance amortization schedule.

Money uses Decimal with ROUND_HALF_UP to the cent. The period rate is solved
(Newton-Raphson, then bisection) and is never hardcoded.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP, localcontext

CENT = Decimal("0.01")
RATE_PLACES = Decimal("0.000000000001")  # 12 decimal places
PERIOD_PERCENT_PLACES = Decimal("0.0001")
PERCENT_PLACES = Decimal("0.01")
SOLVER_TOLERANCE = Decimal("1e-10")
SOLVER_MAX_ITERATIONS = 200
SOLVER_LOW = Decimal("0.000001")
SOLVER_HIGH = Decimal("1")

MATCH_TOTAL_REPAYMENT = "match_total_repayment"
STANDARD_ROUNDING = "standard_rounding"
FINAL_PAYMENT_POLICIES = (MATCH_TOTAL_REPAYMENT, STANDARD_ROUNDING)

FREQUENCY_INTERVAL_DAYS = {
    "daily": 1,
    "weekly": 7,
    "biweekly": 14,
    "monthly": 30,
}
# Daily collection is Monday–Friday. One month is 22 working days.
WORKING_DAYS_PER_MONTH = 22


class AmortizationError(ValueError):
    """Raised when loan inputs cannot produce a schedule."""


@dataclass(frozen=True)
class ScheduleRow:
    number: int
    due_date: date | None
    payment: Decimal
    interest: Decimal
    principal: Decimal
    balance: Decimal


@dataclass(frozen=True)
class AmortizationResult:
    principal: Decimal
    add_on_rate: Decimal
    number_of_payments: int
    payment_interval_days: int
    first_due_date: date
    payment: Decimal
    total_repayment: Decimal
    total_interest: Decimal
    period_rate: Decimal
    nominal_annual_rate: Decimal
    effective_annual_rate: Decimal
    final_payment_policy: str
    rows: tuple[ScheduleRow, ...]

    @property
    def payments(self) -> tuple[ScheduleRow, ...]:
        """Installment rows only (excludes the opening balance row)."""
        return tuple(row for row in self.rows if row.number > 0)

    @property
    def add_on_percent(self) -> Decimal:
        return _percent(self.add_on_rate, PERCENT_PLACES)

    @property
    def period_rate_percent(self) -> Decimal:
        return _percent(self.period_rate, PERIOD_PERCENT_PLACES)

    @property
    def nominal_annual_percent(self) -> Decimal:
        return _percent(self.nominal_annual_rate, PERCENT_PLACES)

    @property
    def effective_annual_percent(self) -> Decimal:
        return _percent(self.effective_annual_rate, PERCENT_PLACES)


def money(value) -> Decimal:
    """Round a monetary amount to cents with ROUND_HALF_UP."""
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def _percent(rate, places) -> Decimal:
    return (Decimal(rate) * Decimal("100")).quantize(places, rounding=ROUND_HALF_UP)


def _as_decimal(value, label) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        return Decimal(str(value))
    try:
        return Decimal(str(value))
    except Exception as exc:
        raise AmortizationError(f"{label} must be a number.") from exc


def validate_inputs(principal, add_on_rate, number_of_payments, payment_interval_days, first_due_date):
    """Return normalized inputs, or raise AmortizationError."""
    principal = money(_as_decimal(principal, "principal"))
    add_on_rate = _as_decimal(add_on_rate, "add_on_rate")
    if isinstance(number_of_payments, bool) or not isinstance(number_of_payments, int):
        try:
            if isinstance(number_of_payments, float) and not number_of_payments.is_integer():
                raise AmortizationError("number_of_payments must be an integer.")
            number_of_payments = int(number_of_payments)
        except (TypeError, ValueError) as exc:
            raise AmortizationError("number_of_payments must be an integer.") from exc
    if isinstance(payment_interval_days, bool):
        raise AmortizationError("payment_interval_days must be an integer.")
    try:
        if isinstance(payment_interval_days, float) and not payment_interval_days.is_integer():
            raise AmortizationError("payment_interval_days must be an integer.")
        payment_interval_days = int(payment_interval_days)
    except (TypeError, ValueError) as exc:
        raise AmortizationError("payment_interval_days must be an integer.") from exc
    if not isinstance(first_due_date, date):
        raise AmortizationError("first_due_date must be a date.")

    if principal <= 0:
        raise AmortizationError("principal must be greater than 0.")
    if number_of_payments < 1:
        raise AmortizationError("number_of_payments must be at least 1.")
    if add_on_rate < 0:
        raise AmortizationError("add_on_rate must be 0 or greater.")
    if payment_interval_days < 1:
        raise AmortizationError("payment_interval_days must be at least 1.")
    return principal, add_on_rate, number_of_payments, payment_interval_days, first_due_date


def interval_days_for_frequency(frequency) -> int:
    key = str(frequency or "monthly").lower()
    try:
        return FREQUENCY_INTERVAL_DAYS[key]
    except KeyError as exc:
        raise AmortizationError(f"Unknown payment frequency: {frequency}.") from exc


def payment_count_for_term(term_months, interval_days) -> int:
    """Payments in the term.

    Daily (1-day) plans use working days only: 22 weekdays per month.
    Other intervals use a 365-day year (12 months / 14 days → 26).
    """
    try:
        term_months = int(term_months)
        interval_days = int(interval_days)
    except (TypeError, ValueError) as exc:
        raise AmortizationError("term and interval must be integers.") from exc
    if term_months < 1:
        raise AmortizationError("term_months must be at least 1.")
    if interval_days < 1:
        raise AmortizationError("payment_interval_days must be at least 1.")
    if interval_days == 1:
        return max(1, term_months * WORKING_DAYS_PER_MONTH)
    span = Decimal(term_months) * Decimal(365) / Decimal(12)
    count = (span / Decimal(interval_days)).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return max(1, int(count))


def align_to_weekday(value: date) -> date:
    """Move Saturday or Sunday forward to Monday. Weekdays stay put."""
    while value.weekday() >= 5:
        value += timedelta(days=1)
    return value


def working_day_due_dates(first_due: date, periods: int) -> list[date]:
    """One due date per working day (Monday–Friday), starting on or after `first_due`."""
    current = align_to_weekday(first_due)
    dates: list[date] = []
    for _ in range(periods):
        dates.append(current)
        current += timedelta(days=1)
        while current.weekday() >= 5:
            current += timedelta(days=1)
    return dates


def schedule_due_dates(first_due: date, periods: int, interval_days: int) -> list[date]:
    """Due dates that never fall on Saturday or Sunday.

    Daily plans are one working day apart. Longer intervals step by that many
    calendar days, then move a Saturday or Sunday forward to Monday.
    """
    if interval_days == 1:
        return working_day_due_dates(first_due, periods)
    current = align_to_weekday(first_due)
    dates: list[date] = []
    for _ in range(periods):
        dates.append(current)
        nxt = align_to_weekday(current + timedelta(days=interval_days))
        if nxt <= current:
            nxt = align_to_weekday(current + timedelta(days=1))
        current = nxt
    return dates


def addon_rate_for_term(monthly_percent, term_months) -> Decimal:
    """Total add-on rate for the term from a monthly flat percentage.

    A 2.75% monthly rate for 12 months is an add-on rate of 0.33 (33%).
    """
    monthly = _as_decimal(monthly_percent, "interest rate") / Decimal("100")
    if monthly < 0:
        raise AmortizationError("add_on_rate must be 0 or greater.")
    try:
        term_months = int(term_months)
    except (TypeError, ValueError) as exc:
        raise AmortizationError("term_months must be an integer.") from exc
    if term_months < 1:
        raise AmortizationError("term_months must be at least 1.")
    return monthly * Decimal(term_months)


def rate_summary(period_rate, interval_days, add_on_rate) -> dict:
    """Nominal and effective annual rates from a stored period rate.

    Does not re-solve i. nominal = i × (365 / interval); EAR = (1 + i)^(365 / interval) − 1.
    """
    period_rate = _as_decimal(period_rate, "period_rate")
    add_on_rate = _as_decimal(add_on_rate or 0, "add_on_rate")
    interval_days = int(interval_days)
    if interval_days < 1:
        raise AmortizationError("payment_interval_days must be at least 1.")
    nominal, effective = _rates_from_period_rate(period_rate, interval_days)
    return {
        "period_rate": period_rate,
        "add_on_rate": add_on_rate,
        "nominal_annual_rate": nominal,
        "effective_annual_rate": effective,
        "period_rate_percent": _percent(period_rate, PERIOD_PERCENT_PLACES),
        "add_on_percent": _percent(add_on_rate, PERCENT_PLACES),
        "nominal_annual_percent": _percent(nominal, PERCENT_PLACES),
        "effective_annual_percent": _percent(effective, PERCENT_PLACES),
    }


def configured_final_payment_policy() -> str:
    from django.conf import settings

    value = getattr(settings, "LOAN_FINAL_PAYMENT_POLICY", MATCH_TOTAL_REPAYMENT)
    if value not in FINAL_PAYMENT_POLICIES:
        return MATCH_TOTAL_REPAYMENT
    return value


def _annuity_payment(principal, rate, periods) -> Decimal:
    """Level payment that amortizes `principal` over `periods` at `rate` per period."""
    if rate == 0:
        return principal / Decimal(periods)
    with localcontext() as ctx:
        ctx.prec = 50
        growth = (Decimal(1) + rate) ** periods
        return principal * rate * growth / (growth - Decimal(1))


def solve_period_rate(principal, payment, periods, *, tolerance=SOLVER_TOLERANCE, max_iterations=SOLVER_MAX_ITERATIONS) -> Decimal:
    """Solve i in A = P * i / (1 - (1 + i) ** -n).

    Newton-Raphson first, then bisection on (0.000001, 1). Returns 0 when the
    payment carries no interest.
    """
    principal = Decimal(principal)
    payment = Decimal(payment)
    periods = int(periods)
    if periods < 1:
        raise AmortizationError("number_of_payments must be at least 1.")
    if principal <= 0:
        raise AmortizationError("principal must be greater than 0.")
    if payment <= 0:
        raise AmortizationError("payment must be greater than 0.")
    # No interest: each payment is principal only (within a cent of P / n).
    if payment * periods <= principal + CENT:
        return Decimal("0")

    def residual(rate: Decimal) -> Decimal:
        return _annuity_payment(principal, rate, periods) - payment

    def derivative(rate: Decimal) -> Decimal:
        """d/di of P * i * (1+i)^n / ((1+i)^n - 1)."""
        with localcontext() as ctx:
            ctx.prec = 50
            growth = (Decimal(1) + rate) ** periods
            # num = i * growth, den = growth - 1
            d_growth = periods * growth / (Decimal(1) + rate)
            d_num = growth + rate * d_growth
            d_den = d_growth
            den = growth - Decimal(1)
            return principal * (d_num * den - rate * growth * d_den) / (den * den)

    guess_interest = payment * periods - principal
    guess = (Decimal(2) * guess_interest) / (principal * Decimal(periods + 1))
    if guess <= SOLVER_LOW:
        guess = Decimal("0.01")
    if guess >= SOLVER_HIGH:
        guess = Decimal("0.05")

    rate = guess
    for _ in range(max_iterations):
        diff = residual(rate)
        if abs(diff) <= tolerance:
            return rate
        slope = derivative(rate)
        if slope == 0:
            break
        nxt = rate - diff / slope
        if nxt <= 0 or nxt >= SOLVER_HIGH:
            break
        if abs(nxt - rate) <= tolerance:
            return nxt
        rate = nxt

    low = SOLVER_LOW
    high = SOLVER_HIGH
    for _ in range(max_iterations):
        mid = (low + high) / 2
        diff = residual(mid)
        if abs(diff) <= tolerance or (high - low) <= tolerance:
            return mid
        if diff > 0:
            high = mid
        else:
            low = mid
    return (low + high) / 2


def _rates_from_period_rate(period_rate, interval_days):
    with localcontext() as ctx:
        ctx.prec = 50
        nominal = period_rate * Decimal(365) / Decimal(interval_days)
        effective = (Decimal(1) + period_rate) ** (Decimal(365) / Decimal(interval_days)) - Decimal(1)
    return nominal, effective


def calculate_amortization(
    principal,
    add_on_rate,
    number_of_payments,
    payment_interval_days,
    first_due_date,
    *,
    period_rate=None,
    final_payment_policy=None,
) -> AmortizationResult:
    """Build the declining-balance schedule for an add-on rate loan.

    `period_rate`, when supplied, is the stored rate and is not solved again.
    `final_payment_policy`:
      - match_total_repayment: last interest is set so payments sum to total repayment
      - standard_rounding: last interest stays round(balance × i, 2)
    """
    principal, add_on_rate, periods, interval, first_due = validate_inputs(
        principal, add_on_rate, number_of_payments, payment_interval_days, first_due_date
    )
    policy = final_payment_policy or MATCH_TOTAL_REPAYMENT
    if policy not in FINAL_PAYMENT_POLICIES:
        raise AmortizationError(f"Unknown final payment policy: {policy}.")

    total_repayment = money(principal * (Decimal(1) + add_on_rate))
    regular_payment = money(total_repayment / Decimal(periods))
    total_interest = money(total_repayment - principal)

    if add_on_rate == 0 or total_interest == 0:
        solved = Decimal("0")
    elif period_rate is None:
        solved = solve_period_rate(principal, regular_payment, periods)
        solved = solved.quantize(RATE_PLACES, rounding=ROUND_HALF_UP)
    else:
        solved = _as_decimal(period_rate, "period_rate")
        if solved < 0:
            raise AmortizationError("period_rate must be 0 or greater.")
        solved = solved.quantize(RATE_PLACES, rounding=ROUND_HALF_UP)

    nominal, effective = _rates_from_period_rate(solved, interval)

    rows = [
        ScheduleRow(
            number=0,
            due_date=None,
            payment=Decimal("0.00"),
            interest=Decimal("0.00"),
            principal=Decimal("0.00"),
            balance=principal,
        )
    ]
    balance = principal
    payments_so_far = Decimal("0.00")
    due_dates = schedule_due_dates(first_due, periods, interval)
    for number in range(1, periods + 1):
        due = due_dates[number - 1]
        interest = money(balance * solved)
        if number == periods:
            principal_part = balance
            if policy == MATCH_TOTAL_REPAYMENT:
                interest = money(total_repayment - payments_so_far - principal_part)
            payment = money(principal_part + interest)
            balance = Decimal("0.00")
        else:
            payment = regular_payment
            principal_part = money(payment - interest)
            balance = money(balance - principal_part)
        payments_so_far = money(payments_so_far + payment)
        rows.append(
            ScheduleRow(
                number=number,
                due_date=due,
                payment=payment,
                interest=interest,
                principal=principal_part,
                balance=balance,
            )
        )

    return AmortizationResult(
        principal=principal,
        add_on_rate=add_on_rate,
        number_of_payments=periods,
        payment_interval_days=interval,
        first_due_date=first_due,
        payment=regular_payment,
        total_repayment=total_repayment,
        total_interest=total_interest,
        period_rate=solved,
        nominal_annual_rate=nominal,
        effective_annual_rate=effective,
        final_payment_policy=policy,
        rows=tuple(rows),
    )
