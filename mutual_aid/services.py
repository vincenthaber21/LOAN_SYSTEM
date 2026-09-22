from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from .models import MutualAidClaim, MutualAidContribution, MutualAidMembership, MutualAidPeriod, MutualAidPlan

KAP_MUTUAL_AID_PLAN_NAME = "KAPAMILYA MUTUAL AID PROGRAM"
# Matches the disbursement deduction label used as the plan name on some installs.
_LEGACY_KAP_MUTUAL_AID_NAMES = (
    "Initial contribution for KAPAMILYA MUTUAL AID PROGRAM",
    "KAP Mutual Aid",
    "KAPAMILYA MUTUAL AID",
)


class MutualAidError(Exception):
    pass


def _datetime_on_date(value):
    current = timezone.localtime()
    return current.replace(year=value.year, month=value.month, day=value.day)


def resolve_kap_mutual_aid_plan():
    """Return the KAP mutual aid plan used for compulsory disbursement contributions."""
    plan = MutualAidPlan.objects.filter(
        name__iexact=KAP_MUTUAL_AID_PLAN_NAME,
        is_active=True,
    ).first()
    if plan:
        return plan

    for legacy_name in _LEGACY_KAP_MUTUAL_AID_NAMES:
        plan = MutualAidPlan.objects.filter(name__iexact=legacy_name).first()
        if plan:
            if plan.name != KAP_MUTUAL_AID_PLAN_NAME:
                plan.name = KAP_MUTUAL_AID_PLAN_NAME
                plan.is_active = True
                plan.save(update_fields=["name", "is_active"])
            elif not plan.is_active:
                plan.is_active = True
                plan.save(update_fields=["is_active"])
            return plan

    plan = MutualAidPlan.objects.filter(name__icontains="KAPAMILYA").first()
    if plan:
        if not plan.is_active:
            plan.is_active = True
            plan.save(update_fields=["is_active"])
        return plan

    plan, _ = MutualAidPlan.objects.get_or_create(
        name=KAP_MUTUAL_AID_PLAN_NAME,
        defaults={
            "description": "Compulsory initial contribution credited from loan disbursement.",
            "contribution_amount": Decimal("200.00"),
            "contribution_frequency": MutualAidPlan.ContributionFrequency.MONTHLY,
            "max_benefit_amount": Decimal("10000.00"),
            "waiting_period_days": 90,
            "min_membership_months": 0,
            "is_active": True,
        },
    )
    if not plan.is_active:
        plan.is_active = True
        plan.save(update_fields=["is_active"])
    return plan


def get_or_enroll_member(member, plan, enrolled_by=None, notes="", enrolled_on=None):
    """Return the member's active membership for the plan, enrolling if needed."""
    membership = (
        MutualAidMembership.objects.select_for_update()
        .filter(
            member=member,
            plan=plan,
            status=MutualAidMembership.Status.ACTIVE,
        )
        .first()
    )
    if membership:
        return membership
    return enroll_member(
        member,
        plan,
        enrolled_by=enrolled_by,
        notes=notes,
        enrolled_on=enrolled_on,
    )


@transaction.atomic
def credit_kap_mutual_aid_from_disbursement(
    member,
    amount,
    *,
    loan_reference="",
    recorded_by=None,
    disbursed_date=None,
):
    """Enroll (if needed) and record the KAP mutual aid contribution from a loan release."""
    amount = Decimal(str(amount))
    if amount <= 0:
        return None

    credited_on = disbursed_date or timezone.localdate()
    plan = resolve_kap_mutual_aid_plan()
    membership = get_or_enroll_member(
        member,
        plan,
        enrolled_by=recorded_by,
        notes="Enrolled automatically from loan disbursement.",
        enrolled_on=credited_on,
    )
    reference = f"DISB-{loan_reference}" if loan_reference else "DISB-KAP-MUTUAL-AID"
    notes = "Initial contribution for KAPAMILYA MUTUAL AID PROGRAM deducted from loan disbursement."
    return record_contribution(
        membership,
        amount,
        MutualAidContribution.Method.ONLINE,
        reference=reference[:60],
        recorded_by=recorded_by,
        notes=notes,
        occurred_on=credited_on,
    )


