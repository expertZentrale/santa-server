import json
import os
import subprocess
import sys
from unittest import skipUnless

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django_prometheus.db import execute_total, query_duration_seconds

from santa.db.base import connect_seconds
from santa.middleware import view_db_connect_seconds, view_db_queries, view_db_seconds
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

    @needs_metrics
    def test_queries_by_view(self):
        self.client.force_login(get_user_model().objects.create_superuser("metrics", password="x"))
        labels = ("console:events", "GET", "1")
        count = view_db_queries.labels(*labels)._sum.get()
        timed = view_db_seconds.labels(*labels)._sum.get()
        self.client.get(reverse("console:events") + "?view=blocked&days=7", headers={"X-Santa-Rows": "1"})
        self.assertGreater(view_db_queries.labels(*labels)._sum.get(), count)
        self.assertGreater(view_db_seconds.labels(*labels)._sum.get(), timed)
        self.assertContains(self.client.get("/metrics"), 'santa_view_db_queries_count{method="GET",rows="1",'
                                                         'view="console:events"}')

    @needs_metrics
    def test_new_connection_is_timed(self):
        self.client.force_login(get_user_model().objects.create_superuser("metrics", password="x"))
        before = connect_seconds.labels(connection.alias)._sum.get()
        labels = ("console:events", "GET", "1")
        by_view = sum(bucket.get() for bucket in view_db_connect_seconds.labels(*labels)._buckets)
        # the test case's connection is in a transaction: a second connection object stands in for a new one
        connection.get_new_connection(connection.get_connection_params()).close()
        self.assertGreater(connect_seconds.labels(connection.alias)._sum.get(), before)
        self.assertIsNotNone(connection.connect_seconds)
        # the middleware resets it per request: no connection opened in this one
        self.client.get(reverse("console:events") + "?view=blocked&days=7", headers={"X-Santa-Rows": "1"})
        self.assertEqual(sum(bucket.get() for bucket in view_db_connect_seconds.labels(*labels)._buckets), by_view)

    @override_settings(METRICS_ENABLED=False)
    def test_metrics_page_switched_off(self):
        self.assertEqual(self.client.get("/metrics").status_code, 404)


# the settings of the image, read in a new process: no connection is made
IMAGE_SETTINGS = """
import json, django
django.setup()
from django.conf import settings
print(json.dumps({"apps": settings.INSTALLED_APPS, "middleware": settings.MIDDLEWARE,
                  "engine": settings.DATABASES["default"]["ENGINE"], "cache": settings.CACHES["default"]["BACKEND"],
                  "conn_max_age": settings.DATABASES["default"]["CONN_MAX_AGE"]}))
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
        self.assertIn("santa.middleware.ViewMetricsMiddleware", values["middleware"])

    def test_connections_are_kept(self):
        self.assertEqual(self.image_settings()["conn_max_age"], 600)
        self.assertEqual(self.image_settings(DB_CONN_MAX_AGE="0")["conn_max_age"], 0)

    def test_switched_off(self):
        values = self.image_settings(METRICS_ENABLED="false")
        self.assertNotIn("django_prometheus", values["apps"])
        self.assertFalse([name for name in values["middleware"] if "prometheus" in name or "Metrics" in name])
        self.assertEqual(values["engine"], "mssql")
        self.assertEqual(values["cache"], "django.core.cache.backends.redis.RedisCache")
