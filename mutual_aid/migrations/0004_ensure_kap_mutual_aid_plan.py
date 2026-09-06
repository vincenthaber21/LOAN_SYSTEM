from decimal import Decimal

from django.db import migrations


KAP_MUTUAL_AID_PLAN_NAME = "KAPAMILYA MUTUAL AID PROGRAM"
_LEGACY_KAP_MUTUAL_AID_NAMES = (
    "Initial contribution for KAPAMILYA MUTUAL AID PROGRAM",
    "KAP Mutual Aid",
    "KAPAMILYA MUTUAL AID",
)


def ensure_kap_mutual_aid_plan(apps, schema_editor):
    MutualAidPlan = apps.get_model("mutual_aid", "MutualAidPlan")

    plan = MutualAidPlan.objects.filter(
        name__iexact=KAP_MUTUAL_AID_PLAN_NAME,
        is_active=True,
    ).first()
    if plan:
        return

    for legacy_name in _LEGACY_KAP_MUTUAL_AID_NAMES:
        plan = MutualAidPlan.objects.filter(name__iexact=legacy_name).first()
        if plan:
            plan.name = KAP_MUTUAL_AID_PLAN_NAME
            plan.is_active = True
            plan.save(update_fields=["name", "is_active"])
            return

    plan = MutualAidPlan.objects.filter(name__icontains="KAPAMILYA").first()
    if plan:
        if not plan.is_active:
            plan.is_active = True
            plan.save(update_fields=["is_active"])
        return

    plan, _ = MutualAidPlan.objects.get_or_create(
        name=KAP_MUTUAL_AID_PLAN_NAME,
        defaults={
            "description": "Compulsory initial contribution credited from loan disbursement.",
            "contribution_amount": Decimal("200.00"),
            "contribution_frequency": "monthly",
            "max_benefit_amount": Decimal("10000.00"),
            "waiting_period_days": 90,
            "min_membership_months": 0,
            "is_active": True,
        },
    )
    if not plan.is_active:
        plan.is_active = True
        plan.save(update_fields=["is_active"])


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("mutual_aid", "0003_remove_mutualaidcontribution_period_label_and_more"),
    ]

    operations = [
        migrations.RunPython(ensure_kap_mutual_aid_plan, noop_reverse),
    ]
