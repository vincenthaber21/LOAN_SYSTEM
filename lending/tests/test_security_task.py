from datetime import timedelta

from django.contrib.sessions.backends.db import SessionStore
from django.test import TestCase
from django.utils import timezone

from lending.models import ActivityLog, LoginLogoutLog, User
from lending.security_task import (
    FAILED_SIGN_IN_LIMIT,
    clear_expired_sessions,
    close_inactive_sessions,
    lock_accounts_after_failed_signins,
    run_daily_security,
)


class DailySecurityTaskTests(TestCase):
    def setUp(self):
        self.now = timezone.now()
        self.officer = User.objects.create_user(
            username="officer.daily",
            email="officer.daily@example.com",
            password="secret-pass",
            full_name="Daily Officer",
            role=User.Role.OFFICER,
        )
        self.admin = User.objects.create_user(
            username="admin.daily",
            email="admin.daily@example.com",
            password="secret-pass",
            full_name="Daily Admin",
            role=User.Role.ADMIN,
            is_staff=True,
            is_superuser=True,
        )
        self.member = User.objects.create_user(
            username="member.daily",
            email="member.daily@example.com",
            password="secret-pass",
            full_name="Daily Member",
            role=User.Role.MEMBER,
        )

    def _fail(self, user, *, minutes_ago):
        LoginLogoutLog.objects.create(
            user=user,
            event=LoginLogoutLog.Event.LOGIN_FAILED,
            created_at=self.now - timedelta(minutes=minutes_ago),
        )

    def _succeed(self, user, *, minutes_ago):
        LoginLogoutLog.objects.create(
            user=user,
            event=LoginLogoutLog.Event.LOGIN,
            created_at=self.now - timedelta(minutes=minutes_ago),
        )

    def _session_for(self, user):
        store = SessionStore()
        store["_auth_user_id"] = str(user.pk)
        store.create()
        return store.session_key

    def test_locks_staff_after_repeated_failed_sign_ins(self):
        session_key = self._session_for(self.officer)
        for minutes in range(FAILED_SIGN_IN_LIMIT):
            self._fail(self.officer, minutes_ago=minutes + 1)

        locked, skipped = lock_accounts_after_failed_signins(now=self.now)

        self.officer.refresh_from_db()
        self.assertFalse(self.officer.is_active)
        self.assertEqual([user.username for user, _streak in locked], ["officer.daily"])
        self.assertEqual(skipped, [])
        log = ActivityLog.objects.get(actor=self.officer, action=ActivityLog.Action.ACCOUNT_LOCKED)
        self.assertEqual(log.kind, ActivityLog.Kind.SECURITY)
        self.assertEqual(log.title, "Account locked")
        self.assertEqual(close_inactive_sessions(), 1)
        from django.contrib.sessions.models import Session

        self.assertFalse(Session.objects.filter(session_key=session_key).exists())

    def test_successful_login_resets_the_streak(self):
        for minutes in range(FAILED_SIGN_IN_LIMIT):
            self._fail(self.officer, minutes_ago=30 + minutes)
        self._succeed(self.officer, minutes_ago=5)
        self._fail(self.officer, minutes_ago=1)

        locked, skipped = lock_accounts_after_failed_signins(now=self.now)

        self.officer.refresh_from_db()
        self.assertTrue(self.officer.is_active)
        self.assertEqual(locked, [])
        self.assertEqual(skipped, [])

    def test_last_administrator_stays_active(self):
        for minutes in range(FAILED_SIGN_IN_LIMIT):
            self._fail(self.admin, minutes_ago=minutes + 1)

        locked, skipped = lock_accounts_after_failed_signins(now=self.now)

        self.admin.refresh_from_db()
        self.assertTrue(self.admin.is_active)
        self.assertEqual(locked, [])
        self.assertEqual([user.username for user, _streak in skipped], ["admin.daily"])

    def test_member_failures_do_not_lock_the_account(self):
        for minutes in range(FAILED_SIGN_IN_LIMIT):
            self._fail(self.member, minutes_ago=minutes + 1)

        lock_accounts_after_failed_signins(now=self.now)

        self.member.refresh_from_db()
        self.assertTrue(self.member.is_active)
        self.assertFalse(ActivityLog.objects.filter(action=ActivityLog.Action.ACCOUNT_LOCKED).exists())

    def test_running_twice_in_one_day_records_one_lock(self):
        for minutes in range(FAILED_SIGN_IN_LIMIT):
            self._fail(self.officer, minutes_ago=minutes + 1)
        lock_accounts_after_failed_signins(now=self.now)
        self.officer.is_active = True
        self.officer.save(update_fields=["is_active"])

        lock_accounts_after_failed_signins(now=self.now)

        self.assertEqual(
            ActivityLog.objects.filter(actor=self.officer, action=ActivityLog.Action.ACCOUNT_LOCKED).count(),
            1,
        )

    def test_expired_sessions_are_removed(self):
        from django.contrib.sessions.models import Session

        key = self._session_for(self.officer)
        Session.objects.filter(session_key=key).update(expire_date=self.now - timedelta(minutes=1))

        self.assertEqual(clear_expired_sessions(now=self.now), 1)
        self.assertFalse(Session.objects.filter(session_key=key).exists())

    def test_run_daily_security_prints_a_summary(self):
        code = run_daily_security()
        self.assertEqual(code, 0)
