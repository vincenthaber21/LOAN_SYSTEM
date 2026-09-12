from decimal import Decimal
from datetime import time, timedelta
from uuid import uuid4

from django.contrib.auth.models import AbstractUser, UserManager
from django.core.validators import MinValueValidator
from django.db import models
from django.utils import timezone


class User(AbstractUser):
    class Role(models.TextChoices):
        MEMBER = "member", "Member"
        OFFICER = "officer", "Loan Officer"
        MANAGER = "manager", "Manager"
        ADMIN = "admin", "Admin"

    role = models.CharField(max_length=20, choices=Role.choices, default=Role.MEMBER)
    full_name = models.CharField(max_length=160, blank=True)
    middle_initial = models.CharField(
        "middle name",
        max_length=80,
        blank=True,
        help_text="Optional middle name.",
    )
    phone = models.CharField(max_length=30, blank=True)
    address = models.TextField(blank=True)
    date_of_birth = models.DateField(null=True, blank=True)
    employment_status = models.CharField(max_length=40, blank=True)
    monthly_income = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    credit_score = models.DecimalField(
        max_digits=8,
        decimal_places=3,
        default=Decimal("100.000"),
        validators=[MinValueValidator(Decimal("0"))],
    )
    last_seen = models.DateTimeField(null=True, blank=True)

    ONLINE_THRESHOLD = timedelta(minutes=5)

    @property
    def is_officer(self):
        return self.role in {self.Role.OFFICER, self.Role.MANAGER, self.Role.ADMIN} or self.is_staff

    @property
    def is_member(self):
        return self.role == self.Role.MEMBER and not self.is_staff

    @classmethod
    def member_accounts(cls):
        """Borrower accounts visible to loan officers — role member only, no staff/admin."""
        return cls.objects.filter(role=cls.Role.MEMBER, is_staff=False, is_superuser=False)

    @property
    def is_manager(self):
        return self.role in {self.Role.MANAGER, self.Role.ADMIN} or self.is_superuser

    @property
    def is_admin(self):
        return self.role == self.Role.ADMIN or self.is_superuser

    @property
    def role_label(self):
        return self.get_role_display()

    def display_name(self):
        return self.full_name or self.get_full_name() or self.username

    @property
    def initials(self):
        parts = self.display_name().split()
        return "".join(part[0] for part in parts[:2]).upper()

    @property
    def joined_at(self):
        return self.date_joined.strftime("%b %d, %Y")

    @property
    def reference(self):
        return f"BR-{self.pk:05d}"

    @property
    def status(self):
        return "active" if self.is_active else "inactive"

    @property
    def status_label(self):
        return "Active" if self.is_active else "Deactivated"

    @property
    def has_active_loan(self):
        return Loan.objects.filter(application__borrower=self, status__in=[Loan.Status.ACTIVE, Loan.Status.OVERDUE]).exists()

    @property
    def current_loan(self):
        loans = Loan.objects.filter(application__borrower=self)
        return loans.filter(status__in=[Loan.Status.ACTIVE, Loan.Status.OVERDUE]).first() or loans.first()

    @property
    def is_online(self):
        if not self.last_seen:
            return False
        return timezone.now() - self.last_seen <= self.ONLINE_THRESHOLD

    @property
    def presence_status(self):
        if self.is_online:
            return "online"
        if self.last_seen:
            return "away"
        return "offline"

    @property
    def presence_label(self):
        if self.is_online:
            return "Online now"
        if self.last_seen:
            return f"Last seen {timezone.localtime(self.last_seen).strftime('%b %d · %I:%M %p')}"
        return "Never signed in"


class RoleScopedManager(UserManager):
    """Limits a proxy model to the accounts holding one role."""

    def __init__(self, role):
        super().__init__()
        self.role = role

    def get_queryset(self):
        return super().get_queryset().filter(role=self.role)


class RoleDefaultMixin:
    """Stamps the section's role onto newly created accounts, unless a form
    has already assigned one explicitly (see AdministratorCreationForm)."""

    ROLE = None

    def save(self, *args, **kwargs):
        if self._state.adding and self.ROLE and not getattr(self, "_role_explicit", False):
            self.role = self.ROLE
        return super().save(*args, **kwargs)


class Member(RoleDefaultMixin, User):
    ROLE = User.Role.MEMBER
    objects = RoleScopedManager(User.Role.MEMBER)

    class Meta:
        proxy = True
        verbose_name = "Member"
        verbose_name_plural = "Members"


class LoanOfficer(RoleDefaultMixin, User):
    ROLE = User.Role.OFFICER
    objects = RoleScopedManager(User.Role.OFFICER)

    class Meta:
        proxy = True
        verbose_name = "Loan Officer"
        verbose_name_plural = "Loan Officers"


class Manager(RoleDefaultMixin, User):
    ROLE = User.Role.MANAGER
    objects = RoleScopedManager(User.Role.MANAGER)

    class Meta:
        proxy = True
        verbose_name = "Manager"
        verbose_name_plural = "Managers"


class Administrator(RoleDefaultMixin, User):
    ROLE = User.Role.ADMIN
    objects = RoleScopedManager(User.Role.ADMIN)

    class Meta:
        proxy = True
        verbose_name = "Admin"
        verbose_name_plural = "Admins"


class LoanProduct(models.Model):
    class LoanType(models.TextChoices):
        PERSONAL = "personal", "Personal"
        BUSINESS = "business", "Business"
        EDUCATION = "education", "Education"
        EMERGENCY = "emergency", "Emergency"

    name = models.CharField(max_length=120)
    loan_type = models.CharField(max_length=20, choices=LoanType.choices)
    min_amount = models.DecimalField(max_digits=12, decimal_places=2)
    max_amount = models.DecimalField(max_digits=12, decimal_places=2)
    interest_rate = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        help_text="Flat monthly percentage charged on principal for each month of the term.",
    )
    min_term_months = models.PositiveIntegerField(default=1)
    max_term_months = models.PositiveIntegerField(default=24)
    processing_fee_percent = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    grace_period_days = models.PositiveIntegerField(
        default=0,
        help_text="Days after disbursement before interest starts accruing.",
    )
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return self.name

    def estimate_payment(self, amount, term_months):
        from .services import calculate_flat_loan_amounts

        return calculate_flat_loan_amounts(amount, self.interest_rate, term_months)["per_month"]

    def fee_for(self, amount):
        return (Decimal(str(amount)) * self.processing_fee_percent / Decimal("100")).quantize(Decimal("0.01"))


