from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q, Sum
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render

from lending.decorators import role_required
from lending.models import User

from .forms import (
    ClaimDisburseForm,
    ClaimReviewForm,
    MutualAidPeriodForm,
    MutualAidPlanForm,
    OfficerClaimForm,
    OfficerContributionForm,
    OfficerEnrollForm,
    unavailable_mutual_aid_plan_ids_for_member,
)
from .models import MutualAidClaim, MutualAidContribution, MutualAidMembership, MutualAidPeriod, MutualAidPlan
from .services import (
    MutualAidError,
    create_period,
    disburse_claim,
    enroll_member,
    reactivate_membership,
    record_contribution,
    review_claim,
    submit_claim,
    suspend_membership,
    terminate_membership,
)


def _membership_ledger(membership):
    contributions = membership.contributions.all()
    agg = contributions.aggregate(
        contribution_count=Count("id"),
        total_amount=Sum("amount"),
    )
    last_contribution = contributions.select_related("recorded_by").order_by("-created_at").first()
    return {
        "contribution_count": agg["contribution_count"] or 0,
        "total_amount": agg["total_amount"] or Decimal("0.00"),
        "last_contribution": last_contribution,
    }


@login_required
@role_required("officer")
def officer_mutual_aid_memberships(request):
    query = request.GET.get("q", "").strip()
    status = request.GET.get("status", "").strip()
    qs = MutualAidMembership.objects.select_related("member", "plan").annotate(
        claim_count=Count("claims"),
    )
    if query:
        qs = qs.filter(
            Q(membership_number__icontains=query)
            | Q(member__full_name__icontains=query)
            | Q(member__email__icontains=query)
        )
    if status:
        qs = qs.filter(status=status)
    qs = qs.order_by("-enrolled_at")

    active_qs = qs.filter(status=MutualAidMembership.Status.ACTIVE)
    total_contributed = active_qs.aggregate(value=Sum("total_contributed"))["value"] or Decimal("0.00")
    total_benefits = active_qs.aggregate(value=Sum("benefits_claimed"))["value"] or Decimal("0.00")

    return render(request, "officer/mutual_aid_memberships.html", {
        "memberships": qs,
        "membership_count": qs.count(),
        "active_count": active_qs.count(),
        "total_contributed": total_contributed,
        "total_benefits": total_benefits,
        "filters": {"q": query, "status": status},
        "status_filters": MutualAidMembership.Status.choices,
    })


@login_required
@role_required("officer")
def officer_available_mutual_aid_plans(request):
    member_id = request.GET.get("member")
    member = User.member_accounts().filter(pk=member_id, is_active=True).first()
    member_inactive = bool(member_id) and not member
    blocked_plan_ids = unavailable_mutual_aid_plan_ids_for_member(member) if member else set()
    all_plans = MutualAidPlan.objects.filter(is_active=True).order_by("name")
    return JsonResponse({
        "member_inactive": member_inactive,
        "blocked_plan_count": len(blocked_plan_ids),
        "plans": [
            {
                "id": plan.pk,
                "label": f"{plan.name} ({plan.contribution_summary})",
                "available": not member_inactive and (not member or plan.pk not in blocked_plan_ids),
            }
            for plan in all_plans
        ],
    })


@login_required
@role_required("officer")
def officer_enroll_mutual_aid(request):
    form = OfficerEnrollForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            membership = enroll_member(
                form.cleaned_data["member"],
                form.cleaned_data["plan"],
                enrolled_by=request.user,
                notes=form.cleaned_data.get("notes", ""),
            )
            messages.success(
                request,
                f"Enrolled {membership.member.display_name()} in {membership.plan_name}.",
            )
            return redirect("officer_mutual_aid_membership_detail", membership_id=membership.pk)
        except MutualAidError as exc:
            messages.error(request, str(exc))
    plans = MutualAidPlan.objects.filter(is_active=True).order_by("name")
    return render(request, "officer/enroll_mutual_aid.html", {"form": form, "plans": plans})


