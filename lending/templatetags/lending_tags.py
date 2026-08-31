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
def peso_whole(value):
    """Format a number as ₱1,234,567 (no decimals)"""
    try:
        amount = Decimal(str(value))
        return f"₱{amount:,.0f}"
    except (InvalidOperation, TypeError, ValueError):
        return f"₱{value}"