class LoanApplication(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        SUBMITTED = "submitted", "Submitted"
        UNDER_REVIEW = "under_review", "Under review"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"
        DISBURSED = "disbursed", "Disbursed"
        ACTIVE = "active", "Active"
        CLOSED = "closed", "Closed"
        DEFAULTED = "defaulted", "Defaulted"

    class PaymentFrequency(models.TextChoices):
        DAILY = "daily", "Daily"
        WEEKLY = "weekly", "Weekly"
        BIWEEKLY = "biweekly", "Biweekly"
        MONTHLY = "monthly", "Monthly"

    class ApplicationType(models.TextChoices):
        NEW = "new", "New Application"
        RENEW = "renew", "Renew Application"

    class LoanPurpose(models.TextChoices):
        ADDITIONAL_CAPITAL = "additional_capital", "Additional Capital / Business Expansion"
        EXISTING_IMPROVEMENT = "existing_improvement", "Existing Improvement / Repair"
        OTHERS = "others", "Others"

    class DwellingOwnership(models.TextChoices):
        OWNED = "owned", "Owned"
        RENTED = "rented", "Rented"
        MORTGAGED = "mortgaged", "Mortgaged"
        USED_FREE = "used_free", "Used Free"

    class Citizenship(models.TextChoices):
        FILIPINO = "filipino", "Filipino"
        OTHERS = "others", "Others"

    class Gender(models.TextChoices):
        MALE = "male", "Male"
        FEMALE = "female", "Female"

    class CivilStatus(models.TextChoices):
        SINGLE = "single", "Single"
        MARRIED = "married", "Married"
        WIDOWED = "widowed", "Widowed"
        SEPARATED = "separated", "Separated"

    class CoborrowerRelationship(models.TextChoices):
        SPOUSE = "spouse", "Spouse"
        OTHERS = "others", "Others"

    class InsuranceProposed(models.TextChoices):
        LIFE_3YR = "life_3yr", "3 Years Life"
        POG = "pog", "POG"
        NONE = "none", "No Insurance"

    class OfficeDecision(models.TextChoices):
        APPROVED = "approved", "Approved"
        DISAPPROVED = "disapproved", "Disapproved"
        HOLD = "hold", "Hold"

    # --- Core / system ---
    borrower = models.ForeignKey(User, on_delete=models.CASCADE, related_name="loan_applications")
    loan_product = models.ForeignKey(
        LoanProduct,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="applications",
    )
    amount_requested = models.DecimalField(max_digits=12, decimal_places=2)
    purpose = models.TextField(blank=True, help_text="Details when loan purpose is Others, or free-text notes.")
    term_months = models.PositiveIntegerField()
    payment_frequency = models.CharField(
        max_length=20,
        choices=PaymentFrequency.choices,
        default=PaymentFrequency.WEEKLY,
    )
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    applied_on = models.DateField(null=True, blank=True, help_text="Date the borrower applied for this loan.")
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name="created_applications"
    )
    reviewed_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name="reviewed_applications"
    )
    review_notes = models.TextField(blank=True)
    decision_date = models.DateTimeField(null=True, blank=True)
    final_interest_rate = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    final_term_months = models.PositiveIntegerField(null=True, blank=True)

    # --- KAP header ---
    branch_name = models.CharField(max_length=120, blank=True)
    form_ref_no = models.CharField("Ref No.", max_length=60, blank=True)
    application_type = models.CharField(
        max_length=20,
        choices=ApplicationType.choices,
        default=ApplicationType.NEW,
        blank=True,
    )
    loan_purpose = models.CharField(max_length=40, choices=LoanPurpose.choices, blank=True)
    borrower_photo = models.ImageField(upload_to="loan-applications/photos/%Y/%m/", blank=True, null=True)
    coborrower_photo = models.ImageField(upload_to="loan-applications/photos/%Y/%m/", blank=True, null=True)

    # --- Borrower personal data ---
    borrower_surname = models.CharField(max_length=80, blank=True)
    borrower_first_name = models.CharField(max_length=80, blank=True)
    borrower_middle_name = models.CharField(max_length=80, blank=True)
    borrower_present_address = models.CharField(
        max_length=255, blank=True, help_text="House #, Street, Subd., Brgy."
    )
    borrower_municipality_city = models.CharField("Municipality, Province / City", max_length=160, blank=True)
    borrower_period_of_staying = models.CharField(max_length=80, blank=True)
    borrower_dwelling_ownership = models.CharField(
        max_length=20, choices=DwellingOwnership.choices, blank=True
    )
    borrower_permanent_address = models.CharField(max_length=255, blank=True)
    borrower_permanent_municipality_city = models.CharField(
        "Permanent Municipality, Province / City", max_length=160, blank=True
    )
    borrower_tel_mobile = models.CharField("Tel or Mobile", max_length=40, blank=True)
    borrower_date_of_birth = models.DateField(null=True, blank=True)
    borrower_age = models.PositiveSmallIntegerField(null=True, blank=True)
    borrower_citizenship = models.CharField(max_length=20, choices=Citizenship.choices, blank=True)
    borrower_place_of_birth = models.CharField(max_length=120, blank=True)
    borrower_gender = models.CharField(max_length=10, choices=Gender.choices, blank=True)
    borrower_civil_status = models.CharField(max_length=20, choices=CivilStatus.choices, blank=True)
    borrower_nationality = models.CharField(max_length=80, blank=True)
    borrower_occupation = models.CharField(max_length=120, blank=True)
    borrower_id_presented = models.CharField("I.D. Presented", max_length=120, blank=True)
    borrower_contact_network = models.CharField("Contact # / Network", max_length=80, blank=True)
    borrower_tin_sss = models.CharField("TIN or SSS #", max_length=60, blank=True)
    borrower_email = models.EmailField(blank=True)
    borrower_spouse_name = models.CharField("Name of Spouse (if married)", max_length=160, blank=True)

    # --- Co-borrower ---
    coborrower_relationship = models.CharField(
        max_length=20, choices=CoborrowerRelationship.choices, blank=True
    )
    coborrower_surname = models.CharField(max_length=80, blank=True)
    coborrower_first_name = models.CharField(max_length=80, blank=True)
    coborrower_middle_name = models.CharField(max_length=80, blank=True)
    coborrower_present_address = models.CharField(
        max_length=255, blank=True, help_text="House #, Street, Subd., Brgy."
    )
    coborrower_municipality_city = models.CharField("Municipality, Province / City", max_length=160, blank=True)
    coborrower_period_of_staying = models.CharField(max_length=80, blank=True)
    coborrower_dwelling_ownership = models.CharField(
        max_length=20, choices=DwellingOwnership.choices, blank=True
    )
    coborrower_permanent_address = models.CharField(max_length=255, blank=True)
    coborrower_permanent_municipality_city = models.CharField(
        "Permanent Municipality, Province / City", max_length=160, blank=True
    )
    coborrower_tel_mobile = models.CharField("Tel or Mobile", max_length=40, blank=True)
    coborrower_date_of_birth = models.DateField(null=True, blank=True)
    coborrower_age = models.PositiveSmallIntegerField(null=True, blank=True)
    coborrower_citizenship = models.CharField(max_length=20, choices=Citizenship.choices, blank=True)
    coborrower_place_of_birth = models.CharField(max_length=120, blank=True)
    coborrower_gender = models.CharField(max_length=10, choices=Gender.choices, blank=True)
    coborrower_civil_status = models.CharField(max_length=20, choices=CivilStatus.choices, blank=True)
    coborrower_nationality = models.CharField(max_length=80, blank=True)
    coborrower_occupation = models.CharField(max_length=120, blank=True)
    coborrower_id_presented = models.CharField("I.D. Presented", max_length=120, blank=True)
    coborrower_contact_network = models.CharField("Contact # / Network", max_length=80, blank=True)
    coborrower_tin_sss = models.CharField("TIN or SSS #", max_length=60, blank=True)
    coborrower_email = models.EmailField(blank=True)
    coborrower_spouse_name = models.CharField("Name of Spouse (if married)", max_length=160, blank=True)

    # --- Enterprise data (new borrowers) ---
    primary_business = models.CharField(max_length=160, blank=True)
    business_name = models.CharField(max_length=160, blank=True)
    business_ownership = models.CharField(max_length=20, choices=DwellingOwnership.choices, blank=True)
    business_address = models.CharField(max_length=255, blank=True)
    reg_dti = models.BooleanField("DTI", default=False)
    reg_barangay = models.BooleanField("Barangay Clearance / Permit", default=False)
    reg_mayor = models.BooleanField("Mayor's Permit", default=False)
    reg_bir = models.BooleanField("BIR", default=False)
    reg_others = models.BooleanField("Other registration", default=False)
    reg_others_text = models.CharField("Other registration details", max_length=120, blank=True)
    years_in_operation = models.CharField(max_length=40, blank=True)
    persons_employed = models.CharField("No. of persons employed", max_length=40, blank=True)
    additional_business_1_type = models.CharField("Type of Business 1", max_length=120, blank=True)
    additional_business_1_name = models.CharField("Business 1 name", max_length=160, blank=True)
    additional_business_1_address = models.CharField("Business 1 address", max_length=255, blank=True)
    additional_business_2_type = models.CharField("Type of Business 2", max_length=120, blank=True)
    additional_business_2_name = models.CharField("Business 2 name", max_length=160, blank=True)
    additional_business_2_address = models.CharField("Business 2 address", max_length=255, blank=True)

    # --- Certification ---
    borrower_signed_name = models.CharField("Borrower printed name", max_length=160, blank=True)
    borrower_signed_date = models.DateField(null=True, blank=True)
    borrower_signed_place = models.CharField(max_length=120, blank=True)
    borrower_signature = models.ImageField(
        upload_to="loan-applications/signatures/%Y/%m/",
        blank=True,
        null=True,
        help_text="Digital signature of the borrower.",
    )
    coborrower_signed_name = models.CharField("Co-borrower printed name", max_length=160, blank=True)
    coborrower_signed_date = models.DateField(null=True, blank=True)
    coborrower_signed_place = models.CharField(max_length=120, blank=True)
    coborrower_signature = models.ImageField(
        upload_to="loan-applications/signatures/%Y/%m/",
        blank=True,
        null=True,
        help_text="Digital signature of the co-borrower.",
    )

    # --- Office use only ---
    recommended_loan_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    recommended_loan_period = models.CharField(max_length=60, blank=True)
    recommended_by_name = models.CharField(max_length=120, blank=True)
    recommended_by_date = models.DateField(null=True, blank=True)
    validated_by_name = models.CharField(max_length=120, blank=True)
    validated_by_date = models.DateField(null=True, blank=True)
    hold_out_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    insurance_proposed = models.CharField(max_length=20, choices=InsuranceProposed.choices, blank=True)
    office_decision = models.CharField(max_length=20, choices=OfficeDecision.choices, blank=True)
    branch_manager_name = models.CharField(max_length=120, blank=True)
    branch_manager_date = models.DateField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Loan application"
        verbose_name_plural = "Loan applications"

    def __str__(self):
        return f"APP-{self.pk:05d}"

    def save(self, *args, **kwargs):
        if not (self.purpose or "").strip() and self.loan_purpose:
            if self.loan_purpose == self.LoanPurpose.OTHERS:
                self.purpose = self.purpose or "Others"
            else:
                self.purpose = self.get_loan_purpose_display()
        if not (self.purpose or "").strip():
            self.purpose = "KAP loan application"
        super().save(*args, **kwargs)

    @property
    def reference(self):
        return self.form_ref_no or f"APP-{self.pk:05d}"

    @property
    def monthly_estimate(self):
        if not self.loan_product:
            return Decimal("0.00")
        return self.loan_product.estimate_payment(
            self.amount_requested, self.final_term_months or self.term_months
        )

    @property
    def is_ready_for_disbursement(self):
        return self.status == self.Status.APPROVED and not hasattr(self, "loan")

    @property
    def is_editable(self):
        """Officers can edit KAP details until a loan has been released."""
        locked = {
            self.Status.DISBURSED,
            self.Status.ACTIVE,
            self.Status.CLOSED,
            self.Status.DEFAULTED,
        }
        if self.status in locked:
            return False
        return not hasattr(self, "loan")

    @property
    def borrower_name(self):
        parts = [self.borrower_surname, self.borrower_first_name, self.borrower_middle_name]
        kap_name = " ".join(p for p in parts if p).strip()
        return kap_name or self.borrower.display_name()

    @property
    def initials(self):
        return self.borrower.initials

    @property
    def email(self):
        return self.borrower_email or self.borrower.email

    @property
    def monthly_income(self):
        return self.borrower.monthly_income or Decimal("0.00")

    @property
    def amount(self):
        return self.amount_requested

    @property
    def product_name(self):
        return self.loan_product.name if self.loan_product else "Removed product"

    @property
    def submitted_at(self):
        applied = self.applied_on or self.created_at.date()
        return applied.strftime("%b %d, %Y")

    @property
    def received_at(self):
        return self.submitted_at

    @property
    def assigned_officer(self):
        return self.reviewed_by.display_name() if self.reviewed_by else ""

    @property
    def status_label(self):
        return self.get_status_display()

    @property
    def recommendation(self):
        return "Ready for careful review"

    @property
    def recommendation_reason(self):
        return "Review the income profile and supporting documents before deciding."