def _membership_period_rows(membership):
    contributions_by_period = {
        c.period_id: c
        for c in membership.contributions.select_related("period").filter(period__isnull=False)
    }
    rows = []
    for period in membership.plan.periods.all():
        contribution = contributions_by_period.get(period.pk)
        rows.append({
            "period": period,
            "contribution": contribution,
            "is_paid": contribution is not None,
        })
    return rows


@login_required
@role_required("officer")
def officer_mutual_aid_membership_detail(request, membership_id):
    membership = get_object_or_404(
        MutualAidMembership.objects.select_related("member", "plan", "enrolled_by"),
        pk=membership_id,
    )
    contribution_form = OfficerContributionForm(membership=membership)
    claim_form = OfficerClaimForm(membership=membership)
    period_form = MutualAidPeriodForm(plan=membership.plan)

    if request.method == "POST":
        action = request.POST.get("action")
        if action == "contribution":
            contribution_form = OfficerContributionForm(request.POST, membership=membership)
            if contribution_form.is_valid():
                data = contribution_form.cleaned_data
                try:
                    record_contribution(
                        membership,
                        data["amount"],
                        data["method"],
                        reference=data.get("reference_number", ""),
                        period=data.get("period"),
                        recorded_by=request.user,
                        notes=data.get("notes", ""),
                    )
                    messages.success(request, "Contribution recorded.")
                    return redirect("officer_mutual_aid_membership_detail", membership_id=membership.pk)
                except MutualAidError as exc:
                    messages.error(request, str(exc))
        elif action == "add_period":
            period_form = MutualAidPeriodForm(request.POST, plan=membership.plan)
            if period_form.is_valid():
                data = period_form.cleaned_data
                try:
                    create_period(
                        membership.plan,
                        data["date_from"],
                        data["date_to"],
                        label=data.get("label", ""),
                    )
                    messages.success(request, "Contribution period added.")
                    return redirect("officer_mutual_aid_membership_detail", membership_id=membership.pk)
                except MutualAidError as exc:
                    messages.error(request, str(exc))
        elif action == "claim":
            claim_form = OfficerClaimForm(request.POST, membership=membership)
            if claim_form.is_valid():
                data = claim_form.cleaned_data
                try:
                    claim = submit_claim(
                        membership,
                        data["claim_type"],
                        data["amount_requested"],
                        data["reason"],
                    )
                    messages.success(request, f"Claim {claim.reference} submitted for review.")
                    return redirect("officer_mutual_aid_claim_review", claim_id=claim.pk)
                except MutualAidError as exc:
                    messages.error(request, str(exc))
        elif action == "suspend":
            try:
                suspend_membership(membership)
                messages.success(request, "Membership suspended.")
                return redirect("officer_mutual_aid_membership_detail", membership_id=membership.pk)
            except MutualAidError as exc:
                messages.error(request, str(exc))
        elif action == "reactivate":
            try:
                reactivate_membership(membership)
                messages.success(request, "Membership reactivated.")
                return redirect("officer_mutual_aid_membership_detail", membership_id=membership.pk)
            except MutualAidError as exc:
                messages.error(request, str(exc))
        elif action == "terminate":
            try:
                terminate_membership(membership)
                messages.success(request, "Membership terminated.")
                return redirect("officer_mutual_aid_memberships")
            except MutualAidError as exc:
                messages.error(request, str(exc))

    contributions = list(
        membership.contributions.select_related("recorded_by", "period").order_by("-created_at")[:50]
    )
    claims = membership.claims.select_related("reviewed_by", "disbursed_by").order_by("-created_at")[:20]
    ledger = _membership_ledger(membership)
    period_rows = _membership_period_rows(membership)

    return render(request, "officer/mutual_aid_membership_detail.html", {
        "membership": membership,
        "contributions": contributions,
        "claims": claims,
        "ledger": ledger,
        "period_rows": period_rows,
        "contribution_form": contribution_form,
        "claim_form": claim_form,
        "period_form": period_form,
        "is_eligible": membership.is_eligible_for_claim(),
    })


