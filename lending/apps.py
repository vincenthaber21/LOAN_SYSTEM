from django.apps import AppConfig


class LendingConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "lending"

    def ready(self):
        from . import signals  # noqa: F401