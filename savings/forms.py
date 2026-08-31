from decimal import Decimal

from django import forms
from django.utils import timezone

from .models import SavingsAccount, SavingsProduct, SavingsTransaction
from .reports import default_export_dates


class SavingsProductForm(forms.ModelForm):
    class Meta:
        model = SavingsProduct
        fields = ("name", "description", "interest_rate", "interest_term_months", "min_balance", "min_deposit", "is_active")
        widgets = {
            "name": forms.TextInput(attrs={"class": "form-control", "placeholder": "Regular savings"}),
            "description": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
            "interest_rate": forms.NumberInput(attrs={"class": "form-control", "step": "0.01", "min": "0"}),
            "interest_term_months": forms.NumberInput(attrs={"class": "form-control", "min": "1", "max": "120", "step": "1"}),
            "min_balance": forms.NumberInput(attrs={"class": "form-control", "step": "0.01", "min": "0"}),
            "min_deposit": forms.NumberInput(attrs={"class": "form-control", "step": "0.01", "min": "0.01"}),
            "is_active": forms.CheckboxInput(attrs={"class": "form-check-input"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["interest_term_months"].label = "Interest credit term (months)"
        self.fields["interest_term_months"].help_text = "Interest is credited to accounts on this schedule (e.g. 1 = monthly, 12 = yearly)."


def unavailable_savings_product_ids_for_member(member):
    """Product IDs the member already has an active savings account for."""
    if not member:
        return set()
    return {
        pk
        for pk in SavingsAccount.objects.filter(
            member=member,
            status=SavingsAccount.Status.ACTIVE,
        ).values_list("product_id", flat=True)
        if pk
    }


def available_savings_products_for_member(member):
    """Active savings products a member can open."""
    products = SavingsProduct.objects.filter(is_active=True)
    blocked = unavailable_savings_product_ids_for_member(member)
    if blocked:
        products = products.exclude(pk__in=blocked)
    return products


class OpenAccountForm(forms.Form):
    product = forms.ModelChoiceField(
        queryset=SavingsProduct.objects.filter(is_active=True),
        widget=forms.Select(attrs={"class": "form-select"}),
        empty_label="Choose a savings product",
    )
    initial_deposit = forms.DecimalField(
        required=False,
        min_value=Decimal("0"),
        widget=forms.NumberInput(attrs={"class": "form-control", "step": "0.01", "min": "0", "placeholder": "0.00"}),
        label="Initial deposit (optional)",
    )

    def clean(self):
        cleaned = super().clean()
        product = cleaned.get("product")
        initial_deposit = cleaned.get("initial_deposit") or Decimal("0")
        if product and initial_deposit > 0 and initial_deposit < product.min_deposit:
            self.add_error(
                "initial_deposit",
                f"Initial deposit must be at least ₱{product.min_deposit:,.2f} for {product.name}.",
            )
        return cleaned


class SavingsTransactionForm(forms.Form):
    amount = forms.DecimalField(
        min_value=Decimal("0.01"),
        widget=forms.NumberInput(attrs={"class": "form-control", "step": "0.01", "min": "0.01"}),
    )
    method = forms.ChoiceField(
        choices=SavingsTransaction.Method.choices,
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

    def __init__(self, *args, account=None, transaction_type="deposit", **kwargs):
        self.account = account
        self.transaction_type = transaction_type
        super().__init__(*args, **kwargs)
        if transaction_type == "withdrawal" and account:
            self.fields["amount"].widget.attrs["max"] = str(account.balance)

    def clean_amount(self):
        amount = self.cleaned_data.get("amount")
        if amount is None or not self.account:
            return amount
        if self.transaction_type == "withdrawal" and amount > self.account.balance:
            raise forms.ValidationError(
                f"Amount cannot exceed your available balance of ₱{self.account.balance:,.2f}."
            )
        if self.transaction_type == "deposit" and amount < self.account.product.min_deposit:
            if self.account.balance == 0:
                raise forms.ValidationError(
                    f"First deposit must be at least ₱{self.account.product.min_deposit:,.2f}."
                )
        return amount


class OfficerSavingsTransactionForm(forms.Form):
    action = forms.ChoiceField(
        choices=[("deposit", "Deposit"), ("withdraw", "Withdraw")],
        widget=forms.Select(attrs={"class": "form-select"}),
        initial="deposit",
    )
    amount = forms.DecimalField(
        min_value=Decimal("0.01"),
        widget=forms.NumberInput(attrs={"class": "form-control", "step": "0.01", "min": "0.01"}),
    )
    method = forms.ChoiceField(
        choices=SavingsTransaction.Method.choices,
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

    def __init__(self, *args, account=None, **kwargs):
        self.account = account
        super().__init__(*args, **kwargs)

    def clean(self):
        cleaned = super().clean()
        amount = cleaned.get("amount")
        action = cleaned.get("action")
        if amount is None or not self.account or not action:
            return cleaned
        if action == "withdraw":
            if amount > self.account.balance:
                self.add_error(
                    "amount",
                    f"Amount cannot exceed the available balance of ₱{self.account.balance:,.2f}.",
                )
            remaining = self.account.balance - amount
            if remaining > 0 and remaining < self.account.product.min_balance:
                self.add_error(
                    "amount",
                    f"Withdrawal would leave a balance below the minimum of ₱{self.account.product.min_balance:,.2f}.",
                )
        elif action == "deposit" and self.account.balance == 0 and amount < self.account.product.min_deposit:
            self.add_error(
                "amount",
                f"First deposit must be at least ₱{self.account.product.min_deposit:,.2f}.",
            )
        return cleaned


class OfficerOpenAccountForm(forms.Form):
    member = forms.ModelChoiceField(
        queryset=None,
        label="Member",
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    product = forms.ModelChoiceField(
        queryset=SavingsProduct.objects.filter(is_active=True),
        label="Savings product",
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    initial_deposit = forms.DecimalField(
        required=False,
        min_value=Decimal("0"),
        widget=forms.NumberInput(attrs={"class": "form-control", "step": "0.01", "min": "0"}),
        label="Initial deposit (optional)",
    )
    opened_on = forms.DateField(
        label="Account open date",
        widget=forms.DateInput(attrs={"class": "form-control", "type": "date"}),
        initial=timezone.localdate,
        help_text="The date this account is considered opened.",
    )

    def __init__(self, *args, **kwargs):
        from lending.models import User

        super().__init__(*args, **kwargs)
        self.fields["member"].queryset = User.objects.filter(role=User.Role.MEMBER, is_active=True).order_by("full_name", "username")

    def clean(self):
        cleaned = super().clean()
        member = cleaned.get("member")
        product = cleaned.get("product")
        initial_deposit = cleaned.get("initial_deposit") or Decimal("0")
        if member and product:
            if product.pk in unavailable_savings_product_ids_for_member(member):
                self.add_error("product", f"{member.display_name()} already has an active {product.name} account.")
        if product and initial_deposit > 0 and initial_deposit < product.min_deposit:
            self.add_error(
                "initial_deposit",
                f"Initial deposit must be at least ₱{product.min_deposit:,.2f}.",
            )
        opened_on = cleaned.get("opened_on")
        if opened_on and opened_on > timezone.localdate():
            self.add_error("opened_on", "Account open date cannot be in the future.")
        return cleaned


class SavingsInterestExportForm(forms.Form):
    date_from = forms.DateField(
        label="From",
        widget=forms.DateInput(attrs={"class": "form-control", "type": "date"}),
    )
    date_to = forms.DateField(
        label="To",
        widget=forms.DateInput(attrs={"class": "form-control", "type": "date"}),
    )
    format = forms.ChoiceField(
        choices=[("csv", "CSV"), ("pdf", "PDF")],
        widget=forms.HiddenInput(),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        date_from, date_to = default_export_dates()
        if not self.is_bound:
            self.fields["date_from"].initial = date_from
            self.fields["date_to"].initial = date_to

    def clean(self):
        cleaned = super().clean()
        date_from = cleaned.get("date_from")
        date_to = cleaned.get("date_to")
        if date_from and date_to:
            if date_from > date_to:
                self.add_error("date_to", "End date must be on or after the start date.")
            elif date_to > timezone.localdate():
                self.add_error("date_to", "End date cannot be in the future.")
        return cleaned
