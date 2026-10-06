import json
import os
import subprocess
import sys
from unittest import skipUnless

from django.conf import settings
from django.db import connection
from django.test import SimpleTestCase, TestCase, override_settings
from django_prometheus.db import execute_total, query_duration_seconds

from santa.models import Group

# METRICS_ENABLED=false in the environment (e.g. .devcontainer/.env) leaves django-prometheus out at the start: then
# there is nothing to count (the CI runs with the default)
needs_metrics = skipUnless(settings.METRICS_ENABLED, "METRICS_ENABLED=false in the environment")


class DatabaseMetricsTestCase(TestCase):
    @needs_metrics
    def test_queries_are_counted(self):
        labels = (connection.alias, connection.vendor)
        # the metrics count for the whole process: compare with the values before these queries
        executed = execute_total.labels(*labels)._value.get()
        timed = query_duration_seconds.labels(*labels)._sum.get()
        list(Group.objects.all())
        Group.objects.create(name="Metrics")
        self.assertEqual(connection.vendor, "microsoft")
        self.assertGreaterEqual(execute_total.labels(*labels)._value.get() - executed, 2)
        self.assertGreater(query_duration_seconds.labels(*labels)._sum.get(), timed)

    @needs_metrics
    def test_metrics_page_shows_them(self):
        list(Group.objects.all())
        response = self.client.get("/metrics")
        self.assertContains(response, 'django_db_execute_total{alias="default",vendor="microsoft"}')

    @override_settings(METRICS_ENABLED=False)
    def test_metrics_page_switched_off(self):
        self.assertEqual(self.client.get("/metrics").status_code, 404)


# the settings of the image, read in a new process: no connection is made
IMAGE_SETTINGS = """
import json, django
django.setup()
from django.conf import settings
print(json.dumps({"apps": settings.INSTALLED_APPS, "middleware": settings.MIDDLEWARE,
                  "engine": settings.DATABASES["default"]["ENGINE"], "cache": settings.CACHES["default"]["BACKEND"]}))
"""


class MetricsSettingsTestCase(SimpleTestCase):
    def image_settings(self, **env):
        environment = {key: value for key, value in os.environ.items() if not key.startswith(("DB_", "METRICS"))}
        environment.update({
            "DJANGO_SETTINGS_MODULE": "santa_server.settings", "SECRET_KEY": "test",
            "SANTA_PUBLIC_BASE_URL": "https://santa.example.com", "DB_HOST": "db", "DB_USER": "santa",
            "DB_PASSWORD": "x", "REDIS_URL": "redis://redis:6379/0", **env})
        result = subprocess.run([sys.executable, "-c", IMAGE_SETTINGS], env=environment, cwd=settings.BASE_DIR,
                                capture_output=True, text=True, check=True)
        return json.loads(result.stdout.strip().splitlines()[-1])

    def test_on_by_default(self):
        values = self.image_settings()
        self.assertIn("django_prometheus", values["apps"])
        self.assertEqual(values["engine"], "santa.db")
        self.assertEqual(values["cache"], "django_prometheus.cache.backends.redis.RedisCache")

    def test_switched_off(self):
        values = self.image_settings(METRICS_ENABLED="false")
        self.assertNotIn("django_prometheus", values["apps"])
        self.assertFalse([name for name in values["middleware"] if "prometheus" in name])
        self.assertEqual(values["engine"], "mssql")
        self.assertEqual(values["cache"], "django.core.cache.backends.redis.RedisCache")
