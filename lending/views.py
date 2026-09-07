import csv
from datetime import datetime, timedelta
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Count, Q, Sum
from django.db.models.functions import TruncMonth
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from .decorators import role_required
from .forms import BalanceExtensionForm, BorrowerLoanApplicationForm, CharacterReferenceFormSet, DocumentForm, LoanApplicationForm, LoanProductEditForm, LoanProductForm, ManagerAccountEditForm, ManagerAccountForm, OfficerAccountEditForm, OfficerAccountForm, OfficerLoanApplicationForm, OfficerMemberEditForm, OfficerMemberForm, PaymentForm, ProfileForm, RegistrationForm, ReviewForm, available_loan_products_for_borrower, unavailable_product_ids_for_borrower
from .models import Document, Installment, Loan, LoanApplication, LoanOfficer, LoanProduct, Manager, Notification, Payment, User
from .services import BalanceExtensionError, adjust_payment, balance_extension_previews, can_extend_loan_balance, disburse_application, ensure_schedule_current, extend_loan_balance, format_activity_timestamp, format_credit_score, get_borrower_credit_summary, get_officer_activity_log, mark_overdue_installments, normalize_credit_score, original_schedule_display_rows, record_payment, reject_superseded_applications, credit_score_blocks_loans, credit_score_loan_block_message, schedule_display_rows, standard_disbursement_deductions, application_schedule_view_mode, application_type_for_member, next_due_for_display, BALANCE_EXTENSION_RATE


def _parse_disbursed_date(value):
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def _applications_on_or_before(qs, selected_date):
    return qs.filter(
        Q(applied_on__lte=selected_date)
        | Q(applied_on__isnull=True, created_at__date__lte=selected_date)
    )


def _applications_on_date(qs, selected_date):
    return qs.filter(
        Q(applied_on=selected_date)
        | Q(applied_on__isnull=True, created_at__date=selected_date)
    )


def _pending_applications_as_of(qs, selected_date):
    return _applications_on_or_before(qs, selected_date).filter(
        Q(decision_date__isnull=True) | Q(decision_date__date__gt=selected_date)
    ).exclude(status=LoanApplication.Status.DRAFT)


def _overdue_loans_as_of(selected_date):
    return (
        Installment.objects.filter(
            loan__disbursed_date__lte=selected_date,
            due_date__lt=selected_date,
        )
        .filter(Q(paid_date__isnull=True) | Q(paid_date__gt=selected_date))
        .values("loan")
        .distinct()
        .count()
    )


def dashboard(request):
    if not request.user.is_authenticated:
        return redirect("login")
    return redirect("officer_dashboard" if request.user.is_officer else "borrower_dashboard")


def logout_view(request):
    if request.user.is_authenticated:
        logout(request)
        messages.success(request, "You have been signed out.")
    return redirect("login")


def register(request):
    if request.user.is_authenticated:
        return redirect("dashboard")
    form = RegistrationForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = form.save()
        login(request, user)
        messages.success(request, "Welcome. Let's get your application started.")
        return redirect("borrower_dashboard")
    return render(request, "accounts/register.html", {"form": form})


REQUIRED_BORROWER_DOC_TYPES = (Document.DocType.VALID_ID, Document.DocType.PROOF_OF_INCOME)


def _greeting_for_now():
    hour = timezone.localtime().hour
    if hour < 12:
        return "Good morning"
    if hour < 17:
        return "Good afternoon"
    return "Good evening"


def _borrower_first_name(user):
    name = (user.full_name or user.get_full_name() or "").strip()
    if name:
        return name.split()[0]
    return user.first_name or user.username or "there"


def _borrower_document_counts(applications):
    application_count = applications.count()
    required = application_count * len(REQUIRED_BORROWER_DOC_TYPES)
    uploaded_required = 0
    for application in applications:
        types = set(
            application.documents.filter(doc_type__in=REQUIRED_BORROWER_DOC_TYPES).values_list("doc_type", flat=True)
        )
        uploaded_required += len(types)
    return uploaded_required, required, max(0, required - uploaded_required)


def _borrower_next_steps(user, latest_application, active_loan, next_installment, uploaded_docs, required_docs):
    steps = []
    profile_done = profile_completion(user) == 100
    steps.append({
        "title": "Complete your profile",
        "description": "Name, contact details, and income help us review your request.",
        "complete": profile_done,
        "action_url": reverse("profile"),
        "action_label": "Update profile",
        "due_label": "Completed" if profile_done else f"{profile_completion(user)}% complete",
    })

    if not latest_application:
        if credit_score_blocks_loans(user):
            steps.append({
                "title": "Loan applications unavailable",
                "description": credit_score_loan_block_message(user),
                "complete": False,
                "action_url": "",
                "action_label": "",
                "due_label": "Credit score too low",
            })
        else:
            steps.append({
                "title": "Start a loan application",
                "description": "Choose a product, term, and amount to apply.",
                "complete": False,
                "action_url": reverse("loan_application"),
                "action_label": "Start application",
                "due_label": "Start now",
            })
    elif latest_application.status == LoanApplication.Status.DRAFT:
        steps.append({
            "title": "Finish and submit your application",
            "description": f"{latest_application.reference} is still a draft.",
            "complete": False,
            "action_url": reverse("loan_application") + f"?draft={latest_application.pk}",
            "action_label": "Continue draft",
            "due_label": "Next up",
        })
    else:
        decided = latest_application.status in {
            LoanApplication.Status.APPROVED,
            LoanApplication.Status.ACTIVE,
            LoanApplication.Status.CLOSED,
            LoanApplication.Status.DISBURSED,
            LoanApplication.Status.REJECTED,
        }
        steps.append({
            "title": f"Application {latest_application.reference}",
            "description": f"{latest_application.product_name} · {latest_application.amount_requested:,.2f} requested · {latest_application.status_label}.",
            "complete": decided,
            "action_url": reverse("application-detail", args=[latest_application.pk]),
            "action_label": "View application",
            "due_label": latest_application.status_label,
        })

    docs_complete = required_docs == 0 or uploaded_docs >= required_docs
    steps.append({
        "title": "Upload required documents",
        "description": "Valid ID and proof of income for each application.",
        "complete": docs_complete,
        "action_url": reverse("loan_application"),
        "action_label": "Add documents",
        "due_label": "Completed" if docs_complete else f"{uploaded_docs} of {required_docs} on file",
    })

    if active_loan:
        if next_installment:
            overdue = next_installment.status == Installment.Status.OVERDUE
            steps.append({
                "title": "Next installment",
                "description": f"₱{next_installment.remaining:,.2f} due {next_installment.due_date:%b %d, %Y}. Payments are recorded by your loan officer.",
                "complete": False,
                "action_url": reverse("repayment_schedule", args=[active_loan.pk]),
                "action_label": "View schedule",
                "due_label": "Overdue" if overdue else next_installment.due_date.strftime("%b %d, %Y"),
            })
        else:
            steps.append({
                "title": "Loan is paid in full",
                "description": f"{active_loan.reference} has no remaining installments.",
                "complete": True,
                "due_label": "Completed",
            })
    return steps


def _activity_sort_value(value):
    if isinstance(value, datetime):
        if timezone.is_naive(value):
            return timezone.make_aware(value)
        return value
    return timezone.make_aware(datetime.combine(value, datetime.min.time()))


def _borrower_recent_activity(user, applications, loans):
    events = []
    for application in applications:
        events.append({
            "sort": _activity_sort_value(application.created_at),
            "icon": "bi-file-earmark-text",
            "title": f"{application.reference} · {application.status_label}",
            "description": f"{application.product_name} · ₱{application.amount_requested:,.2f}",
            "created_at": format_activity_timestamp(application.created_at),
        })
        if application.decision_date:
            events.append({
                "sort": _activity_sort_value(application.decision_date),
                "icon": "bi-clipboard-check",
                "title": f"Decision on {application.reference}",
                "description": application.status_label,
                "created_at": format_activity_timestamp(application.decision_date),
            })
        for document in application.documents.all():
            events.append({
                "sort": _activity_sort_value(document.uploaded_at),
                "icon": "bi-paperclip",
                "title": f"{document.name} uploaded",
                "description": application.reference,
                "created_at": format_activity_timestamp(document.uploaded_at),
            })
    for loan in loans:
        events.append({
            "sort": _activity_sort_value(loan.disbursed_date),
            "icon": "bi-arrow-down-left-circle",
            "title": f"{loan.reference} disbursed",
            "description": f"₱{loan.principal:,.2f} released",
            "created_at": loan.start_date,
        })
    payments = Payment.objects.filter(loan__application__borrower=user).select_related("loan").order_by("-payment_date", "-id")
    for payment in payments:
        events.append({
            "sort": _activity_sort_value(payment.payment_date),
            "icon": "bi-cash-coin",
            "title": f"Payment of ₱{payment.amount:,.2f}",
            "description": f"{payment.loan.reference} · {payment.get_method_display()}",
            "created_at": payment.payment_date.strftime("%b %d, %Y"),
        })
    events.sort(key=lambda item: item["sort"], reverse=True)
    return [{k: v for k, v in event.items() if k != "sort"} for event in events[:8]]


