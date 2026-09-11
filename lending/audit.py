"""Append-only staff activity logs for security review."""

from datetime import datetime, time

from django.db import IntegrityError
from django.utils import timezone

from .models import ActivityLog, Loan, LoanApplication, LoginLogoutLog, Payment, User

KIND_BY_FILTER = {
    "application": ActivityLog.Kind.APPLICATION,
    "payment": ActivityLog.Kind.PAYMENT,
    "collection": ActivityLog.Kind.PAYMENT,
    "disbursement": ActivityLog.Kind.DISBURSEMENT,
    "member": ActivityLog.Kind.MEMBER,
    "savings": ActivityLog.Kind.SAVINGS,
    "mutual_aid": ActivityLog.Kind.MUTUAL_AID,
    "account": ActivityLog.Kind.ACCOUNT,
    "security": ActivityLog.Kind.SECURITY,
}


def client_ip(request):
    if request is None:
        return None
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    candidate = forwarded.split(",")[0].strip() if forwarded else request.META.get("REMOTE_ADDR", "")
    candidate = (candidate or "")[:45]
    if not candidate or candidate.lower() == "unknown":
        return None
    return candidate


def _user_agent(request):
    if request is None:
        return ""
    return (request.META.get("HTTP_USER_AGENT") or "")[:255]


def browser_label(user_agent):
    """Short device label for security audit rows."""
    if not user_agent:
        return ""
    ua = user_agent.lower()
    if "edg/" in ua or "edge/" in ua:
        browser = "Edge"
    elif "chrome/" in ua and "chromium" not in ua:
        browser = "Chrome"
    elif "firefox/" in ua:
        browser = "Firefox"
    elif "safari/" in ua:
        browser = "Safari"
    else:
        browser = "Browser"
    if "windows" in ua:
        system = "Windows"
    elif "mac os" in ua or "macintosh" in ua:
        system = "macOS"
    elif "android" in ua:
        system = "Android"
    elif "iphone" in ua or "ipad" in ua:
        system = "iOS"
    elif "linux" in ua:
        system = "Linux"
    else:
        system = ""
    return f"{browser} on {system}" if system else browser


def _member_name(member):
    if member is None:
        return ""
    return member.display_name()


def _is_staff_actor(actor):
    if actor is None or getattr(actor, "is_anonymous", False):
        return False
    if not getattr(actor, "pk", None):
        return False
    return bool(getattr(actor, "is_officer", False))


def _session_key(request):
    if request is None:
        return ""
    session = getattr(request, "session", None)
    if session is None:
        return ""
    return (getattr(session, "session_key", None) or "")[:40]


def record_staff_auth_event(actor, event, request=None):
    """Save a LoginLogoutLog row, then copy it into Activity history."""
    if not _is_staff_actor(actor):
        return None
    if event == LoginLogoutLog.Event.LOGIN:
        action, title, status, status_label, description = (
            ActivityLog.Action.SIGNED_IN,
            "Logged in",
            "online",
            "Login",
            "Staff session started.",
        )
    elif event == LoginLogoutLog.Event.LOGOUT:
        action, title, status, status_label, description = (
            ActivityLog.Action.SIGNED_OUT,
            "Logged out",
            "neutral",
            "Logout",
            "Staff session ended.",
        )
    else:
        event = LoginLogoutLog.Event.LOGIN_FAILED
        action, title, status, status_label, description = (
            ActivityLog.Action.SIGN_IN_FAILED,
            "Failed login",
            "rejected",
            "Failed",
            "Incorrect password for this account.",
        )

    log = LoginLogoutLog.objects.create(
        user=actor,
        event=event,
        ip_address=client_ip(request),
        user_agent=_user_agent(request),
        session_key=_session_key(request),
    )
    return record_activity(
        actor,
        action=action,
        kind=ActivityLog.Kind.SECURITY,
        title=title,
        description=description,
        reference=getattr(actor, "username", "") or "",
        status=status,
        status_label=status_label,
        request=request,
        source_key=f"login_logout:{log.pk}",
    )


def record_activity(
    actor,
    *,
    action,
    kind,
    title,
    description="",
    member=None,
    member_name="",
    reference="",
    amount=None,
    status="",
    status_label="",
    url_name="",
    url_kwargs=None,
    request=None,
    source_key=None,
    created_at=None,
):
    """Persist one staff action. Non-staff actors are ignored so member self-service stays off officer logs."""
    if not _is_staff_actor(actor):
        return None

    payload = {
        "actor": actor,
        "action": action,
        "kind": kind,
        "title": title[:200],
        "description": description or "",
        "member": member,
        "member_name": (member_name or _member_name(member))[:160],
        "reference": (reference or "")[:80],
        "amount": amount,
        "status": (status or "")[:30],
        "status_label": (status_label or "")[:80],
        "url_name": (url_name or "")[:80],
        "url_kwargs": url_kwargs or {},
        "ip_address": client_ip(request),
        "user_agent": _user_agent(request),
        "source_key": source_key or None,
    }
    if created_at is not None:
        payload["created_at"] = created_at

    try:
        return ActivityLog.objects.create(**payload)
    except IntegrityError:
        if source_key:
            return ActivityLog.objects.filter(source_key=source_key).first()
        raise


