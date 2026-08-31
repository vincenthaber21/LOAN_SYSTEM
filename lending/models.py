from decimal import Decimal
from datetime import timedelta
from uuid import uuid4

from django.contrib.auth.models import AbstractUser, UserManager
from django.core.validators import MinValueValidator
from django.db import models
from django.utils import timezone


class User(AbstractUser):
    class Role(models.TextChoices):
        MEMBER = "member", "Member"
        OFFICER = "officer", "Loan Officer"
        ADMIN = "admin", "Admin"

    role = models.CharField(max_length=20, choices=Role.choices, default=Role.MEMBER)
    full_name = models.CharField(max_length=160, blank=True)
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
        return self.role in {self.Role.OFFICER, self.Role.ADMIN} or self.is_staff

    @property
    def is_member(self):
        return self.role == self.Role.MEMBER and not self.is_staff

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
    interest_rate = models.DecimalField(max_digits=5, decimal_places=2, help_text="Annual percentage")
    min_term_months = models.PositiveIntegerField(default=3)
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
        principal = Decimal(str(amount))
        periods = int(term_months)
        monthly_rate = (self.interest_rate / Decimal("100")) / Decimal("12")
        if monthly_rate == 0:
            return (principal / periods).quantize(Decimal("0.01"))
        factor = (Decimal("1") + monthly_rate) ** periods
        payment = principal * monthly_rate * factor / (factor - Decimal("1"))
        return payment.quantize(Decimal("0.01"))

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
        MONTHLY = "monthly", "Monthly"
        BIWEEKLY = "biweekly", "Biweekly"

    borrower = models.ForeignKey(User, on_delete=models.CASCADE, related_name="loan_applications")
    loan_product = models.ForeignKey(
        LoanProduct,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="applications",
    )
    amount_requested = models.DecimalField(max_digits=12, decimal_places=2)
    purpose = models.TextField()
    term_months = models.PositiveIntegerField()
    payment_frequency = models.CharField(max_length=20, choices=PaymentFrequency.choices, default=PaymentFrequency.MONTHLY)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    applied_on = models.DateField(null=True, blank=True, help_text="Date the borrower applied for this loan.")
    created_at = models.DateTimeField(auto_now_add=True)
    reviewed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="reviewed_applications")
    review_notes = models.TextField(blank=True)
    decision_date = models.DateTimeField(null=True, blank=True)
    final_interest_rate = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    final_term_months = models.PositiveIntegerField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"APP-{self.pk:05d}"

    @property
    def reference(self):
        return f"APP-{self.pk:05d}"

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
    def borrower_name(self):
        return self.borrower.display_name()

    @property
    def initials(self):
        return self.borrower.initials

    @property
    def email(self):
        return self.borrower.email

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
    other_fees_description = models.CharField(max_length=120, blank=True)
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
        return self.principal

    @property
    def total_deductions(self):
        return self.processing_fee + self.other_fees

    @property
    def net_release_amount(self):
        return self.principal - self.total_deductions

    @property
    def disbursement_receipt_number(self):
        return f"DIS-{self.pk:05d}"

    @property
    def remaining_balance(self):
        return self.outstanding_balance

    @property
    def status_label(self):
        return self.get_status_display()

    @property
    def start_date(self):
        return self.disbursed_date.strftime("%b %d, %Y")

    @property
    def payment_frequency(self):
        return self.application.get_payment_frequency_display()

    @property
    def periodic_payment(self):
        from .services import calculate_periodic_payment, loan_term_months

        payment, _ = calculate_periodic_payment(
            self.principal,
            self.interest_rate,
            loan_term_months(self),
            self.application.payment_frequency,
        )
        return payment

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

    class Meta:
        ordering = ["installment_number"]
        constraints = [
            models.UniqueConstraint(fields=["loan", "installment_number"], name="unique_loan_installment")
        ]

    @property
    def remaining(self):
        return max(Decimal("0.00"), self.amount_due - self.amount_paid)

    def mark_overdue_if_needed(self):
        if self.status == self.Status.PENDING and self.due_date < timezone.localdate():
            self.status = self.Status.OVERDUE
            self.save(update_fields=["status"])

    @property
    def amount(self):
        return self.amount_due

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
    payment_date = models.DateField(default=timezone.localdate)
    method = models.CharField(max_length=30, choices=Method.choices, default=Method.BANK_TRANSFER)
    reference_number = models.CharField(max_length=80, default="", blank=True)
    recorded_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="recorded_payments")

    def __str__(self):
        return self.reference_number or f"PAY-{self.pk:05d}"


class Document(models.Model):
    class DocType(models.TextChoices):
        VALID_ID = "valid_id", "Valid ID"
        PROOF_OF_INCOME = "proof_of_income", "Proof of income"
        OTHER = "other", "Other"

    application = models.ForeignKey(LoanApplication, on_delete=models.CASCADE, related_name="documents")
    doc_type = models.CharField(max_length=30, choices=DocType.choices)
    file = models.FileField(upload_to="loan-documents/%Y/%m/")
    uploaded_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.get_doc_type_display()} — {self.application.reference}"

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
        return self.file.url


class Features(models.Model):
    """Singleton site branding — store name, tagline, and logo."""

    store_name = models.CharField(max_length=120, default="Harborline")
    tagline = models.CharField(max_length=120, default="Lending workspace", blank=True)
    logo = models.ImageField(upload_to="branding/", blank=True, null=True)

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
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj