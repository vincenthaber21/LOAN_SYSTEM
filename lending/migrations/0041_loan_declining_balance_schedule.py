from decimal import Decimal

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("lending", "0040_activitylog_account_locked"),
    ]

    operations = [
        migrations.AddField(
            model_name="loan",
            name="addon_rate",
            field=models.DecimalField(
                blank=True,
                decimal_places=6,
                help_text="Total add-on rate for the term (for example 0.330000 for 33%).",
                max_digits=12,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="loan",
            name="number_of_payments",
            field=models.PositiveIntegerField(
                blank=True,
                help_text="Number of payments in the stored declining-balance schedule.",
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="loan",
            name="payment_interval_days",
            field=models.PositiveIntegerField(
                blank=True,
                help_text="Calendar days between scheduled payments.",
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="loan",
            name="period_rate",
            field=models.DecimalField(
                blank=True,
                decimal_places=12,
                help_text="Declining-balance interest rate per payment period, solved from the add-on rate.",
                max_digits=18,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="installment",
            name="ending_balance",
            field=models.DecimalField(
                decimal_places=2,
                default=Decimal("0.00"),
                help_text="Principal balance remaining after this payment.",
                max_digits=12,
            ),
        ),
    ]
