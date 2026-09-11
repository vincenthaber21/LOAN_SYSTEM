from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


def backfill_activity_logs(apps, schema_editor):
    from lending.audit import backfill_activity_logs as run_backfill

    run_backfill()


class Migration(migrations.Migration):

    dependencies = [
        ("lending", "0031_loanapplication_created_by"),
    ]

    operations = [
        migrations.CreateModel(
            name="ActivityLog",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("action", models.CharField(choices=[
                    ("member_created", "Member created"),
                    ("member_updated", "Member updated"),
                    ("application_created", "Application created"),
                    ("application_updated", "Application updated"),
                    ("application_approved", "Application approved"),
                    ("application_rejected", "Application rejected"),
                    ("application_info_requested", "More information requested"),
                    ("application_deleted", "Application deleted"),
                    ("review_note", "Review note added"),
                    ("payment_recorded", "Pay collection"),
                    ("loan_disbursed", "Loan disbursed"),
                    ("balance_extended", "Balance extended"),
                    ("officer_created", "Officer created"),
                    ("officer_updated", "Officer updated"),
                    ("manager_created", "Manager created"),
                    ("manager_updated", "Manager updated"),
                    ("product_created", "Product created"),
                    ("product_updated", "Product updated"),
                    ("savings_opened", "Savings account opened"),
                    ("savings_deposit", "Savings deposit"),
                    ("savings_withdrawal", "Savings withdrawal"),
                    ("savings_closed", "Savings account closed"),
                    ("mutual_aid_enrolled", "Mutual aid enrolled"),
                    ("mutual_aid_contribution", "Mutual aid contribution"),
                    ("mutual_aid_claim", "Mutual aid claim submitted"),
                    ("mutual_aid_claim_reviewed", "Mutual aid claim reviewed"),
                    ("mutual_aid_claim_disbursed", "Mutual aid claim disbursed"),
                    ("membership_status", "Membership status changed"),
                    ("profile_updated", "Profile updated"),
                    ("data_exported", "Data exported"),
                    ("signed_in", "Signed in"),
                    ("signed_out", "Signed out"),
                    ("sign_in_failed", "Failed sign-in"),
                ], max_length=40)),
                ("kind", models.CharField(choices=[
                    ("member", "Member"),
                    ("application", "Application"),
                    ("payment", "Pay collection"),
                    ("disbursement", "Disbursement"),
                    ("savings", "Savings"),
                    ("mutual_aid", "Mutual aid"),
                    ("account", "Account"),
                    ("security", "Security"),
                ], db_index=True, max_length=20)),
                ("title", models.CharField(max_length=200)),
                ("description", models.TextField(blank=True)),
                ("member_name", models.CharField(blank=True, max_length=160)),
                ("reference", models.CharField(blank=True, max_length=80)),
                ("amount", models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True)),
                ("status", models.CharField(blank=True, max_length=30)),
                ("status_label", models.CharField(blank=True, max_length=80)),
                ("url_name", models.CharField(blank=True, max_length=80)),
                ("url_kwargs", models.JSONField(blank=True, default=dict)),
                ("ip_address", models.GenericIPAddressField(blank=True, null=True)),
                ("user_agent", models.CharField(blank=True, max_length=255)),
                ("source_key", models.CharField(blank=True, max_length=80, null=True, unique=True)),
                ("created_at", models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                ("actor", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="activity_logs", to="lending.user")),
                ("member", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="subject_activity_logs", to="lending.user")),
            ],
            options={
                "ordering": ["-created_at", "-id"],
            },
        ),
        migrations.AddIndex(
            model_name="activitylog",
            index=models.Index(fields=["actor", "created_at"], name="lending_act_actor_i_idx"),
        ),
        migrations.AddIndex(
            model_name="activitylog",
            index=models.Index(fields=["actor", "kind", "created_at"], name="lending_act_actor_k_idx"),
        ),
        migrations.RunPython(backfill_activity_logs, migrations.RunPython.noop),
    ]
