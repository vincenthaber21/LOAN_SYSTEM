from decimal import Decimal
from uuid import uuid4

from django.conf import settings
from django.core.validators import MinValueValidator, MaxValueValidator
from django.db import models
from django.utils import timezone


class MutualAidPlan(models.Model):
    class ContributionFrequency(models.TextChoices):
        MONTHLY = "monthly", "Monthly"
        BIWEEKLY = "biweekly", "Biweekly"
        QUARTERLY = "quarterly", "Quarterly"

    name = models.CharField(max_length=120)
    description = models.TextField(blank=True)
    contribution_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
        help_text="Fixed contribution amount per period.",
    )
    contribution_frequency = models.CharField(
        max_length=20,
        choices=ContributionFrequency.choices,
        default=ContributionFrequency.MONTHLY,
    )
    max_benefit_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
        help_text="Maximum benefit payout per approved claim.",
    )
    waiting_period_days = models.PositiveSmallIntegerField(
        default=90,
        validators=[MinValueValidator(0), MaxValueValidator(730)],
        help_text="Days after enrollment before a member can file a claim.",
    )
    min_membership_months = models.PositiveSmallIntegerField(
        default=0,
        validators=[MinValueValidator(0), MaxValueValidator(120)],
        help_text="Minimum months of active membership before eligibility.",
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    @property
    def contribution_frequency_label(self):
        return self.get_contribution_frequency_display()

    @property
    def contribution_summary(self):
        return f"{self.contribution_amount} / {self.contribution_frequency_label.lower()}"


class MutualAidPeriod(models.Model):
    plan = models.ForeignKey(
        MutualAidPlan,
        on_delete=models.CASCADE,
        related_name="periods",
    )
    date_from = models.DateField()
    date_to = models.DateField()
    label = models.CharField(
        max_length=60,
        blank=True,
        help_text="Optional short label, e.g. 'Jan 2026'.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-date_from"]
        indexes = [
            models.Index(fields=["plan", "-date_from"]),
        ]

    def __str__(self):
        return self.display_label

    def clean(self):
        from django.core.exceptions import ValidationError

        super().clean()
        if self.date_from and self.date_to and self.date_to < self.date_from:
            raise ValidationError({"date_to": "End date must be on or after the start date."})

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    @property
    def display_label(self):
        if self.label:
            return self.label
        return f"{self.date_from.strftime('%b %d, %Y')} – {self.date_to.strftime('%b %d, %Y')}"

    @property
    def date_range_short(self):
        if self.date_from.year == self.date_to.year and self.date_from.month == self.date_to.month:
            return f"{self.date_from.strftime('%b %d')} – {self.date_to.strftime('%d, %Y')}"
        return f"{self.date_from.strftime('%b %d, %Y')} – {self.date_to.strftime('%b %d, %Y')}"


class MutualAidMembership(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        SUSPENDED = "suspended", "Suspended"
        TERMINATED = "terminated", "Terminated"

    member = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="mutual_aid_memberships",
    )
    plan = models.ForeignKey(
        MutualAidPlan,
        on_delete=models.PROTECT,
        related_name="memberships",
    )
    membership_number = models.CharField(max_length=20, unique=True, editable=False)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)
    total_contributed = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0"))],
    )
    benefits_claimed = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0"))],
    )
    enrolled_at = models.DateTimeField(default=timezone.now)
    enrolled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="mutual_aid_memberships_opened",
    )
    terminated_at = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-enrolled_at"]
        indexes = [
            models.Index(fields=["member", "plan", "status"]),
        ]

    def clean(self):
        from django.core.exceptions import ValidationError

        super().clean()
        if self.status != self.Status.ACTIVE:
            return
        qs = MutualAidMembership.objects.filter(
            member=self.member,
            plan=self.plan,
            status=self.Status.ACTIVE,
        )
        if self.pk:
            qs = qs.exclude(pk=self.pk)
        if qs.exists():
            raise ValidationError(
                {"member": "This member already has an active membership in this plan."}
            )

    def save(self, *args, **kwargs):
        if not self.membership_number:
            self.membership_number = f"MA{uuid4().hex[:10].upper()}"
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.reference} · {self.member.display_name()}"

    @property
    def reference(self):
        return self.membership_number

    @property
    def plan_name(self):
        return self.plan.name

    @property
    def status_label(self):
        return self.get_status_display()

    @property
    def is_operational(self):
        return self.status == self.Status.ACTIVE

    @property
    def net_balance(self):
        return self.total_contributed - self.benefits_claimed

    @property
    def membership_months(self):
        end = self.terminated_at or timezone.now()
        delta = end - self.enrolled_at
        return max(delta.days // 30, 0)

    def is_eligible_for_claim(self):
        if not self.is_operational:
            return False
        if self.plan.min_membership_months and self.membership_months < self.plan.min_membership_months:
            return False
        waiting_days = (timezone.now() - self.enrolled_at).days
        return waiting_days >= self.plan.waiting_period_days


class MutualAidContribution(models.Model):
    class Method(models.TextChoices):
        CASH = "cash", "Cash"
        BANK_TRANSFER = "bank_transfer", "Bank transfer"
        CHECK = "check", "Check"
        ONLINE = "online", "Online"
        PAYROLL_DEDUCTION = "payroll_deduction", "Payroll deduction"

    membership = models.ForeignKey(
        MutualAidMembership,
        on_delete=models.PROTECT,
        related_name="contributions",
    )
    amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    method = models.CharField(max_length=20, choices=Method.choices, default=Method.CASH)
    reference_number = models.CharField(max_length=60, blank=True)
    period = models.ForeignKey(
        MutualAidPeriod,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="contributions",
    )
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="mutual_aid_contributions_recorded",
    )

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["membership", "-created_at"]),
        ]

    def __str__(self):
        return f"Contribution · {self.reference_number or self.pk}"

    @property
    def method_label(self):
        return self.get_method_display()

    @property
    def period_display(self):
        return self.period.display_label if self.period else "—"


