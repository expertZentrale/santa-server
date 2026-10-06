from django.db import connection
from django.test import TestCase
from django_prometheus.db import execute_total, query_duration_seconds

from santa.models import Group


class DatabaseMetricsTestCase(TestCase):
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

    def test_metrics_page_shows_them(self):
        list(Group.objects.all())
        response = self.client.get("/metrics")
        self.assertContains(response, 'django_db_execute_total{alias="default",vendor="microsoft"}')
