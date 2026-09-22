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
from lending.audit import record_activity
from lending.models import ActivityLog, User

from .forms import (
    OfficerEditSavingsAccountForm,
    OfficerEditSavingsTransactionForm,
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
    delete_account,
    delete_transaction,
    interest_schedule_state,
    open_account,
    record_deposit,
    record_withdrawal,
    resolve_membership_savings_product,
    update_account,
    update_transaction,
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
    record_activity(
        request.user,
        action=ActivityLog.Action.DATA_EXPORTED,
        kind=ActivityLog.Kind.SAVINGS,
        title="Savings interest exported",
        description="Savings interest report downloaded.",
        status="neutral",
        status_label="Export",
        request=request,
    )
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
                        record_activity(
                            request.user,
                            action=ActivityLog.Action.SAVINGS_DEPOSIT,
                            kind=ActivityLog.Kind.SAVINGS,
                            title=f"Savings deposit · {account.reference}",
                            description=f"{data['method']} · {account.member.display_name()}",
                            member=account.member,
                            reference=data.get("reference_number") or account.reference,
                            amount=data["amount"],
                            status="paid",
                            status_label="Deposit",
                            url_name="officer_savings_account_detail",
                            url_kwargs={"account_id": account.pk},
                            request=request,
                        )
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
                        record_activity(
                            request.user,
                            action=ActivityLog.Action.SAVINGS_WITHDRAWAL,
                            kind=ActivityLog.Kind.SAVINGS,
                            title=f"Savings withdrawal · {account.reference}",
                            description=f"{data['method']} · {account.member.display_name()}",
                            member=account.member,
                            reference=data.get("reference_number") or account.reference,
                            amount=data["amount"],
                            status="pending",
                            status_label="Withdrawal",
                            url_name="officer_savings_account_detail",
                            url_kwargs={"account_id": account.pk},
                            request=request,
                        )
                    return redirect("officer_savings_account_detail", account_id=account.pk)
                except SavingsError as exc:
                    messages.error(request, str(exc))
        elif action == "delete_transaction":
            tx = get_object_or_404(
                SavingsTransaction,
                pk=request.POST.get("tx_id"),
                account=account,
            )
            tx_type_label = tx.type_label
            tx_amount = tx.amount
            tx_reference = tx.reference_number or account.reference
            try:
                delete_transaction(tx)
                record_activity(
                    request.user,
                    action=ActivityLog.Action.SAVINGS_TRANSACTION_DELETED,
                    kind=ActivityLog.Kind.SAVINGS,
                    title=f"Savings {tx_type_label.lower()} deleted · {account.reference}",
                    description=(
                        f"Removed mistaken {tx_type_label.lower()} of ₱{tx_amount:,.2f} "
                        f"for {account.member.display_name()}."
                    ),
                    member=account.member,
                    reference=tx_reference,
                    amount=tx_amount,
                    status="deleted",
                    status_label="Deleted",
                    url_name="officer_savings_account_detail",
                    url_kwargs={"account_id": account.pk},
                    request=request,
                )
                messages.success(request, f"{tx_type_label} entry deleted and balance updated.")
                return redirect("officer_savings_account_detail", account_id=account.pk)
            except SavingsError as exc:
                messages.error(request, str(exc))
        elif action == "edit_transaction":
            tx = get_object_or_404(
                SavingsTransaction,
                pk=request.POST.get("tx_id"),
                account=account,
            )
            edit_form = OfficerEditSavingsTransactionForm(
                request.POST,
                account=account,
                transaction=tx,
            )
            if edit_form.is_valid():
                data = edit_form.cleaned_data
                try:
                    update_transaction(
                        tx,
                        amount=data["amount"],
                        method=data["method"],
                        reference=data.get("reference_number", ""),
                        notes=data.get("notes", ""),
                        occurred_on=data.get("transaction_date"),
                        transaction_type=data.get("transaction_type"),
                    )
                    record_activity(
                        request.user,
                        action=ActivityLog.Action.SAVINGS_TRANSACTION_UPDATED,
                        kind=ActivityLog.Kind.SAVINGS,
                        title=f"Savings {tx.type_label.lower()} updated · {account.reference}",
                        description=(
                            f"Corrected {tx.type_label.lower()} to ₱{data['amount']:,.2f} "
                            f"for {account.member.display_name()}."
                        ),
                        member=account.member,
                        reference=data.get("reference_number") or account.reference,
                        amount=data["amount"],
                        status="updated",
                        status_label="Updated",
                        url_name="officer_savings_account_detail",
                        url_kwargs={"account_id": account.pk},
                        request=request,
                    )
                    messages.success(request, f"{tx.type_label} entry updated and balance recalculated.")
                    return redirect("officer_savings_account_detail", account_id=account.pk)
                except SavingsError as exc:
                    messages.error(request, str(exc))
            else:
                for field_errors in edit_form.errors.values():
                    for error in field_errors:
                        messages.error(request, error)
        elif action == "close" and request.user.is_admin:
            try:
                close_account(account, closed_by=request.user)
                record_activity(
                    request.user,
                    action=ActivityLog.Action.SAVINGS_CLOSED,
                    kind=ActivityLog.Kind.SAVINGS,
                    title=f"Savings account {account.reference} closed",
                    description=f"Closed for {account.member.display_name()}.",
                    member=account.member,
                    reference=account.reference,
                    amount=account.balance,
                    status="closed",
                    status_label="Closed",
                    request=request,
                )
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
            record_activity(
                request.user,
                action=ActivityLog.Action.SAVINGS_OPENED,
                kind=ActivityLog.Kind.SAVINGS,
                title=f"Savings account {account.reference} opened",
                description=f"{account.product_name} for {account.member.display_name()}.",
                member=account.member,
                reference=account.reference,
                amount=account.balance,
                status="active",
                status_label="Opened",
                url_name="officer_savings_account_detail",
                url_kwargs={"account_id": account.pk},
                request=request,
                source_key=f"savings_opened:{account.pk}",
            )
            messages.success(request, f"Opened savings account {account.reference} for {account.member.display_name()}.")
            return redirect("officer_savings_account_detail", account_id=account.pk)
        except SavingsError as exc:
            messages.error(request, str(exc))
    products = SavingsProduct.objects.filter(is_active=True).order_by("name")
    return render(request, "officer/open_savings_account.html", {"form": form, "products": products})


@login_required
@role_required("manager")
def officer_edit_savings_account(request, account_id):
    account = get_object_or_404(
        SavingsAccount.objects.select_related("member", "product"),
        pk=account_id,
    )
    form = OfficerEditSavingsAccountForm(
        request.POST or None,
        account=account,
        initial={
            "member": account.member_id,
            "product": account.product_id,
            "opened_on": timezone.localtime(account.opened_at).date(),
        },
    )
    if request.method == "POST" and form.is_valid():
        try:
            update_account(
                account,
                member=form.cleaned_data["member"],
                product=form.cleaned_data["product"],
                opened_on=form.cleaned_data.get("opened_on"),
            )
            account.refresh_from_db()
            record_activity(
                request.user,
                action=ActivityLog.Action.SAVINGS_ACCOUNT_UPDATED,
                kind=ActivityLog.Kind.SAVINGS,
                title=f"Savings account {account.reference} updated",
                description=(
                    f"Corrected account details for {account.member.display_name()} "
                    f"· {account.product_name}."
                ),
                member=account.member,
                reference=account.reference,
                amount=account.balance,
                status="updated",
                status_label="Updated",
                url_name="officer_savings_account_detail",
                url_kwargs={"account_id": account.pk},
                request=request,
            )
            messages.success(request, f"Savings account {account.reference} updated.")
            return redirect("officer_savings_accounts")
        except SavingsError as exc:
            messages.error(request, str(exc))
    return render(request, "officer/edit_savings_account.html", {
        "account": account,
        "form": form,
    })


@login_required
@role_required("manager")
def officer_delete_savings_account(request, account_id):
    account = get_object_or_404(
        SavingsAccount.objects.select_related("member", "product"),
        pk=account_id,
    )
    if request.method != "POST":
        return redirect("officer_savings_accounts")
    try:
        deleted = delete_account(account)
        record_activity(
            request.user,
            action=ActivityLog.Action.SAVINGS_ACCOUNT_DELETED,
            kind=ActivityLog.Kind.SAVINGS,
            title=f"Savings account {deleted['reference']} deleted",
            description=(
                f"Removed mistaken {deleted['product_name']} account "
                f"for {deleted['member'].display_name()}."
            ),
            member=deleted["member"],
            reference=deleted["reference"],
            amount=deleted["balance"],
            status="deleted",
            status_label="Deleted",
            request=request,
        )
        messages.success(
            request,
            f"Savings account {deleted['reference']} and its ledger entries were deleted.",
        )
    except SavingsError as exc:
        messages.error(request, str(exc))
    return redirect("officer_savings_accounts")


@login_required
@role_required("admin")
def officer_savings_products(request):
    # Ensure the compulsory Membership/Savings Deposit product always exists.
    resolve_membership_savings_product()
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
        record_activity(
            request.user,
            action=ActivityLog.Action.PRODUCT_CREATED,
            kind=ActivityLog.Kind.ACCOUNT,
            title=f'Savings product "{product.name}" created',
            description=f"{product.interest_rate}% p.a.",
            reference=product.name,
            status="active" if product.is_active else "inactive",
            status_label="Created",
            request=request,
            source_key=f"savings_product:{product.pk}",
        )
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
        record_activity(
            request.user,
            action=ActivityLog.Action.PRODUCT_UPDATED,
            kind=ActivityLog.Kind.ACCOUNT,
            title=f'Savings product "{product.name}" updated',
            description=f"{product.interest_rate}% p.a.",
            reference=product.name,
            status="active" if product.is_active else "inactive",
            status_label="Updated",
            request=request,
        )
        messages.success(request, f'Savings product "{product.name}" updated successfully.')
        return redirect("officer_savings_products")
    return render(request, "officer/edit_savings_product.html", {"form": form, "product": product})