class CharacterReference(models.Model):
    application = models.ForeignKey(
        LoanApplication, on_delete=models.CASCADE, related_name="character_references"
    )
    name = models.CharField(max_length=160)
    address = models.CharField(max_length=255, blank=True)
    relationship = models.CharField(max_length=80, blank=True)
    contact_number = models.CharField("Contact #", max_length=40, blank=True)
    sort_order = models.PositiveSmallIntegerField(default=1)

    class Meta:
        ordering = ["sort_order", "id"]
        verbose_name = "Character reference"
        verbose_name_plural = "Character references"

    def __str__(self):
        return self.name or f"Reference {self.pk}"


class Loan(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        PAID = "paid", "Paid"
        OVERDUE = "overdue", "Overdue"
        DEFAULTED = "defaulted", "Defaulted"

    application = models.OneToOneField(LoanApplication, on_delete=models.CASCADE, related_name="loan")
    principal = models.DecimalField(max_digits=12, decimal_places=2)
    interest_rate = models.DecimalField(max_digits=5, decimal_places=2)
    term_months = models.PositiveIntegerField()
    disbursed_date = models.DateField(default=timezone.localdate)
    total_payable = models.DecimalField(max_digits=12, decimal_places=2)
    outstanding_balance = models.DecimalField(max_digits=12, decimal_places=2)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)
    disbursement_method = models.CharField(max_length=40, default="Bank transfer")
    disbursement_reference = models.CharField(max_length=80, blank=True)
    processing_fee = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    other_fees = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    other_fees_description = models.CharField(max_length=255, blank=True)
    disbursed_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="disbursed_loans",
    )
    grace_period_days = models.PositiveIntegerField(
        default=0,
        help_text="Days after disbursement before interest starts accruing.",
    )
    disbursed_principal = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Original principal released at disbursement (unchanged by balance extensions).",
    )
    schedule_start_date = models.DateField(
        null=True,
        blank=True,
        help_text="When set, the repayment schedule starts from this date instead of disbursed_date.",
    )
    original_interest_rate = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Interest rate before the first balance-extension reschedule.",
    )
    original_term_months = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Term months before the first balance-extension reschedule.",
    )

    class Meta:
        ordering = ["-disbursed_date", "-id"]

    def __str__(self):
        return f"LN-{self.pk:05d}"

    @property
    def reference(self):
        return f"LN-{self.pk:05d}"

    @property
    def total_paid(self):
        return self.total_payable - self.outstanding_balance

    @property
    def progress_percent(self):
        if not self.total_payable:
            return 0
        return min(100, int((self.total_paid / self.total_payable) * 100))

    @property
    def product_name(self):
        product = self.application.loan_product
        return product.name if product else "Removed product"

    @property
    def original_amount(self):
        return self.disbursed_principal if self.disbursed_principal is not None else self.principal

    @property
    def is_rescheduled(self):
        """True when remaining balance was restructured into a new term."""
        return self.schedule_start_date is not None

    @property
    def reschedule_date_display(self):
        if not self.schedule_start_date:
            return None
        return self.schedule_start_date.strftime("%b %d, %Y")

    @property
    def reschedule_interest_start_display(self):
        """First Monday on/after the reschedule date — when the new term begins collecting."""
        if not self.schedule_start_date:
            return None
        from .services import _interest_start_monday

        return _interest_start_monday(self.schedule_start_date).strftime("%b %d, %Y")

    def original_schedule_terms(self):
        """Principal, rate, and term used for the pre-reschedule schedule."""
        from .services import calculate_flat_loan_amounts

        principal = self.disbursed_principal if self.disbursed_principal is not None else self.principal
        rate = self.original_interest_rate
        term = self.original_term_months
        application = getattr(self, "application", None)
        if rate is None and application is not None:
            rate = application.final_interest_rate
            if rate is None and application.loan_product_id:
                rate = application.loan_product.interest_rate
        if term is None and application is not None:
            term = application.final_term_months or application.term_months
        if rate is None:
            rate = self.interest_rate
        if term is None:
            term = self.term_months
        amounts = calculate_flat_loan_amounts(principal, rate, term)
        return {
            "principal": amounts["principal"],
            "interest_rate": Decimal(str(rate)),
            "term_months": int(term),
            "total_interest": amounts["total_interest"],
            "total_payable": amounts["total_payable"],
            "per_day": amounts["per_day"],
            "per_month": amounts["per_month"],
            "periods": amounts["periods"],
            "start_date": self.disbursed_date,
        }

    @property
    def total_deductions(self):
        return self.processing_fee + self.other_fees

    @property
    def deduction_line_items(self):
        """Prefer the standard fee breakdown when amounts match; otherwise show stored fees."""
        from .services import standard_disbursement_deductions

        standard = standard_disbursement_deductions()
        if (
            self.processing_fee == standard["processing_fee"]
            and self.other_fees == standard["other_fees"]
        ):
            return standard["line_items"]
        items = []
        if self.processing_fee:
            items.append({"label": "Processing fee", "amount": self.processing_fee})
        if self.other_fees:
            label = self.other_fees_description or "Other fees"
            items.append({"label": label, "amount": self.other_fees})
        return items

    @property
    def net_release_amount(self):
        return self.original_amount - self.total_deductions

    @property
    def disbursement_receipt_number(self):
        return f"DIS-{self.pk:05d}"

    @property
    def remaining_balance(self):
        return self.outstanding_balance

    @property
    def adjusted_outstanding_balance(self):
        from .services import adjust_payment

        return adjust_payment(self.outstanding_balance)

    @property
    def adjusted_total_payable(self):
        """Cash-adjusted total payable (single round of the loan total)."""
        from .services import adjust_payment

        return adjust_payment(self.total_payable)

    @property
    def status_label(self):
        return self.get_status_display()

    @property
    def start_date(self):
        return self.disbursed_date.strftime("%b %d, %Y")

    @property
    def payment_frequency(self):
        return self.application.get_payment_frequency_display()

    def _flat_amounts(self):
        from .services import calculate_flat_loan_amounts, loan_term_months

        return calculate_flat_loan_amounts(
            self.principal,
            self.interest_rate,
            loan_term_months(self),
        )

    @property
    def periodic_payment(self):
        """Daily working-day payment (Mon–Fri)."""
        return self._flat_amounts()["per_day"]

    @property
    def daily_payment(self):
        return self._flat_amounts()["per_day"]

    @property
    def adjusted_daily_payment(self):
        from .services import adjust_payment

        return adjust_payment(self.daily_payment)

    @property
    def weekly_payment(self):
        """Five working days (Mon–Fri)."""
        return (self.daily_payment * Decimal("5")).quantize(Decimal("0.01"))

    @property
    def adjusted_weekly_payment(self):
        from .services import adjust_payment

        return adjust_payment(self.weekly_payment)

    @property
    def term_weeks_total(self):
        """Calendar weeks for this term (months × 4). Display only."""
        from .services import loan_term_months, term_weeks_total

        return term_weeks_total(loan_term_months(self))

    @property
    def term_weeks_label(self):
        from .services import loan_term_months, term_weeks_summary

        return term_weeks_summary(loan_term_months(self))["label"]

    @property
    def biweekly_payment(self):
        """Ten working days (two weeks)."""
        return (self.daily_payment * Decimal("10")).quantize(Decimal("0.01"))

    @property
    def adjusted_biweekly_payment(self):
        from .services import adjust_payment

        return adjust_payment(self.biweekly_payment)

    @property
    def monthly_payment(self):
        return self._flat_amounts()["per_month"]

    @property
    def adjusted_monthly_payment(self):
        from .services import adjust_payment

        return adjust_payment(self.monthly_payment)

    def suggested_payment_for(self, frequency, adjust=True):
        """Suggested remittance amount for a pay period, capped at outstanding balance.

        When adjust=True (default), the amount is rounded to a cash-friendly
        multiple of ₱10 via adjust_payment() before capping.
        """
        from .services import WORKING_DAYS_PER_MONTH, adjust_payment

        mapping = {
            "daily": self.daily_payment,
            "weekly": self.weekly_payment,
            "biweekly": self.biweekly_payment,
            "monthly": self.monthly_payment,
        }
        amount = mapping.get(frequency, self.daily_payment)
        if frequency == "monthly" and not amount:
            amount = (self.daily_payment * Decimal(WORKING_DAYS_PER_MONTH)).quantize(Decimal("0.01"))
        outstanding = self.outstanding_balance or Decimal("0.00")
        if outstanding <= 0:
            return Decimal("0.00")
        if adjust:
            amount = adjust_payment(amount)
        return min(amount, outstanding)

    @property
    def total_interest(self):
        return self._flat_amounts()["total_interest"]

    @property
    def working_days(self):
        return self._flat_amounts()["periods"]

    @property
    def next_installment(self):
        return self.installments.exclude(status=Installment.Status.PAID).order_by("due_date").first()

    @property
    def next_payment_date(self):
        item = self.next_installment
        return item.due_date.strftime("%b %d, %Y") if item else "Paid in full"

    @property
    def next_payment_amount(self):
        item = self.next_installment
        return item.remaining if item else Decimal("0.00")

    @property
    def payments_remaining(self):
        return self.installments.exclude(status=Installment.Status.PAID).count()

    @property
    def maturity_date(self):
        items = self.installments.order_by("-due_date")
        last = items.first()
        return last.due_date.strftime("%b %d, %Y") if last else "Not scheduled"

    @property
    def paid_percentage(self):
        return self.progress_percent

    @property
    def payments_made(self):
        return self.payments.count()

    @property
    def next_step_text(self):
        item = self.next_installment
        return f"Your next payment of ₱{item.remaining:,.2f} is due {item.due_date:%b %d}." if item else "Your loan is paid in full."


