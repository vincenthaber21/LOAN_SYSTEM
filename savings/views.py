from decimal import Decimal
import csv

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q, Sum
from django.db.models.functions import Coalesce
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from lending.decorators import role_required
from lending.models import User

from .forms import (
    OfficerOpenAccountForm,
    OfficerSavingsTransactionForm,
    OpenAccountForm,
    SavingsInterestExportForm,
    SavingsProductForm,
    SavingsTransactionForm,
    available_savings_products_for_member,
    unavailable_savings_product_ids_for_member,
)
from .models import SavingsAccount, SavingsProduct, SavingsTransaction
from .reports import default_export_dates, interest_report_context, total_interest_earned
from .services import (
    SavingsError,
    apply_due_interest,
    close_account,
    interest_schedule_state,
    open_account,
    record_deposit,
    record_withdrawal,
)


def _ledger_summary(account, last_movement=None):
    qs = account.transactions.all()
    agg = qs.aggregate(
        total_deposits=Sum("amount", filter=Q(transaction_type=SavingsTransaction.Type.DEPOSIT)),
        total_interest_earned=Sum("amount", filter=Q(transaction_type=SavingsTransaction.Type.INTEREST)),
        total_withdrawals=Sum("amount", filter=Q(transaction_type=SavingsTransaction.Type.WITHDRAWAL)),
        transaction_count=Count("id"),
    )
    total_deposits = agg["total_deposits"] or Decimal("0.00")
    total_interest_earned = agg["total_interest_earned"] or Decimal("0.00")
    total_withdrawals = agg["total_withdrawals"] or Decimal("0.00")
    if last_movement is None:
        last_movement = qs.select_related("created_by").order_by("-created_at").first()
    return {
        "total_deposits": total_deposits,
        "total_interest_earned": total_interest_earned,
        "total_withdrawals": total_withdrawals,
        "net_flow": total_deposits + total_interest_earned - total_withdrawals,
        "transaction_count": agg["transaction_count"] or 0,
        "last_movement": last_movement,
    }


def _auto_apply_due_interest(account, user):
    """Apply overdue interest credits when an officer opens the account page."""
    if not account.is_operational:
        return None
    interest = interest_schedule_state(account)
    if not interest["is_due"]:
        return None
    applied = apply_due_interest(account, created_by=user, state=interest)
    if not applied:
        return None
    total = sum(tx.amount for tx in applied)
    return len(applied), total


def _member_accounts(user):
    return SavingsAccount.objects.filter(member=user).select_related("product")


@login_required
@role_required("member")
def savings_dashboard(request):
    accounts = _member_accounts(request.user).filter(status=SavingsAccount.Status.ACTIVE)
    total_balance = accounts.aggregate(value=Sum("balance"))["value"] or Decimal("0.00")
    recent_transactions = (
        SavingsTransaction.objects.filter(account__member=request.user)
        .select_related("account", "account__product")
        .order_by("-created_at")[:8]
    )
    available_products = SavingsProduct.objects.filter(is_active=True).exclude(
        pk__in=accounts.values_list("product_id", flat=True)
    )
    return render(request, "savings/dashboard.html", {
        "accounts": accounts,
        "total_balance": total_balance,
        "recent_transactions": recent_transactions,
        "available_products": available_products,
        "account_count": accounts.count(),
    })


@login_required
@role_required("member")
def savings_open_account(request):
    form = OpenAccountForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            account = open_account(
                request.user,
                form.cleaned_data["product"],
                opened_by=request.user,
                initial_deposit=form.cleaned_data.get("initial_deposit"),
            )
            messages.success(request, f"Savings account {account.reference} is ready.")
            return redirect("savings_account_detail", account_id=account.pk)
        except SavingsError as exc:
            messages.error(request, str(exc))
    return render(request, "savings/open_account.html", {"form": form})


@login_required
@role_required("member")
def savings_account_detail(request, account_id):
    account = get_object_or_404(_member_accounts(request.user), pk=account_id)
    transactions = account.transactions.select_related("created_by").order_by("-created_at")[:50]
    return render(request, "savings/account_detail.html", {
        "account": account,
        "transactions": transactions,
    })


@login_required
@role_required("member")
def savings_deposit(request, account_id):
    account = get_object_or_404(_member_accounts(request.user), pk=account_id, status=SavingsAccount.Status.ACTIVE)
    form = SavingsTransactionForm(request.POST or None, account=account, transaction_type="deposit")
    if request.method == "POST" and form.is_valid():
        try:
            record_deposit(
                account,
                form.cleaned_data["amount"],
                form.cleaned_data["method"],
                reference=form.cleaned_data.get("reference_number", ""),
                created_by=request.user,
                notes=form.cleaned_data.get("notes", ""),
            )
            messages.success(request, "Deposit recorded successfully.")
            return redirect("savings_account_detail", account_id=account.pk)
        except SavingsError as exc:
            messages.error(request, str(exc))
    return render(request, "savings/transaction_form.html", {
        "account": account,
        "form": form,
        "action": "deposit",
        "action_label": "Deposit",
    })


