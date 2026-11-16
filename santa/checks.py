from django.conf import settings
from django.core.checks import Warning, register


@register()
def metrics_setting(app_configs, **kwargs):
    """settings.py switches the metrics (django_prometheus, its middleware and backends) on or off while it is
    imported: METRICS_ENABLED changed afterwards by a deployment's module doesn't reach them"""
    installed = "django_prometheus" in settings.INSTALLED_APPS
    if bool(settings.METRICS_ENABLED) == installed:
        return []
    return [Warning(
        f"METRICS_ENABLED is {settings.METRICS_ENABLED}, but django_prometheus is "
        f"{'installed' if installed else 'not installed'}.",
        hint="Set METRICS_ENABLED as an environment variable (in a settings module: in os.environ before "
             "`from santa_server.settings import *`), not as a setting after that import.",
        id="santa.W001")]
