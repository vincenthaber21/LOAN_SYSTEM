from datetime import datetime
from decimal import Decimal

from django import forms
from django.contrib import admin, messages
from django.contrib.auth.admin import UserAdmin
from django.db.models import Sum
from django.http import HttpResponseRedirect
from django.urls import reverse
from django.utils import timezone

try:  # Django 5.1+ splits the admin-facing creation form out
    from django.contrib.auth.forms import AdminUserCreationForm as BaseUserCreationForm
except ImportError:  # pragma: no cover - older Django
    from django.contrib.auth.forms import UserCreationForm as BaseUserCreationForm

from .forms import DisbursementAdminForm
from .services import disbursement_start_time_label, normalize_credit_score
from .models import (
    ActivityLog,
    Administrator,
    CharacterReference,
    Disbursement,
    DisbursementSetting,
    Document,
    Features,
    Installment,
    Loan,
    LoanApplication,
    LoanOfficer,
    LoanProduct,
    Manager,
    Member,
    Notification,
    Payment,
    User,
)

ROLE_CHANGELIST_URLNAME = {
    User.Role.MEMBER: "admin:lending_member_changelist",
    User.Role.OFFICER: "admin:lending_loanofficer_changelist",
    User.Role.MANAGER: "admin:lending_manager_changelist",
    User.Role.ADMIN: "admin:lending_administrator_changelist",
}
ROLE_CHANGE_URLNAME = {
    User.Role.MEMBER: "admin:lending_member_change",
    User.Role.OFFICER: "admin:lending_loanofficer_change",
    User.Role.MANAGER: "admin:lending_manager_change",
    User.Role.ADMIN: "admin:lending_administrator_change",
}

PROFILE_FIELDS = ("role", "full_name", "phone", "address", "date_of_birth", "employment_status", "monthly_income")


class HarborlineAdminPermissionMixin:
    """Grant Django admin access to Harborline admin-role staff."""

    def _is_harborline_admin(self, request):
        user = request.user
        return user.is_active and user.is_staff and (
            user.is_superuser or getattr(user, "role", None) == User.Role.ADMIN
        )

    def has_module_permission(self, request):
        return self._is_harborline_admin(request) or super().has_module_permission(request)

    def has_view_permission(self, request, obj=None):
        return self._is_harborline_admin(request) or super().has_view_permission(request, obj)

    def has_add_permission(self, request):
        return self._is_harborline_admin(request) or super().has_add_permission(request)

    def has_change_permission(self, request, obj=None):
        return self._is_harborline_admin(request) or super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        return self._is_harborline_admin(request) or super().has_delete_permission(request, obj)


class RoleScopedUserAdmin(UserAdmin):
    """Shared admin for the per-role user sections."""

    list_display = ("username", "full_name", "email", "role", "is_active", "date_joined")
    list_filter = ("is_active", "date_joined")
    search_fields = ("username", "email", "full_name", "phone")
    ordering = ("full_name", "username")
    fieldsets = (
        (None, {"fields": ("username", "password")}),
        ("Profile", {"fields": PROFILE_FIELDS}),
        ("Contact", {"fields": ("email", "first_name", "middle_initial", "last_name")}),
        ("Permissions", {"fields": ("is_active", "is_staff", "is_superuser", "groups", "user_permissions")}),
        ("Important dates", {"fields": ("last_login", "date_joined")}),
    )
    add_fieldsets = (
        (None, {
            "classes": ("wide",),
            "fields": ("username", "email", "full_name", "phone", "password1", "password2"),
        }),
    )


class MemberCreationForm(BaseUserCreationForm):
    """Captures the member's profile up front instead of username/password only."""

    class Meta(BaseUserCreationForm.Meta):
        model = Member
        fields = ("username", "email", "full_name", "phone", "date_of_birth", "address", "employment_status", "monthly_income")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["username"].help_text = "Used to sign in. An email address works well here."
        self.fields["email"].required = True
        self.fields["full_name"].required = True
        placeholders = {
            "username": "maria@example.com",
            "email": "maria@example.com",
            "full_name": "Maria Dela Cruz",
            "phone": "+63 917 555 0142",
            "address": "Street, Barangay, City, Province",
            "employment_status": "Employed / Self-employed / Retired",
            "monthly_income": "0.00",
        }
        for name, text in placeholders.items():
            if name in self.fields:
                self.fields[name].widget.attrs.setdefault("placeholder", text)