@login_required
@role_required("member")
def borrower_dashboard(request):
    mark_overdue_installments()
    user = request.user
    loans = list(
        Loan.objects.filter(application__borrower=user)
        .select_related("application", "application__loan_product")
        .prefetch_related("installments")
    )
    applications = list(
        LoanApplication.objects.filter(borrower=user)
        .select_related("loan_product")
        .prefetch_related("documents")
    )
    active_loan = next((loan for loan in loans if loan.status in [Loan.Status.ACTIVE, Loan.Status.OVERDUE]), None)
    if active_loan:
        ensure_schedule_current(active_loan)
        active_loan.refresh_from_db()
    next_installment = active_loan.next_installment if active_loan else None
    latest_application = applications[0] if applications else None

    uploaded_docs, required_docs, outstanding_docs = _borrower_document_counts(
        LoanApplication.objects.filter(borrower=user)
    )
    total_paid = Payment.objects.filter(loan__application__borrower=user).aggregate(value=Sum("amount"))["value"] or Decimal("0.00")
    outstanding = sum((loan.outstanding_balance for loan in loans), Decimal("0.00"))
    payable = sum((loan.total_payable for loan in loans), Decimal("0.00"))
    payment_progress = min(100, int((total_paid / payable) * 100)) if payable else 0

    if latest_application:
        status_note = f"{latest_application.product_name} · ₱{latest_application.amount_requested:,.2f} · {latest_application.term_months} mo"
    else:
        status_note = "Start when you are ready."

    return render(request, "borrower/dashboard.html", {
        "borrower": user,
        "greeting": _greeting_for_now(),
        "first_name": _borrower_first_name(user),
        "credit_score": format_credit_score(user.credit_score),
        "credit_score_blocked": credit_score_blocks_loans(user),
        "loans": loans,
        "applications": applications,
        "active_loan": active_loan,
        "next_installment": next_installment,
        "outstanding_balance": outstanding,
        "profile_completion": profile_completion(user),
        "application_status_label": latest_application.status_label if latest_application else "No application",
        "application_status_note": status_note,
        "total_paid": total_paid,
        "payment_progress": payment_progress,
        "verified_document_count": uploaded_docs,
        "required_document_count": required_docs,
        "outstanding_document_count": outstanding_docs,
        "next_steps": _borrower_next_steps(
            user, latest_application, active_loan, next_installment, uploaded_docs, required_docs
        ),
        "recent_activity": _borrower_recent_activity(user, applications, loans),
    })


def _kap_profile_for_member(member):
    """Build KAP personal-data defaults from account + latest prior application."""
    names = _split_member_name(member)
    profile = {
        "borrower_surname": names["surname"],
        "borrower_first_name": names["first_name"],
        "borrower_middle_name": names["middle_name"],
        "borrower_present_address": (member.address or "").strip(),
        "borrower_permanent_address": (member.address or "").strip(),
        "borrower_tel_mobile": (member.phone or "").strip(),
        "borrower_contact_network": (member.phone or "").strip(),
        "borrower_date_of_birth": member.date_of_birth.isoformat() if member.date_of_birth else "",
        "borrower_age": _age_from_dob(member.date_of_birth),
        "borrower_occupation": (member.employment_status or "").strip(),
        "borrower_email": (member.email or "").strip(),
        "borrower_signed_name": (member.full_name or member.display_name() or "").strip(),
    }

    latest = (
        LoanApplication.objects.filter(borrower=member)
        .exclude(Q(borrower_surname="") & Q(borrower_first_name=""))
        .order_by("-created_at")
        .first()
    )
    if latest:
        kap_fields = (
            "borrower_surname",
            "borrower_first_name",
            "borrower_middle_name",
            "borrower_present_address",
            "borrower_municipality_city",
            "borrower_period_of_staying",
            "borrower_dwelling_ownership",
            "borrower_permanent_address",
            "borrower_permanent_municipality_city",
            "borrower_tel_mobile",
            "borrower_date_of_birth",
            "borrower_age",
            "borrower_citizenship",
            "borrower_place_of_birth",
            "borrower_gender",
            "borrower_civil_status",
            "borrower_nationality",
            "borrower_occupation",
            "borrower_id_presented",
            "borrower_contact_network",
            "borrower_tin_sss",
            "borrower_email",
            "borrower_spouse_name",
            "borrower_signed_name",
            "borrower_signed_place",
            "primary_business",
            "business_name",
            "business_ownership",
            "business_address",
            "years_in_operation",
            "persons_employed",
        )
        for field in kap_fields:
            value = getattr(latest, field, None)
            if value in (None, ""):
                continue
            if hasattr(value, "isoformat"):
                profile[field] = value.isoformat()
            else:
                profile[field] = value
        if latest.borrower_age:
            profile["borrower_age"] = latest.borrower_age
        elif latest.borrower_date_of_birth:
            profile["borrower_age"] = _age_from_dob(latest.borrower_date_of_birth)

    if member.email:
        profile["borrower_email"] = member.email
    if member.phone:
        profile["borrower_tel_mobile"] = member.phone
        profile.setdefault("borrower_contact_network", member.phone)
    if member.date_of_birth and not profile.get("borrower_date_of_birth"):
        profile["borrower_date_of_birth"] = member.date_of_birth.isoformat()
        profile["borrower_age"] = _age_from_dob(member.date_of_birth)

    app_type = application_type_for_member(member)
    return {
        "profile": profile,
        "source": "previous_application" if latest else "member_account",
        "application_type": app_type,
        "application_type_label": dict(LoanApplication.ApplicationType.choices).get(app_type, "New Application"),
        "has_loan_history": app_type == LoanApplication.ApplicationType.RENEW,
    }


@login_required
@role_required("member")
def application_create(request):
    document_specs = [
        ("valid_id", Document.DocType.VALID_ID),
        ("proof_of_income", Document.DocType.PROOF_OF_INCOME),
        ("other", Document.DocType.OTHER),
    ]
    products = list(available_loan_products_for_borrower(request.user))
    credit_blocked = credit_score_blocks_loans(request.user)
    kap_defaults = _kap_profile_for_member(request.user)

    if request.method == "POST":
        form = BorrowerLoanApplicationForm(
            request.POST, request.FILES, borrower=request.user
        )
        reference_formset = CharacterReferenceFormSet(
            request.POST, prefix="refs", instance=LoanApplication()
        )
        document_forms = [
            DocumentForm(request.POST, request.FILES, prefix=prefix, initial={"doc_type": doc_type})
            for prefix, doc_type in document_specs
        ]
        if "create_application" not in request.POST:
            messages.error(request, "Complete the form and confirm to submit your application.")
        elif credit_blocked or not products:
            messages.error(
                request,
                credit_score_loan_block_message(request.user)
                if credit_blocked
                else "No loan products are available for you right now.",
            )
        else:
            document_errors = any(
                request.FILES.get(f"{prefix}-file") and not doc_form.is_valid()
                for prefix, doc_form in zip((item[0] for item in document_specs), document_forms)
            )
            if form.is_valid() and reference_formset.is_valid() and not document_errors:
                application = form.save(commit=False)
                application.borrower = request.user
                application.status = LoanApplication.Status.SUBMITTED
                if not application.applied_on:
                    application.applied_on = timezone.localdate()
                application.save()
                reference_formset.instance = application
                references = reference_formset.save(commit=False)
                for index, reference in enumerate(references, start=1):
                    if not reference.name:
                        continue
                    reference.application = application
                    if not reference.sort_order:
                        reference.sort_order = index
                    reference.save()
                for doc_form in document_forms:
                    if not request.FILES.get(f"{doc_form.prefix}-file"):
                        continue
                    if doc_form.is_valid() and doc_form.cleaned_data.get("file"):
                        doc = doc_form.save(commit=False)
                        doc.application = application
                        doc.save()
                messages.success(request, "Application submitted for review.")
                return redirect("application-detail", application.pk)
    else:
        initial = dict(kap_defaults["profile"])
        initial["application_type"] = kap_defaults["application_type"]
        form = BorrowerLoanApplicationForm(borrower=request.user, initial=initial)
        reference_formset = CharacterReferenceFormSet(prefix="refs")
        for index, ref_form in enumerate(reference_formset.forms, start=1):
            ref_form.fields["sort_order"].initial = index
        document_forms = [
            DocumentForm(prefix=prefix, initial={"doc_type": doc_type})
            for prefix, doc_type in document_specs
        ]

    return render(request, "borrower/application_wizard.html", {
        "form": form,
        "reference_formset": reference_formset,
        "document_forms": document_forms,
        "products": products,
        "credit_score_blocked": credit_blocked,
        "credit_score_block_message": credit_score_loan_block_message(request.user),
        "application_type_label": kap_defaults["application_type_label"],
        "has_loan_history": kap_defaults["has_loan_history"],
        "profile_source": kap_defaults["source"],
    })


@login_required
@role_required("member")
def application_detail(request, pk):
    application = get_object_or_404(LoanApplication.objects.select_related("loan_product", "borrower", "reviewed_by"), pk=pk, borrower=request.user)
    return render(request, "borrower/application_detail.html", {"application": application, "documents": application.documents.all()})


@login_required
@role_required("member")
def application_submit(request, pk):
    application = get_object_or_404(LoanApplication, pk=pk, borrower=request.user)
    if request.method == "POST" and application.status == LoanApplication.Status.DRAFT:
        application.status = LoanApplication.Status.SUBMITTED
        application.save(update_fields=["status"])
        messages.success(request, "Your application is now in the review queue.")
    return redirect("application-detail", pk)


@login_required
@role_required("member")
def loan_detail(request, loan_id):
    loan = get_object_or_404(Loan.objects.select_related("application", "application__loan_product"), pk=loan_id, application__borrower=request.user)
    payments = loan.payments.select_related("recorded_by", "installment").order_by("-payment_date", "-id")
    return render(request, "borrower/loan_detail.html", {"loan": loan, "schedule": loan.installments.all(), "payments": payments})


@login_required
@role_required("member")
def schedule(request, loan_id):
    loan = get_object_or_404(
        Loan.objects.select_related("application", "application__loan_product"),
        pk=loan_id,
        application__borrower=request.user,
    )
    mark_overdue_installments()
    ensure_schedule_current(loan)
    view_mode = request.GET.get("view") or application_schedule_view_mode(loan)
    month = request.GET.get("month", "")
    plan = (request.GET.get("plan") or "current").lower()
    showing_original = bool(loan.is_rescheduled and plan == "original")
    if showing_original:
        display = original_schedule_display_rows(loan, view_mode=view_mode, month=month)
        original_terms = display.get("original_terms") or loan.original_schedule_terms()
        next_payment = None
    else:
        display = schedule_display_rows(loan, view_mode=view_mode, month=month)
        original_terms = loan.original_schedule_terms() if loan.is_rescheduled else None
        next_payment = loan.next_installment
    return render(
        request,
        "borrower/repayment_schedule.html",
        {
            "loan": loan,
            "installments": loan.installments.all(),
            "schedule": display["schedule"],
            "payment_count": display["payment_count"],
            "paid_payment_count": display["paid_payment_count"],
            "next_payment": next_payment,
            "view_mode": display["view_mode"],
            "month_options": display["month_options"],
            "selected_month": display["selected_month"],
            "application_pay_frequency": loan.application.payment_frequency,
            "schedule_plan": "original" if showing_original else "current",
            "showing_original": showing_original,
            "original_terms": original_terms,
        },
    )


