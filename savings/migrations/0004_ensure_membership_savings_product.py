from decimal import Decimal

from django.db import migrations


MEMBERSHIP_SAVINGS_PRODUCT_NAME = "Membership/Savings Deposit"
_LEGACY_MEMBERSHIP_SAVINGS_NAMES = ("LOAN SAVINGS", "Loan Savings")


def ensure_membership_savings_product(apps, schema_editor):
    SavingsProduct = apps.get_model("savings", "SavingsProduct")

    product = SavingsProduct.objects.filter(
        name__iexact=MEMBERSHIP_SAVINGS_PRODUCT_NAME,
        is_active=True,
    ).first()
    if product:
        return

    for legacy_name in _LEGACY_MEMBERSHIP_SAVINGS_NAMES:
        product = SavingsProduct.objects.filter(name__iexact=legacy_name).first()
        if product:
            product.name = MEMBERSHIP_SAVINGS_PRODUCT_NAME
            product.is_active = True
            product.save(update_fields=["name", "is_active"])
            return

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


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("savings", "0003_savings_transaction_index"),
    ]

    operations = [
        migrations.RunPython(ensure_membership_savings_product, noop_reverse),
    ]
