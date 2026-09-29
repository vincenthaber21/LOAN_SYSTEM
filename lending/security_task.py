"""Daily security cleanup for the PythonAnywhere scheduled task.

The Tasks tab command is:

    /home/calica/.virtualenvs/venv/bin/python /home/calica/LOAN_SYSTEM/daily_task.py
"""

from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.contrib.sessions.models import Session
from django.db.models import Q
from django.utils import timezone

from .audit import record_activity
from .models import ActivityLog, LoginLogoutLog, User

# Lock a staff account after this many failed sign-ins in a row.
FAILED_SIGN_IN_LIMIT = 8
FAILED_SIGN_IN_WINDOW = timedelta(hours=24)


def run_daily_security():
    """Lock brute-forced staff accounts, drop dead sessions, and tighten file modes."""
    now = timezone.now()
    locked, skipped = lock_accounts_after_failed_signins(now=now)
    closed = close_inactive_sessions()
    expired = clear_expired_sessions(now=now)
    restricted = restrict_private_files()

    print("KAP daily security task")
    print(f"Locked staff accounts: {len(locked)}")
    for user, streak in locked:
        print(f"  locked {user.username} after {streak} failed sign-ins")
    for user, streak in skipped:
        print(f"  left {user.username} active (last administrator; {streak} failed sign-ins)")
    print(f"Closed sessions for inactive accounts: {closed}")
    print(f"Cleared expired sessions: {expired}")
    if restricted:
        print("Private files restricted to the account owner: " + ", ".join(restricted))
    elif os.name != "posix":
        print("Private-file permissions skipped (not a Unix host).")
    else:
        print("Private files already absent or unchanged.")
    return 0


def lock_accounts_after_failed_signins(now=None):
    """Deactivate staff with a long failed-sign-in streak. Keep the last administrator."""
    now = now or timezone.now()
    window_start = now - FAILED_SIGN_IN_WINDOW
    staff = list(
        User.objects.filter(is_active=True)
        .filter(Q(role__in=[User.Role.OFFICER, User.Role.MANAGER, User.Role.ADMIN]) | Q(is_staff=True) | Q(is_superuser=True))
        .distinct()
    )
    remaining_admins = {user.pk for user in staff if _is_protected_admin(user)}
    locked = []
    skipped = []

    for user in staff:
        streak = failed_sign_in_streak(user, window_start)
        if streak < FAILED_SIGN_IN_LIMIT:
            continue
        if _is_protected_admin(user) and len(remaining_admins) <= 1:
            skipped.append((user, streak))
            continue
        user.is_active = False
        user.save(update_fields=["is_active"])
        remaining_admins.discard(user.pk)
        source_key = f"account_locked:{user.pk}:{timezone.localdate(now).isoformat()}"
        if not ActivityLog.objects.filter(source_key=source_key).exists():
            record_activity(
                user,
                action=ActivityLog.Action.ACCOUNT_LOCKED,
                kind=ActivityLog.Kind.SECURITY,
                title="Account locked",
                description=(
                    f"Deactivated after {streak} failed sign-ins in 24 hours. "
                    "An administrator can turn the account back on."
                ),
                reference=user.username,
                status="locked",
                status_label="Locked",
                source_key=source_key,
                created_at=now,
            )
        locked.append((user, streak))
    return locked, skipped


def failed_sign_in_streak(user, window_start):
    """Count failed sign-ins since the latest successful login, inside the window."""
    logs = LoginLogoutLog.objects.filter(user=user, created_at__gte=window_start).order_by("-created_at", "-id")
    streak = 0
    for log in logs:
        if log.event == LoginLogoutLog.Event.LOGIN:
            break
        if log.event == LoginLogoutLog.Event.LOGIN_FAILED:
            streak += 1
    return streak


def close_inactive_sessions():
    """Drop database sessions whose user is missing or deactivated."""
    stale_keys = []
    for session in Session.objects.iterator():
        try:
            user_id = session.get_decoded().get("_auth_user_id")
        except Exception:
            stale_keys.append(session.session_key)
            continue
        if not user_id:
            continue
        if not User.objects.filter(pk=user_id, is_active=True).exists():
            stale_keys.append(session.session_key)
    if not stale_keys:
        return 0
    Session.objects.filter(session_key__in=stale_keys).delete()
    return len(stale_keys)


def clear_expired_sessions(now=None):
    now = now or timezone.now()
    expired = Session.objects.filter(expire_date__lt=now)
    count = expired.count()
    expired.delete()
    return count


def private_files():
    files = []
    env_file = Path(settings.BASE_DIR) / ".env"
    if env_file.is_file():
        files.append(env_file)
    db_name = settings.DATABASES["default"].get("NAME")
    if db_name:
        db_path = Path(db_name)
        if db_path.is_file():
            files.append(db_path)
    return files


def restrict_private_files(files=None):
    """Make .env and the database readable only by this account (Unix hosts)."""
    if os.name != "posix":
        return []
    restricted = []
    for path in private_files() if files is None else files:
        path = Path(path)
        if not path.is_file():
            continue
        os.chmod(path, 0o600)
        restricted.append(path.name)
    return restricted


def _is_protected_admin(user):
    return user.role == User.Role.ADMIN or user.is_superuser
