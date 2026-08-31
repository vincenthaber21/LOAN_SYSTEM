import re
import unicodedata
from decimal import Decimal

from django import forms
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm
from django.utils import timezone

from .models import Disbursement, Document, Loan, LoanApplication, LoanProduct, Payment, User
from .services import credit_score_blocks_loans, credit_score_loan_block_message

PENDING_APPLICATION_STATUSES = [
    LoanApplication.Status.DRAFT,
    LoanApplication.Status.SUBMITTED,
    LoanApplication.Status.UNDER_REVIEW,
    LoanApplication.Status.APPROVED,
]

OPEN_APPLICATION_STATUSES = PENDING_APPLICATION_STATUSES


def _style_form_fields(form):
    for field in form.fields.values():
        widget = field.widget
        if isinstance(widget, (forms.Select, forms.SelectMultiple)):
            widget.attrs.setdefault("class", "form-select")
        else:
            widget.attrs.setdefault("class", "form-control")


def unavailable_product_ids_for_borrower(borrower):
    """Product IDs the borrower cannot apply for (active loan or open application)."""
    if not borrower:
        return set()
    active_product_ids = Loan.objects.filter(
        application__borrower=borrower,
        status__in=[Loan.Status.ACTIVE, Loan.Status.OVERDUE],
    ).values_list("application__loan_product_id", flat=True)
    pending_product_ids = LoanApplication.objects.filter(
        borrower=borrower,
        status__in=PENDING_APPLICATION_STATUSES,
    ).values_list("loan_product_id", flat=True)
    return {pk for pk in active_product_ids if pk} | {pk for pk in pending_product_ids if pk}


def available_loan_products_for_borrower(borrower):
    """Active products a borrower may apply for."""
    if credit_score_blocks_loans(borrower):
        return LoanProduct.objects.none()
    products = LoanProduct.objects.filter(is_active=True)
    blocked = unavailable_product_ids_for_borrower(borrower)
    if blocked:
        products = products.exclude(pk__in=blocked)
    return products


def duplicate_application_error(borrower, product, exclude_pk=None):
    if not borrower or not product:
        return None
    if Loan.objects.filter(
        application__borrower=borrower,
        application__loan_product=product,
        status__in=[Loan.Status.ACTIVE, Loan.Status.OVERDUE],
    ).exists():
        return f"This borrower already has an active {product.name} loan."
    qs = LoanApplication.objects.filter(
        borrower=borrower,
        loan_product=product,
        status__in=PENDING_APPLICATION_STATUSES,
    )
    if exclude_pk:
        qs = qs.exclude(pk=exclude_pk)
    if qs.exists():
        return f"There is already an open {product.name} application for this borrower."
    return None