@transaction.atomic
def credit_kap_mutual_aid_from_loan_payment(
    member,
    amount,
    *,
    loan_reference="",
    payment_reference="",
    recorded_by=None,
    pay_frequency="daily",
    occurred_on=None,
):
    """Credit daily mutual aid collected with a loan remittance."""
    amount = Decimal(str(amount))
    if amount <= 0:
        return None

    credited_on = occurred_on or timezone.localdate()
    plan = resolve_kap_mutual_aid_plan()
    membership = get_or_enroll_member(
        member,
        plan,
        enrolled_by=recorded_by,
        notes="Enrolled automatically from loan payment mutual aid.",
        enrolled_on=credited_on,
    )
    reference = (payment_reference or f"PAY-{loan_reference}-MA")[:60]
    freq = (pay_frequency or "daily").replace("_", " ")
    notes = (
        f"Daily mutual aid ({freq}) collected with loan payment "
        f"{payment_reference or loan_reference or ''}.".strip()
    )
    return record_contribution(
        membership,
        amount,
        MutualAidContribution.Method.CASH,
        reference=reference,
        recorded_by=recorded_by,
        notes=notes,
        occurred_on=credited_on,
    )


@transaction.atomic
def enroll_member(member, plan, enrolled_by=None, notes="", enrolled_on=None):
    if not plan.is_active:
        raise MutualAidError("This mutual aid plan is not available.")

    existing = MutualAidMembership.objects.filter(
        member=member,
        plan=plan,
        status=MutualAidMembership.Status.ACTIVE,
    ).exists()
    if existing:
        raise MutualAidError(f"{member.display_name()} already has an active {plan.name} membership.")

    return MutualAidMembership.objects.create(
        member=member,
        plan=plan,
        enrolled_by=enrolled_by,
        notes=notes,
        enrolled_at=_datetime_on_date(enrolled_on) if enrolled_on else timezone.now(),
    )


@transaction.atomic
def update_membership(membership, *, member, plan, notes="", enrolled_on=None):
    """Correct mistaken membership details (member, plan, notes, or enroll date)."""
    membership = (
        MutualAidMembership.objects.select_for_update()
        .select_related("member", "plan")
        .get(pk=membership.pk)
    )

    if membership.status == MutualAidMembership.Status.ACTIVE:
        conflict = (
            MutualAidMembership.objects.filter(
                member=member,
                plan=plan,
                status=MutualAidMembership.Status.ACTIVE,
            )
            .exclude(pk=membership.pk)
            .exists()
        )
        if conflict:
            raise MutualAidError(
                f"{member.display_name()} already has an active {plan.name} membership."
            )

    plan_changed = membership.plan_id != plan.pk
    if enrolled_on is not None:
        if enrolled_on > timezone.localdate():
            raise MutualAidError("Enrollment date cannot be in the future.")
        membership.enrolled_at = _datetime_on_date(enrolled_on)

    membership.member = member
    membership.plan = plan
    membership.notes = notes or ""
    membership.save(update_fields=["member", "plan", "notes", "enrolled_at"])

    if plan_changed:
        membership.contributions.filter(period__isnull=False).update(period=None)

    return membership


@transaction.atomic
def delete_membership(membership):
    """Remove a mistaken membership and all related contributions and claims."""
    membership = (
        MutualAidMembership.objects.select_for_update()
        .select_related("member", "plan")
        .get(pk=membership.pk)
    )
    reference = membership.membership_number
    member = membership.member
    plan_name = membership.plan_name
    total_contributed = membership.total_contributed
    membership.contributions.all().delete()
    membership.claims.all().delete()
    membership.delete()
    return {
        "reference": reference,
        "member": member,
        "plan_name": plan_name,
        "total_contributed": total_contributed,
    }