@admin.register(Member)
class MemberAdmin(RoleScopedUserAdmin):
    list_display = ("username", "full_name", "email", "phone", "monthly_income", "credit_score_display", "is_active", "date_joined")
    add_form = MemberCreationForm
    add_form_template = "admin/lending/member/add_form.html"
    fieldsets = (
        (None, {"fields": ("username", "password")}),
        ("Profile", {"fields": PROFILE_FIELDS}),
        ("Credit standing", {
            "description": "Starts at 100. Deducts 0.1 for each late loan month.",
            "fields": ("credit_score",),
        }),
        ("Contact", {"fields": ("email", "first_name", "middle_initial", "last_name")}),
        ("Permissions", {"fields": ("is_active", "is_staff", "is_superuser", "groups", "user_permissions")}),
        ("Important dates", {"fields": ("last_login", "date_joined")}),
    )
    add_fieldsets = (
        ("Account", {
            "description": "How this member signs in to Harborline.",
            "fields": ("username", "email"),
        }),
        ("Personal information", {
            "description": "The member can update these later from their profile page.",
            "fields": ("full_name", "middle_initial", "phone", "date_of_birth", "address"),
        }),
        ("Financial profile", {
            "description": "Optional, but it speeds up their first loan application.",
            "fields": ("employment_status", "monthly_income"),
        }),
        ("Password", {
            "description": "Share these credentials with the member securely.",
            "fields": ("password1", "password2"),
        }),
    )

    def _can_edit_credit_score(self, request):
        user = request.user
        return user.is_superuser or getattr(user, "role", None) == User.Role.ADMIN

    def get_readonly_fields(self, request, obj=None):
        readonly = list(super().get_readonly_fields(request, obj))
        if obj is not None and not self._can_edit_credit_score(request):
            readonly.append("credit_score")
        return readonly

    def save_model(self, request, obj, form, change):
        obj.credit_score = normalize_credit_score(obj.credit_score)
        super().save_model(request, obj, form, change)

    @admin.display(description="Credit score", ordering="credit_score")
    def credit_score_display(self, obj):
        from .services import format_credit_score

        return format_credit_score(obj.credit_score)


@admin.register(LoanOfficer)
class LoanOfficerAdmin(RoleScopedUserAdmin):
    list_display = ("username", "full_name", "email", "phone", "is_staff", "is_active", "date_joined")


class AdministratorCreationForm(BaseUserCreationForm):
    """The one add-user form that lets the operator choose the account's role."""

    class Meta(BaseUserCreationForm.Meta):
        model = Administrator
        fields = ("role", "username", "email", "full_name", "phone", "date_of_birth", "address", "employment_status", "monthly_income")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["role"].widget = forms.RadioSelect(choices=self.fields["role"].choices)
        self.fields["role"].help_text = ""
        self.fields["role"].initial = User.Role.ADMIN
        self.fields["username"].help_text = "Used to sign in. An email address works well here."
        self.fields["email"].required = True
        self.fields["full_name"].required = True
        placeholders = {
            "username": "name@example.com",
            "email": "name@example.com",
            "full_name": "Full name",
            "phone": "+63 917 555 0142",
            "address": "Street, Barangay, City, Province",
            "employment_status": "Employed / Self-employed / Retired",
            "monthly_income": "0.00",
        }
        for name, text in placeholders.items():
            if name in self.fields:
                self.fields[name].widget.attrs.setdefault("placeholder", text)

    def save(self, commit=True):
        user = super().save(commit=False)
        role = self.cleaned_data["role"]
        user._role_explicit = True
        user.role = role
        user.is_staff = role in (User.Role.OFFICER, User.Role.MANAGER, User.Role.ADMIN)
        user.is_superuser = role == User.Role.ADMIN
        if commit:
            user.save()
        return user


@admin.register(Manager)
class ManagerAdmin(RoleScopedUserAdmin):
    list_display = ("username", "full_name", "email", "phone", "is_staff", "is_active", "date_joined")


