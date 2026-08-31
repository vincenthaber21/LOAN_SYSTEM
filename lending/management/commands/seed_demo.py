from datetime import timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.utils import timezone

from lending.models import LoanApplication, LoanProduct, Payment, User
from lending.services import disburse_application, record_payment
from savings.models import SavingsProduct
from savings.services import open_account, record_deposit


class Command(BaseCommand):
    help = "Create or refresh the demo users, products, applications, and loans."

    def handle(self, *args, **options):
        officer, _ = User.objects.get_or_create(
            username="officer@lumen.test",
            defaults={
                "email": "officer@lumen.test",
                "full_name": "Avery Santos",
                "role": User.Role.OFFICER,
                "is_staff": True,
                "is_superuser": True,
            },
        )
        officer.set_password("Officer123!")
        officer.role = User.Role.OFFICER
        officer.is_staff = True
        officer.is_superuser = True
        officer.save()

        maria, _ = User.objects.get_or_create(
            username="maria@lumen.test",
            defaults={
                "email": "maria@lumen.test",
                "full_name": "Maria Dela Cruz",
                "phone": "+63 917 555 0142",
                "address": "Makati City, Metro Manila",
                "employment_status": "Full-time",
                "monthly_income": Decimal("68000"),
                "role": User.Role.MEMBER,
            },
        )
        maria.set_password("Borrower123!")
        maria.save()

        carlo, _ = User.objects.get_or_create(
            username="carlo@lumen.test",
            defaults={
                "email": "carlo@lumen.test",
                "full_name": "Carlo Reyes",
                "phone": "+63 905 555 0186",
                "address": "Quezon City, Metro Manila",
                "employment_status": "Self-employed",
                "monthly_income": Decimal("92000"),
                "role": User.Role.MEMBER,
            },
        )
        carlo.set_password("Borrower123!")
        carlo.save()

        personal, _ = LoanProduct.objects.update_or_create(
            name="Flex Personal Loan",
            defaults={
                "loan_type": LoanProduct.LoanType.PERSONAL,
                "min_amount": Decimal("10000"),
                "max_amount": Decimal("250000"),
                "interest_rate": Decimal("12.50"),
                "min_term_months": 3,
                "max_term_months": 24,
                "processing_fee_percent": Decimal("1.50"),
                "grace_period_days": 0,
                "is_active": True,
            },
        )
        business, _ = LoanProduct.objects.update_or_create(
            name="Growth Business Loan",
            defaults={
                "loan_type": LoanProduct.LoanType.BUSINESS,
                "min_amount": Decimal("50000"),
                "max_amount": Decimal("750000"),
                "interest_rate": Decimal("10.25"),
                "min_term_months": 6,
                "max_term_months": 36,
                "processing_fee_percent": Decimal("1.00"),
                "grace_period_days": 30,
                "is_active": True,
            },
        )

        active_app, _ = LoanApplication.objects.get_or_create(
            borrower=maria,
            loan_product=personal,
            purpose="Renovate my home office and consolidate two smaller balances.",
            defaults={
                "amount_requested": Decimal("120000"),
                "term_months": 12,
                "payment_frequency": LoanApplication.PaymentFrequency.MONTHLY,
                "status": LoanApplication.Status.APPROVED,
                "reviewed_by": officer,
                "review_notes": "Stable income and complete documents.",
                "final_interest_rate": Decimal("12.50"),
                "final_term_months": 12,
                "decision_date": timezone.now() - timedelta(days=70),
            },
        )
        if not hasattr(active_app, "loan"):
            loan = disburse_application(
                active_app,
                active_app.amount_requested,
                "Bank transfer",
                "LUMEN-240617",
                timezone.localdate() - timedelta(days=70),
            )
            if not Payment.objects.filter(loan=loan).exists():
                record_payment(loan, Decimal("11000"), "bank_transfer", "PAY-DEMO-001", maria)

        submitted_app, _ = LoanApplication.objects.get_or_create(
            borrower=carlo,
            loan_product=business,
            purpose="Purchase inventory for my growing neighborhood bakery.",
            defaults={
                "amount_requested": Decimal("300000"),
                "term_months": 18,
                "payment_frequency": LoanApplication.PaymentFrequency.MONTHLY,
                "status": LoanApplication.Status.SUBMITTED,
            },
        )
        under_review_app, _ = LoanApplication.objects.get_or_create(
            borrower=carlo,
            loan_product=personal,
            purpose="Cover a planned family medical expense.",
            defaults={
                "amount_requested": Decimal("85000"),
                "term_months": 9,
                "payment_frequency": LoanApplication.PaymentFrequency.MONTHLY,
                "status": LoanApplication.Status.UNDER_REVIEW,
                "reviewed_by": officer,
            },
        )

        regular_savings, _ = SavingsProduct.objects.update_or_create(
            name="Regular Savings",
            defaults={
                "description": "Everyday savings with steady interest.",
                "interest_rate": Decimal("2.50"),
                "interest_term_months": 1,
                "min_balance": Decimal("500.00"),
                "min_deposit": Decimal("100.00"),
                "is_active": True,
            },
        )
        time_deposit, _ = SavingsProduct.objects.update_or_create(
            name="Time Deposit Plus",
            defaults={
                "description": "Higher rate for members building longer-term reserves.",
                "interest_rate": Decimal("4.00"),
                "interest_term_months": 12,
                "min_balance": Decimal("5000.00"),
                "min_deposit": Decimal("1000.00"),
                "is_active": True,
            },
        )
        from savings.models import SavingsAccount

        if not SavingsAccount.objects.filter(member=maria, product=regular_savings).exists():
            maria_account = open_account(maria, regular_savings, opened_by=officer, initial_deposit=Decimal("2500.00"))
            record_deposit(maria_account, Decimal("1500.00"), "bank_transfer", "SAV-DEMO-001", created_by=maria)

        self.stdout.write(self.style.SUCCESS("Demo data is ready."))
        self.stdout.write("Officer: officer@lumen.test / Officer123!")
        self.stdout.write("Borrower: maria@lumen.test / Borrower123!")
        self.stdout.write("Borrower: carlo@lumen.test / Borrower123!")