def _aware_stamp(value):
    if value is None:
        return timezone.now()
    if isinstance(value, datetime):
        return value if timezone.is_aware(value) else timezone.make_aware(value)
    return timezone.make_aware(datetime.combine(value, time(12, 0)))


def backfill_activity_logs():
    """Copy historical officer actions into the audit trail so older work still appears."""
    applications = LoanApplication.objects.select_related("borrower", "loan_product", "created_by", "reviewed_by")
    for application in applications.iterator():
        if application.created_by_id and getattr(application.created_by, "is_officer", False):
            record_activity(
                application.created_by,
                action=ActivityLog.Action.APPLICATION_CREATED,
                kind=ActivityLog.Kind.APPLICATION,
                title=f"{application.reference} created",
                description=f"{application.product_name} · submitted for {application.borrower_name}.",
                member=application.borrower,
                reference=application.reference,
                amount=application.amount_requested,
                status=application.status,
                status_label="Created",
                url_name="application_review",
                url_kwargs={"application_id": application.pk},
                source_key=f"application_created:{application.pk}",
                created_at=_aware_stamp(application.created_at),
            )
        if application.reviewed_by_id and application.decision_date and getattr(application.reviewed_by, "is_officer", False):
            status = application.status
            if status == LoanApplication.Status.APPROVED:
                action = ActivityLog.Action.APPLICATION_APPROVED
                title = f"{application.reference} approved"
                status_label = "Approved"
            elif status == LoanApplication.Status.REJECTED:
                action = ActivityLog.Action.APPLICATION_REJECTED
                title = f"{application.reference} rejected"
                status_label = "Rejected"
            else:
                action = ActivityLog.Action.APPLICATION_INFO_REQUESTED
                title = f"{application.reference} reviewed"
                status_label = application.status_label
            record_activity(
                application.reviewed_by,
                action=action,
                kind=ActivityLog.Kind.APPLICATION,
                title=title,
                description=application.review_notes or f"{application.product_name} decision recorded.",
                member=application.borrower,
                reference=application.reference,
                amount=application.amount_requested,
                status=status,
                status_label=status_label,
                url_name="application_review",
                url_kwargs={"application_id": application.pk},
                source_key=f"application_decision:{application.pk}",
                created_at=_aware_stamp(application.decision_date),
            )

    payments = Payment.objects.select_related(
        "recorded_by", "loan", "loan__application", "loan__application__borrower"
    )
    for payment in payments.iterator():
        if not payment.recorded_by_id or not getattr(payment.recorded_by, "is_officer", False):
            continue
        loan = payment.loan
        record_activity(
            payment.recorded_by,
            action=ActivityLog.Action.PAYMENT_RECORDED,
            kind=ActivityLog.Kind.PAYMENT,
            title=f"Pay collection · {loan.reference}",
            description=f"{payment.get_method_display()} · ref {payment.reference_number or payment.pk}",
            member=loan.application.borrower,
            reference=payment.reference_number or f"PAY-{payment.pk:05d}",
            amount=payment.amount,
            status="paid",
            status_label="Pay collection",
            url_name="payment_receipt",
            url_kwargs={"payment_id": payment.pk},
            source_key=f"payment:{payment.pk}",
            created_at=_aware_stamp(payment.payment_date),
        )

    loans = Loan.objects.select_related(
        "disbursed_by", "application", "application__borrower", "application__loan_product"
    )
    for loan in loans.iterator():
        if not loan.disbursed_by_id or not getattr(loan.disbursed_by, "is_officer", False):
            continue
        record_activity(
            loan.disbursed_by,
            action=ActivityLog.Action.LOAN_DISBURSED,
            kind=ActivityLog.Kind.DISBURSEMENT,
            title=f"{loan.reference} disbursed",
            description=f"{loan.disbursement_method} · net {loan.net_release_amount:,.2f} released",
            member=loan.application.borrower,
            reference=loan.reference,
            amount=loan.net_release_amount,
            status="disbursed",
            status_label="Disbursed",
            url_name="disbursement_receipt",
            url_kwargs={"disbursement_id": loan.application_id},
            source_key=f"disbursement:{loan.pk}",
            created_at=_aware_stamp(loan.disbursed_date),
        )


def application_decision_log(actor, application, request=None):
    status = application.status
    if status == LoanApplication.Status.APPROVED:
        action = ActivityLog.Action.APPLICATION_APPROVED
        title = f"{application.reference} approved"
        status_label = "Approved"
    elif status == LoanApplication.Status.REJECTED:
        action = ActivityLog.Action.APPLICATION_REJECTED
        title = f"{application.reference} rejected"
        status_label = "Rejected"
    elif status == LoanApplication.Status.UNDER_REVIEW:
        action = ActivityLog.Action.APPLICATION_INFO_REQUESTED
        title = f"{application.reference} marked for more information"
        status_label = "Under review"
    else:
        action = ActivityLog.Action.APPLICATION_UPDATED
        title = f"{application.reference} reviewed"
        status_label = application.status_label
    return record_activity(
        actor,
        action=action,
        kind=ActivityLog.Kind.APPLICATION,
        title=title,
        description=application.review_notes or f"{application.product_name} decision recorded.",
        member=application.borrower,
        reference=application.reference,
        amount=application.amount_requested,
        status=status,
        status_label=status_label,
        url_name="application_review",
        url_kwargs={"application_id": application.pk},
        request=request,
    )