@login_required
@role_required("member")
def savings_withdraw(request, account_id):
    account = get_object_or_404(_member_accounts(request.user), pk=account_id, status=SavingsAccount.Status.ACTIVE)
    form = SavingsTransactionForm(request.POST or None, account=account, transaction_type="withdrawal")
    if request.method == "POST" and form.is_valid():
        try:
            record_withdrawal(
                account,
                form.cleaned_data["amount"],
                form.cleaned_data["method"],
                reference=form.cleaned_data.get("reference_number", ""),
                created_by=request.user,
                notes=form.cleaned_data.get("notes", ""),
            )
            messages.success(request, "Withdrawal recorded successfully.")
            return redirect("savings_account_detail", account_id=account.pk)
        except SavingsError as exc:
            messages.error(request, str(exc))
    return render(request, "savings/transaction_form.html", {
        "account": account,
        "form": form,
        "action": "withdraw",
        "action_label": "Withdraw",
    })


@login_required
@role_required("officer")
def officer_savings_accounts(request):
    query = request.GET.get("q", "").strip()
    status = request.GET.get("status", "").strip()
    qs = SavingsAccount.objects.select_related("member", "product").annotate(
        transaction_count=Count("transactions"),
        total_interest_earned=Coalesce(
            Sum(
                "transactions__amount",
                filter=Q(transactions__transaction_type=SavingsTransaction.Type.INTEREST),
            ),
            Decimal("0.00"),
        ),
    )
    if query:
        qs = qs.filter(
            Q(account_number__icontains=query)
            | Q(member__full_name__icontains=query)
            | Q(member__email__icontains=query)
        )
    if status:
        qs = qs.filter(status=status)
    qs = qs.order_by("-opened_at")
    total_balance = qs.filter(status=SavingsAccount.Status.ACTIVE).aggregate(value=Sum("balance"))["value"] or Decimal("0.00")
    active_count = qs.filter(status=SavingsAccount.Status.ACTIVE).count()
    total_interest_all_time = total_interest_earned(accounts=qs)
    date_from, date_to = default_export_dates()
    return render(request, "officer/savings_accounts.html", {
        "accounts": qs,
        "account_count": qs.count(),
        "active_count": active_count,
        "total_balance": total_balance,
        "total_interest_all_time": total_interest_all_time,
        "filters": {"q": query, "status": status},
        "status_filters": SavingsAccount.Status.choices,
        "interest_export_form": SavingsInterestExportForm(initial={"date_from": date_from, "date_to": date_to}),
    })


@login_required
@role_required("officer")
def officer_export_savings_interest(request):
    export_format = request.GET.get("format", "csv")
    form = SavingsInterestExportForm(request.GET)
    if not form.is_valid():
        for field_errors in form.errors.values():
            for error in field_errors:
                messages.error(request, error)
        return redirect("officer_savings_accounts")

    date_from = form.cleaned_data["date_from"]
    date_to = form.cleaned_data["date_to"]
    report = interest_report_context(date_from, date_to)

    if export_format == "pdf":
        return render(request, "officer/savings_interest_report.html", report)

    response = HttpResponse(content_type="text/csv")
    filename = f"savings-interest-{date_from:%Y%m%d}-{date_to:%Y%m%d}.csv"
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    writer = csv.writer(response)
    writer.writerow(["Savings interest report"])
    writer.writerow(["Period", f"{date_from:%Y-%m-%d}", "to", f"{date_to:%Y-%m-%d}"])
    writer.writerow(["Generated", report["generated_at"].strftime("%Y-%m-%d %H:%M")])
    writer.writerow([])
    writer.writerow(["Member summary"])
    writer.writerow(["Member", "Email", "Accounts", "Interest credits", "Total interest earned"])
    for row in report["summary"]:
        writer.writerow([row["name"], row["email"], row["accounts"], row["credits"], row["total"]])
    writer.writerow(["Total", "", "", report["credit_count"], report["grand_total"]])
    writer.writerow([])
    writer.writerow(["Transaction detail"])
    writer.writerow(["Date", "Member", "Email", "Account", "Product", "Amount", "Reference", "Notes"])
    for tx in report["transactions"]:
        member = tx.account.member
        writer.writerow([
            timezone.localtime(tx.created_at).strftime("%Y-%m-%d"),
            member.display_name(),
            member.email,
            tx.account.reference,
            tx.account.product_name,
            tx.amount,
            tx.reference_number,
            tx.notes,
        ])
    return response


