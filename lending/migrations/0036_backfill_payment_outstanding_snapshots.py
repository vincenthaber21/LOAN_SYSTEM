from django.db import migrations


def backfill_payment_outstanding_snapshots(apps, schema_editor):
    # Use concrete models so loan_amount_applied and receipt helpers are available.
    from lending.models import Payment
    from lending.services import payment_receipt_balances

    for payment in Payment.objects.select_related("loan").order_by("loan_id", "pk"):
        if payment.outstanding_before is not None and payment.outstanding_after is not None:
            continue
        before, after = payment_receipt_balances(payment)
        Payment.objects.filter(pk=payment.pk).update(
            outstanding_before=before,
            outstanding_after=after,
        )


def noop_reverse(apps, schema_editor):
    from lending.models import Payment

    Payment.objects.all().update(outstanding_before=None, outstanding_after=None)


class Migration(migrations.Migration):

    dependencies = [
        ("lending", "0035_payment_outstanding_snapshots"),
    ]

    operations = [
        migrations.RunPython(backfill_payment_outstanding_snapshots, noop_reverse),
    ]