@login_required
@role_required("member")
def my_loan(request):
    loan = request.user.current_loan
    if loan:
        return redirect("loan_detail", loan_id=loan.pk)
    return render(request, "simple_page.html", {
        "title": "My loan",
        "description": "You do not have a loan yet. Start an application when you are ready.",
    })


@login_required
@role_required("member")
def my_schedule(request):
    loan = request.user.current_loan
    if loan:
        return redirect("repayment_schedule", loan_id=loan.pk)
    return render(request, "simple_page.html", {
        "title": "Repayment schedule",
        "description": "Your repayment schedule will appear here once a loan is disbursed.",
    })


@login_required
@role_required("member")
def make_payment(request, loan_id):
    loan = get_object_or_404(Loan, pk=loan_id, application__borrower=request.user)
    messages.info(request, "Payments are recorded by your loan officer. View your schedule for upcoming due dates.")
    return redirect("repayment_schedule", loan_id=loan.pk)


@login_required
@role_required("officer")
def officer_dashboard(request):
    today = timezone.localdate()
    is_all_view = request.GET.get("view") == "all"

    if is_all_view:
        selected_date = today
        mark_overdue_installments()
    else:
        selected_date = _parse_disbursed_date(request.GET.get("date")) or today
        if selected_date > today:
            selected_date = today
        if selected_date == today:
            mark_overdue_installments()

    applications_qs = LoanApplication.objects.select_related("borrower", "loan_product")
    loans = Loan.objects.select_related("application", "application__borrower")

    if is_all_view:
        loans_as_of = loans
        total_disbursed = loans.aggregate(value=Sum("principal"))["value"] or Decimal("0.00")
        overdue_loans = loans.filter(
            Q(status=Loan.Status.OVERDUE) | Q(installments__status=Installment.Status.OVERDUE)
        ).distinct().count()
        pending_count = applications_qs.filter(status__in=["submitted", "under_review"]).count()
        disbursed_on_date = loans
        disbursed_on_date_total = total_disbursed
        collected_on_date = Payment.objects.aggregate(value=Sum("amount"))["value"] or Decimal("0.00")
        applications_on_date = applications_qs.exclude(status=LoanApplication.Status.DRAFT)
        date_note = "All time"
        activity_note = "All records"
        is_today = False
        priority = applications_qs.filter(status__in=["submitted", "under_review"]).order_by("-created_at")
        payments_on_date = (
            Payment.objects.select_related("loan", "loan__application", "loan__application__borrower")
            .order_by("-payment_date", "-id")
        )
        disbursements_on_date = loans.select_related(
            "application", "application__borrower", "application__loan_product"
        ).order_by("-disbursed_date", "-id")
        applications_on_date_list = applications_on_date.order_by("-created_at")
        portfolio_loans = loans.select_related(
            "application", "application__borrower", "application__loan_product"
        ).order_by("-disbursed_date", "-id")
        status_breakdown_qs = applications_qs
        overdue_review_count = applications_qs.filter(status=LoanApplication.Status.SUBMITTED).count()
        overview_date_label = "All time"
        daily_metrics = [
            {
                "label": "Total released",
                "value": f"₱{disbursed_on_date_total:,.0f}",
                "note": f"{disbursed_on_date.count()} loan{'s' if disbursed_on_date.count() != 1 else ''} · {activity_note}",
                "positive": disbursed_on_date_total > 0,
            },
            {
                "label": "Total collected",
                "value": f"₱{collected_on_date:,.0f}",
                "note": f"{payments_on_date.count()} payment{'s' if payments_on_date.count() != 1 else ''} · {activity_note}",
                "positive": collected_on_date > 0,
            },
            {
                "label": "Total applications",
                "value": applications_on_date.count(),
                "note": activity_note,
            },
        ]
    else:
        loans_as_of = loans.filter(disbursed_date__lte=selected_date)
        total_disbursed = loans_as_of.aggregate(value=Sum("principal"))["value"] or Decimal("0.00")
        if selected_date == today:
            overdue_loans = loans.filter(
                Q(status=Loan.Status.OVERDUE) | Q(installments__status=Installment.Status.OVERDUE)
            ).distinct().count()
        else:
            overdue_loans = _overdue_loans_as_of(selected_date)
        pending_count = _pending_applications_as_of(applications_qs, selected_date).count()
        disbursed_on_date = loans.filter(disbursed_date=selected_date)
        disbursed_on_date_total = disbursed_on_date.aggregate(value=Sum("principal"))["value"] or Decimal("0.00")
        collected_on_date = Payment.objects.filter(payment_date=selected_date).aggregate(value=Sum("amount"))["value"] or Decimal("0.00")
        applications_on_date = _applications_on_date(applications_qs, selected_date).exclude(status=LoanApplication.Status.DRAFT)
        is_today = selected_date == today
        date_note = "As of today" if is_today else f"As of {selected_date.strftime('%b %d, %Y')}"
        activity_note = "Today's activity" if is_today else selected_date.strftime("%b %d, %Y")
        if is_today:
            priority = applications_qs.filter(status__in=["submitted", "under_review"]).order_by("-created_at")
        else:
            priority = _pending_applications_as_of(applications_qs, selected_date).order_by("-created_at")
        payments_on_date = (
            Payment.objects.filter(payment_date=selected_date)
            .select_related("loan", "loan__application", "loan__application__borrower")
            .order_by("-id")
        )
        disbursements_on_date = disbursed_on_date.select_related(
            "application", "application__borrower", "application__loan_product"
        ).order_by("-id")
        applications_on_date_list = applications_on_date.order_by("-created_at")
        portfolio_loans = loans_as_of.select_related(
            "application", "application__borrower", "application__loan_product"
        ).order_by("-disbursed_date", "-id")
        status_breakdown_qs = _applications_on_or_before(applications_qs, selected_date)
        overdue_review_count = _pending_applications_as_of(
            applications_qs.filter(status=LoanApplication.Status.SUBMITTED), selected_date
        ).count()
        overview_date_label = "Today" if is_today else selected_date.strftime("%B %d, %Y")
        daily_metrics = [
            {
                "label": "Released",
                "value": f"₱{disbursed_on_date_total:,.0f}",
                "note": f"{disbursed_on_date.count()} loan{'s' if disbursed_on_date.count() != 1 else ''} · {activity_note}",
                "positive": disbursed_on_date_total > 0,
            },
            {
                "label": "Collected",
                "value": f"₱{collected_on_date:,.0f}",
                "note": activity_note,
                "positive": collected_on_date > 0,
            },
            {
                "label": "Applications in",
                "value": applications_on_date.count(),
                "note": activity_note,
            },
        ]

    dashboard_metrics = [
        {"label": "Portfolio loans", "value": loans_as_of.count(), "note": date_note},
        {"label": "Total disbursed", "value": f"₱{total_disbursed:,.0f}", "note": date_note, "positive": True},
        {"label": "Pending review", "value": pending_count, "note": date_note},
        {"label": "Overdue loans", "value": overdue_loans, "note": date_note},
    ]

    total_outstanding = loans_as_of.aggregate(value=Sum("outstanding_balance"))["value"] or Decimal("0.00")
    total_adjusted_outstanding = sum(
        (adjust_payment(balance) for balance in loans_as_of.values_list("outstanding_balance", flat=True)),
        Decimal("0.00"),
    )
    dashboard_metrics.insert(
        2,
        {
            "label": "Outstanding",
            "value": f"₱{total_adjusted_outstanding:,.0f}",
            "note": (
                f"Cash-adjusted · Exact ₱{total_outstanding:,.0f}"
                if total_outstanding != total_adjusted_outstanding
                else date_note
            ),
            "positive": True,
        },
    )

    status_breakdown = [
        {
            "label": dict(LoanApplication.Status.choices).get(row["status"], row["status"]),
            "count": row["total"],
        }
        for row in status_breakdown_qs.values("status").annotate(total=Count("id")).order_by("-total")
    ]

    context = {
        "officer": request.user,
        "selected_date": "" if is_all_view else selected_date.isoformat(),
        "today": today.isoformat(),
        "overview_date_label": overview_date_label,
        "is_today_view": is_today if not is_all_view else False,
        "is_all_view": is_all_view,
        "total_loans": loans_as_of.count(),
        "total_disbursed": total_disbursed,
        "total_outstanding": total_outstanding,
        "total_adjusted_outstanding": total_adjusted_outstanding,
        "pending_applications": pending_count,
        "overdue_loans": overdue_loans,
        "portfolio_at_risk": Decimal("7.4"),
        "dashboard_metrics": dashboard_metrics,
        "daily_metrics": daily_metrics,
        "priority_applications": priority,
        "disbursements_on_date": disbursements_on_date,
        "payments_on_date": payments_on_date,
        "applications_on_date_list": applications_on_date_list,
        "portfolio_loans": portfolio_loans,
        "status_breakdown": status_breakdown,
        "overdue_review_count": overdue_review_count,
    }
    if request.user.is_admin:
        team = list(
            User.objects.filter(role__in=[User.Role.OFFICER, User.Role.ADMIN], is_active=True)
        )
        team.sort(key=lambda member: (not member.is_online, -(member.last_seen.timestamp() if member.last_seen else 0)))
        context["officer_presence"] = team
        context["online_officer_count"] = sum(1 for member in team if member.is_online)
    return render(request, "officer/dashboard.html", context)