class LoginForm(AuthenticationForm):
    email = forms.CharField(
        required=True,
        label="Email or username",
        widget=forms.TextInput(attrs={"autocomplete": "username"}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["username"].required = False

    def clean(self):
        identifier = (self.cleaned_data.get("email") or "").strip()
        if identifier:
            if "@" in identifier:
                try:
                    user = User.objects.get(email__iexact=identifier)
                    self.cleaned_data["username"] = user.username
                except User.DoesNotExist:
                    self.cleaned_data["username"] = identifier
            else:
                self.cleaned_data["username"] = identifier
        return super().clean()


class RegistrationForm(UserCreationForm):
    email = forms.EmailField()
    first_name = forms.CharField(max_length=80)
    last_name = forms.CharField(max_length=80)
    phone = forms.CharField(max_length=30)

    class Meta:
        model = User
        fields = ("first_name", "last_name", "email", "phone", "password1", "password2")

    def save(self, commit=True):
        user = super().save(commit=False)
        user.username = self.cleaned_data["email"].lower()
        user.email = self.cleaned_data["email"].lower()
        user.first_name = self.cleaned_data["first_name"]
        user.last_name = self.cleaned_data["last_name"]
        user.full_name = f"{user.first_name} {user.last_name}".strip()
        if commit:
            user.save()
        return user


USERNAME_RE = re.compile(r"^[a-z0-9._-]+$")


def suggest_username(first_name, last_name):
    """Slugifies a first/last name pair into a login-friendly username, e.g. 'maria.delacruz'."""

    def slugify_part(value):
        value = unicodedata.normalize("NFKD", value or "")
        value = value.encode("ascii", "ignore").decode("ascii")
        return re.sub(r"[^a-z0-9]", "", value.lower())

    parts = [part for part in (slugify_part(first_name), slugify_part(last_name)) if part]
    return ".".join(parts) or "member"


class BaseAccountCreationForm(UserCreationForm):
    """Shared account fields for the officer-workspace 'add a person' forms."""

    ROLE = None

    username = forms.CharField(
        max_length=150,
        help_text="Auto-filled from the name below — you can still edit it.",
    )
    email = forms.EmailField()
    first_name = forms.CharField(max_length=80, label="First name")
    last_name = forms.CharField(max_length=80, label="Last name")
    phone = forms.CharField(max_length=30, required=False)

    def clean_username(self):
        username = self.cleaned_data["username"].strip().lower()
        if not USERNAME_RE.match(username):
            raise forms.ValidationError("Usernames may only contain lowercase letters, numbers, dots, hyphens, and underscores.")
        if User.objects.filter(username__iexact=username).exists():
            raise forms.ValidationError("This username is already taken — try adding a number or initial.")
        return username

    def clean_email(self):
        email = self.cleaned_data["email"].strip().lower()
        if User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError("An account with this email already exists.")
        return email

    def save(self, commit=True):
        user = super().save(commit=False)
        user.username = self.cleaned_data["username"]
        user.email = self.cleaned_data["email"]
        user.first_name = self.cleaned_data["first_name"]
        user.last_name = self.cleaned_data["last_name"]
        user.full_name = f"{user.first_name} {user.last_name}".strip()
        user.phone = self.cleaned_data.get("phone", "")
        if self.ROLE:
            user.role = self.ROLE
        if commit:
            user.save()
        return user


class OfficerMemberForm(BaseAccountCreationForm):
    ROLE = User.Role.MEMBER

    date_of_birth = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))
    address = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 3}))
    employment_status = forms.CharField(max_length=40, required=False)
    monthly_income = forms.DecimalField(required=False, max_digits=12, decimal_places=2, widget=forms.NumberInput(attrs={"step": "0.01"}))

    class Meta:
        model = User
        fields = ("username", "first_name", "last_name", "email", "phone", "date_of_birth", "address", "employment_status", "monthly_income", "password1", "password2")

    def save(self, commit=True):
        user = super().save(commit=False)
        user.date_of_birth = self.cleaned_data.get("date_of_birth")
        user.address = self.cleaned_data.get("address", "")
        user.employment_status = self.cleaned_data.get("employment_status", "")
        user.monthly_income = self.cleaned_data.get("monthly_income")
        if commit:
            user.save()
        return user


class OfficerMemberEditForm(forms.ModelForm):
    """Lets an officer update an existing member's profile and account status."""

    username = forms.CharField(max_length=150, help_text="Used to sign in — must stay unique.")
    email = forms.EmailField()
    full_name = forms.CharField(max_length=160, label="Full name")
    phone = forms.CharField(max_length=30, required=False)
    date_of_birth = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))
    address = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 3}))
    employment_status = forms.CharField(max_length=40, required=False)
    monthly_income = forms.DecimalField(required=False, max_digits=12, decimal_places=2, widget=forms.NumberInput(attrs={"step": "0.01"}))
    is_active = forms.BooleanField(required=False, label="Active account", help_text="Uncheck to suspend this member's ability to sign in.")

    class Meta:
        model = User
        fields = ("username", "full_name", "email", "phone", "date_of_birth", "address", "employment_status", "monthly_income", "is_active")

    def clean_username(self):
        username = self.cleaned_data["username"].strip().lower()
        unchanged = self.instance.pk and username == self.instance.username.lower()
        if not unchanged and not USERNAME_RE.match(username):
            raise forms.ValidationError("Usernames may only contain lowercase letters, numbers, dots, hyphens, and underscores.")
        if User.objects.filter(username__iexact=username).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError("This username is already taken — try adding a number or initial.")
        return username

    def clean_email(self):
        email = self.cleaned_data["email"].strip().lower()
        if User.objects.filter(email__iexact=email).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError("An account with this email already exists.")
        return email

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("is_active") is False and self.instance.pk and self.instance.has_active_loan:
            self.add_error("is_active", "This member has an active loan and cannot be deactivated. Settle or close the loan first.")
        return cleaned

    def save(self, commit=True):
        user = super().save(commit=False)
        if commit:
            user.save()
        return user