@admin.register(Administrator)
class AdministratorAdmin(RoleScopedUserAdmin):
    list_display = ("username", "full_name", "email", "role", "is_staff", "is_superuser", "is_active", "date_joined")
    add_form = AdministratorCreationForm
    add_form_template = "admin/lending/administrator/add_form.html"
    add_fieldsets = (
        ("Role", {
            "description": "What this account can do in Harborline.",
            "fields": ("role",),
        }),
        ("Account", {
            "description": "How this person signs in.",
            "fields": ("username", "email"),
        }),
        ("Personal information", {
            "description": "Shown on their profile; they can update it later.",
            "fields": ("full_name", "phone", "date_of_birth", "address"),
        }),
        ("Financial profile", {
            "description": "Optional — mainly relevant for Member accounts.",
            "fields": ("employment_status", "monthly_income"),
        }),
        ("Password", {
            "description": "Share these credentials with the person securely.",
            "fields": ("password1", "password2"),
        }),
    )

    def response_add(self, request, obj, post_url_continue=None):
        role = obj.role
        label = obj.get_role_display()
        changelist_url = reverse(ROLE_CHANGELIST_URLNAME.get(role, "admin:lending_administrator_changelist"))

        if "_addanother" in request.POST:
            self.message_user(request, f"The {label} “{obj}” was added successfully. You may add another below.", messages.SUCCESS)
            return HttpResponseRedirect(reverse("admin:lending_administrator_add"))

        if "_continue" in request.POST:
            change_url = reverse(ROLE_CHANGE_URLNAME.get(role, "admin:lending_administrator_change"), args=(obj.pk,))
            self.message_user(request, f"The {label} “{obj}” was added successfully. You may edit it again below.", messages.SUCCESS)
            return HttpResponseRedirect(change_url)

        self.message_user(request, f"The {label} “{obj}” was added successfully.", messages.SUCCESS)
        return HttpResponseRedirect(changelist_url)


@admin.register(LoanProduct)
class LoanProductAdmin(HarborlineAdminPermissionMixin, admin.ModelAdmin):
    list_display = ("name", "loan_type", "min_amount", "max_amount", "interest_rate", "grace_period_days", "is_active", "application_count")
    list_filter = ("loan_type", "is_active")
    search_fields = ("name",)
    actions = ("delete_selected",)

    @admin.display(description="Applications")
    def application_count(self, obj):
        return obj.applications.count()


class CharacterReferenceInline(admin.TabularInline):
    model = CharacterReference
    extra = 2
    max_num = 2
    fields = ("sort_order", "name", "address", "relationship", "contact_number")
    ordering = ("sort_order", "id")


class DocumentInline(admin.TabularInline):
    model = Document
    extra = 0
    fields = ("doc_type", "file", "uploaded_at")
    readonly_fields = ("uploaded_at",)