@login_required
@role_required("officer")
def applications(request):
    qs = LoanApplication.objects.select_related("borrower", "loan_product")
    query = request.GET.get("q", "").strip()
    status = request.GET.get("status", "")
    loan_type = request.GET.get("loan_type", "")
    if query:
        qs = qs.filter(Q(borrower__full_name__icontains=query) | Q(borrower__email__icontains=query) | Q(pk__icontains=query))
    if status:
        qs = qs.filter(status=status)
    else:
        qs = qs.filter(status__in=[LoanApplication.Status.SUBMITTED, LoanApplication.Status.UNDER_REVIEW])
    if loan_type:
        qs = qs.filter(loan_product__loan_type=loan_type)
    return render(request, "officer/applications_queue.html", {
        "applications": qs,
        "query": query,
        "filters": {"q": query, "status": status, "product": loan_type, "sort": "recent"},
        "result_count": qs.count(),
        "status_filters": [{"value": value, "label": label} for value, label in LoanApplication.Status.choices],
        "products": [{"value": value, "label": label} for value, label in LoanProduct.LoanType.choices],
        "sort_options": [{"value": "recent", "label": "Recent activity"}, {"value": "amount", "label": "Amount"}],
    })


@login_required
@role_required("officer")
def application_review(request, application_id):
    application = get_object_or_404(
        LoanApplication.objects.select_related("borrower", "loan_product").prefetch_related("character_references"),
        pk=application_id,
    )
    if request.method == "POST":
        form = ReviewForm(request.POST, instance=application)
        if form.is_valid():
            if form.cleaned_data["decision"] == "approve" and not request.user.is_admin:
                messages.error(request, "Only administrators can approve loan applications.")
                return redirect("application_review", application_id)
            app = form.save(commit=False)
            app.reviewed_by = request.user
            app.decision_date = timezone.now()
            app.status = LoanApplication.Status.APPROVED if form.cleaned_data["decision"] == "approve" else LoanApplication.Status.REJECTED
            app.save()
            if app.status == LoanApplication.Status.APPROVED:
                reject_superseded_applications(app, request.user)
                messages.success(request, "Application approved — proceed to release the funds.")
                return redirect("disbursement_detail", disbursement_id=application_id)
            messages.success(request, "Application marked as rejected.")
            return redirect("application_review", application_id)
    else:
        form = ReviewForm(instance=application, initial={"final_interest_rate": application.loan_product.interest_rate, "final_term_months": application.term_months})
    credit_summary = get_borrower_credit_summary(application.borrower, exclude_application=application)
    return render(request, "officer/application_review.html", {
        "application": application,
        "form": form,
        "credit_summary": credit_summary,
        "documents": application.documents.all(),
        "applicant_snapshot": [
            {"label": "Date of application", "value": application.submitted_at},
            {"label": "Email", "value": application.borrower.email},
            {"label": "Phone", "value": application.borrower_tel_mobile or application.borrower.phone or "Not provided"},
            {"label": "Monthly income", "value": f"₱{application.borrower.monthly_income:,.0f}" if application.borrower.monthly_income else "Not provided"},
            {"label": "Credit score", "value": format_credit_score(application.borrower.credit_score)},
            {"label": "Employment", "value": application.borrower_occupation or application.borrower.employment_status or "Not provided"},
            {"label": "Requested term", "value": f"{application.term_months} months"},
            {"label": "Payment frequency", "value": application.get_payment_frequency_display()},
        ],
        "review_notes": [{"author": application.reviewed_by.display_name(), "created_at": application.decision_date, "body": application.review_notes}] if application.review_notes and application.reviewed_by else [],
    })


@login_required
@role_required("officer")
def application_pdf(request, application_id):
    application = get_object_or_404(
        LoanApplication.objects.select_related("borrower", "loan_product").prefetch_related("character_references"),
        pk=application_id,
    )
    from .kap_pdf import build_kap_application_pdf

    pdf_bytes = build_kap_application_pdf(application)
    filename = f"KAP-Application-{application.reference}.pdf"
    response = HttpResponse(pdf_bytes, content_type="application/pdf")
    disposition = "attachment" if request.GET.get("download") == "1" else "inline"
    response["Content-Disposition"] = f'{disposition}; filename="{filename}"'
    return response


@login_required
@role_required("admin")
def application_delete(request, application_id):
    application = get_object_or_404(LoanApplication, pk=application_id)
    if request.method != "POST":
        return redirect("applications_queue")
    reference = application.reference
    application.delete()
    messages.success(request, f"Application {reference} was deleted.")
    return redirect("applications_queue")


@login_required
@role_required("officer")
def officer_available_products(request):
    borrower_id = request.GET.get("borrower")
    borrower = User.member_accounts().filter(pk=borrower_id, is_active=True).first()
    borrower_inactive = bool(borrower_id) and not borrower
    borrower_has_active_loan = bool(borrower and borrower.has_active_loan)
    credit_score_blocked = bool(borrower and credit_score_blocks_loans(borrower))
    blocked_product_count = len(unavailable_product_ids_for_borrower(borrower)) if borrower else 0
    if borrower_inactive or credit_score_blocked:
        products = LoanProduct.objects.none()
    else:
        products = available_loan_products_for_borrower(borrower)
    return JsonResponse({
        "borrower_inactive": borrower_inactive,
        "borrower_has_active_loan": borrower_has_active_loan,
        "credit_score_blocked": credit_score_blocked,
        "credit_score_block_message": credit_score_loan_block_message(borrower) if credit_score_blocked else "",
        "blocked_product_count": blocked_product_count,
        "products": [
            {
                "id": product.pk,
                "label": f"{product.name} — {product.get_loan_type_display()} ({product.interest_rate}% / mo)",
                "min_amount": str(product.min_amount),
                "max_amount": str(product.max_amount),
            }
            for product in products.order_by("name")
        ]
    })


def _split_member_name(member):
    """Best-effort surname / first / middle from stored member name fields."""
    first = (member.first_name or "").strip()
    last = (member.last_name or "").strip()
    full = (member.full_name or member.get_full_name() or "").strip()
    if first or last:
        parts = full.split() if full else []
        middle = ""
        if parts and first and parts[0].lower() == first.lower() and last:
            middle_parts = [p for p in parts[1:] if p.lower() != last.lower()]
            middle = " ".join(middle_parts)
        return {"surname": last, "first_name": first, "middle_name": middle}
    if not full:
        return {"surname": "", "first_name": "", "middle_name": ""}
    parts = full.split()
    if len(parts) == 1:
        return {"surname": "", "first_name": parts[0], "middle_name": ""}
    if len(parts) == 2:
        return {"surname": parts[-1], "first_name": parts[0], "middle_name": ""}
    return {
        "surname": parts[-1],
        "first_name": parts[0],
        "middle_name": " ".join(parts[1:-1]),
    }


def _age_from_dob(dob):
    if not dob:
        return None
    today = timezone.localdate()
    return today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))


@login_required
@role_required("officer")
def officer_member_profile(request):
    """Return member profile (+ latest KAP personal data) to autofill the application form."""
    borrower_id = request.GET.get("borrower")
    member = User.member_accounts().filter(pk=borrower_id, is_active=True).first()
    if not member:
        return JsonResponse({"ok": False, "error": "Member not found."}, status=404)
    payload = _kap_profile_for_member(member)
    payload["ok"] = True
    return JsonResponse(payload)


@login_required
@role_required("admin")
def add_loan_product(request):
    if request.method != "POST":
        return JsonResponse({"ok": False, "errors": {}}, status=405)
    form = LoanProductForm(request.POST)
    if form.is_valid():
        product = form.save()
        return JsonResponse({
            "ok": True,
            "id": product.pk,
            "name": f"{product.name} — {product.get_loan_type_display()} ({product.interest_rate}% / mo)",
        })
    return JsonResponse({"ok": False, "errors": form.errors}, status=422)


@login_required
@role_required("admin")
def add_loan_product_page(request):
    form = LoanProductForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        product = form.save()
        messages.success(request, f'Loan product "{product.name}" added successfully.')
        return redirect("loan_products")
    return render(request, "officer/add_loan_product.html", {
        "form": form,
        "existing_products": LoanProduct.objects.all().order_by("name")[:8],
    })


@login_required
@role_required("admin")
def loan_products(request):
    query = request.GET.get("q", "").strip()
    loan_type = request.GET.get("loan_type", "").strip()
    status = request.GET.get("status", "").strip()
    qs = LoanProduct.objects.annotate(applications_count=Count("applications"))
    if query:
        qs = qs.filter(Q(name__icontains=query))
    if loan_type:
        qs = qs.filter(loan_type=loan_type)
    if status == "active":
        qs = qs.filter(is_active=True)
    elif status == "inactive":
        qs = qs.filter(is_active=False)
    qs = qs.order_by("name")
    product_count = qs.count()
    active_count = qs.filter(is_active=True).count()
    return render(request, "officer/loan_products.html", {
        "products": qs,
        "product_count": product_count,
        "active_count": active_count,
        "filters": {"q": query, "loan_type": loan_type, "status": status},
        "product_type_filters": LoanProduct.LoanType.choices,
        "product_status_filters": [{"value": "active", "label": "Active"}, {"value": "inactive", "label": "Inactive"}],
    })


@login_required
@role_required("admin")
def edit_loan_product(request, product_id):
    product = get_object_or_404(LoanProduct, pk=product_id)
    form = LoanProductEditForm(request.POST or None, instance=product)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, f'Loan product "{product.name}" was updated.')
        return redirect("loan_products")
    return render(request, "officer/edit_loan_product.html", {"form": form, "product": product})


