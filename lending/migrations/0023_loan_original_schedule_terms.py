from decimal import Decimal

from django.db import migrations, models


def backfill_original_schedule_terms(apps, schema_editor):
    Loan = apps.get_model("lending", "Loan")
    for loan in Loan.objects.filter(schedule_start_date__isnull=False).select_related(
        "application", "application__loan_product"
    ):
        application = loan.application
        rate = loan.original_interest_rate
        term = loan.original_term_months
        if rate is None and application is not None:
            rate = application.final_interest_rate
            if rate is None and application.loan_product_id:
                rate = application.loan_product.interest_rate
        if term is None and application is not None:
            term = application.final_term_months or application.term_months
        updates = {}
        if loan.original_interest_rate is None and rate is not None:
            updates["original_interest_rate"] = rate
        if loan.original_term_months is None and term is not None:
            updates["original_term_months"] = term
        if updates:
            Loan.objects.filter(pk=loan.pk).update(**updates)


class Migration(migrations.Migration):

    dependencies = [
        ("lending", "0022_notification"),
    ]

    operations = [
        migrations.AddField(
            model_name="loan",
            name="original_interest_rate",
            field=models.DecimalField(
                blank=True,
                decimal_places=2,
                help_text="Interest rate before the first balance-extension reschedule.",
                max_digits=5,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="loan",
            name="original_term_months",
            field=models.PositiveIntegerField(
                blank=True,
                help_text="Term months before the first balance-extension reschedule.",
                null=True,
            ),
        ),
        migrations.RunPython(backfill_original_schedule_terms, migrations.RunPython.noop),
    ]
