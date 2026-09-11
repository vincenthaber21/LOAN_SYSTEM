"""Full-database backup and restore for Django admin (Features).

Exports lending, savings, mutual aid, and auth groups as a ZIP of Django
fixtures plus uploaded media. Restore replaces that application data.
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import zipfile
from pathlib import Path

from django.apps import apps
from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection, transaction
from django.utils import timezone

BACKUP_FORMAT = "kap-backup"
BACKUP_FORMAT_VERSION = 1
BACKUP_APPS = ("lending", "savings", "mutual_aid")
EXTRA_LABELS = ("auth.Group",)
MAX_UNCOMPRESSED_BYTES = 500 * 1024 * 1024


class BackupError(ValueError):
    """Raised when a backup file cannot be built, read, or restored."""


def backup_models():
    """Concrete, managed models included in a backup (no proxy models)."""
    models = []
    seen = set()
    for app_label in BACKUP_APPS:
        config = apps.get_app_config(app_label)
        for model in config.get_models():
            if model._meta.proxy or not model._meta.managed:
                continue
            table = model._meta.db_table
            if table in seen:
                continue
            seen.add(table)
            models.append(model)
    for label in EXTRA_LABELS:
        model = apps.get_model(label)
        if model._meta.db_table not in seen:
            seen.add(model._meta.db_table)
            models.append(model)
    return models


def dumpdata_labels(models=None):
    return [f"{model._meta.app_label}.{model._meta.object_name}" for model in (models or backup_models())]


def record_counts(models=None):
    counts = {}
    for model in models or backup_models():
        counts[f"{model._meta.app_label}.{model._meta.model_name}"] = model._default_manager.count()
    return counts


def media_file_count():
    root = Path(settings.MEDIA_ROOT)
    if not root.is_dir():
        return 0
    return sum(1 for path in root.rglob("*") if path.is_file())


def _iter_media_files():
    root = Path(settings.MEDIA_ROOT)
    if not root.is_dir():
        return
    for path in root.rglob("*"):
        if path.is_file():
            yield path, path.relative_to(root)


def serialize_database():
    buffer = io.StringIO()
    call_command(
        "dumpdata",
        *dumpdata_labels(),
        natural_foreign=True,
        indent=2,
        verbosity=0,
        stdout=buffer,
    )
    return buffer.getvalue()


def build_manifest(json_text, media_count):
    counts = _fixture_counts(json_text)
    return {
        "format": BACKUP_FORMAT,
        "version": BACKUP_FORMAT_VERSION,
        "created_at": timezone.localtime().isoformat(),
        "apps": list(BACKUP_APPS) + list(EXTRA_LABELS),
        "record_counts": counts,
        "record_total": sum(counts.values()),
        "media_files": media_count,
    }


def build_backup_zip():
    """Return (filename, bytes) for a downloadable backup archive."""
    json_text = serialize_database()
    buffer = io.BytesIO()
    media_count = 0
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("data.json", json_text.encode("utf-8"))
        for path, relative in _iter_media_files():
            archive.write(path, Path("media", relative).as_posix())
            media_count += 1
        manifest = build_manifest(json_text, media_count)
        archive.writestr("manifest.json", json.dumps(manifest, indent=2).encode("utf-8"))
    stamp = timezone.localtime().strftime("%Y%m%d-%H%M%S")
    filename = f"kap-backup-{stamp}.zip"
    return filename, buffer.getvalue(), manifest


def _fixture_counts(json_text):
    try:
        payload = json.loads(json_text)
    except json.JSONDecodeError as exc:
        raise BackupError("Backup data.json is not valid JSON.") from exc
    if not isinstance(payload, list):
        raise BackupError("Backup data.json must be a Django fixture list.")
    counts = {}
    for row in payload:
        if not isinstance(row, dict) or "model" not in row:
            raise BackupError("Backup file is not a valid Django fixture.")
        label = str(row["model"]).lower()
        counts[label] = counts.get(label, 0) + 1
    return counts


def preview_fixture(json_text):
    counts = _fixture_counts(json_text)
    if counts.get("lending.user", 0) < 1:
        raise BackupError("This backup has no user accounts. Restore cancelled to avoid locking you out.")
    return counts


def _wipe_backup_tables(models):
    seen = set()
    to_wipe = []

    def add(model):
        if not model._meta.managed:
            return
        table = model._meta.db_table
        if table in seen:
            return
        seen.add(table)
        to_wipe.append(model)

    for model in models:
        for field in model._meta.local_many_to_many:
            add(field.remote_field.through)
        add(model)

    using = "default"
    for model in to_wipe:
        model._base_manager.using(using).all()._raw_delete(using)


def restore_database(json_text):
    preview_fixture(json_text)
    models = backup_models()
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    try:
        Path(path).write_text(json_text, encoding="utf-8")
        with transaction.atomic():
            with connection.constraint_checks_disabled():
                _wipe_backup_tables(models)
                try:
                    call_command("loaddata", path, verbosity=0, ignore=True)
                except CommandError as exc:
                    raise BackupError(f"Could not load backup data: {exc}") from exc
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _safe_media_destination(relative):
    media_root = Path(settings.MEDIA_ROOT).resolve()
    relative = Path(relative)
    if relative.is_absolute() or ".." in relative.parts:
        raise BackupError("Backup contains an unsafe media path.")
    destination = (media_root / relative).resolve()
    try:
        destination.relative_to(media_root)
    except ValueError as exc:
        raise BackupError("Backup contains an unsafe media path.") from exc
    return destination


def restore_media(archive):
    restored = 0
    total_bytes = 0
    for info in archive.infolist():
        name = info.filename.replace("\\", "/")
        if not name.startswith("media/") or name.endswith("/"):
            continue
        relative = name[len("media/"):]
        if not relative:
            continue
        total_bytes += info.file_size
        if total_bytes > MAX_UNCOMPRESSED_BYTES:
            raise BackupError("Backup media is too large to restore.")
        destination = _safe_media_destination(relative)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(info) as source, open(destination, "wb") as target:
            while True:
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                target.write(chunk)
        restored += 1
    return restored


def _read_zip_backup(uploaded):
    try:
        archive = zipfile.ZipFile(uploaded)
    except zipfile.BadZipFile as exc:
        raise BackupError("That file is not a valid ZIP backup.") from exc
    names = set(archive.namelist())
    if "data.json" not in names:
        raise BackupError("This ZIP is missing data.json.")
    uncompressed = sum(info.file_size for info in archive.infolist())
    if uncompressed > MAX_UNCOMPRESSED_BYTES:
        raise BackupError("This backup is too large to restore.")
    if "manifest.json" in names:
        try:
            manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise BackupError("Backup manifest.json is not valid JSON.") from exc
        if manifest.get("format") not in {BACKUP_FORMAT, None} or int(manifest.get("version", 1)) > BACKUP_FORMAT_VERSION:
            raise BackupError("This backup was created by a newer system and cannot be restored here.")
    json_text = archive.read("data.json").decode("utf-8")
    return json_text, archive


def _read_json_backup(uploaded):
    raw = uploaded.read()
    if len(raw) > MAX_UNCOMPRESSED_BYTES:
        raise BackupError("This backup is too large to restore.")
    try:
        json_text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise BackupError("That JSON backup is not valid UTF-8.") from exc
    return json_text, None


def load_backup_file(uploaded):
    """Parse an uploaded .zip or .json backup. Returns (json_text, zip_or_none)."""
    if uploaded is None:
        raise BackupError("Choose a backup file to restore.")
    name = (getattr(uploaded, "name", "") or "").lower()
    uploaded.seek(0)
    header = uploaded.read(4)
    uploaded.seek(0)
    if name.endswith(".zip") or header.startswith(b"PK"):
        return _read_zip_backup(uploaded)
    if name.endswith(".json") or header.startswith(b"[") or header.startswith(b"\xef\xbb\xbf"):
        return _read_json_backup(uploaded)
    raise BackupError("Upload a KAP backup ZIP, or a Django fixture JSON file.")


def restore_backup_file(uploaded):
    json_text, archive = load_backup_file(uploaded)
    media_restored = 0
    media_error = None
    try:
        counts = preview_fixture(json_text)
        restore_database(json_text)
        if archive is not None:
            try:
                media_restored = restore_media(archive)
            except BackupError as exc:
                media_error = str(exc)
    finally:
        if archive is not None:
            archive.close()
    return {
        "record_counts": counts,
        "record_total": sum(counts.values()),
        "media_files": media_restored,
        "media_error": media_error,
    }


def backup_summary_lines(counts):
    labels = {
        "lending.user": "Accounts",
        "lending.loanapplication": "Loan applications",
        "lending.loan": "Loans",
        "lending.payment": "Payments",
        "lending.installment": "Installments",
        "savings.savingsaccount": "Savings accounts",
        "savings.savingstransaction": "Savings transactions",
        "mutual_aid.mutualaidmembership": "Mutual aid memberships",
        "mutual_aid.mutualaidcontribution": "Mutual aid contributions",
        "lending.activitylog": "Activity logs",
    }
    lines = []
    for key, label in labels.items():
        if key in counts:
            lines.append((label, counts[key]))
    return lines