@login_required
@role_required("officer")
def officer_apply_loan(request):
    document_specs = [
        ("valid_id", Document.DocType.VALID_ID),
        ("proof_of_income", Document.DocType.PROOF_OF_INCOME),
        ("other", Document.DocType.OTHER),
    ]
    if request.method == "POST":
        form = OfficerLoanApplicationForm(request.POST, request.FILES)
        reference_formset = CharacterReferenceFormSet(
            request.POST, prefix="refs", instance=LoanApplication()
        )
        document_forms = [
            DocumentForm(request.POST, request.FILES, prefix=prefix, initial={"doc_type": doc_type})
            for prefix, doc_type in document_specs
        ]
        if "create_application" not in request.POST:
            messages.error(request, "Complete the form and confirm to create an application.")
        else:
            document_errors = any(
                request.FILES.get(f"{prefix}-file") and not doc_form.is_valid()
                for prefix, doc_form in zip((item[0] for item in document_specs), document_forms)
            )
            if form.is_valid() and reference_formset.is_valid() and not document_errors:
                application = form.save(commit=False)
                application.status = LoanApplication.Status.SUBMITTED
                application.save()
                reference_formset.instance = application
                references = reference_formset.save(commit=False)
                for index, reference in enumerate(references, start=1):
                    if not reference.name:
                        continue
                    reference.application = application
                    if not reference.sort_order:
                        reference.sort_order = index
                    reference.save()
                for doc_form in document_forms:
                    if not request.FILES.get(f"{doc_form.prefix}-file"):
                        continue
                    if doc_form.is_valid() and doc_form.cleaned_data.get("file"):
                        doc = doc_form.save(commit=False)
                        doc.application = application
                        doc.save()
                messages.success(request, f"Application {application.reference} created for {application.borrower_name}.")
                return redirect("application_review", application_id=application.pk)
    else:
        form = OfficerLoanApplicationForm()
        reference_formset = CharacterReferenceFormSet(prefix="refs")
        for index, ref_form in enumerate(reference_formset.forms, start=1):
            ref_form.fields["sort_order"].initial = index
        document_forms = [
            DocumentForm(prefix=prefix, initial={"doc_type": doc_type})
            for prefix, doc_type in document_specs
        ]

    products = LoanProduct.objects.filter(is_active=True)
    return render(request, "officer/apply_loan.html", {
        "form": form,
        "reference_formset": reference_formset,
        "document_forms": document_forms,
        "products": products,
    })


def _members_queryset(query="", status="", active_loans_only=False):
    qs = User.member_accounts().annotate(
        active_loans=Count("loan_applications__loan", filter=Q(loan_applications__loan__status__in=["active", "overdue"])),
    ).prefetch_related("loan_applications__loan")
    if query:
        qs = qs.filter(Q(full_name__icontains=query) | Q(email__icontains=query) | Q(phone__icontains=query))
    if status == "active":
        qs = qs.filter(is_active=True)
    elif status == "inactive":
        qs = qs.filter(is_active=False)
    if active_loans_only:
        qs = qs.filter(active_loans__gt=0)
    return qs


def _enrich_members(qs):
    total_active_loans = 0
    total_outstanding = Decimal("0.00")
    total_adjusted_outstanding = Decimal("0.00")
    members = list(qs)
    for member in members:
        member.active_loan_count = member.active_loans
        loan_balances = [
            loan
            for application in member.loan_applications.all()
            for loan in ([getattr(application, "loan", None)] if hasattr(application, "loan") else [])
            if loan
        ]
        member.total_outstanding = sum((loan.outstanding_balance for loan in loan_balances), Decimal("0.00"))
        member.total_adjusted_outstanding = sum(
            (loan.adjusted_outstanding_balance for loan in loan_balances),
            Decimal("0.00"),
        )
        # Primary list display: cash-adjusted (₱5 remittance) outstanding
        member.total_balance = member.total_adjusted_outstanding
        member.last_activity = member.joined_at
        member.account_status = "active" if member.is_active else "inactive"
        if credit_score_blocks_loans(member):
            member.has_available_loan = False
            member.loan_availability_label = "Credit score too low"
            member.loan_availability_status = "overdue"
        else:
            member.has_available_loan = member.active_loan_count == 0
            member.loan_availability_label = "Available" if member.has_available_loan else "Has active loan"
            member.loan_availability_status = "active" if member.has_available_loan else "inactive"
        total_active_loans += member.active_loan_count
        total_outstanding += member.total_outstanding
        total_adjusted_outstanding += member.total_adjusted_outstanding
    return members, total_active_loans, total_outstanding, total_adjusted_outstanding


def _member_list_context(members, total_active_loans, total_outstanding, query, status, total_adjusted_outstanding=None):
    if total_adjusted_outstanding is None:
        total_adjusted_outstanding = total_outstanding
    return {
        "members": members,
        "member_count": len(members),
        "total_active_loans": total_active_loans,
        "total_outstanding": total_outstanding,
        "total_adjusted_outstanding": total_adjusted_outstanding,
        "query": query,
        "filters": {"q": query, "status": status},
        "borrower_status_filters": [{"value": "active", "label": "Active"}, {"value": "inactive", "label": "Inactive"}],
    }


@login_required
@role_required("officer")
def borrowers(request):
    query = request.GET.get("q", "").strip()
    status = request.GET.get("status", "").strip()
    members, total_active_loans, total_outstanding, total_adjusted_outstanding = _enrich_members(
        _members_queryset(query, status, active_loans_only=True),
    )
    return render(
        request,
        "officer/borrowers.html",
        _member_list_context(
            members,
            total_active_loans,
            total_outstanding,
            query,
            status,
            total_adjusted_outstanding=total_adjusted_outstanding,
        ),
    )


@login_required
@role_required("officer")
def all_members(request):
    query = request.GET.get("q", "").strip()
    status = request.GET.get("status", "").strip()
    members, total_active_loans, total_outstanding, total_adjusted_outstanding = _enrich_members(
        _members_queryset(query, status)
    )
    members_with_loans = sum(1 for member in members if member.active_loan_count > 0)
    context = _member_list_context(
        members,
        total_active_loans,
        total_outstanding,
        query,
        status,
        total_adjusted_outstanding=total_adjusted_outstanding,
    )
    context["members_with_loans"] = members_with_loans
    return render(request, "officer/all_members.html", context)


@login_required
@role_required("officer")
def add_member(request):
    form = OfficerMemberForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        member = form.save()
        messages.success(request, f"{member.display_name()} was added as a member.")
        return redirect("all_members")
    return render(request, "officer/add_member.html", {"form": form})


@login_required
@role_required("officer")
def edit_member(request, borrower_id):
    borrower = get_object_or_404(User.member_accounts(), pk=borrower_id)
    form = OfficerMemberEditForm(request.POST or None, instance=borrower)
    if request.method == "POST" and form.is_valid():
        member = form.save()
        messages.success(request, f"{member.display_name()}'s profile was updated.")
        return redirect("borrower_detail", borrower_id=member.id)
    return render(request, "officer/edit_member.html", {"form": form, "borrower": borrower, "has_active_loan": borrower.has_active_loan})


@login_required
@role_required("admin")
def loan_officers(request):
    query = request.GET.get("q", "").strip()
    status = request.GET.get("status", "").strip()
    qs = LoanOfficer.objects.annotate(applications_reviewed=Count("reviewed_applications"))
    if query:
        qs = qs.filter(Q(full_name__icontains=query) | Q(email__icontains=query) | Q(username__icontains=query))
    if status == "active":
        qs = qs.filter(is_active=True)
    elif status == "inactive":
        qs = qs.filter(is_active=False)
    officer_count = qs.count()
    active_count = qs.filter(is_active=True).count()
    return render(request, "officer/loan_officers.html", {
        "officers": qs,
        "officer_count": officer_count,
        "active_count": active_count,
        "filters": {"q": query, "status": status},
        "officer_status_filters": [{"value": "active", "label": "Active"}, {"value": "inactive", "label": "Inactive"}],
    })


@login_required
@role_required("admin")
def add_officer(request):
    form = OfficerAccountForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        officer = form.save()
        messages.success(request, f"{officer.display_name()} was added as a loan officer.")
        return redirect("loan_officers")
    return render(request, "officer/add_officer.html", {"form": form})


@login_required
@role_required("admin")
def edit_officer(request, officer_id):
    officer = get_object_or_404(User, pk=officer_id, role=User.Role.OFFICER)
    form = OfficerAccountEditForm(request.POST or None, instance=officer)
    if request.method == "POST" and form.is_valid():
        member = form.save()
        messages.success(request, f"{member.display_name()}'s profile was updated.")
        return redirect("loan_officers")
    return render(request, "officer/edit_officer.html", {"form": form, "officer": officer})


@login_required
@role_required("admin")
def managers(request):
    query = request.GET.get("q", "").strip()
    status = request.GET.get("status", "").strip()
    qs = Manager.objects.annotate(applications_reviewed=Count("reviewed_applications"))
    if query:
        qs = qs.filter(Q(full_name__icontains=query) | Q(email__icontains=query) | Q(username__icontains=query))
    if status == "active":
        qs = qs.filter(is_active=True)
    elif status == "inactive":
        qs = qs.filter(is_active=False)
    manager_count = qs.count()
    active_count = qs.filter(is_active=True).count()
    return render(request, "officer/managers.html", {
        "managers": qs,
        "manager_count": manager_count,
        "active_count": active_count,
        "filters": {"q": query, "status": status},
        "manager_status_filters": [{"value": "active", "label": "Active"}, {"value": "inactive", "label": "Inactive"}],
    })


@login_required
@role_required("admin")
def add_manager(request):
    form = ManagerAccountForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        manager = form.save()
        messages.success(request, f"{manager.display_name()} was added as a manager.")
        return redirect("managers")
    return render(request, "officer/add_manager.html", {"form": form})


@login_required
@role_required("admin")
def edit_manager(request, manager_id):
    manager = get_object_or_404(User, pk=manager_id, role=User.Role.MANAGER)
    form = ManagerAccountEditForm(request.POST or None, instance=manager)
    if request.method == "POST" and form.is_valid():
        member = form.save()
        messages.success(request, f"{member.display_name()}'s profile was updated.")
        return redirect("managers")
    return render(request, "officer/edit_manager.html", {"form": form, "manager": manager})


