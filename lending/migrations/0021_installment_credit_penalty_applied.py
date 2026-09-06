from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("lending", "0020_loan_balance_extension_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="installment",
            name="credit_penalty_applied",
            field=models.BooleanField(
                default=False,
                help_text="True when the −0.1 credit-score penalty for this loan month has already been applied.",
            ),
        ),
    ]