class OfficerAccountForm(BaseAccountCreationForm):
    """Lets an admin create a Loan Officer account from the officer workspace."""

    ROLE = User.Role.OFFICER

    class Meta:
        model = User
        fields = ("username", "first_name", "last_name", "email", "phone", "password1", "password2")

    def save(self, commit=True):
        user = super().save(commit=False)
        user.is_staff = True
        if commit:
            user.save()
        return user


class OfficerAccountEditForm(forms.ModelForm):
    """Lets an admin update an existing loan officer's profile and account status."""

    username = forms.CharField(max_length=150, help_text="Used to sign in — must stay unique.")
    email = forms.EmailField()
    full_name = forms.CharField(max_length=160, label="Full name")
    phone = forms.CharField(max_length=30, required=False)
    is_active = forms.BooleanField(required=False, label="Active account", help_text="Uncheck to suspend this officer's ability to sign in.")

    class Meta:
        model = User
        fields = ("username", "full_name", "email", "phone", "is_active")

    def clean_username(self):
        username = self.cleaned_data["username"].strip().lower()
        unchanged = self.instance.pk and username == self.instance.username.lower()
        if not unchanged and not USERNAME_RE.match(username):
            raise forms.ValidationError("Usernames may only contain lowercase letters, numbers, dots, hyphens, and underscores.")
        if User.objects.filter(username__iexact=username).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError("This username is already taken — try adding a number or initial.")
        return username

    def clean_email(self):
        email = self.cleaned_data["email"].strip().lower()
        if User.objects.filter(email__iexact=email).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError("An account with this email already exists.")
        return email

    def save(self, commit=True):
        user = super().save(commit=False)
        if commit:
            user.save()
        return user