@login_required
@role_required("officer")
def officer_mutual_aid_plans(request):
    query = request.GET.get("q", "").strip()
    status = request.GET.get("status", "").strip()
    qs = MutualAidPlan.objects.annotate(memberships_count=Count("memberships"))
    if query:
        qs = qs.filter(name__icontains=query)
    if status == "active":
        qs = qs.filter(is_active=True)
    elif status == "inactive":
        qs = qs.filter(is_active=False)
    qs = qs.order_by("name")
    return render(request, "officer/mutual_aid_plans.html", {
        "plans": qs,
        "plan_count": qs.count(),
        "active_count": qs.filter(is_active=True).count(),
        "filters": {"q": query, "status": status},
    })


@login_required
@role_required("officer")
def officer_add_mutual_aid_plan(request):
    form = MutualAidPlanForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        plan = form.save()
        messages.success(request, f'Mutual aid plan "{plan.name}" added successfully.')
        return redirect("officer_mutual_aid_plans")
    return render(request, "officer/add_mutual_aid_plan.html", {
        "form": form,
        "existing_plans": MutualAidPlan.objects.all().order_by("name")[:8],
    })


@login_required
@role_required("officer")
def officer_edit_mutual_aid_plan(request, plan_id):
    plan = get_object_or_404(MutualAidPlan, pk=plan_id)
    form = MutualAidPlanForm(request.POST or None, instance=plan)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Mutual aid plan updated.")
        return redirect("officer_mutual_aid_plans")
    return render(request, "officer/edit_mutual_aid_plan.html", {"form": form, "plan": plan})


@login_required
@role_required("officer")
def officer_mutual_aid_claims(request):
    query = request.GET.get("q", "").strip()
    status = request.GET.get("status", "").strip()
    qs = MutualAidClaim.objects.select_related(
        "membership__member",
        "membership__plan",
        "reviewed_by",
    )
    if query:
        qs = qs.filter(
            Q(membership__membership_number__icontains=query)
            | Q(membership__member__full_name__icontains=query)
            | Q(membership__member__email__icontains=query)
        )
    if status:
        qs = qs.filter(status=status)
    else:
        qs = qs.exclude(status=MutualAidClaim.Status.DRAFT)
    qs = qs.order_by("-created_at")

    pending_count = MutualAidClaim.objects.filter(
        status__in=[MutualAidClaim.Status.SUBMITTED, MutualAidClaim.Status.UNDER_REVIEW]
    ).count()

    return render(request, "officer/mutual_aid_claims.html", {
        "claims": qs,
        "claim_count": qs.count(),
        "pending_count": pending_count,
        "filters": {"q": query, "status": status},
        "status_filters": MutualAidClaim.Status.choices,
    })


@login_required
@role_required("officer")
def officer_mutual_aid_claim_review(request, claim_id):
    claim = get_object_or_404(
        MutualAidClaim.objects.select_related(
            "membership__member",
            "membership__plan",
            "reviewed_by",
            "disbursed_by",
        ),
        pk=claim_id,
    )
    review_form = ClaimReviewForm(claim=claim)
    disburse_form = ClaimDisburseForm()

    if request.method == "POST":
        action = request.POST.get("action")
        if action in {"approve", "reject", "review"}:
            review_form = ClaimReviewForm(request.POST, claim=claim)
            if review_form.is_valid():
                data = review_form.cleaned_data
                try:
                    if action == "approve" and not data.get("amount_approved"):
                        raise MutualAidError("Approved amount is required when approving a claim.")
                    review_claim(
                        claim,
                        action,
                        request.user,
                        review_notes=data.get("review_notes", ""),
                        amount_approved=data.get("amount_approved"),
                    )
                    label = {"approve": "approved", "reject": "rejected", "review": "marked under review"}[action]
                    messages.success(request, f"Claim {claim.reference} {label}.")
                    return redirect("officer_mutual_aid_claim_review", claim_id=claim.pk)
                except MutualAidError as exc:
                    messages.error(request, str(exc))
        elif action == "disburse":
            disburse_form = ClaimDisburseForm(request.POST)
            if disburse_form.is_valid():
                try:
                    disburse_claim(
                        claim,
                        request.user,
                        disbursement_reference=disburse_form.cleaned_data.get("disbursement_reference", ""),
                    )
                    messages.success(request, f"Claim {claim.reference} disbursed.")
                    return redirect("officer_mutual_aid_claims")
                except MutualAidError as exc:
                    messages.error(request, str(exc))

    membership = claim.membership
    return render(request, "officer/mutual_aid_claim_review.html", {
        "claim": claim,
        "membership": membership,
        "review_form": review_form,
        "disburse_form": disburse_form,
    })