@login_required
@role_required("admin")
def manager_activity_log(request, manager_id):
    manager = get_object_or_404(Manager, pk=manager_id)
    activity_type = request.GET.get("type", "all")
    if activity_type not in {"all", "application", "payment", "disbursement"}:
        activity_type = "all"
    events = get_officer_activity_log(manager, activity_type=activity_type)
    for event in events:
        event["created_at_display"] = format_activity_timestamp(event["created_at"])
        if event.get("url_name"):
            event["detail_url"] = reverse(event["url_name"], kwargs=event["url_kwargs"])
    paginator = Paginator(events, 20)
    page_obj = paginator.get_page(request.GET.get("page"))
    query_string = f"&type={activity_type}" if activity_type != "all" else ""
    application_count = manager.reviewed_applications.filter(decision_date__isnull=False).count()
    payment_count = manager.recorded_payments.count()
    disbursement_count = manager.disbursed_loans.count()
    payment_total = manager.recorded_payments.aggregate(total=Sum("amount"))["total"] or Decimal("0.00")
    disbursed_total = manager.disbursed_loans.aggregate(total=Sum("principal"))["total"] or Decimal("0.00")
    return render(request, "officer/manager_activity_log.html", {
        "manager": manager,
        "activity": page_obj,
        "page_obj": page_obj,
        "is_paginated": page_obj.has_other_pages(),
        "query_string": query_string,
        "filters": {"type": activity_type},
        "activity_type_filters": [
            {"value": "all", "label": "All activity"},
            {"value": "application", "label": "Applications"},
            {"value": "payment", "label": "Payments"},
            {"value": "disbursement", "label": "Disbursements"},
        ],
        "activity_summary": [
            {"label": "Applications processed", "value": application_count, "note": "Decisions recorded"},
            {"label": "Payments recorded", "value": payment_count, "note": f"₱{payment_total:,.0f} collected"},
            {"label": "Disbursements released", "value": disbursement_count, "note": f"₱{disbursed_total:,.0f} principal"},
        ],
    })


@login_required
@role_required("admin")
def officer_activity_log(request, officer_id):
    officer = get_object_or_404(LoanOfficer, pk=officer_id)
    activity_type = request.GET.get("type", "all")
    if activity_type not in {"all", "application", "payment", "disbursement"}:
        activity_type = "all"
    events = get_officer_activity_log(officer, activity_type=activity_type)
    for event in events:
        event["created_at_display"] = format_activity_timestamp(event["created_at"])
        if event.get("url_name"):
            event["detail_url"] = reverse(event["url_name"], kwargs=event["url_kwargs"])
    paginator = Paginator(events, 20)
    page_obj = paginator.get_page(request.GET.get("page"))
    query_string = f"&type={activity_type}" if activity_type != "all" else ""
    application_count = officer.reviewed_applications.filter(decision_date__isnull=False).count()
    payment_count = officer.recorded_payments.count()
    disbursement_count = officer.disbursed_loans.count()
    payment_total = officer.recorded_payments.aggregate(total=Sum("amount"))["total"] or Decimal("0.00")
    disbursed_total = officer.disbursed_loans.aggregate(total=Sum("principal"))["total"] or Decimal("0.00")
    return render(request, "officer/officer_activity_log.html", {
        "officer": officer,
        "activity": page_obj,
        "page_obj": page_obj,
        "is_paginated": page_obj.has_other_pages(),
        "query_string": query_string,
        "filters": {"type": activity_type},
        "activity_type_filters": [
            {"value": "all", "label": "All activity"},
            {"value": "application", "label": "Applications"},
            {"value": "payment", "label": "Payments"},
            {"value": "disbursement", "label": "Disbursements"},
        ],
        "activity_summary": [
            {"label": "Applications processed", "value": application_count, "note": "Decisions recorded"},
            {"label": "Payments recorded", "value": payment_count, "note": f"₱{payment_total:,.0f} collected"},
            {"label": "Disbursements released", "value": disbursement_count, "note": f"₱{disbursed_total:,.0f} principal"},
        ],
    })


def _activity_sort_key(value):
    if isinstance(value, datetime):
        return value if timezone.is_aware(value) else timezone.make_aware(value)
    return timezone.make_aware(datetime.combine(value, datetime.min.time()))


@login_required
@role_required("officer")
def borrower_detail(request, borrower_id):
    borrower = get_object_or_404(User.member_accounts(), pk=borrower_id)
    loans = list(
        Loan.objects.filter(application__borrower=borrower).select_related("application", "application__loan_product")
    )
    for loan in loans:
        loan.next_due = next_due_for_display(loan, application_schedule_view_mode(loan))
    applications = borrower.loan_applications.select_related("loan_product", "reviewed_by")

    activity = []
    for application in applications:
        activity.append({
            "title": f"{application.reference} submitted",
            "description": f"{application.product_name} · ₱{application.amount_requested:,.2f} requested",
            "author": application.borrower_name,
            "created_at": application.created_at,
            "complete": True,
        })
        if application.decision_date:
            activity.append({
                "title": f"{application.reference} {application.status_label.lower()}",
                "description": application.review_notes or f"Marked {application.status_label.lower()} by {application.assigned_officer or 'the system'}.",
                "author": application.assigned_officer or "System",
                "created_at": application.decision_date,
                "complete": True,
            })
    for loan in loans:
        activity.append({
            "title": f"{loan.reference} disbursed",
            "description": f"₱{loan.principal:,.2f} released via {loan.disbursement_method}",
            "author": "System",
            "created_at": loan.disbursed_date,
            "complete": True,
        })
        for payment in loan.payments.select_related("recorded_by"):
            activity.append({
                "title": f"Payment recorded · {loan.reference}",
                "description": f"₱{payment.amount:,.2f} via {payment.get_method_display()}",
                "author": payment.recorded_by.display_name() if payment.recorded_by else "System",
                "created_at": payment.payment_date,
                "complete": True,
            })
    activity.sort(key=lambda event: _activity_sort_key(event["created_at"]), reverse=True)
    for event in activity[:10]:
        created_at = event["created_at"]
        event["created_at"] = (
            timezone.localtime(created_at).strftime("%b %d, %Y · %I:%M %p")
            if isinstance(created_at, datetime)
            else created_at.strftime("%b %d, %Y")
        )
    activity = activity[:10]

    return render(request, "officer/borrower_detail.html", {
        "borrower": borrower,
        "applications": applications,
        "loans": loans,
        "borrower_snapshot": [
            {"label": "Borrower ID", "value": borrower.reference},
            {"label": "Username", "value": borrower.username},
            {"label": "Full name", "value": borrower.full_name or "Not provided"},
            {"label": "Email", "value": borrower.email or "Not provided"},
            {"label": "Phone", "value": borrower.phone or "Not provided"},
            {"label": "Address", "value": borrower.address or "Not provided"},
            {"label": "Date of birth", "value": borrower.date_of_birth.strftime("%b %d, %Y") if borrower.date_of_birth else "Not provided"},
            {"label": "Employment", "value": borrower.employment_status or "Not provided"},
            {"label": "Monthly income", "value": f"₱{borrower.monthly_income:,.0f}" if borrower.monthly_income else "Not provided"},
            {"label": "Credit score", "value": format_credit_score(borrower.credit_score)},
            {"label": "Account status", "value": borrower.status_label},
            {"label": "Member since", "value": borrower.joined_at},
            {"label": "Last login", "value": timezone.localtime(borrower.last_login).strftime("%b %d, %Y · %I:%M %p") if borrower.last_login else "Never logged in"},
        ],
        "activity": activity,
    })


@login_required
@role_required("officer")
def officer_loan_detail(request, loan_id):
    loan = get_object_or_404(Loan.objects.select_related("application", "application__borrower", "application__loan_product"), pk=loan_id)
    mark_overdue_installments()
    payments = loan.payments.select_related("recorded_by", "installment").order_by("-payment_date", "-id")
    next_due = next_due_for_display(loan, application_schedule_view_mode(loan))
    can_extend = can_extend_loan_balance(loan)
    extension_form = BalanceExtensionForm(request.POST or None) if can_extend else None
    extension_previews = [
        {
            "months": row["months"],
            "principal": str(row["principal"]),
            "total_interest": str(row["total_interest"]),
            "total_payable": str(row["total_payable"]),
            "per_day": str(row["per_day"]),
            "per_month": str(row["per_month"]),
        }
        for row in (balance_extension_previews(loan) if can_extend else [])
    ]

    if request.method == "POST" and can_extend and extension_form.is_valid():
        try:
            loan, quote = extend_loan_balance(loan, extension_form.cleaned_data["months"])
        except BalanceExtensionError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(
                request,
                (
                    f"Remaining balance restructured over {quote['months']} month"
                    f"{'s' if quote['months'] != 1 else ''} at {BALANCE_EXTENSION_RATE}% interest. "
                    f"New total due: ₱{quote['total_payable']:,.2f}."
                ),
            )
            return redirect("officer_loan_detail", loan_id=loan.id)

    return render(
        request,
        "officer/loan_detail.html",
        {
            "loan": loan,
            "payments": payments,
            "next_due": next_due,
            "can_extend_balance": can_extend,
            "extension_form": extension_form,
            "extension_previews": extension_previews,
            "extension_rate": BALANCE_EXTENSION_RATE,
        },
    )


@login_required
@role_required("officer")
def officer_schedule(request, loan_id):
    loan = get_object_or_404(
        Loan.objects.select_related("application", "application__borrower", "application__loan_product"),
        pk=loan_id,
    )
    mark_overdue_installments()
    ensure_schedule_current(loan)

    view_mode = request.GET.get("view") or application_schedule_view_mode(loan)
    month = request.GET.get("month", "")
    plan = (request.GET.get("plan") or "current").lower()
    showing_original = bool(loan.is_rescheduled and plan == "original")
    if showing_original:
        display = original_schedule_display_rows(loan, view_mode=view_mode, month=month)
        original_terms = display.get("original_terms") or loan.original_schedule_terms()
        next_due = None
    else:
        display = schedule_display_rows(loan, view_mode=view_mode, month=month)
        original_terms = loan.original_schedule_terms() if loan.is_rescheduled else None
        next_due = next_due_for_display(loan, display["view_mode"])

    return render(
        request,
        "officer/schedule.html",
        {
            "loan": loan,
            "installments": loan.installments.all(),
            "schedule": display["schedule"],
            "payment_count": display["payment_count"],
            "paid_payment_count": display["paid_payment_count"],
            "next_payment": None if showing_original else loan.next_installment,
            "next_due": next_due,
            "view_mode": display["view_mode"],
            "month_options": display["month_options"],
            "selected_month": display["selected_month"],
            "application_pay_frequency": loan.application.payment_frequency,
            "schedule_plan": "original" if showing_original else "current",
            "showing_original": showing_original,
            "original_terms": original_terms,
        },
    )


