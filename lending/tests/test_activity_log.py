from django.test import TestCase
from django.urls import reverse

from lending.models import ActivityLog, LoginLogoutLog, User


class OfficerAuthActivityLogTests(TestCase):
    def setUp(self):
        self.password = "secret-pass"
        self.admin = User.objects.create_user(
            username="audit-admin",
            email="audit-admin@example.com",
            password=self.password,
            full_name="Audit Admin",
            role=User.Role.ADMIN,
            is_staff=True,
            is_superuser=True,
        )
        self.officer = User.objects.create_user(
            username="officer.three",
            email="officer3@example.com",
            password=self.password,
            full_name="Officer Three",
            role=User.Role.OFFICER,
        )
        self.member = User.objects.create_user(
            username="member.one",
            email="member@example.com",
            password=self.password,
            full_name="Member One",
            role=User.Role.MEMBER,
        )
        self.login_headers = {
            "HTTP_USER_AGENT": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0 Safari/537.36",
            "REMOTE_ADDR": "203.0.113.10",
        }

    def _login(self, user, password=None, **headers):
        payload = {"email": user.email, "password": password or self.password}
        return self.client.post(reverse("login"), payload, **headers)

    def test_officer_login_and_logout_are_recorded(self):
        self._login(self.officer, **self.login_headers)
        login_session = LoginLogoutLog.objects.get(user=self.officer, event=LoginLogoutLog.Event.LOGIN)
        self.assertEqual(login_session.ip_address, "203.0.113.10")
        self.assertIn("Chrome", login_session.user_agent)
        login_log = ActivityLog.objects.get(actor=self.officer, action=ActivityLog.Action.SIGNED_IN)
        self.assertEqual(login_log.kind, ActivityLog.Kind.SECURITY)
        self.assertEqual(login_log.title, "Logged in")
        self.assertEqual(login_log.status_label, "Login")
        self.assertEqual(login_log.ip_address, "203.0.113.10")
        self.assertIn("Chrome", login_log.user_agent)

        self.client.post(reverse("logout"), **self.login_headers)
        self.assertEqual(
            LoginLogoutLog.objects.filter(user=self.officer, event=LoginLogoutLog.Event.LOGOUT).count(),
            1,
        )
        logout_logs = ActivityLog.objects.filter(actor=self.officer, action=ActivityLog.Action.SIGNED_OUT)
        self.assertEqual(logout_logs.count(), 1)
        logout_log = logout_logs.get()
        self.assertEqual(logout_log.title, "Logged out")
        self.assertEqual(logout_log.status_label, "Logout")
        self.assertEqual(logout_log.ip_address, "203.0.113.10")

    def test_failed_officer_login_is_recorded(self):
        self._login(self.officer, password="wrong-pass", **self.login_headers)
        self.assertTrue(
            LoginLogoutLog.objects.filter(user=self.officer, event=LoginLogoutLog.Event.LOGIN_FAILED).exists()
        )
        failed = ActivityLog.objects.get(actor=self.officer, action=ActivityLog.Action.SIGN_IN_FAILED)
        self.assertEqual(failed.title, "Failed login")
        self.assertFalse(
            ActivityLog.objects.filter(actor=self.officer, action=ActivityLog.Action.SIGNED_IN).exists()
        )

    def test_member_login_is_not_recorded_on_staff_trail(self):
        self._login(self.member, **self.login_headers)
        self.client.post(reverse("logout"))
        self.assertFalse(LoginLogoutLog.objects.filter(user=self.member).exists())
        self.assertFalse(ActivityLog.objects.filter(actor=self.member).exists())

    def test_officer_activity_page_shows_login_and_logout(self):
        self._login(self.officer, **self.login_headers)
        self.client.post(reverse("logout"), **self.login_headers)

        self.client.force_login(self.admin)
        url = reverse("officer_activity_log", kwargs={"officer_id": self.officer.pk})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Security audit trail")
        self.assertContains(response, "Logged in")
        self.assertContains(response, "Logged out")
        self.assertContains(response, "IP 203.0.113.10")
        self.assertContains(response, "Chrome on Windows")
        self.assertContains(response, "Login &amp; logout")

        filtered = self.client.get(url, {"type": "security"})
        self.assertContains(filtered, "Logged in")
        self.assertContains(filtered, "Logged out")
        self.assertContains(filtered, 'option value="security" selected')
