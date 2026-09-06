from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("lending", "0016_kap_signatures"),
    ]

    operations = [
        migrations.AlterField(
            model_name="loan",
            name="other_fees_description",
            field=models.CharField(blank=True, max_length=255),
        ),
    ]
