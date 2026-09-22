from decimal import Decimal

from django import forms
from django.utils import timezone

from lending.models import User

from .models import MutualAidClaim, MutualAidContribution, MutualAidMembership, MutualAidPeriod, MutualAidPlan


def unavailable_mutual_aid_plan_ids_for_member(member):
    """Plan IDs the member already has an active mutual aid membership for."""
    if not member:
        return set()
    return {
        pk
        for pk in MutualAidMembership.objects.filter(
            member=member,
            status=MutualAidMembership.Status.ACTIVE,
        ).values_list("plan_id", flat=True)
        if pk
    }


def available_mutual_aid_plans_for_member(member):
    """Active mutual aid plans a member can enroll in."""
    plans = MutualAidPlan.objects.filter(is_active=True)
    blocked = unavailable_mutual_aid_plan_ids_for_member(member)
    if blocked:
        plans = plans.exclude(pk__in=blocked)
    return plans


class MutualAidPlanForm(forms.ModelForm):
    class Meta:
        model = MutualAidPlan
        fields = (
            "name",
            "description",
            "contribution_amount",
            "contribution_frequency",
            "max_benefit_amount",
            "waiting_period_days",
            "min_membership_months",
            "is_active",
        )
        widgets = {
            "name": forms.TextInput(attrs={"class": "form-control", "placeholder": "Basic mutual aid"}),
            "description": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
            "contribution_amount": forms.NumberInput(attrs={"class": "form-control", "step": "0.01", "min": "0.01"}),
            "contribution_frequency": forms.Select(attrs={"class": "form-select"}),
            "max_benefit_amount": forms.NumberInput(attrs={"class": "form-control", "step": "0.01", "min": "0.01"}),
            "waiting_period_days": forms.NumberInput(attrs={"class": "form-control", "min": "0", "max": "730"}),
            "min_membership_months": forms.NumberInput(attrs={"class": "form-control", "min": "0", "max": "120"}),
            "is_active": forms.CheckboxInput(attrs={"class": "form-check-input"}),
        }


class OfficerEnrollForm(forms.Form):
    member = forms.ModelChoiceField(
        queryset=None,
        label="Member",
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    plan = forms.ModelChoiceField(
        queryset=MutualAidPlan.objects.filter(is_active=True),
        label="Mutual aid plan",
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    notes = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"class": "form-control", "rows": 2, "placeholder": "Optional enrollment notes"}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["member"].queryset = User.member_accounts().filter(
            is_active=True
        ).order_by("full_name", "username")
        if not self.is_bound and not self.initial.get("plan"):
            from .services import KAP_MUTUAL_AID_PLAN_NAME

            default = MutualAidPlan.objects.filter(
                name__iexact=KAP_MUTUAL_AID_PLAN_NAME,
                is_active=True,
            ).first()
            if default:
                self.fields["plan"].initial = default.pk

    def clean(self):
        cleaned = super().clean()
        member = cleaned.get("member")
        plan = cleaned.get("plan")
        if member and plan:
            if plan.pk in unavailable_mutual_aid_plan_ids_for_member(member):
                self.add_error("plan", f"{member.display_name()} already has an active {plan.name} membership.")
        return cleaned


class OfficerEditMembershipForm(forms.Form):
    member = forms.ModelChoiceField(
        queryset=None,
        label="Member",
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    plan = forms.ModelChoiceField(
        queryset=MutualAidPlan.objects.none(),
        label="Mutual aid plan",
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    enrolled_on = forms.DateField(
        label="Enrollment date",
        widget=forms.DateInput(attrs={"class": "form-control", "type": "date"}),
        help_text="The date this membership is considered enrolled.",
    )
    notes = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"class": "form-control", "rows": 2, "placeholder": "Optional enrollment notes"}),
    )

    def __init__(self, *args, membership=None, **kwargs):
        self.membership = membership
        super().__init__(*args, **kwargs)
        self.fields["member"].queryset = User.member_accounts().filter(is_active=True).order_by(
            "full_name", "username"
        )
        self.fields["enrolled_on"].widget.attrs["max"] = timezone.localdate().isoformat()
        plans = MutualAidPlan.objects.filter(is_active=True)
        if membership and membership.plan_id:
            from django.db.models import Q

            plans = MutualAidPlan.objects.filter(Q(is_active=True) | Q(pk=membership.plan_id))
        self.fields["plan"].queryset = plans.order_by("name")
        if membership and not self.is_bound:
            self.fields["member"].initial = membership.member_id
            self.fields["plan"].initial = membership.plan_id
            self.fields["enrolled_on"].initial = timezone.localtime(membership.enrolled_at).date()
            self.fields["notes"].initial = membership.notes

    def clean(self):
        cleaned = super().clean()
        member = cleaned.get("member")
        plan = cleaned.get("plan")
        if member and plan and self.membership:
            conflict = (
                MutualAidMembership.objects.filter(
                    member=member,
                    plan=plan,
                    status=MutualAidMembership.Status.ACTIVE,
                )
                .exclude(pk=self.membership.pk)
                .exists()
            )
            if conflict:
                self.add_error(
                    "plan",
                    f"{member.display_name()} already has an active {plan.name} membership.",
                )
        enrolled_on = cleaned.get("enrolled_on")
        if enrolled_on and enrolled_on > timezone.localdate():
            self.add_error("enrolled_on", "Enrollment date cannot be in the future.")
        return cleaned


