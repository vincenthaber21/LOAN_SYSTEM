from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from lending.models import User
from mutual_aid.models import MutualAidClaim, MutualAidContribution, MutualAidMembership, MutualAidPlan


class OfficerMutualAidLedgerTests(TestCase):
    def setUp(self):
        self.password = "secret-pass"
        self.officer = User.objects.create_user(
            username="mutual-officer",
            email="mutual-officer@example.com",
            password=self.password,
            full_name="Mutual Officer",
            role=User.Role.OFFICER,
        )
        self.member = User.objects.create_user(
            username="mutual-member",
            email="mutual-member@example.com",
            password=self.password,
            full_name="Mutual Member",
            role=User.Role.MEMBER,
        )
        plan = MutualAidPlan.objects.create(
            name="Ledger Plan",
            contribution_amount=Decimal("10.00"),
            max_benefit_amount=Decimal("1000.00"),
            waiting_period_days=0,
        )
        self.membership = MutualAidMembership.objects.create(
            member=self.member,
            plan=plan,
            enrolled_by=self.officer,
            total_contributed=Decimal("1.00"),
            benefits_claimed=Decimal("0.00"),
        )
        now = timezone.now()
        MutualAidContribution.objects.bulk_create(
            [
                MutualAidContribution(
                    membership=self.membership,
                    amount=Decimal("10.00"),
                    notes=f"C-{index:03d}",
                    created_at=now - timedelta(minutes=25 - index),
                    recorded_by=self.officer,
                )
                for index in range(1, 26)
            ]
        )
        MutualAidClaim.objects.create(
            membership=self.membership,
            claim_type=MutualAidClaim.ClaimType.EMERGENCY,
            amount_requested=Decimal("40.00"),
            amount_approved=Decimal("40.00"),
            reason="Disbursed benefit",
            status=MutualAidClaim.Status.DISBURSED,
        )
        self.url = reverse(
            "officer_mutual_aid_membership_detail",
            kwargs={"membership_id": self.membership.pk},
        )
        self.client.login(username=self.officer.username, password=self.password)

    def test_page_shows_full_ledger_and_database_totals(self):
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        contributions = list(response.context["contributions"])
        self.assertEqual(len(contributions), 20)
        self.assertEqual(contributions[0].notes, "C-025")
        ledger = response.context["ledger"]
        self.assertEqual(ledger["contribution_count"], 25)
        self.assertEqual(ledger["total_contributed"], Decimal("250.00"))
        self.assertEqual(ledger["benefits_claimed"], Decimal("40.00"))
        self.assertEqual(ledger["net_balance"], Decimal("210.00"))
        self.assertContains(response, "All member contributions.")
        self.assertContains(response, "Showing 1–20 of 25 contributions.")
        self.membership.refresh_from_db()
        self.assertEqual(self.membership.total_contributed, Decimal("250.00"))
        self.assertEqual(self.membership.benefits_claimed, Decimal("40.00"))

        later = self.client.get(self.url, {"page": 2})
        notes = [row.notes for row in later.context["contributions"]]
        self.assertEqual(notes, ["C-005", "C-004", "C-003", "C-002", "C-001"])
        self.assertContains(later, "Showing 21–25 of 25 contributions.")