@admin.register(LoanApplication)
class LoanApplicationAdmin(HarborlineAdminPermissionMixin, admin.ModelAdmin):
    list_display = (
        "reference",
        "borrower_name",
        "branch_name",
        "application_type",
        "amount_requested",
        "loan_purpose",
        "status",
        "applied_on",
    )
    list_filter = (
        "status",
        "application_type",
        "loan_purpose",
        "payment_frequency",
        "office_decision",
        "created_at",
    )
    search_fields = (
        "borrower__email",
        "borrower__full_name",
        "borrower__username",
        "borrower_surname",
        "borrower_first_name",
        "form_ref_no",
        "branch_name",
        "business_name",
    )
    actions = ("delete_selected",)
    readonly_fields = ("created_at",)
    date_hierarchy = "applied_on"
    inlines = (CharacterReferenceInline, DocumentInline)
    autocomplete_fields = ("loan_product",)
    raw_id_fields = ("borrower", "reviewed_by")
    fieldsets = (
        ("KAP Microfinance Loan Application", {
            "fields": (
                ("branch_name", "form_ref_no", "applied_on"),
                ("application_type", "status"),
                ("borrower_photo", "coborrower_photo"),
            ),
            "description": "Reminders: use capital letters where applicable. Put N/A in fields that do not apply.",
        }),
        ("Account link", {
            "fields": ("borrower", "loan_product", "term_months", "reviewed_by", "created_at"),
            "description": "System account and product used for disbursement and schedule generation.",
        }),
        ("Proposed plan payment", {
            "fields": (
                ("payment_frequency", "amount_requested"),
                "loan_purpose",
                "purpose",
            ),
            "description": "Pay daily / weekly / monthly. Amount requested and loan purpose from the KAP form.",
        }),
        ("Borrower information", {
            "fields": (
                ("borrower_surname", "borrower_first_name", "borrower_middle_name"),
                "borrower_present_address",
                ("borrower_municipality_city", "borrower_period_of_staying"),
                "borrower_dwelling_ownership",
                "borrower_permanent_address",
                ("borrower_permanent_municipality_city", "borrower_tel_mobile"),
                ("borrower_date_of_birth", "borrower_age"),
                ("borrower_citizenship", "borrower_place_of_birth"),
                ("borrower_gender", "borrower_civil_status"),
                ("borrower_nationality", "borrower_occupation"),
                "borrower_id_presented",
                ("borrower_contact_network", "borrower_tin_sss"),
                "borrower_email",
                "borrower_spouse_name",
            ),
        }),
        ("Co-borrower information", {
            "fields": (
                "coborrower_relationship",
                ("coborrower_surname", "coborrower_first_name", "coborrower_middle_name"),
                "coborrower_present_address",
                ("coborrower_municipality_city", "coborrower_period_of_staying"),
                "coborrower_dwelling_ownership",
                "coborrower_permanent_address",
                ("coborrower_permanent_municipality_city", "coborrower_tel_mobile"),
                ("coborrower_date_of_birth", "coborrower_age"),
                ("coborrower_citizenship", "coborrower_place_of_birth"),
                ("coborrower_gender", "coborrower_civil_status"),
                ("coborrower_nationality", "coborrower_occupation"),
                "coborrower_id_presented",
                ("coborrower_contact_network", "coborrower_tin_sss"),
                "coborrower_email",
                "coborrower_spouse_name",
            ),
            "classes": ("collapse",),
        }),
        ("Enterprise data (for new borrower)", {
            "fields": (
                ("primary_business", "business_name"),
                "business_ownership",
                "business_address",
                ("reg_dti", "reg_barangay", "reg_mayor", "reg_bir", "reg_others"),
                "reg_others_text",
                ("years_in_operation", "persons_employed"),
                ("additional_business_1_type", "additional_business_1_name"),
                "additional_business_1_address",
                ("additional_business_2_type", "additional_business_2_name"),
                "additional_business_2_address",
            ),
            "classes": ("collapse",),
        }),
        ("Certification / signatures", {
            "fields": (
                ("borrower_signed_name", "borrower_signed_date", "borrower_signed_place"),
                "borrower_signature",
                ("coborrower_signed_name", "coborrower_signed_date", "coborrower_signed_place"),
                "coborrower_signature",
            ),
            "classes": ("collapse",),
        }),
        ("Office use only — loan recommendation", {
            "fields": (
                ("recommended_loan_amount", "recommended_loan_period"),
                ("recommended_by_name", "recommended_by_date"),
                ("validated_by_name", "validated_by_date"),
                ("hold_out_amount", "insurance_proposed"),
            ),
            "classes": ("collapse",),
        }),
        ("Office use only — loan approval", {
            "fields": (
                "office_decision",
                ("branch_manager_name", "branch_manager_date"),
                ("final_interest_rate", "final_term_months"),
                "review_notes",
                "decision_date",
            ),
            "classes": ("collapse",),
        }),
    )

    @admin.display(description="Borrower", ordering="borrower_surname")
    def borrower_name(self, obj):
        return obj.borrower_name


class InstallmentInline(admin.TabularInline):
    model = Installment
    extra = 0
    fields = ("installment_number", "due_date", "amount_due", "amount_paid", "status", "paid_date")
    ordering = ("installment_number",)


class PaymentInline(admin.TabularInline):
    model = Payment
    extra = 0
    fields = ("amount", "payment_date", "method", "reference_number", "installment", "recorded_by")
    ordering = ("-payment_date",)


@admin.register(Loan)
class LoanAdmin(HarborlineAdminPermissionMixin, admin.ModelAdmin):
    list_display = ("reference", "borrower_name", "product_name", "principal", "status", "disbursed_date", "outstanding_balance")
    list_filter = ("status", "disbursed_date")
    search_fields = (
        "application__borrower__full_name",
        "application__borrower__email",
        "application__borrower__username",
        "disbursement_reference",
    )
    date_hierarchy = "disbursed_date"
    inlines = (InstallmentInline, PaymentInline)
    actions = ("delete_selected",)

    @admin.display(description="Borrower", ordering="application__borrower__full_name")
    def borrower_name(self, obj):
        return obj.application.borrower_name

    @admin.display(description="Product")
    def product_name(self, obj):
        return obj.product_name

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("application", "application__borrower", "application__loan_product")

    def has_add_permission(self, request):
        # Loans are created through the disbursement workflow (Disbursement admin),
        # which generates the installment schedule — raw admin "add" would skip that.
        return False