@login_required
@role_required("officer")
def officer_make_payment(request, loan_id):
    loan = get_object_or_404(Loan.objects.select_related("application", "application__borrower"), pk=loan_id)
    installment_id = request.GET.get("installment")
    installment = loan.installments.filter(pk=installment_id).first() if installment_id else None
    max_amount = installment.remaining if installment else loan.outstanding_balance
    max_amount_label = "remaining on this installment" if installment else "outstanding balance"

    pay_frequency = request.POST.get("pay_frequency") or request.GET.get("pay") or (loan.application.payment_frequency or "daily")
    if pay_frequency not in {"daily", "weekly", "biweekly", "monthly"}:
        pay_frequency = "daily"

    def _capped(frequency):
        amount = loan.suggested_payment_for(frequency)
        return min(amount, max_amount) if max_amount is not None else amount

    pay_amounts = {
        "daily": _capped("daily"),
        "weekly": _capped("weekly"),
        "biweekly": _capped("biweekly"),
        "monthly": _capped("monthly"),
    }
    suggested = pay_amounts[pay_frequency]
    form = PaymentForm(
        request.POST or None,
        instance=Payment(installment=installment),
        initial={"amount": suggested},
        max_amount=max_amount,
        max_amount_label=max_amount_label,
    )
    if request.method == "POST" and form.is_valid():
        payment = record_payment(loan, form.cleaned_data["amount"], form.cleaned_data["method"], form.cleaned_data["reference_number"], request.user, installment)
        messages.success(request, f"Payment of ₱{payment.amount:,.2f} recorded for {loan.reference}.")
        return redirect("payment_receipt", payment_id=payment.pk)
    return render(
        request,
        "officer/payment_form.html",
        {
            "form": form,
            "loan": loan,
            "installment": installment,
            "max_payment_amount": max_amount,
            "max_payment_label": max_amount_label,
            "pay_frequency": pay_frequency,
            "pay_frequency_choices": [
                ("daily", "Daily"),
                ("weekly", "Weekly"),
                ("biweekly", "Biweekly"),
                ("monthly", "Monthly"),
            ],
            "pay_amounts": pay_amounts,
        },
    )


@login_required
@role_required("officer")
def disbursement(request):
    ready = LoanApplication.objects.filter(status=LoanApplication.Status.APPROVED).select_related("borrower", "loan_product")
    for item in ready:
        item.destination_masked = item.reference
        item.destination_type = "Reference number"
        item.approved_at = item.decision_date.strftime("%b %d, %Y") if item.decision_date else "Recently"
        item.check_status = "approved"
        item.check_status_label = "Ready"
    released_qs = (
        Loan.objects.select_related("application", "application__borrower", "application__loan_product")
        .order_by("-disbursed_date", "-id")
    )
    released_paginator = Paginator(released_qs, 10)
    released_page = released_paginator.get_page(request.GET.get("page"))
    return render(request, "officer/disbursements.html", {
        "applications": ready,
        "disbursements": ready,
        "released_loans": released_page,
        "released_total_count": released_paginator.count,
        "page_obj": released_page,
        "is_paginated": released_page.has_other_pages(),
        "ready_count": ready.count(),
        "disbursement_metrics": [
            {"label": "Ready to release", "value": f"₱{sum((item.amount_requested for item in ready), Decimal('0')):,.0f}", "note": "Approved principal"},
            {"label": "Awaiting check", "value": ready.count(), "note": "Two-part verification"},
            {"label": "Released this month", "value": f"₱{Loan.objects.filter(disbursed_date__month=timezone.localdate().month).aggregate(value=Sum('principal'))['value'] or Decimal('0'):,.0f}", "note": "Recorded in portfolio"},
        ],
    })


@login_required
@role_required("officer")
def disburse(request, disbursement_id):
    application = get_object_or_404(
        LoanApplication,
        pk=disbursement_id,
        status__in=[LoanApplication.Status.APPROVED, LoanApplication.Status.ACTIVE, LoanApplication.Status.DISBURSED],
    )
    existing_loan = getattr(application, "loan", None)
    deductions = standard_disbursement_deductions()
    if existing_loan:
        return render(
            request,
            "officer/disburse_form.html",
            {
                "application": application,
                "existing_loan": existing_loan,
                "standard_deductions": deductions,
            },
        )
    if request.method == "POST":
        amount = application.amount_requested
        processing_fee = deductions["processing_fee"]
        other_fees = deductions["other_fees"]
        if deductions["total"] > amount:
            messages.error(request, "Standard deductions exceed the amount to release.")
            return render(
                request,
                "officer/disburse_form.html",
                {
                    "application": application,
                    "form_data": request.POST,
                    "default_disbursed_date": timezone.localdate().isoformat(),
                    "standard_deductions": deductions,
                    "net_release_preview": amount - deductions["total"],
                    "processing_fee_error": "Standard deductions exceed the amount to release.",
                },
            )
        loan = disburse_application(
            application,
            amount,
            request.POST.get("method", "Bank transfer"),
            request.POST.get("reference", ""),
            disbursed_date=_parse_disbursed_date(request.POST.get("disbursed_date")),
            processing_fee=processing_fee,
            other_fees=other_fees,
            other_fees_description=deductions["other_fees_description"],
            disbursed_by=request.user,
        )
        membership_deposit = next(
            (item["amount"] for item in deductions["line_items"] if item["key"] == "membership_savings"),
            Decimal("0.00"),
        )
        kap_contribution = next(
            (item["amount"] for item in deductions["line_items"] if item["key"] == "kap_mutual_aid"),
            Decimal("0.00"),
        )
        extras = []
        if membership_deposit > 0:
            extras.append(f"₱{membership_deposit:,.2f} credited to Membership/Savings Deposit")
        if kap_contribution > 0:
            extras.append(f"₱{kap_contribution:,.2f} recorded for KAPAMILYA MUTUAL AID")
        if extras:
            messages.success(
                request,
                f"{loan.reference} is now active, its repayment schedule has been generated, "
                f"and {', '.join(extras)}.",
            )
        else:
            messages.success(request, f"{loan.reference} is now active and its repayment schedule has been generated.")
        return redirect("disbursement_receipt", disbursement_id=application.pk)
    amount = application.amount_requested
    return render(request, "officer/disburse_form.html", {
        "application": application,
        "form_data": {},
        "default_disbursed_date": timezone.localdate().isoformat(),
        "standard_deductions": deductions,
        "net_release_preview": amount - deductions["total"],
    })


@login_required
@role_required("officer")
def reports(request):
    report_periods = [
        {"value": "30", "label": "Last 30 days"},
        {"value": "90", "label": "Last 90 days"},
        {"value": "365", "label": "Last 12 months"},
    ]
    period_days = {"30": 30, "90": 90, "365": 365}
    selected_period = request.GET.get("period", "30")
    if selected_period not in period_days:
        selected_period = "30"
    period_label = next(p["label"] for p in report_periods if p["value"] == selected_period)

    loans = Loan.objects.select_related("application", "application__loan_product")
    product_id = request.GET.get("product", "")
    if product_id:
        loans = loans.filter(application__loan_product_id=product_id)

    cutoff = timezone.localdate() - timedelta(days=period_days[selected_period])
    period_loans = loans.filter(disbursed_date__gte=cutoff)

    total_disbursed = period_loans.aggregate(value=Sum("principal"))["value"] or Decimal("0.00")
    collected = period_loans.aggregate(value=Sum("payments__amount"))["value"] or Decimal("0.00")
    outstanding = period_loans.aggregate(value=Sum("outstanding_balance"))["value"] or Decimal("0.00")

    period_count = period_loans.count()
    defaulted_count = period_loans.filter(status=Loan.Status.DEFAULTED).count()
    default_rate = (Decimal(defaulted_count) / Decimal(period_count) * 100) if period_count else Decimal("0.00")

    report_metrics = [
        {"label": "Disbursed", "value": f"₱{total_disbursed:,.0f}", "note": "Principal released", "positive": True},
        {"label": "Collected", "value": f"₱{collected:,.0f}", "note": "Payments recorded", "positive": True},
        {"label": "Outstanding", "value": f"₱{outstanding:,.0f}", "note": "Current balance"},
        {"label": "Default rate", "value": f"{default_rate:.1f}%", "note": "Of loans in this period"},
    ]

    today = timezone.localdate()
    year_start = today.replace(month=1, day=1)
    year_end = today.replace(month=12, day=31)
    year_loans = loans.filter(disbursed_date__gte=year_start, disbursed_date__lte=year_end)
    monthly = (
        year_loans
        .annotate(month=TruncMonth("disbursed_date"))
        .values("month")
        .annotate(total=Sum("principal"))
        .order_by("month")
    )
    month_totals = {row["month"].month: row["total"] for row in monthly}
    max_month_total = max(month_totals.values(), default=Decimal("0.00"))
    chart_max = max_month_total or Decimal("1.00")
    month_labels = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    origination_chart = []
    for index, label in enumerate(month_labels):
        month_num = index + 1
        value = month_totals.get(month_num, Decimal("0.00"))
        height = max(6, int((value / chart_max) * 100)) if value else 0
        origination_chart.append({
            "label": label,
            "height": height,
            "value": value,
            "is_current": month_num == today.month,
        })
    origination_chart_y_ticks = [
        (chart_max * Decimal(i) / Decimal("4")).quantize(Decimal("1"))
        for i in range(4, -1, -1)
    ]
    origination_year_total = sum(month_totals.values(), Decimal("0.00"))
    origination_chart_period_label = f"Jan – Dec {today.year}"

    mix_colors = ["var(--teal)", "var(--apricot)", "var(--aqua)", "var(--sand)"]
    product_mix = (
        period_loans
        .values("application__loan_product__name")
        .annotate(total_principal=Sum("principal"), total_outstanding=Sum("outstanding_balance"))
        .order_by("-total_principal")
    )
    portfolio_mix = [
        {
            "label": row["application__loan_product__name"],
            "percentage": round((row["total_principal"] / total_disbursed) * 100) if total_disbursed else 0,
            "amount": row["total_outstanding"],
            "color": mix_colors[index % len(mix_colors)],
        }
        for index, row in enumerate(product_mix[:3])
    ]

    return render(request, "officer/reports.html", {
        "loans": period_loans,
        "total_disbursed": total_disbursed,
        "collected": collected,
        "outstanding": outstanding,
        "default_rate": default_rate,
        "loan_type_filter": product_id,
        "report_periods": report_periods,
        "selected_period": selected_period,
        "products": [
            {"value": str(product.pk), "label": f"{product.name} ({product.get_loan_type_display()})"}
            for product in LoanProduct.objects.all().order_by("name")
        ],
        "report_metrics": report_metrics,
        "origination_chart": origination_chart,
        "origination_chart_y_ticks": origination_chart_y_ticks,
        "origination_year_total": origination_year_total,
        "origination_chart_period_label": origination_chart_period_label,
        "chart_period_label": period_label,
        "portfolio_mix": portfolio_mix,
        "available_exports": [
            {"label": "Portfolio CSV", "description": "All disbursed loans", "url": reverse("export_portfolio_csv")},
            {"label": "Application CSV", "description": "Application queue", "url": reverse("export_applications_csv")},
            {"label": "Borrower CSV", "description": "Borrower directory", "url": reverse("export_borrowers_csv")},
        ],
    })