class Disbursement(Loan):
    """Proxy for Django admin — a released loan disbursement."""

    class Meta:
        proxy = True
        verbose_name = "Disbursement"
        verbose_name_plural = "Disbursements"


class DisbursementSetting(models.Model):
    """Singleton — which weekday/time fund releases are allowed, and whether the rule is on."""

    class Weekday(models.IntegerChoices):
        MONDAY = 0, "Monday"
        TUESDAY = 1, "Tuesday"
        WEDNESDAY = 2, "Wednesday"
        THURSDAY = 3, "Thursday"
        FRIDAY = 4, "Friday"
        SATURDAY = 5, "Saturday"
        SUNDAY = 6, "Sunday"

    disbursement_weekday = models.PositiveSmallIntegerField(
        choices=Weekday.choices,
        default=Weekday.FRIDAY,
        help_text="Calendar day of the week when loan fund releases are allowed.",
    )
    disbursement_start_time = models.TimeField(
        default=time(8, 0),
        help_text="Local time when officers may start releasing funds on the selected weekday.",
    )
    condition_enabled = models.BooleanField(
        default=True,
        help_text="When enabled, officers can only disburse on the selected weekday after "
        "the start time. When disabled, disbursement is allowed any day.",
    )

    class Meta:
        verbose_name = "Disbursement setting"
        verbose_name_plural = "Disbursement settings"

    def __str__(self):
        day = self.get_disbursement_weekday_display()
        state = "enabled" if self.condition_enabled else "disabled"
        return f"Disburse on {day} from {self.start_time_display()} ({state})"

    def start_time_display(self):
        value = self.disbursement_start_time or time(8, 0)
        formatted = value.strftime("%I:%M %p")
        return formatted.lstrip("0") if formatted.startswith("0") else formatted

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        pass

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(
            pk=1,
            defaults={
                "disbursement_weekday": cls.Weekday.FRIDAY,
                "disbursement_start_time": time(8, 0),
                "condition_enabled": True,
            },
        )
        return obj


