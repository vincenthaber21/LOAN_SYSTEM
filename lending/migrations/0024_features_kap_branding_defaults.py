from pathlib import Path
from shutil import copyfile

from django.conf import settings
from django.db import migrations, models


DEFAULT_STORE_NAME = "KAP"
DEFAULT_TAGLINE = "Kaakibat ang Pag-unlad Microfinancing Inc."
DEFAULT_LOGO_STATIC = "branding/kap_logo.png"
DEFAULT_LOGO_MEDIA = "branding/KAP_logo_transparent_1_csXgoGL.png"


def _find_bundled_logo():
    try:
        from django.contrib.staticfiles import finders

        found = finders.find(DEFAULT_LOGO_STATIC)
        if found:
            return Path(found if isinstance(found, str) else found[0])
    except Exception:
        pass

    for candidate in (
        Path(settings.BASE_DIR) / "static" / DEFAULT_LOGO_STATIC,
        *(Path(root) / DEFAULT_LOGO_STATIC for root in getattr(settings, "STATICFILES_DIRS", ())),
        Path(getattr(settings, "STATIC_ROOT", "")) / DEFAULT_LOGO_STATIC,
    ):
        if candidate.is_file():
            return candidate
    return None


def apply_kap_branding(apps, schema_editor):
    Features = apps.get_model("lending", "Features")
    obj, _ = Features.objects.get_or_create(
        pk=1,
        defaults={
            "store_name": DEFAULT_STORE_NAME,
            "tagline": DEFAULT_TAGLINE,
        },
    )
    changed = False
    if obj.store_name in ("", "Harborline"):
        obj.store_name = DEFAULT_STORE_NAME
        changed = True
    if obj.tagline in ("", "Lending workspace"):
        obj.tagline = DEFAULT_TAGLINE
        changed = True

    media_root = Path(settings.MEDIA_ROOT)
    target = media_root / DEFAULT_LOGO_MEDIA
    needs_logo = not obj.logo or not (media_root / str(obj.logo)).is_file()
    if needs_logo:
        if not target.is_file():
            source = _find_bundled_logo()
            if source is not None:
                target.parent.mkdir(parents=True, exist_ok=True)
                copyfile(source, target)
        if target.is_file():
            obj.logo = DEFAULT_LOGO_MEDIA
            changed = True

    if changed:
        obj.save()


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("lending", "0023_loan_original_schedule_terms"),
    ]

    operations = [
        migrations.AlterField(
            model_name="features",
            name="store_name",
            field=models.CharField(default="KAP", max_length=120),
        ),
        migrations.AlterField(
            model_name="features",
            name="tagline",
            field=models.CharField(
                blank=True,
                default="Kaakibat ang Pag-unlad Microfinancing Inc.",
                max_length=120,
            ),
        ),
        migrations.RunPython(apply_kap_branding, noop_reverse),
    ]