class MutualAidPeriodForm(forms.ModelForm):
    class Meta:
        model = MutualAidPeriod
        fields = ("date_from", "date_to", "label")
        widgets = {
            "date_from": forms.DateInput(attrs={"class": "form-control", "type": "date"}),
            "date_to": forms.DateInput(attrs={"class": "form-control", "type": "date"}),
            "label": forms.TextInput(attrs={"class": "form-control", "placeholder": "e.g. Jan 2026 (optional)"}),
        }

    def __init__(self, *args, plan=None, **kwargs):
        self.plan = plan
        super().__init__(*args, **kwargs)
        self.fields["label"].required = False

    def clean(self):
        cleaned = super().clean()
        date_from = cleaned.get("date_from")
        date_to = cleaned.get("date_to")
        if date_from and date_to:
            if date_to < date_from:
                self.add_error("date_to", "End date must be on or after the start date.")
            elif self.plan:
                qs = MutualAidPeriod.objects.filter(
                    plan=self.plan,
                    date_from__lte=date_to,
                    date_to__gte=date_from,
                )
                if self.instance.pk:
                    qs = qs.exclude(pk=self.instance.pk)
                if qs.exists():
                    self.add_error("date_to", "This period overlaps with an existing period for this plan.")
        return cleaned


class OfficerContributionForm(forms.Form):
    amount = forms.DecimalField(
        min_value=Decimal("0.01"),
        widget=forms.NumberInput(attrs={"class": "form-control", "step": "0.01", "min": "0.01"}),
    )
    contribution_date = forms.DateField(
        label="Contribution date",
        widget=forms.DateInput(attrs={"class": "form-control", "type": "date"}),
        initial=timezone.localdate,
        help_text="Use the same date as the related cash movement or loan disbursement.",
    )
    method = forms.ChoiceField(
        choices=MutualAidContribution.Method.choices,
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    period = forms.ModelChoiceField(
        queryset=MutualAidPeriod.objects.none(),
        required=False,
        empty_label="Select period (optional)",
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    reference_number = forms.CharField(
        required=False,
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "Optional reference"}),
    )
    notes = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"class": "form-control", "rows": 2, "placeholder": "Optional note"}),
    )

    def __init__(self, *args, membership=None, **kwargs):
        self.membership = membership
        super().__init__(*args, **kwargs)
        self.fields["contribution_date"].widget.attrs["max"] = timezone.localdate().isoformat()
        if membership:
            paid_period_ids = membership.contributions.filter(period__isnull=False).values_list("period_id", flat=True)
            self.fields["period"].queryset = (
                membership.plan.periods.exclude(pk__in=paid_period_ids).order_by("-date_from")
            )
            self.fields["period"].label_from_instance = lambda obj: f"{obj.display_label} ({obj.date_from:%Y-%m-%d} to {obj.date_to:%Y-%m-%d})"

    def clean_contribution_date(self):
        contribution_date = self.cleaned_data.get("contribution_date")
        if contribution_date and contribution_date > timezone.localdate():
            raise forms.ValidationError("Contribution date cannot be in the future.")
        return contribution_date


