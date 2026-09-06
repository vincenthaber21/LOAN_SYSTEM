from django.db import migrations, models


def copy_principal_to_disbursed_principal(apps, schema_editor):
    Loan = apps.get_model("lending", "Loan")
    for loan in Loan.objects.all().iterator():
        Loan.objects.filter(pk=loan.pk).update(disbursed_principal=loan.principal)


class Migration(migrations.Migration):

    dependencies = [
        ("lending", "0019_manager_role"),
    ]

    operations = [
        migrations.AddField(
            model_name="loan",
            name="disbursed_principal",
            field=models.DecimalField(
                blank=True,
                decimal_places=2,
                help_text="Original principal released at disbursement (unchanged by balance extensions).",
                max_digits=12,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="loan",
            name="schedule_start_date",
            field=models.DateField(
                blank=True,
                help_text="When set, the repayment schedule starts from this date instead of disbursed_date.",
                null=True,
            ),
        ),
        migrations.RunPython(copy_principal_to_disbursed_principal, migrations.RunPython.noop),
    ]