class MutualAidClaim(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        SUBMITTED = "submitted", "Submitted"
        UNDER_REVIEW = "under_review", "Under review"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"
        DISBURSED = "disbursed", "Disbursed"

    class ClaimType(models.TextChoices):
        DEATH = "death", "Death of member or dependent"
        ILLNESS = "illness", "Serious illness"
        HOSPITALIZATION = "hospitalization", "Hospitalization"
        EMERGENCY = "emergency", "Emergency hardship"
        OTHER = "other", "Other"

    membership = models.ForeignKey(
        MutualAidMembership,
        on_delete=models.PROTECT,
        related_name="claims",
    )
    claim_type = models.CharField(max_length=20, choices=ClaimType.choices)
    amount_requested = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    amount_approved = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    reason = models.TextField(help_text="Describe the circumstance requiring mutual aid.")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    submitted_at = models.DateTimeField(null=True, blank=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="mutual_aid_claims_reviewed",
    )
    review_notes = models.TextField(blank=True)
    decision_date = models.DateTimeField(null=True, blank=True)
    disbursed_at = models.DateTimeField(null=True, blank=True)
    disbursed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="mutual_aid_claims_disbursed",
    )
    disbursement_reference = models.CharField(max_length=80, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "-created_at"]),
        ]

    def __str__(self):
        return f"CLM-{self.pk:05d}"

    @property
    def reference(self):
        return f"CLM-{self.pk:05d}"

    @property
    def member_name(self):
        return self.membership.member.display_name()

    @property
    def plan_name(self):
        return self.membership.plan_name

    @property
    def claim_type_label(self):
        return self.get_claim_type_display()

    @property
    def status_label(self):
        return self.get_status_display()

    @property
    def is_pending_review(self):
        return self.status in {self.Status.SUBMITTED, self.Status.UNDER_REVIEW}

    @property
    def is_ready_for_disbursement(self):
        return self.status == self.Status.APPROVED and not self.disbursed_at