@admin.register(Disbursement)
class DisbursementAdmin(HarborlineAdminPermissionMixin, admin.ModelAdmin):
    form = DisbursementAdminForm
    change_list_template = "admin/lending/disbursement/change_list.html"
    change_form_template = "admin/lending/disbursement/change_form.html"
    list_display = (
        "reference",
        "borrower_name",
        "product_name",
        "net_release_amount",
        "disbursement_method",
        "disbursed_date",
        "status",
    )
    list_filter = ("status", "disbursement_method", "disbursed_date")
    search_fields = (
        "application__borrower__full_name",
        "application__borrower__email",
        "disbursement_reference",
        "application__pk",
    )
    readonly_fields = (
        "application",
        "borrower_display",
        "product_display",
        "disbursement_receipt_number",
        "net_release_display",
    )
    fieldsets = (
        ("Release summary", {
            "fields": (
                "application",
                "borrower_display",
                "product_display",
                "disbursement_receipt_number",
                "status",
            ),
        }),
        ("Amounts", {
            "fields": (
                "principal",
                "processing_fee",
                "other_fees",
                "net_release_display",
                "total_payable",
                "outstanding_balance",
                "interest_rate",
                "term_months",
            ),
            "description": "Adjust principal, fees, and loan balance details. Net released updates from principal minus fees.",
        }),
        ("Disbursement details", {
            "fields": (
                "disbursed_date",
                "disbursement_method",
                "disbursement_reference",
                "other_fees_description",
                "disbursed_by",
            ),
            "description": "Update release date, payment method, reference, and officer who released the funds.",
        }),
    )
    actions = ("delete_selected",)
    date_hierarchy = "disbursed_date"

    @admin.display(description="Borrower")
    def borrower_name(self, obj):
        return obj.application.borrower_name

    @admin.display(description="Product")
    def product_name(self, obj):
        return obj.product_name

    @admin.display(description="Net released")
    def net_release_amount(self, obj):
        return obj.net_release_amount

    @admin.display(description="Borrower")
    def borrower_display(self, obj):
        borrower = obj.application.borrower
        return f"{borrower.display_name()} · {borrower.email}"

    @admin.display(description="Product")
    def product_display(self, obj):
        return obj.product_name

    @admin.display(description="Net amount released")
    def net_release_display(self, obj):
        return f"₱{obj.net_release_amount:,.2f}"

    def has_add_permission(self, request):
        return False

    def get_queryset(self, request):
        return super().get_queryset(request).select_related(
            "application",
            "application__borrower",
            "application__loan_product",
            "disbursed_by",
        )

    def _parse_disbursement_start_time(self, raw, fallback):
        if not raw:
            return fallback
        raw = str(raw).strip()
        for fmt in ("%H:%M", "%H:%M:%S"):
            try:
                return datetime.strptime(raw, fmt).time()
            except ValueError:
                continue
        return None

    def _handle_disbursement_setting_post(self, request):
        """Save weekday/start time / enable-disable condition from the changelist card."""
        action = request.POST.get("disbursement_setting_action")
        if action not in {"save_schedule", "save_weekday", "toggle_condition"}:
            return False
        if not self.has_change_permission(request):
            messages.error(request, "You do not have permission to change disbursement settings.")
            return True

        setting = DisbursementSetting.load()
        if action in {"save_schedule", "save_weekday"}:
            try:
                weekday = int(request.POST.get("disbursement_weekday", setting.disbursement_weekday))
            except (TypeError, ValueError):
                messages.error(request, "Choose a valid disbursement weekday.")
                return True
            valid = {choice.value for choice in DisbursementSetting.Weekday}
            if weekday not in valid:
                messages.error(request, "Choose a valid disbursement weekday.")
                return True
            start_time = self._parse_disbursement_start_time(
                request.POST.get("disbursement_start_time"),
                setting.disbursement_start_time,
            )
            if start_time is None:
                messages.error(request, "Choose a valid disbursement start time.")
                return True
            setting.disbursement_weekday = weekday
            setting.disbursement_start_time = start_time
            setting.save(update_fields=["disbursement_weekday", "disbursement_start_time"])
            messages.success(
                request,
                f"Disbursement window set to {setting.get_disbursement_weekday_display()}s "
                f"from {disbursement_start_time_label(setting.disbursement_start_time)}.",
            )
        else:
            setting.condition_enabled = not setting.condition_enabled
            setting.save(update_fields=["condition_enabled"])
            if setting.condition_enabled:
                messages.success(
                    request,
                    f"Disbursement condition enabled — releases only on "
                    f"{setting.get_disbursement_weekday_display()}s from "
                    f"{disbursement_start_time_label(setting.disbursement_start_time)}.",
                )
            else:
                messages.warning(
                    request,
                    "Disbursement condition disabled — officers can release funds any day.",
                )
        return True

    def changelist_view(self, request, extra_context=None):
        if request.method == "POST" and self._handle_disbursement_setting_post(request):
            return HttpResponseRedirect(request.path)

        extra_context = extra_context or {}
        setting = DisbursementSetting.load()
        ready = LoanApplication.objects.filter(
            status=LoanApplication.Status.APPROVED,
        ).select_related("borrower", "loan_product")
        for item in ready:
            item.approved_at = item.decision_date.strftime("%b %d, %Y") if item.decision_date else "Recently"
        released_total = self.get_queryset(request).aggregate(total=Sum("principal"))["total"] or Decimal("0.00")
        ready_total = sum((item.amount_requested for item in ready), Decimal("0.00"))
        month_total = self.get_queryset(request).filter(
            disbursed_date__month=timezone.localdate().month,
            disbursed_date__year=timezone.localdate().year,
        ).aggregate(total=Sum("principal"))["total"] or Decimal("0.00")
        extra_context.update({
            "ready_queue": ready,
            "ready_count": ready.count(),
            "ready_total": ready_total,
            "released_total": released_total,
            "month_total": month_total,
            "disbursement_setting": setting,
            "disbursement_weekday_choices": DisbursementSetting.Weekday.choices,
            "disbursement_metrics": [
                {"label": "Ready to release", "value": f"₱{ready_total:,.0f}", "note": "Approved principal"},
                {"label": "Awaiting release", "value": ready.count(), "note": "Approved applications"},
                {"label": "Released this month", "value": f"₱{month_total:,.0f}", "note": "Principal recorded"},
            ],
        })
        return super().changelist_view(request, extra_context)