class Installment(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        PAID = "paid", "Paid"
        OVERDUE = "overdue", "Overdue"

    loan = models.ForeignKey(Loan, on_delete=models.CASCADE, related_name="installments")
    installment_number = models.PositiveIntegerField()
    due_date = models.DateField()
    principal_component = models.DecimalField(max_digits=12, decimal_places=2)
    interest_component = models.DecimalField(max_digits=12, decimal_places=2)
    amount_due = models.DecimalField(max_digits=12, decimal_places=2)
    amount_paid = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    paid_date = models.DateField(null=True, blank=True)
    credit_penalty_applied = models.BooleanField(
        default=False,
        help_text="True when the −0.1 credit-score penalty for this loan month has already been applied.",
    )

    class Meta:
        ordering = ["installment_number"]
        constraints = [
            models.UniqueConstraint(fields=["loan", "installment_number"], name="unique_loan_installment")
        ]

    @property
    def remaining(self):
        return max(Decimal("0.00"), self.amount_due - self.amount_paid)

    @property
    def adjusted_remaining(self):
        from .services import adjust_payment

        return adjust_payment(self.remaining)

    def mark_overdue_if_needed(self):
        if self.status == self.Status.PENDING and self.due_date < timezone.localdate():
            self.status = self.Status.OVERDUE
            self.save(update_fields=["status"])

    @property
    def amount(self):
        return self.amount_due

    @property
    def adjusted_amount(self):
        from .services import adjust_payment

        return adjust_payment(self.amount_due)

    @property
    def principal(self):
        return self.principal_component

    @property
    def interest(self):
        return self.interest_component

    @property
    def status_label(self):
        return self.get_status_display()

    @property
    def is_next(self):
        return self.status in [self.Status.PENDING, self.Status.OVERDUE] and not self.loan.installments.filter(
            installment_number__lt=self.installment_number, status__in=[self.Status.PENDING, self.Status.OVERDUE]
        ).exists()

    @property
    def description(self):
        return "Principal and interest for this payment period."


class Payment(models.Model):
    class Method(models.TextChoices):
        BANK_TRANSFER = "bank_transfer", "Bank transfer"
        GCASH = "gcash", "GCash"
        CASH = "cash", "Cash"
        CARD = "card", "Card"

    loan = models.ForeignKey(Loan, on_delete=models.CASCADE, related_name="payments")
    installment = models.ForeignKey(Installment, on_delete=models.SET_NULL, null=True, blank=True, related_name="payments")
    amount = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))])
    savings_adjustment = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        help_text="Cash-rounding surplus (adjusted − exact) credited to the member's savings.",
    )
    mutual_aid_contribution = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        help_text="Portion of this remittance credited to KAP mutual aid (₱15 × working days).",
    )
    payment_date = models.DateField(default=timezone.localdate)
    method = models.CharField(max_length=30, choices=Method.choices, default=Method.BANK_TRANSFER)
    reference_number = models.CharField(max_length=80, default="", blank=True)
    recorded_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="recorded_payments")

    def __str__(self):
        return self.reference_number or f"PAY-{self.pk:05d}"

    @property
    def loan_amount_applied(self):
        """Portion applied to the loan (excludes savings surplus and mutual aid)."""
        extras = (self.savings_adjustment or Decimal("0.00")) + (
            self.mutual_aid_contribution or Decimal("0.00")
        )
        return max(Decimal("0.00"), self.amount - extras)


