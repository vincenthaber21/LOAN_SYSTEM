from decimal import Decimal, InvalidOperation

from django import template

register = template.Library()


@register.filter
def credit_score(value):
    """Format a credit score to one decimal place, consistently rounded."""
    from lending.services import format_credit_score

    return format_credit_score(value)


@register.filter
def peso(value):
    """Format a number as ₱1,234,567.89"""
    try:
        amount = Decimal(str(value))
        return f"₱{amount:,.2f}"
    except (InvalidOperation, TypeError, ValueError):
        return f"₱{value}"


@register.filter
def iso_date(value):
    """Format a date for <input type="date">, which only accepts YYYY-MM-DD."""
    if not value:
        return ""
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%d")
    text = str(value).strip()
    if len(text) >= 10 and text[4:5] == "-" and text[7:8] == "-":
        return text[:10]
    return text


@register.filter
def peso_whole(value):
    """Format a number as ₱1,234,567 (no decimals)"""
    try:
        amount = Decimal(str(value))
        return f"₱{amount:,.0f}"
    except (InvalidOperation, TypeError, ValueError):
        return f"₱{value}"


@register.filter
def term_weeks(value):
    """months → '16 weeks' (display only)."""
    from lending.services import term_weeks_summary

    return term_weeks_summary(value)["label"]


@register.filter
def term_weeks_total(value):
    """months → total calendar weeks (months × 4)."""
    from lending.services import term_weeks_total as weeks_total

    return weeks_total(value)
