from decimal import Decimal, ROUND_HALF_UP

from django.db import migrations


CREDIT_SCORE_PRECISION = Decimal("0.1")


def normalize_credit_scores(apps, schema_editor):
    User = apps.get_model("lending", "User")
    for user in User.objects.exclude(credit_score__isnull=True):
        normalized = Decimal(str(user.credit_score)).quantize(CREDIT_SCORE_PRECISION, rounding=ROUND_HALF_UP)
        normalized = max(Decimal("0"), normalized)
        if user.credit_score != normalized:
            user.credit_score = normalized
            user.save(update_fields=["credit_score"])


class Migration(migrations.Migration):

    dependencies = [
        ("lending", "0012_features"),
    ]

    operations = [
        migrations.RunPython(normalize_credit_scores, migrations.RunPython.noop),
    ]
