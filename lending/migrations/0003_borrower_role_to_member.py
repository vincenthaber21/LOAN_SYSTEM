from django.db import migrations


def borrower_to_member(apps, schema_editor):
    User = apps.get_model("lending", "User")
    User.objects.filter(role="borrower").update(role="member")


def member_to_borrower(apps, schema_editor):
    User = apps.get_model("lending", "User")
    User.objects.filter(role="member").update(role="borrower")


class Migration(migrations.Migration):

    dependencies = [
        ("lending", "0002_administrator_loanofficer_member_alter_user_role"),
    ]

    operations = [
        migrations.RunPython(borrower_to_member, member_to_borrower),
    ]