@login_required
@role_required("officer")
def export_portfolio_csv(request):
    loans = Loan.objects.select_related("application__borrower", "application__loan_product")
    product_id = request.GET.get("product", "")
    if product_id:
        loans = loans.filter(application__loan_product_id=product_id)
    period_days = {"30": 30, "90": 90, "365": 365}
    period = request.GET.get("period", "")
    if period in period_days:
        cutoff = timezone.localdate() - timedelta(days=period_days[period])
        loans = loans.filter(disbursed_date__gte=cutoff)
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="lumen-loan-portfolio.csv"'
    writer = csv.writer(response)
    writer.writerow(["Loan ID", "Borrower", "Type", "Principal", "Outstanding", "Status", "Disbursed"])
    for loan in loans:
        writer.writerow([loan.reference, loan.application.borrower.display_name(), loan.application.loan_product.get_loan_type_display(), loan.principal, loan.outstanding_balance, loan.get_status_display(), loan.disbursed_date])
    return response


@login_required
@role_required("officer")
def export_applications_csv(request):
    applications = LoanApplication.objects.select_related("borrower", "loan_product")
    query = request.GET.get("q", "").strip()
    status = request.GET.get("status", "")
    loan_type = request.GET.get("product", "")
    if query:
        applications = applications.filter(Q(borrower__full_name__icontains=query) | Q(borrower__email__icontains=query) | Q(pk__icontains=query))
    if status:
        applications = applications.filter(status=status)
    if loan_type:
        applications = applications.filter(loan_product__loan_type=loan_type)
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="lumen-application-queue.csv"'
    writer = csv.writer(response)
    writer.writerow(["Reference", "Borrower", "Email", "Product", "Amount requested", "Term (months)", "Status", "Applied"])
    for application in applications:
        writer.writerow([application.reference, application.borrower_name, application.email, application.product_name, application.amount_requested, application.term_months, application.status_label, application.submitted_at])
    return response


@login_required
@role_required("officer")
def export_borrowers_csv(request):
    members = User.member_accounts()
    query = request.GET.get("q", "").strip()
    status = request.GET.get("status", "").strip()
    if query:
        members = members.filter(Q(full_name__icontains=query) | Q(email__icontains=query) | Q(phone__icontains=query))
    if status == "active":
        members = members.filter(is_active=True)
    elif status == "inactive":
        members = members.filter(is_active=False)
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="lumen-borrower-directory.csv"'
    writer = csv.writer(response)
    writer.writerow(["Name", "Email", "Phone", "Monthly income", "Credit score", "Status", "Joined"])
    for member in members:
        writer.writerow([
            member.display_name(),
            member.email,
            member.phone,
            member.monthly_income or "",
            format_credit_score(member.credit_score),
            "Active" if member.is_active else "Inactive",
            member.joined_at,
        ])
    return response


@login_required
@role_required("officer")
def export_disbursements_csv(request):
    ready = LoanApplication.objects.filter(status=LoanApplication.Status.APPROVED).select_related("borrower", "loan_product")
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="lumen-disbursement-queue.csv"'
    writer = csv.writer(response)
    writer.writerow(["Reference", "Borrower", "Product", "Amount requested", "Approved on"])
    for application in ready:
        writer.writerow([application.reference, application.borrower_name, application.product_name, application.amount_requested, application.decision_date.strftime("%b %d, %Y") if application.decision_date else ""])
    return response


@login_required
def schedule_export(request, loan_id):
    loan_qs = Loan.objects if request.user.is_officer else Loan.objects.filter(application__borrower=request.user)
    loan = get_object_or_404(loan_qs, pk=loan_id)
    ensure_schedule_current(loan)
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="{loan.reference}-schedule.csv"'
    writer = csv.writer(response)
    writer.writerow(["Installment", "Due date", "Principal", "Interest", "Amount due", "Adjusted", "Amount paid", "Status"])
    for item in loan.installments.all():
        writer.writerow([item.installment_number, item.due_date, item.principal_component, item.interest_component, item.amount_due, item.adjusted_amount, item.amount_paid, item.get_status_display()])
    return response


@login_required
def payment_receipt(request, payment_id):
    payment_qs = Payment.objects.select_related(
        "loan", "loan__application", "loan__application__borrower", "loan__application__loan_product", "installment", "recorded_by"
    )
    if not request.user.is_officer:
        payment_qs = payment_qs.filter(loan__application__borrower=request.user)
    payment = get_object_or_404(payment_qs, pk=payment_id)
    loan = payment.loan
    paid_through = loan.payments.filter(pk__lte=payment.pk).aggregate(total=Sum("amount"))["total"] or Decimal("0.00")
    balance_after = max(Decimal("0.00"), loan.total_payable - paid_through)
    balance_before = balance_after + payment.amount
    return render(request, "shared/payment_receipt.html", {
        "payment": payment,
        "loan": loan,
        "balance_before": balance_before,
        "balance_after": balance_after,
    })


@login_required
def disbursement_receipt(request, disbursement_id):
    application_qs = LoanApplication.objects.select_related("borrower", "loan_product", "reviewed_by")
    if not request.user.is_officer:
        application_qs = application_qs.filter(borrower=request.user)
    application = get_object_or_404(application_qs, pk=disbursement_id)
    loan = get_object_or_404(
        Loan.objects.select_related("application", "application__borrower", "application__loan_product"),
        application=application,
    )
    return render(request, "shared/disbursement_receipt.html", {
        "loan": loan,
        "application": application,
    })


@login_required
@role_required("officer")
def application_decision(request, application_id):
    application = get_object_or_404(LoanApplication, pk=application_id)
    if request.method == "POST":
        decision = request.POST.get("decision")
        if decision == "approve" and not request.user.is_admin:
            messages.error(request, "Only administrators can approve loan applications.")
            return redirect("application_review", application_id=application_id)
        application.reviewed_by = request.user
        application.review_notes = request.POST.get("reason", "")
        application.decision_date = timezone.now()
        if decision == "approve":
            application.status = LoanApplication.Status.APPROVED
            messages.success(request, "Application approved and added to the disbursement queue.")
        elif decision == "request_info":
            application.status = LoanApplication.Status.UNDER_REVIEW
            messages.info(request, "Application marked for more information.")
        else:
            application.status = LoanApplication.Status.REJECTED
            messages.success(request, "Application declined with a decision note.")
        application.save()
        if application.status == LoanApplication.Status.APPROVED:
            reject_superseded_applications(application, request.user)
            return redirect("disbursement_detail", disbursement_id=application_id)
    return redirect("application_review", application_id=application_id)


@login_required
@role_required("officer")
def add_review_note(request, application_id):
    application = get_object_or_404(LoanApplication, pk=application_id)
    if request.method == "POST" and request.POST.get("body"):
        application.reviewed_by = request.user
        application.review_notes = request.POST["body"]
        application.save(update_fields=["reviewed_by", "review_notes"])
        messages.success(request, "Private review note added.")
    return redirect("application_review", application_id=application_id)


@login_required
def profile(request):
    form = ProfileForm(request.POST or None, instance=request.user)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Profile updated successfully.")
        return redirect("profile")
    completion = profile_completion(request.user)
    return render(request, "profile.html", {
        "form": form,
        "completion": completion,
        "active_nav": "profile",
    })


def simple_page(request, title, description):
    return render(request, "simple_page.html", {"title": title, "description": description})


@login_required
def notifications(request):
    qs = Notification.objects.filter(user=request.user)
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "mark_all_read":
            qs.filter(is_read=False).update(is_read=True)
            messages.success(request, "All notifications marked as read.")
            return redirect("notifications")
        if action == "delete_selected":
            selected_ids = request.POST.getlist("notification_ids")
            deleted, _ = qs.filter(pk__in=selected_ids).delete()
            if deleted:
                messages.success(
                    request,
                    f"Deleted {deleted} notification{'s' if deleted != 1 else ''}.",
                )
            else:
                messages.info(request, "Select at least one notification to delete.")
            return redirect("notifications")
        if action == "delete_all":
            deleted, _ = qs.delete()
            if deleted:
                messages.success(request, "All notifications deleted.")
            return redirect("notifications")
    items = list(qs[:50])
    unread_count = sum(1 for item in items if not item.is_read)
    return render(
        request,
        "notifications.html",
        {
            "notifications": items,
            "unread_count": unread_count,
            "title": "Notifications",
        },
    )


@login_required
def estimate(request):
    try:
        product = get_object_or_404(LoanProduct, pk=request.GET.get("product"))
        amount = Decimal(request.GET.get("amount", "0"))
        term = int(request.GET.get("term", product.min_term_months))
        return JsonResponse({"interest_rate": str(product.interest_rate), "processing_fee": str(product.fee_for(amount)), "monthly_payment": str(product.estimate_payment(amount, term)), "term_months": term})
    except (ValueError, TypeError, ArithmeticError):
        return JsonResponse({"error": "Enter a valid amount and term."}, status=400)


def profile_completion(user):
    fields = [user.full_name, user.phone, user.address, user.date_of_birth, user.employment_status, user.monthly_income]
    return round(sum(bool(field) for field in fields) / len(fields) * 100)