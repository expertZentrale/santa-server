from django.apps import AppConfig


class SantaConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "santa"
    verbose_name = "Santa"

    def ready(self):
        from . import checks  # noqa: F401