class ExpiredMonthSignature(models.Model):
    """Borrower acknowledgment that a loan month’s payment period has expired unpaid."""

    loan = models.ForeignKey(Loan, on_delete=models.CASCADE, related_name="expired_month_signatures")
    month_number = models.PositiveIntegerField()
    start_date = models.DateField()
    end_date = models.DateField()
    remaining_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    signed_name = models.CharField(max_length=160, blank=True)
    signed_at = models.DateTimeField(default=timezone.now)
    signature = models.ImageField(upload_to="loan-expired-months/signatures/%Y/%m/")
    recorded_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="recorded_expired_month_signatures",
    )

    class Meta:
        ordering = ["month_number", "start_date", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["loan", "month_number", "start_date"],
                name="unique_expired_month_signature",
            )
        ]

    def __str__(self):
        return f"{self.loan_id} · month {self.month_number} signed"


class Notification(models.Model):
    class Kind(models.TextChoices):
        CREDIT_SCORE = "credit_score", "Credit score"
        GENERAL = "general", "General"

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="notifications")
    kind = models.CharField(max_length=30, choices=Kind.choices, default=Kind.GENERAL)
    title = models.CharField(max_length=160)
    message = models.TextField()
    link_url = models.CharField(max_length=255, blank=True)
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"{self.title} — {self.user}"