class OfficerEditContributionForm(forms.Form):
    amount = forms.DecimalField(
        min_value=Decimal("0.01"),
        widget=forms.NumberInput(attrs={"class": "form-control", "step": "0.01", "min": "0.01"}),
    )
    contribution_date = forms.DateField(
        label="Contribution date",
        widget=forms.DateInput(attrs={"class": "form-control", "type": "date"}),
        help_text="Use the same date as the related cash movement or loan disbursement.",
    )
    method = forms.ChoiceField(
        choices=MutualAidContribution.Method.choices,
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    period = forms.ModelChoiceField(
        queryset=MutualAidPeriod.objects.none(),
        required=False,
        empty_label="Select period (optional)",
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    reference_number = forms.CharField(
        required=False,
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "Optional reference"}),
    )
    notes = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"class": "form-control", "rows": 2, "placeholder": "Optional note"}),
    )

    def __init__(self, *args, membership=None, contribution=None, **kwargs):
        self.membership = membership
        self.contribution = contribution
        super().__init__(*args, **kwargs)
        self.fields["contribution_date"].widget.attrs["max"] = timezone.localdate().isoformat()
        if membership:
            paid_qs = membership.contributions.filter(period__isnull=False)
            if contribution:
                paid_qs = paid_qs.exclude(pk=contribution.pk)
            paid_period_ids = paid_qs.values_list("period_id", flat=True)
            self.fields["period"].queryset = (
                membership.plan.periods.exclude(pk__in=paid_period_ids).order_by("-date_from")
            )
            self.fields["period"].label_from_instance = (
                lambda obj: f"{obj.display_label} ({obj.date_from:%Y-%m-%d} to {obj.date_to:%Y-%m-%d})"
            )

    def clean_contribution_date(self):
        contribution_date = self.cleaned_data.get("contribution_date")
        if contribution_date and contribution_date > timezone.localdate():
            raise forms.ValidationError("Contribution date cannot be in the future.")
        return contribution_date


class OfficerClaimForm(forms.Form):
    claim_type = forms.ChoiceField(
        choices=MutualAidClaim.ClaimType.choices,
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    amount_requested = forms.DecimalField(
        min_value=Decimal("0.01"),
        widget=forms.NumberInput(attrs={"class": "form-control", "step": "0.01", "min": "0.01"}),
    )
    reason = forms.CharField(
        widget=forms.Textarea(attrs={"class": "form-control", "rows": 4, "placeholder": "Describe the circumstance requiring aid."}),
    )

    def __init__(self, *args, membership=None, **kwargs):
        self.membership = membership
        super().__init__(*args, **kwargs)
        if membership:
            self.fields["amount_requested"].widget.attrs["max"] = str(membership.plan.max_benefit_amount)

    def clean_amount_requested(self):
        amount = self.cleaned_data.get("amount_requested")
        if amount and self.membership and amount > self.membership.plan.max_benefit_amount:
            raise forms.ValidationError(
                f"Amount cannot exceed the plan maximum of ₱{self.membership.plan.max_benefit_amount:,.2f}."
            )
        return amount


class ClaimReviewForm(forms.Form):
    review_notes = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"class": "form-control", "rows": 4, "placeholder": "Explain the decision."}),
    )
    amount_approved = forms.DecimalField(
        required=False,
        min_value=Decimal("0.01"),
        widget=forms.NumberInput(attrs={"class": "form-control", "step": "0.01", "min": "0.01"}),
        label="Approved amount",
    )

    def __init__(self, *args, claim=None, **kwargs):
        self.claim = claim
        super().__init__(*args, **kwargs)
        if claim and claim.amount_requested:
            self.fields["amount_approved"].initial = claim.amount_requested
            self.fields["amount_approved"].widget.attrs["max"] = str(claim.membership.plan.max_benefit_amount)


class ClaimDisburseForm(forms.Form):
    disbursement_reference = forms.CharField(
        required=False,
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "Reference number or receipt ID"}),
    )
