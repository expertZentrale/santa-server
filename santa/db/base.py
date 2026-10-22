"""SQL Server (mssql-django) with the database metrics of django-prometheus.

django-prometheus only ships wrappers for PostgreSQL, MySQL and SQLite. mssql-django wraps the driver's cursor in its
own CursorWrapper, so the counting wrapper of django-prometheus is put around that one.
"""
import time

from django_prometheus.db.common import DatabaseWrapperMixin, ExportingCursorWrapper
from mssql import base
from prometheus_client import Histogram

# a new connection (TLS and login) can cost more than all queries of a request: CONN_MAX_AGE
connect_seconds = Histogram("santa_db_connect_seconds", "Time to open a new database connection", ["alias"],
                            buckets=(0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10))


class DatabaseWrapper(DatabaseWrapperMixin, base.DatabaseWrapper):
    def get_new_connection(self, conn_params):
        started = time.perf_counter()
        connection = super().get_new_connection(conn_params)
        # ViewMetricsMiddleware takes it for the view of the request
        self.connect_seconds = time.perf_counter() - started
        connect_seconds.labels(self.alias).observe(self.connect_seconds)
        return connection

    def create_cursor(self, name=None):
        cursor_class = ExportingCursorWrapper(base.CursorWrapper, self.alias, self.vendor)
        return cursor_class(self.connection.cursor(), self)
