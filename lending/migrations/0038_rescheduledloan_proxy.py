from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("lending", "0037_kap_application_form_update"),
    ]

    operations = [
        migrations.CreateModel(
            name="RescheduledLoan",
            fields=[],
            options={
                "verbose_name": "Rescheduled loan",
                "verbose_name_plural": "Rescheduled loans",
                "proxy": True,
                "indexes": [],
                "constraints": [],
            },
            bases=("lending.loan",),
        ),
    ]