def _member_memberships(user):
    return MutualAidMembership.objects.filter(member=user).select_related("plan")


@login_required
@role_required("member")
def mutual_aid_dashboard(request):
    memberships = _member_memberships(request.user).filter(status=MutualAidMembership.Status.ACTIVE)
    total_contributed = memberships.aggregate(value=Sum("total_contributed"))["value"] or Decimal("0.00")
    total_benefits = memberships.aggregate(value=Sum("benefits_claimed"))["value"] or Decimal("0.00")
    recent_contributions = (
        MutualAidContribution.objects.filter(membership__member=request.user)
        .select_related("membership", "membership__plan", "period")
        .order_by("-created_at")[:8]
    )
    recent_claims = (
        MutualAidClaim.objects.filter(membership__member=request.user)
        .select_related("membership", "membership__plan")
        .order_by("-created_at")[:8]
    )
    return render(request, "mutual_aid/dashboard.html", {
        "memberships": memberships,
        "membership_count": memberships.count(),
        "total_contributed": total_contributed,
        "total_benefits": total_benefits,
        "recent_contributions": recent_contributions,
        "recent_claims": recent_claims,
    })


@login_required
@role_required("member")
def mutual_aid_membership_detail(request, membership_id):
    membership = get_object_or_404(_member_memberships(request.user), pk=membership_id)
    contributions = membership.contributions.select_related("period").order_by("-created_at")[:50]
    claims = membership.claims.order_by("-created_at")[:20]
    ledger = _membership_ledger(membership)
    return render(request, "mutual_aid/membership_detail.html", {
        "membership": membership,
        "contributions": contributions,
        "claims": claims,
        "ledger": ledger,
        "is_eligible": membership.is_eligible_for_claim(),
    })


@login_required
@role_required("member")
def mutual_aid_file_claim(request, membership_id):
    membership = get_object_or_404(
        _member_memberships(request.user),
        pk=membership_id,
        status=MutualAidMembership.Status.ACTIVE,
    )
    form = OfficerClaimForm(request.POST or None, membership=membership)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        try:
            claim = submit_claim(
                membership,
                data["claim_type"],
                data["amount_requested"],
                data["reason"],
            )
            messages.success(request, f"Claim {claim.reference} submitted. Our team will review it shortly.")
            return redirect("mutual_aid_claim_detail", claim_id=claim.pk)
        except MutualAidError as exc:
            messages.error(request, str(exc))
    return render(request, "mutual_aid/claim_form.html", {
        "membership": membership,
        "form": form,
        "is_eligible": membership.is_eligible_for_claim(),
    })


@login_required
@role_required("member")
def mutual_aid_claim_detail(request, claim_id):
    claim = get_object_or_404(
        MutualAidClaim.objects.select_related("membership__plan", "reviewed_by", "disbursed_by"),
        pk=claim_id,
        membership__member=request.user,
    )
    return render(request, "mutual_aid/claim_detail.html", {
        "claim": claim,
        "membership": claim.membership,
    })