class Document(models.Model):
    class DocType(models.TextChoices):
        VALID_ID = "valid_id", "Valid ID"
        PROOF_OF_INCOME = "proof_of_income", "Proof of income"
        OTHER = "other", "Other"

    IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "gif", "webp"}

    application = models.ForeignKey(LoanApplication, on_delete=models.CASCADE, related_name="documents")
    doc_type = models.CharField(max_length=30, choices=DocType.choices)
    file = models.FileField(upload_to="loan-documents/%Y/%m/")
    uploaded_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.get_doc_type_display()} — {self.application.reference}"

    @property
    def filename(self):
        if not self.file:
            return ""
        return self.file.name.rsplit("/", 1)[-1]

    @property
    def extension(self):
        name = self.filename.lower()
        return name.rsplit(".", 1)[-1] if "." in name else ""

    @property
    def is_image(self):
        return self.extension in self.IMAGE_EXTENSIONS

    @property
    def is_pdf(self):
        return self.extension == "pdf"

    @property
    def name(self):
        return self.get_doc_type_display()

    @property
    def status(self):
        return "approved"

    @property
    def status_label(self):
        return "Uploaded"

    @property
    def file_size(self):
        try:
            size = self.file.size
            return f"{size / 1024:.0f} KB"
        except (OSError, ValueError):
            return "Uploaded file"

    @property
    def download_url(self):
        if not self.pk:
            return self.file.url if self.file else ""
        from django.urls import reverse

        return reverse("application_document", kwargs={"document_id": self.pk})


class Features(models.Model):
    """Singleton site branding — store name, tagline, and logo."""

    DEFAULT_STORE_NAME = "KAP"
    DEFAULT_TAGLINE = "Kaakibat ang Pag-unlad Microfinancing Inc."
    DEFAULT_LOGO_STATIC = "branding/kap_logo.png"
    DEFAULT_LOGO_MARK_STATIC = "branding/kap_logo_mark.png"
    DEFAULT_LOGO_MEDIA = "branding/KAP_logo_transparent_1_csXgoGL.png"

    store_name = models.CharField(max_length=120, default=DEFAULT_STORE_NAME)
    tagline = models.CharField(max_length=120, default=DEFAULT_TAGLINE, blank=True)
    logo = models.ImageField(upload_to="branding/", blank=True, null=True)
    daily_mutual_aid_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("15.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
        help_text="Compulsory mutual aid collected with every working-day remittance (₱). "
        "Weekly = ×5, biweekly = ×10, monthly = ×22.",
    )

    class Meta:
        verbose_name = "Features"
        verbose_name_plural = "Features"

    def __str__(self):
        return self.store_name

    def save(self, *args, **kwargs):
        self.pk = 1
        if self.logo and not self.logo._committed:
            from .logo_utils import strip_logo_background

            self.logo = strip_logo_background(self.logo)
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        pass

    @classmethod
    def default_logo_source_path(cls):
        """Absolute path to the bundled KAP logo (works before/after collectstatic)."""
        from pathlib import Path

        from django.conf import settings
        from django.contrib.staticfiles import finders

        found = finders.find(cls.DEFAULT_LOGO_STATIC)
        if found:
            return Path(found if isinstance(found, str) else found[0])

        for candidate in (
            Path(settings.BASE_DIR) / "static" / cls.DEFAULT_LOGO_STATIC,
            *(Path(root) / cls.DEFAULT_LOGO_STATIC for root in getattr(settings, "STATICFILES_DIRS", ())),
            Path(settings.STATIC_ROOT) / cls.DEFAULT_LOGO_STATIC,
        ):
            if candidate.is_file():
                return candidate
        return None

    @classmethod
    def _ensure_default_logo(cls, obj):
        """Copy the bundled KAP logo into media when Features has no logo file."""
        from pathlib import Path
        from shutil import copyfile

        from django.conf import settings

        if obj.logo and obj.logo.name:
            media_path = Path(settings.MEDIA_ROOT) / obj.logo.name
            if media_path.is_file():
                return obj

        media_root = Path(settings.MEDIA_ROOT)
        target = media_root / cls.DEFAULT_LOGO_MEDIA
        if not target.is_file():
            source = cls.default_logo_source_path()
            if source is None:
                return obj
            target.parent.mkdir(parents=True, exist_ok=True)
            copyfile(source, target)

        if not obj.logo or obj.logo.name != cls.DEFAULT_LOGO_MEDIA:
            obj.logo.name = cls.DEFAULT_LOGO_MEDIA
            obj.save(update_fields=["logo"])
        return obj

    @classmethod
    def load(cls):
        obj, created = cls.objects.get_or_create(
            pk=1,
            defaults={
                "store_name": cls.DEFAULT_STORE_NAME,
                "tagline": cls.DEFAULT_TAGLINE,
            },
        )
        update_fields = []
        # Keep KAP branding as the system default on every host, including
        # production DBs that still have the old Harborline placeholders.
        legacy_names = {"", "Harborline"}
        legacy_taglines = {"", "Lending workspace"}
        if created or obj.store_name in legacy_names:
            if obj.store_name != cls.DEFAULT_STORE_NAME:
                obj.store_name = cls.DEFAULT_STORE_NAME
                update_fields.append("store_name")
        if created or obj.tagline in legacy_taglines:
            if obj.tagline != cls.DEFAULT_TAGLINE:
                obj.tagline = cls.DEFAULT_TAGLINE
                update_fields.append("tagline")
        if update_fields:
            obj.save(update_fields=update_fields)
        return cls._ensure_default_logo(obj)

    def resolved_logo_url(self):
        """Public logo URL; default KAP logo is served from static (WhiteNoise-safe)."""
        from pathlib import Path

        from django.conf import settings
        from django.templatetags.static import static

        # Bundled default must not depend on /media (often unmapped on PaaS hosts).
        logo_name = (self.logo.name if self.logo else "") or ""
        if (
            not logo_name
            or logo_name == self.DEFAULT_LOGO_MEDIA
            or logo_name.startswith("branding/KAP_logo")
        ):
            return static(self.DEFAULT_LOGO_STATIC)

        media_path = Path(settings.MEDIA_ROOT) / logo_name
        if media_path.is_file():
            try:
                return self.logo.url
            except ValueError:
                pass
        return static(self.DEFAULT_LOGO_STATIC)

    def resolved_logo_mark_url(self):
        """Square mark URL; bundled static mark is the production-safe default."""
        from pathlib import Path

        from django.conf import settings
        from django.templatetags.static import static

        from .logo_utils import logo_mark_url

        logo_name = (self.logo.name if self.logo else "") or ""
        if (
            not logo_name
            or logo_name == self.DEFAULT_LOGO_MEDIA
            or logo_name.startswith("branding/KAP_logo")
        ):
            return static(self.DEFAULT_LOGO_MARK_STATIC)

        mark = logo_mark_url(self.logo)
        if mark and settings.DEBUG:
            relative = mark[len(settings.MEDIA_URL) :] if mark.startswith(settings.MEDIA_URL) else None
            if relative and (Path(settings.MEDIA_ROOT) / relative).is_file():
                return mark
        return static(self.DEFAULT_LOGO_MARK_STATIC)