@transaction.atomic
def create_period(plan, date_from, date_to, label=""):
    if date_to < date_from:
        raise MutualAidError("End date must be on or after the start date.")
    overlap = MutualAidPeriod.objects.filter(
        plan=plan,
        date_from__lte=date_to,
        date_to__gte=date_from,
    ).exists()
    if overlap:
        raise MutualAidError("This period overlaps with an existing period for this plan.")
    return MutualAidPeriod.objects.create(
        plan=plan,
        date_from=date_from,
        date_to=date_to,
        label=label,
    )


@transaction.atomic
def record_contribution(
    membership,
    amount,
    method,
    reference="",
    period=None,
    recorded_by=None,
    notes="",
    occurred_on=None,
):
    if not membership.is_operational:
        raise MutualAidError("This membership is not active.")

    amount = Decimal(str(amount))
    if amount <= 0:
        raise MutualAidError("Contribution amount must be greater than zero.")
    if occurred_on and occurred_on > timezone.localdate():
        raise MutualAidError("Contribution date cannot be in the future.")

    if period and period.plan_id != membership.plan_id:
        raise MutualAidError("Selected period does not belong to this membership's plan.")
    if period and membership.contributions.filter(period=period).exists():
        raise MutualAidError(f"A contribution for {period.display_label} has already been recorded.")

    membership.total_contributed += amount
    membership.save(update_fields=["total_contributed"])

    return MutualAidContribution.objects.create(
        membership=membership,
        amount=amount,
        method=method,
        reference_number=reference,
        period=period,
        notes=notes,
        created_at=_datetime_on_date(occurred_on) if occurred_on else timezone.now(),
        recorded_by=recorded_by,
    )


@transaction.atomic
def delete_contribution(contribution):
    """Remove a mistaken contribution and adjust membership totals."""
    membership = (
        MutualAidMembership.objects.select_for_update()
        .select_related("plan")
        .get(pk=contribution.membership_id)
    )
    if not membership.is_operational:
        raise MutualAidError("Cannot delete contributions on an inactive membership.")

    amount = contribution.amount
    if membership.total_contributed - amount < 0:
        raise MutualAidError(
            "Cannot delete this contribution: total contributed would become negative."
        )

    contribution.delete()
    membership.total_contributed -= amount
    membership.save(update_fields=["total_contributed"])
    return membership


@transaction.atomic
def update_contribution(
    contribution,
    *,
    amount,
    method,
    reference="",
    notes="",
    period=None,
    occurred_on=None,
):
    """Correct a mistaken contribution and adjust membership totals."""
    membership = (
        MutualAidMembership.objects.select_for_update()
        .select_related("plan")
        .get(pk=contribution.membership_id)
    )
    if not membership.is_operational:
        raise MutualAidError("Cannot edit contributions on an inactive membership.")

    amount = Decimal(str(amount))
    if amount <= 0:
        raise MutualAidError("Contribution amount must be greater than zero.")
    if occurred_on and occurred_on > timezone.localdate():
        raise MutualAidError("Contribution date cannot be in the future.")

    if period and period.plan_id != membership.plan_id:
        raise MutualAidError("Selected period does not belong to this membership's plan.")
    if period and membership.contributions.filter(period=period).exclude(pk=contribution.pk).exists():
        raise MutualAidError(f"A contribution for {period.display_label} has already been recorded.")

    delta = amount - contribution.amount
    new_total = membership.total_contributed + delta
    if new_total < 0:
        raise MutualAidError(
            "Cannot update this contribution: total contributed would become negative."
        )

    contribution.amount = amount
    contribution.method = method
    contribution.reference_number = reference or ""
    contribution.notes = notes or ""
    contribution.period = period
    if occurred_on is not None:
        current_date = timezone.localtime(contribution.created_at).date()
        if occurred_on != current_date:
            contribution.created_at = _datetime_on_date(occurred_on)
    contribution.save(
        update_fields=[
            "amount",
            "method",
            "reference_number",
            "notes",
            "period",
            "created_at",
        ]
    )

    if delta != 0:
        membership.total_contributed = new_total
        membership.save(update_fields=["total_contributed"])

    return contribution


