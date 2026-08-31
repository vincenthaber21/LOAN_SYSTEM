from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("lending", "0006_loanapplication_applied_on"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="last_seen",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