class LoanApplicationForm(forms.ModelForm):
    class Meta:
        model = LoanApplication
        fields = ("loan_product", "amount_requested", "purpose", "term_months", "payment_frequency")
        widgets = {
            "purpose": forms.Textarea(attrs={"rows": 3}),
            "amount_requested": forms.NumberInput(attrs={"step": "100", "min": "1000"}),
            "term_months": forms.NumberInput(attrs={"min": "1", "max": "60"}),
        }

    def __init__(self, *args, borrower=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.borrower = borrower
        if self.data:
            data = self.data.copy()
            if data.get("amount_requested"):
                data["amount_requested"] = str(data["amount_requested"]).replace(",", "")
            self.data = data
        self.fields["loan_product"].queryset = available_loan_products_for_borrower(borrower)
        self.fields["loan_product"].empty_label = "Select a loan product…"
        self.fields["amount_requested"].widget.attrs.update({"placeholder": "0.00"})
        self.fields["term_months"].widget.attrs.update({"placeholder": "e.g. 12"})
        self.fields["purpose"].widget.attrs.update({"placeholder": "Briefly describe how you plan to use the funds."})
        _style_form_fields(self)

    def clean(self):
        cleaned = super().clean()
        if self.borrower and credit_score_blocks_loans(self.borrower):
            self.add_error(None, credit_score_loan_block_message(self.borrower))
            return cleaned
        product = cleaned.get("loan_product")
        amount = cleaned.get("amount_requested")
        term = cleaned.get("term_months")
        if product and amount is not None and not product.min_amount <= amount <= product.max_amount:
            self.add_error("amount_requested", f"Enter an amount between ₱{product.min_amount:,.0f} and ₱{product.max_amount:,.0f}.")
        if product and term is not None and not product.min_term_months <= term <= product.max_term_months:
            self.add_error("term_months", f"Choose a term between {product.min_term_months} and {product.max_term_months} months.")
        error = duplicate_application_error(self.borrower, product, exclude_pk=self.instance.pk)
        if error:
            self.add_error("loan_product", error)
        return cleaned

    def clean_purpose(self):
        purpose = (self.cleaned_data.get("purpose") or "").strip()
        if not purpose:
            raise forms.ValidationError("Please describe the purpose of the loan.")
        return purpose


class ProfileForm(forms.ModelForm):
    class Meta:
        model = User
        fields = ("full_name", "phone", "date_of_birth", "employment_status", "monthly_income", "address")
        widgets = {
            "date_of_birth": forms.DateInput(attrs={"type": "date"}),
            "monthly_income": forms.NumberInput(attrs={"step": "0.01", "placeholder": "0.00"}),
            "address": forms.Textarea(attrs={"rows": 3, "placeholder": "Street, Barangay, City, Province"}),
            "full_name": forms.TextInput(attrs={"placeholder": "Your legal full name"}),
            "phone": forms.TextInput(attrs={"placeholder": "+63 9XX XXX XXXX"}),
            "employment_status": forms.TextInput(attrs={"placeholder": "e.g. Employed, Self-employed"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _style_form_fields(self)
        if self.instance.pk and not self.is_bound:
            user = self.instance
            self.initial.setdefault("full_name", user.full_name or user.get_full_name() or "")
            self.initial.setdefault("phone", user.phone or "")
            if user.date_of_birth:
                self.initial.setdefault("date_of_birth", user.date_of_birth)
            self.initial.setdefault("employment_status", user.employment_status or "")
            if user.monthly_income is not None:
                self.initial.setdefault("monthly_income", user.monthly_income)
            self.initial.setdefault("address", user.address or "")


class LoanProductForm(forms.ModelForm):
    class Meta:
        model = LoanProduct
        fields = ("name", "loan_type", "min_amount", "max_amount", "interest_rate", "min_term_months", "max_term_months")
        widgets = {
            "interest_rate": forms.NumberInput(attrs={"step": "0.01", "min": "0"}),
            "min_term_months": forms.NumberInput(attrs={"min": "1"}),
            "max_term_months": forms.NumberInput(attrs={"min": "1"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.data:
            data = self.data.copy()
            for field in ("min_amount", "max_amount"):
                if data.get(field):
                    data[field] = str(data[field]).replace(",", "")
            self.data = data

    def clean(self):
        cleaned = super().clean()
        min_a = cleaned.get("min_amount")
        max_a = cleaned.get("max_amount")
        min_t = cleaned.get("min_term_months")
        max_t = cleaned.get("max_term_months")
        if min_a is not None and max_a is not None and min_a >= max_a:
            self.add_error("max_amount", "Max amount must be greater than min amount.")
        if min_t is not None and max_t is not None and min_t >= max_t:
            self.add_error("max_term_months", "Max term must be greater than min term.")
        return cleaned


class LoanProductEditForm(LoanProductForm):
    class Meta(LoanProductForm.Meta):
        fields = LoanProductForm.Meta.fields + ("is_active",)


class OfficerLoanApplicationForm(forms.ModelForm):
    borrower = forms.ModelChoiceField(
        queryset=User.objects.none(),
        label="Borrower",
        empty_label="Select a borrower…",
    )
    applied_on = forms.DateField(
        label="Application date",
        widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
        input_formats=["%Y-%m-%d"],
        help_text="Year, month, and day the borrower applied for this loan.",
    )

    class Meta:
        model = LoanApplication
        fields = ("borrower", "loan_product", "amount_requested", "purpose", "term_months", "payment_frequency", "applied_on")
        widgets = {
            "purpose": forms.Textarea(attrs={"rows": 3}),
            "amount_requested": forms.NumberInput(attrs={"step": "100", "min": "1000"}),
            "term_months": forms.NumberInput(attrs={"min": "1", "max": "60"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.data:
            data = self.data.copy()
            if data.get("amount_requested"):
                data["amount_requested"] = str(data["amount_requested"]).replace(",", "")
            self.data = data
        self.fields["borrower"].queryset = User.objects.filter(role=User.Role.MEMBER, is_active=True).order_by("full_name", "email")
        borrower = None
        if self.is_bound:
            borrower_id = self.data.get(self.add_prefix("borrower") if self.prefix else "borrower")
            if borrower_id:
                borrower = User.objects.filter(pk=borrower_id).first()
        self.fields["loan_product"].queryset = available_loan_products_for_borrower(borrower)
        if not self.instance.pk and not self.is_bound:
            self.fields["applied_on"].initial = timezone.localdate()

    def applied_on_input_value(self):
        value = self["applied_on"].value()
        if hasattr(value, "strftime"):
            return value.strftime("%Y-%m-%d")
        return value or ""

    def today_iso(self):
        return timezone.localdate().isoformat()

    def clean_applied_on(self):
        applied_on = self.cleaned_data.get("applied_on")
        if applied_on and applied_on > timezone.localdate():
            raise forms.ValidationError("Application date cannot be in the future.")
        return applied_on

    def clean(self):
        cleaned = super().clean()
        borrower = cleaned.get("borrower")
        if borrower and credit_score_blocks_loans(borrower):
            self.add_error("borrower", credit_score_loan_block_message(borrower))
            return cleaned
        product = cleaned.get("loan_product")
        amount = cleaned.get("amount_requested")
        term = cleaned.get("term_months")
        borrower_id = self.data.get("borrower")
        if borrower_id and not borrower:
            inactive_member = User.objects.filter(pk=borrower_id, role=User.Role.MEMBER, is_active=False).first()
            if inactive_member:
                self.add_error("borrower", f"{inactive_member.display_name()}'s account is inactive and cannot receive a new loan application.")
        elif borrower and not borrower.is_active:
            self.add_error("borrower", f"{borrower.display_name()}'s account is inactive and cannot receive a new loan application.")
        if product and amount is not None and not product.min_amount <= amount <= product.max_amount:
            self.add_error("amount_requested", f"Enter an amount between ₱{product.min_amount:,.0f} and ₱{product.max_amount:,.0f}.")
        if product and term is not None and not product.min_term_months <= term <= product.max_term_months:
            self.add_error("term_months", f"Choose a term between {product.min_term_months} and {product.max_term_months} months.")
        error = duplicate_application_error(borrower, product, exclude_pk=self.instance.pk)
        if error:
            self.add_error("loan_product", error)
        return cleaned

    def clean_purpose(self):
        purpose = (self.cleaned_data.get("purpose") or "").strip()
        if not purpose:
            raise forms.ValidationError("Please describe the purpose of the loan.")
        return purpose


class ReviewForm(forms.ModelForm):
    decision = forms.ChoiceField(
        choices=(("approve", "Approve application"), ("reject", "Reject application")),
        widget=forms.RadioSelect,
    )

    class Meta:
        model = LoanApplication
        fields = ("final_interest_rate", "final_term_months", "review_notes")
        widgets = {"review_notes": forms.Textarea(attrs={"rows": 4})}


class PaymentForm(forms.ModelForm):
    class Meta:
        model = Payment
        fields = ("amount", "method", "reference_number")
        widgets = {
            "amount": forms.NumberInput(attrs={"step": "0.01", "min": "0.01", "class": "form-control"}),
            "method": forms.Select(attrs={"class": "form-select"}),
            "reference_number": forms.TextInput(attrs={"class": "form-control", "placeholder": "Optional reference"}),
        }

    def __init__(self, *args, max_amount=None, max_amount_label="outstanding balance", **kwargs):
        self.max_amount = max_amount
        self.max_amount_label = max_amount_label
        super().__init__(*args, **kwargs)

    def clean_amount(self):
        amount = self.cleaned_data.get("amount")
        if amount is not None and self.max_amount is not None and amount > self.max_amount:
            raise forms.ValidationError(f"Amount cannot exceed the {self.max_amount_label} of ₱{self.max_amount:,.2f}.")
        return amount


class DocumentForm(forms.ModelForm):
    ALLOWED_EXTENSIONS = {"jpg", "jpeg", "png", "gif", "webp", "pdf", "doc", "docx"}

    class Meta:
        model = Document
        fields = ("doc_type", "file")
        widgets = {
            "file": forms.ClearableFileInput(attrs={"accept": ".jpg,.jpeg,.png,.gif,.webp,.pdf,.doc,.docx"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["file"].required = False
        _style_form_fields(self)

    def clean_file(self):
        file = self.cleaned_data.get("file")
        if file:
            extension = file.name.rsplit(".", 1)[-1].lower() if "." in file.name else ""
            if extension not in self.ALLOWED_EXTENSIONS:
                raise forms.ValidationError("Upload an image (JPG, PNG, GIF, WEBP), PDF, or Word document (DOC, DOCX).")
        return file


class DisbursementAdminForm(forms.ModelForm):
    class Meta:
        model = Disbursement
        fields = (
            "status",
            "principal",
            "processing_fee",
            "other_fees",
            "other_fees_description",
            "total_payable",
            "outstanding_balance",
            "interest_rate",
            "term_months",
            "disbursed_date",
            "disbursement_method",
            "disbursement_reference",
            "disbursed_by",
        )
        widgets = {
            "disbursed_date": forms.DateInput(attrs={"type": "date"}),
            "principal": forms.NumberInput(attrs={"step": "0.01", "min": "0"}),
            "processing_fee": forms.NumberInput(attrs={"step": "0.01", "min": "0"}),
            "other_fees": forms.NumberInput(attrs={"step": "0.01", "min": "0"}),
            "total_payable": forms.NumberInput(attrs={"step": "0.01", "min": "0"}),
            "outstanding_balance": forms.NumberInput(attrs={"step": "0.01", "min": "0"}),
            "interest_rate": forms.NumberInput(attrs={"step": "0.01", "min": "0"}),
            "term_months": forms.NumberInput(attrs={"min": "1"}),
            "disbursement_method": forms.TextInput(attrs={"placeholder": "e.g. Bank transfer"}),
            "disbursement_reference": forms.TextInput(attrs={"placeholder": "Transaction reference"}),
            "other_fees_description": forms.TextInput(attrs={"placeholder": "Optional fee description"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["disbursed_by"].queryset = User.objects.filter(is_staff=True).order_by("full_name", "email")
        self.fields["disbursed_by"].required = False

    def clean(self):
        cleaned = super().clean()
        principal = cleaned.get("principal")
        processing_fee = cleaned.get("processing_fee") or Decimal("0.00")
        other_fees = cleaned.get("other_fees") or Decimal("0.00")
        outstanding_balance = cleaned.get("outstanding_balance")
        total_payable = cleaned.get("total_payable")
        if principal is not None and processing_fee + other_fees > principal:
            self.add_error("processing_fee", "Total fees cannot exceed the principal amount.")
        if total_payable is not None and outstanding_balance is not None and outstanding_balance > total_payable:
            self.add_error("outstanding_balance", "Outstanding balance cannot exceed total payable.")
        return cleaned

    def save(self, commit=True):
        schedule_fields = ("disbursed_date", "principal", "interest_rate", "term_months")
        old = None
        if self.instance.pk:
            old = Disbursement.objects.get(pk=self.instance.pk)
        instance = super().save(commit=commit)
        if commit and old and any(getattr(old, field) != getattr(instance, field) for field in schedule_fields):
            from .services import rebuild_loan_schedule

            rebuild_loan_schedule(instance)
        return instance