@admin.register(Features)
class FeaturesAdmin(HarborlineAdminPermissionMixin, admin.ModelAdmin):
    list_display = ("store_name", "tagline", "daily_mutual_aid_amount")
    fields = ("store_name", "tagline", "logo", "daily_mutual_aid_amount")

    def formfield_for_dbfield(self, db_field, request, **kwargs):
        formfield = super().formfield_for_dbfield(db_field, request, **kwargs)
        if db_field.name == "logo":
            formfield.help_text = (
                "PNG, JPG, or WebP recommended. The background is removed automatically on upload."
            )
        return formfield

    def has_add_permission(self, request):
        return not Features.objects.exists() and super().has_add_permission(request)

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        return HttpResponseRedirect(
            reverse("admin:lending_features_change", args=(Features.load().pk,))
        )


@admin.register(Installment)
class InstallmentAdmin(HarborlineAdminPermissionMixin, admin.ModelAdmin):
    list_display = ("loan", "installment_number", "due_date", "amount_due", "amount_paid", "status")
    list_filter = ("status", "due_date")
    search_fields = (
        "loan__application__borrower__full_name",
        "loan__application__borrower__email",
        "loan__disbursement_reference",
    )
    date_hierarchy = "due_date"


@admin.register(Payment)
class PaymentAdmin(HarborlineAdminPermissionMixin, admin.ModelAdmin):
    list_display = (
        "__str__",
        "loan",
        "amount",
        "savings_adjustment",
        "mutual_aid_contribution",
        "method",
        "payment_date",
        "recorded_by",
    )
    list_filter = ("method", "payment_date")
    search_fields = (
        "loan__application__borrower__full_name",
        "loan__application__borrower__email",
        "reference_number",
    )
    date_hierarchy = "payment_date"


@admin.register(ActivityLog)
class ActivityLogAdmin(HarborlineAdminPermissionMixin, admin.ModelAdmin):
    list_display = ("created_at", "actor", "action", "kind", "title", "member_name", "reference", "ip_address")
    list_filter = ("kind", "action", "created_at")
    search_fields = ("title", "description", "member_name", "reference", "actor__username", "actor__full_name", "ip_address")
    date_hierarchy = "created_at"
    readonly_fields = [field.name for field in ActivityLog._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Notification)
class NotificationAdmin(HarborlineAdminPermissionMixin, admin.ModelAdmin):
    list_display = ("title", "user", "kind", "is_read", "created_at")
    list_filter = ("kind", "is_read", "created_at")
    search_fields = ("title", "message", "user__username", "user__full_name", "user__email")
    date_hierarchy = "created_at"


admin.site.register(Document)
