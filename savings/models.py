from decimal import Decimal
from uuid import uuid4

from django.conf import settings
from django.core.validators import MinValueValidator, MaxValueValidator
from django.db import models
from django.utils import timezone


class SavingsProduct(models.Model):
    name = models.CharField(max_length=120)
    description = models.TextField(blank=True)
    interest_rate = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("2.00"),
        validators=[MinValueValidator(Decimal("0"))],
        help_text="Annual interest rate (%)",
    )
    min_balance = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("500.00"),
        validators=[MinValueValidator(Decimal("0"))],
    )
    min_deposit = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("100.00"),
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    interest_term_months = models.PositiveSmallIntegerField(
        default=1,
        validators=[MinValueValidator(1), MaxValueValidator(120)],
        help_text="How often interest is credited to the account, in months.",
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    @property
    def interest_credit_label(self):
        months = self.interest_term_months
        unit = "month" if months == 1 else "months"
        return f"Every {months} {unit}"


class SavingsAccount(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        CLOSED = "closed", "Closed"
        FROZEN = "frozen", "Frozen"

    member = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="savings_accounts",
    )
    product = models.ForeignKey(
        SavingsProduct,
        on_delete=models.PROTECT,
        related_name="accounts",
    )
    account_number = models.CharField(max_length=20, unique=True, editable=False)
    balance = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0"))],
    )
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)
    opened_at = models.DateTimeField(default=timezone.now)
    opened_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="savings_accounts_opened",
    )
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-opened_at"]

    def __str__(self):
        return f"{self.reference} · {self.member.display_name()}"

    def save(self, *args, **kwargs):
        if not self.account_number:
            self.account_number = f"SV{uuid4().hex[:10].upper()}"
        super().save(*args, **kwargs)

    @property
    def reference(self):
        return self.account_number

    @property
    def product_name(self):
        return self.product.name

    @property
    def status_label(self):
        return self.get_status_display()

    @property
    def is_operational(self):
        return self.status == self.Status.ACTIVE


class SavingsTransaction(models.Model):
    class Type(models.TextChoices):
        DEPOSIT = "deposit", "Deposit"
        WITHDRAWAL = "withdrawal", "Withdrawal"
        INTEREST = "interest", "Interest"

    class Method(models.TextChoices):
        CASH = "cash", "Cash"
        BANK_TRANSFER = "bank_transfer", "Bank transfer"
        CHECK = "check", "Check"
        ONLINE = "online", "Online"

    account = models.ForeignKey(
        SavingsAccount,
        on_delete=models.PROTECT,
        related_name="transactions",
    )
    transaction_type = models.CharField(max_length=20, choices=Type.choices)
    amount = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))])
    method = models.CharField(max_length=20, choices=Method.choices, default=Method.CASH)
    reference_number = models.CharField(max_length=60, blank=True)
    balance_after = models.DecimalField(max_digits=12, decimal_places=2)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="savings_transactions_recorded",
    )

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["account", "transaction_type", "-created_at"]),
        ]

    def __str__(self):
        return f"{self.get_transaction_type_display()} · {self.reference_number or self.pk}"

    @property
    def type_label(self):
        return self.get_transaction_type_display()

    @property
    def is_credit(self):
        return self.transaction_type in {self.Type.DEPOSIT, self.Type.INTEREST}
