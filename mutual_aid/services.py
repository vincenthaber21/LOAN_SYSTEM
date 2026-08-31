from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from .models import MutualAidClaim, MutualAidContribution, MutualAidMembership, MutualAidPeriod, MutualAidPlan


class MutualAidError(Exception):
    pass


@transaction.atomic
def enroll_member(member, plan, enrolled_by=None, notes=""):
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
    )


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
def record_contribution(membership, amount, method, reference="", period=None, recorded_by=None, notes=""):
    if not membership.is_operational:
        raise MutualAidError("This membership is not active.")

    amount = Decimal(str(amount))
    if amount <= 0:
        raise MutualAidError("Contribution amount must be greater than zero.")

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
        recorded_by=recorded_by,
    )


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
