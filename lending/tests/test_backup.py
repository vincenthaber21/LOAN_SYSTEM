import io
import zipfile
from decimal import Decimal

from django.contrib.messages import get_messages
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from lending.backup import (
    BackupError,
    build_backup_zip,
    preview_fixture,
    restore_backup_file,
    restore_database,
)
from lending.models import Features, LoanProduct, User


class BackupRoundtripTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username="backup-admin",
            email="admin@example.com",
            password="secret-pass",
            full_name="Backup Admin",
            role=User.Role.ADMIN,
            is_staff=True,
            is_superuser=True,
        )
        self.product = LoanProduct.objects.create(
            name="Backup Product",
            loan_type=LoanProduct.LoanType.PERSONAL,
            min_amount=Decimal("1000.00"),
            max_amount=Decimal("50000.00"),
            interest_rate=Decimal("2.50"),
        )
        features = Features.load()
        features.store_name = "Backup Co"
        features.tagline = "Keep a copy"
        features.save(update_fields=["store_name", "tagline"])

    def test_export_then_restore_replaces_changed_data(self):
        filename, payload, manifest = build_backup_zip()
        self.assertTrue(filename.endswith(".zip"))
        self.assertGreaterEqual(manifest["record_counts"]["lending.user"], 1)
        self.assertGreaterEqual(manifest["record_counts"]["lending.loanproduct"], 1)

        self.product.name = "Changed Product"
        self.product.save(update_fields=["name"])
        LoanProduct.objects.create(
            name="Extra Product",
            loan_type=LoanProduct.LoanType.BUSINESS,
            min_amount=Decimal("2000.00"),
            max_amount=Decimal("80000.00"),
            interest_rate=Decimal("3.00"),
        )
        Features.objects.filter(pk=1).update(store_name="Temporarily Different")

        uploaded = SimpleUploadedFile(filename, payload, content_type="application/zip")
        result = restore_backup_file(uploaded)

        self.assertGreaterEqual(result["record_total"], 2)
        self.assertEqual(LoanProduct.objects.count(), 1)
        self.assertEqual(LoanProduct.objects.get().name, "Backup Product")
        self.assertEqual(Features.objects.get(pk=1).store_name, "Backup Co")
        self.assertTrue(User.objects.filter(username="backup-admin").exists())

    def test_invalid_json_does_not_wipe_data(self):
        uploaded = SimpleUploadedFile("broken.json", b"not-json", content_type="application/json")
        with self.assertRaises(BackupError):
            restore_backup_file(uploaded)
        self.assertTrue(LoanProduct.objects.filter(name="Backup Product").exists())

    def test_fixture_without_users_is_rejected(self):
        with self.assertRaises(BackupError):
            preview_fixture("[]")
        self.assertTrue(User.objects.filter(username="backup-admin").exists())

    def test_restore_rejects_incomplete_user_fixture(self):
        original_count = LoanProduct.objects.count()
        with self.assertRaises((BackupError, Exception)):
            restore_database('[{"model": "lending.user", "pk": 1, "fields": {"username": null}}]')
        self.assertEqual(LoanProduct.objects.count(), original_count)
        self.assertTrue(User.objects.filter(username="backup-admin").exists())


class BackupAdminViewTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username="site-admin",
            email="site-admin@example.com",
            password="secret-pass",
            full_name="Site Admin",
            role=User.Role.ADMIN,
            is_staff=True,
            is_superuser=True,
        )
        Features.load()
        self.client.force_login(self.admin)

    def test_features_page_shows_backup_controls(self):
        url = reverse("admin:lending_features_change", args=(1,))
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Download backup ZIP")
        self.assertContains(response, "Restore from backup")
        self.assertContains(response, "export-backup")

    def test_export_downloads_zip(self):
        url = reverse("admin:lending_features_export_backup")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/zip")
        content = b"".join(response.streaming_content)
        self.assertTrue(content.startswith(b"PK"))
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            self.assertIn("data.json", archive.namelist())
            self.assertIn("manifest.json", archive.namelist())

    def test_import_requires_confirmation(self):
        filename, payload, _manifest = build_backup_zip()
        url = reverse("admin:lending_features_import_backup")
        response = self.client.post(
            url,
            {"backup_file": SimpleUploadedFile(filename, payload, content_type="application/zip")},
        )
        self.assertEqual(response.status_code, 302)
        messages = [str(item) for item in get_messages(response.wsgi_request)]
        self.assertTrue(any("Confirm" in message for message in messages))

    def test_anonymous_user_cannot_export(self):
        self.client.logout()
        response = self.client.get(reverse("admin:lending_features_export_backup"))
        self.assertEqual(response.status_code, 302)