@login_required
@role_required("officer")
def officer_savings_account_detail(request, account_id):
    account = get_object_or_404(
        SavingsAccount.objects.select_related("member", "product"),
        pk=account_id,
    )
    transaction_form = OfficerSavingsTransactionForm(account=account)
    if request.method == "POST":
        action = request.POST.get("action")
        if action in {"deposit", "withdraw"}:
            transaction_form = OfficerSavingsTransactionForm(request.POST, account=account)
            if transaction_form.is_valid():
                data = transaction_form.cleaned_data
                try:
                    if data["action"] == "deposit":
                        record_deposit(
                            account,
                            data["amount"],
                            data["method"],
                            reference=data.get("reference_number", ""),
                            created_by=request.user,
                            notes=data.get("notes", ""),
                            occurred_on=data.get("transaction_date"),
                        )
                        messages.success(request, "Deposit recorded.")
                    else:
                        record_withdrawal(
                            account,
                            data["amount"],
                            data["method"],
                            reference=data.get("reference_number", ""),
                            created_by=request.user,
                            notes=data.get("notes", ""),
                            occurred_on=data.get("transaction_date"),
                        )
                        messages.success(request, "Withdrawal recorded.")
                    return redirect("officer_savings_account_detail", account_id=account.pk)
                except SavingsError as exc:
                    messages.error(request, str(exc))
        elif action == "close" and request.user.is_admin:
            try:
                close_account(account, closed_by=request.user)
                messages.success(request, "Account closed.")
                return redirect("officer_savings_accounts")
            except SavingsError as exc:
                messages.error(request, str(exc))

    try:
        applied_info = _auto_apply_due_interest(account, request.user)
    except SavingsError as exc:
        applied_info = None
        messages.error(request, str(exc))
    else:
        if applied_info:
            count, total = applied_info
            account.refresh_from_db()
            messages.success(
                request,
                f"Applied {count} interest credit{'s' if count != 1 else ''} totaling ₱{total:,.2f} automatically.",
            )

    transactions = list(
        account.transactions.select_related("created_by").order_by("-created_at")[:50]
    )
    last_movement = transactions[0] if transactions else None
    ledger = _ledger_summary(account, last_movement=last_movement)
    interest = interest_schedule_state(account)
    return render(request, "officer/savings_account_detail.html", {
        "account": account,
        "transactions": transactions,
        "ledger": ledger,
        "interest": interest,
        "transaction_form": transaction_form,
    })


@login_required
@role_required("officer")
def officer_available_savings_products(request):
    member_id = request.GET.get("member")
    member = User.member_accounts().filter(pk=member_id, is_active=True).first()
    member_inactive = bool(member_id) and not member
    blocked_product_count = len(unavailable_savings_product_ids_for_member(member)) if member else 0
    if member_inactive:
        products = SavingsProduct.objects.none()
    else:
        products = available_savings_products_for_member(member)
    return JsonResponse({
        "member_inactive": member_inactive,
        "blocked_product_count": blocked_product_count,
        "products": [
            {
                "id": product.pk,
                "label": f"{product.name} ({product.interest_rate}% p.a.)",
                "min_deposit": str(product.min_deposit),
                "min_balance": str(product.min_balance),
                "rate": str(product.interest_rate),
            }
            for product in products.order_by("name")
        ],
    })


@login_required
@role_required("officer")
def officer_open_savings_account(request):
    form = OfficerOpenAccountForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            account = open_account(
                form.cleaned_data["member"],
                form.cleaned_data["product"],
                opened_by=request.user,
                initial_deposit=form.cleaned_data.get("initial_deposit"),
                opened_on=form.cleaned_data.get("opened_on"),
            )
            messages.success(request, f"Opened savings account {account.reference} for {account.member.display_name()}.")
            return redirect("officer_savings_account_detail", account_id=account.pk)
        except SavingsError as exc:
            messages.error(request, str(exc))
    products = SavingsProduct.objects.filter(is_active=True).order_by("name")
    return render(request, "officer/open_savings_account.html", {"form": form, "products": products})


@login_required
@role_required("admin")
def officer_savings_products(request):
    query = request.GET.get("q", "").strip()
    status = request.GET.get("status", "").strip()
    qs = SavingsProduct.objects.annotate(accounts_count=Count("accounts"))
    if query:
        qs = qs.filter(name__icontains=query)
    if status == "active":
        qs = qs.filter(is_active=True)
    elif status == "inactive":
        qs = qs.filter(is_active=False)
    qs = qs.order_by("name")
    return render(request, "officer/savings_products.html", {
        "products": qs,
        "product_count": qs.count(),
        "active_count": qs.filter(is_active=True).count(),
        "filters": {"q": query, "status": status},
    })


@login_required
@role_required("admin")
def officer_add_savings_product(request):
    form = SavingsProductForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        product = form.save()
        messages.success(request, f'Savings product "{product.name}" added successfully.')
        return redirect("officer_savings_products")
    return render(request, "officer/add_savings_product.html", {
        "form": form,
        "existing_products": SavingsProduct.objects.all().order_by("name")[:8],
    })


@login_required
@role_required("admin")
def officer_edit_savings_product(request, product_id):
    product = get_object_or_404(SavingsProduct, pk=product_id)
    form = SavingsProductForm(request.POST or None, instance=product)
    if request.method == "POST" and form.is_valid():
        product = form.save()
        messages.success(request, f'Savings product "{product.name}" updated successfully.')
        return redirect("officer_savings_products")
    return render(request, "officer/edit_savings_product.html", {"form": form, "product": product})