@transaction.atomic
def suspend_membership(membership):
    if membership.status != MutualAidMembership.Status.ACTIVE:
        raise MutualAidError("Only active memberships can be suspended.")
    membership.status = MutualAidMembership.Status.SUSPENDED
    membership.save(update_fields=["status"])
    return membership


@transaction.atomic
def terminate_membership(membership):
    if membership.status == MutualAidMembership.Status.TERMINATED:
        raise MutualAidError("This membership is already terminated.")
    membership.status = MutualAidMembership.Status.TERMINATED
    membership.terminated_at = timezone.now()
    membership.save(update_fields=["status", "terminated_at"])
    return membership


@transaction.atomic
def reactivate_membership(membership):
    if membership.status != MutualAidMembership.Status.SUSPENDED:
        raise MutualAidError("Only suspended memberships can be reactivated.")
    conflict = MutualAidMembership.objects.filter(
        member=membership.member,
        plan=membership.plan,
        status=MutualAidMembership.Status.ACTIVE,
    ).exclude(pk=membership.pk).exists()
    if conflict:
        raise MutualAidError(
            f"{membership.member.display_name()} already has another active {membership.plan.name} membership."
        )
    membership.status = MutualAidMembership.Status.ACTIVE
    membership.save(update_fields=["status"])
    return membership


@transaction.atomic
def submit_claim(membership, claim_type, amount_requested, reason, submitted_by=None):
    if not membership.is_operational:
        raise MutualAidError("Claims can only be filed on active memberships.")
    if not membership.is_eligible_for_claim():
        raise MutualAidError(
            "Member is not yet eligible. Check waiting period and minimum membership requirements."
        )

    amount_requested = Decimal(str(amount_requested))
    if amount_requested <= 0:
        raise MutualAidError("Requested amount must be greater than zero.")
    if amount_requested > membership.plan.max_benefit_amount:
        raise MutualAidError(
            f"Requested amount exceeds the plan maximum of ₱{membership.plan.max_benefit_amount:,.2f}."
        )

    return MutualAidClaim.objects.create(
        membership=membership,
        claim_type=claim_type,
        amount_requested=amount_requested,
        reason=reason,
        status=MutualAidClaim.Status.SUBMITTED,
        submitted_at=timezone.now(),
    )


@transaction.atomic
def review_claim(claim, decision, reviewer, review_notes="", amount_approved=None):
    if claim.status not in {MutualAidClaim.Status.SUBMITTED, MutualAidClaim.Status.UNDER_REVIEW}:
        raise MutualAidError("This claim is no longer pending review.")

    if decision == "approve":
        approved = Decimal(str(amount_approved)) if amount_approved is not None else claim.amount_requested
        if approved <= 0:
            raise MutualAidError("Approved amount must be greater than zero.")
        if approved > claim.membership.plan.max_benefit_amount:
            raise MutualAidError(
                f"Approved amount cannot exceed the plan maximum of ₱{claim.membership.plan.max_benefit_amount:,.2f}."
            )
        claim.status = MutualAidClaim.Status.APPROVED
        claim.amount_approved = approved
    elif decision == "reject":
        claim.status = MutualAidClaim.Status.REJECTED
        claim.amount_approved = None
    elif decision == "review":
        claim.status = MutualAidClaim.Status.UNDER_REVIEW
    else:
        raise MutualAidError("Invalid review decision.")

    claim.reviewed_by = reviewer
    claim.review_notes = review_notes
    claim.decision_date = timezone.now()
    claim.save()
    return claim


@transaction.atomic
def disburse_claim(claim, disbursed_by, disbursement_reference=""):
    if not claim.is_ready_for_disbursement:
        raise MutualAidError("This claim is not ready for disbursement.")

    amount = claim.amount_approved or claim.amount_requested
    membership = claim.membership
    membership.benefits_claimed += amount
    membership.save(update_fields=["benefits_claimed"])

    claim.status = MutualAidClaim.Status.DISBURSED
    claim.disbursed_at = timezone.now()
    claim.disbursed_by = disbursed_by
    claim.disbursement_reference = disbursement_reference
    claim.save()
    return claim