class ActivityLog(models.Model):
    """Append-only staff audit trail. Rows are kept even if the related record is later changed or deleted."""

    class Kind(models.TextChoices):
        MEMBER = "member", "Member"
        APPLICATION = "application", "Application"
        PAYMENT = "payment", "Pay collection"
        DISBURSEMENT = "disbursement", "Disbursement"
        SAVINGS = "savings", "Savings"
        MUTUAL_AID = "mutual_aid", "Mutual aid"
        ACCOUNT = "account", "Account"
        SECURITY = "security", "Security"

    class Action(models.TextChoices):
        MEMBER_CREATED = "member_created", "Member created"
        MEMBER_UPDATED = "member_updated", "Member updated"
        APPLICATION_CREATED = "application_created", "Application created"
        APPLICATION_UPDATED = "application_updated", "Application updated"
        APPLICATION_APPROVED = "application_approved", "Application approved"
        APPLICATION_REJECTED = "application_rejected", "Application rejected"
        APPLICATION_INFO_REQUESTED = "application_info_requested", "More information requested"
        APPLICATION_DELETED = "application_deleted", "Application deleted"
        REVIEW_NOTE = "review_note", "Review note added"
        PAYMENT_RECORDED = "payment_recorded", "Pay collection"
        LOAN_DISBURSED = "loan_disbursed", "Loan disbursed"
        BALANCE_EXTENDED = "balance_extended", "Balance extended"
        EXPIRED_MONTH_SIGNED = "expired_month_signed", "Expired month signed"
        OFFICER_CREATED = "officer_created", "Officer created"
        OFFICER_UPDATED = "officer_updated", "Officer updated"
        MANAGER_CREATED = "manager_created", "Manager created"
        MANAGER_UPDATED = "manager_updated", "Manager updated"
        PRODUCT_CREATED = "product_created", "Product created"
        PRODUCT_UPDATED = "product_updated", "Product updated"
        SAVINGS_OPENED = "savings_opened", "Savings account opened"
        SAVINGS_DEPOSIT = "savings_deposit", "Savings deposit"
        SAVINGS_WITHDRAWAL = "savings_withdrawal", "Savings withdrawal"
        SAVINGS_CLOSED = "savings_closed", "Savings account closed"
        MUTUAL_AID_ENROLLED = "mutual_aid_enrolled", "Mutual aid enrolled"
        MUTUAL_AID_CONTRIBUTION = "mutual_aid_contribution", "Mutual aid contribution"
        MUTUAL_AID_CLAIM = "mutual_aid_claim", "Mutual aid claim submitted"
        MUTUAL_AID_CLAIM_REVIEWED = "mutual_aid_claim_reviewed", "Mutual aid claim reviewed"
        MUTUAL_AID_CLAIM_DISBURSED = "mutual_aid_claim_disbursed", "Mutual aid claim disbursed"
        MEMBERSHIP_STATUS = "membership_status", "Membership status changed"
        PROFILE_UPDATED = "profile_updated", "Profile updated"
        DATA_EXPORTED = "data_exported", "Data exported"
        DATA_IMPORTED = "data_imported", "Data imported"
        SIGNED_IN = "signed_in", "Signed in"
        SIGNED_OUT = "signed_out", "Signed out"
        SIGN_IN_FAILED = "sign_in_failed", "Failed sign-in"

    actor = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="activity_logs",
    )
    action = models.CharField(max_length=40, choices=Action.choices)
    kind = models.CharField(max_length=20, choices=Kind.choices, db_index=True)
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    member = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="subject_activity_logs",
    )
    member_name = models.CharField(max_length=160, blank=True)
    reference = models.CharField(max_length=80, blank=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    status = models.CharField(max_length=30, blank=True)
    status_label = models.CharField(max_length=80, blank=True)
    url_name = models.CharField(max_length=80, blank=True)
    url_kwargs = models.JSONField(default=dict, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=255, blank=True)
    source_key = models.CharField(max_length=80, null=True, blank=True, unique=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["actor", "created_at"]),
            models.Index(fields=["actor", "kind", "created_at"]),
        ]

    def __str__(self):
        who = self.actor.display_name() if self.actor_id else "Unknown"
        return f"{who} · {self.title}"

    def as_event(self):
        borrower_id = None
        if self.member_id and self.member and self.member.role == User.Role.MEMBER and not self.member.is_staff:
            borrower_id = self.member_id
        return {
            "kind": self.kind,
            "title": self.title,
            "description": self.description,
            "member_name": self.member_name,
            "borrower_id": borrower_id,
            "reference": self.reference,
            "amount": self.amount,
            "created_at": self.created_at,
            "status": self.status or "neutral",
            "status_label": self.status_label or self.get_action_display(),
            "url_name": self.url_name,
            "url_kwargs": self.url_kwargs or {},
            "ip_address": self.ip_address,
            "user_agent": self.user_agent,
            "device": self._device_label(),
        }

    def _device_label(self):
        from .audit import browser_label

        return browser_label(self.user_agent)


class LoginLogoutLog(models.Model):
    """Append-only login and logout records, copied into Activity history for staff monitoring."""

    class Event(models.TextChoices):
        LOGIN = "login", "Login"
        LOGOUT = "logout", "Logout"
        LOGIN_FAILED = "login_failed", "Failed login"

    user = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="login_logout_logs",
    )
    event = models.CharField(max_length=20, choices=Event.choices, db_index=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=255, blank=True)
    session_key = models.CharField(max_length=40, blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        verbose_name = "Login / logout log"
        verbose_name_plural = "Login / logout logs"
        indexes = [
            models.Index(fields=["user", "created_at"]),
            models.Index(fields=["user", "event", "created_at"]),
        ]

    def __str__(self):
        who = self.user.display_name() if self.user_id else "Unknown"
        return f"{who} · {self.get_event_display()}"