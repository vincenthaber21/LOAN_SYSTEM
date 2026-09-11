from django.contrib.auth.signals import user_logged_in, user_logged_out, user_login_failed
from django.dispatch import receiver

from .audit import record_activity
from .models import ActivityLog, User


def _staff_user(user):
    return user if user is not None and getattr(user, "is_officer", False) else None


@receiver(user_logged_in)
def log_staff_sign_in(sender, request, user, **kwargs):
    actor = _staff_user(user)
    if not actor:
        return
    record_activity(
        actor,
        action=ActivityLog.Action.SIGNED_IN,
        kind=ActivityLog.Kind.SECURITY,
        title="Signed in",
        description=f"{actor.display_name()} signed in.",
        status="online",
        status_label="Signed in",
        request=request,
    )


@receiver(user_logged_out)
def log_staff_sign_out(sender, request, user, **kwargs):
    actor = _staff_user(user)
    if not actor:
        return
    record_activity(
        actor,
        action=ActivityLog.Action.SIGNED_OUT,
        kind=ActivityLog.Kind.SECURITY,
        title="Signed out",
        description=f"{actor.display_name()} signed out.",
        status="neutral",
        status_label="Signed out",
        request=request,
    )


@receiver(user_login_failed)
def log_staff_sign_in_failed(sender, credentials, request, **kwargs):
    username = (credentials or {}).get("username") or ""
    if not username:
        return
    user = User.objects.filter(username__iexact=username).first()
    if user is None and "@" in username:
        user = User.objects.filter(email__iexact=username).first()
    actor = _staff_user(user)
    if not actor:
        return
    record_activity(
        actor,
        action=ActivityLog.Action.SIGN_IN_FAILED,
        kind=ActivityLog.Kind.SECURITY,
        title="Failed sign-in",
        description=f"Incorrect password for {actor.display_name()}.",
        status="rejected",
        status_label="Failed",
        request=request,
    )
