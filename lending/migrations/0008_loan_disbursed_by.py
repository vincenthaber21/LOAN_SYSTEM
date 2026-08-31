from django.db import migrations, models
import django.db.models.deletion
from django.conf import settings


class Migration(migrations.Migration):

    dependencies = [
        ("lending", "0007_user_last_seen"),
    ]

    operations = [
        migrations.AddField(
            model_name="loan",
            name="disbursed_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="disbursed_loans",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
    ]
