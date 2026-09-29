from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from lending.models import User
from savings.models import SavingsAccount, SavingsProduct, SavingsTransaction


class OfficerSavingsLedgerPaginationTests(TestCase):
    def setUp(self):
        self.password = "secret-pass"
        self.officer = User.objects.create_user(
            username="ledger-officer",
            email="ledger-officer@example.com",
            password=self.password,
            full_name="Ledger Officer",
            role=User.Role.OFFICER,
        )
        self.member = User.objects.create_user(
            username="ledger-member",
            email="ledger-member@example.com",
            password=self.password,
            full_name="Ledger Member",
            role=User.Role.MEMBER,
        )
        product = SavingsProduct.objects.create(
            name="Ledger Savings",
            interest_rate=Decimal("0.00"),
            min_balance=Decimal("0.00"),
        )
        self.account = SavingsAccount.objects.create(
            member=self.member,
            product=product,
            balance=Decimal("0.00"),
            opened_by=self.officer,
        )
        now = timezone.now()
        SavingsTransaction.objects.bulk_create(
            [
                SavingsTransaction(
                    account=self.account,
                    transaction_type=SavingsTransaction.Type.DEPOSIT,
                    amount=Decimal("10.00"),
                    balance_after=Decimal("10.00"),
                    notes=f"TX-{index:03d}",
                    created_at=now - timedelta(minutes=46 - index),
                    created_by=self.officer,
                )
                for index in range(1, 46)
            ]
        )
        self.url = reverse("officer_savings_account_detail", kwargs={"account_id": self.account.pk})
        self.client.login(username=self.officer.username, password=self.password)

    def test_first_page_shows_newest_slice_and_full_count(self):
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        transactions = list(response.context["transactions"])
        self.assertEqual(len(transactions), 20)
        self.assertEqual(transactions[0].notes, "TX-045")
        self.assertEqual(transactions[-1].notes, "TX-026")
        self.assertEqual(response.context["ledger"]["transaction_count"], 45)
        self.assertEqual(response.context["ledger"]["last_movement"].notes, "TX-045")
        self.assertContains(response, "All movements on this account.")
        self.assertContains(response, "Showing 1–20 of 45 movements.")
        self.assertContains(response, "1 of 3")
        self.assertNotContains(response, "Showing the latest")

    def test_later_page_reaches_older_movements(self):
        response = self.client.get(self.url, {"page": 3})

        transactions = list(response.context["transactions"])
        self.assertEqual(len(transactions), 5)
        self.assertEqual([tx.notes for tx in transactions], ["TX-005", "TX-004", "TX-003", "TX-002", "TX-001"])
        self.assertEqual(response.context["ledger"]["last_movement"].notes, "TX-045")
        self.assertContains(response, "Showing 41–45 of 45 movements.")
        self.assertContains(response, "3 of 3")

    def test_totals_follow_every_ledger_row(self):
        SavingsTransaction.objects.create(
            account=self.account,
            transaction_type=SavingsTransaction.Type.DEPOSIT,
            amount=Decimal("3.33"),
            balance_after=Decimal("9999.00"),
            reference_number="ADJ-PAY-1",
            notes="Cash-rounding adjustment from loan payment on LN-00001.",
            created_by=self.officer,
        )

        response = self.client.get(self.url)
        ledger = response.context["ledger"]

        self.assertEqual(ledger["total_deposits"], Decimal("450.00"))
        self.assertEqual(ledger["total_adjustments"], Decimal("3.33"))
        self.assertEqual(ledger["current_balance"], Decimal("453.33"))
        self.assertEqual(response.context["account"].balance, Decimal("453.33"))
        self.account.refresh_from_db()
        newest = self.account.transactions.order_by("-created_at", "-pk").first()
        self.assertEqual(newest.balance_after, Decimal("453.33"))
