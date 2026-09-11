from django.contrib.auth.signals import user_logged_in, user_logged_out, user_login_failed
from django.dispatch import receiver

from .audit import record_staff_auth_event
from .models import LoginLogoutLog, User


def _staff_user(user):
    return user if user is not None and getattr(user, "is_officer", False) else None


@receiver(user_logged_in, dispatch_uid="lending.log_staff_sign_in")
def log_staff_sign_in(sender, request, user, **kwargs):
    actor = _staff_user(user)
    if not actor:
        return
    record_staff_auth_event(actor, LoginLogoutLog.Event.LOGIN, request)


@receiver(user_logged_out, dispatch_uid="lending.log_staff_sign_out")
def log_staff_sign_out(sender, request, user, **kwargs):
    if request is not None and getattr(request, "_staff_auth_event_logged", False):
        return
    actor = _staff_user(user)
    if not actor:
        return
    record_staff_auth_event(actor, LoginLogoutLog.Event.LOGOUT, request)


@receiver(user_login_failed, dispatch_uid="lending.log_staff_sign_in_failed")
def log_staff_sign_in_failed(sender, credentials, request, **kwargs):
    username = (credentials or {}).get("username") or (credentials or {}).get("email") or ""
    if not username:
        return
    user = User.objects.filter(username__iexact=username).first()
    if user is None and "@" in username:
        user = User.objects.filter(email__iexact=username).first()
    actor = _staff_user(user)
    if not actor:
        return
    record_staff_auth_event(actor, LoginLogoutLog.Event.LOGIN_FAILED, request)
