"""SQL Server (mssql-django) with the database metrics of django-prometheus.

django-prometheus only ships wrappers for PostgreSQL, MySQL and SQLite. mssql-django wraps the driver's cursor in its
own CursorWrapper, so the counting wrapper of django-prometheus is put around that one.
"""
from django_prometheus.db.common import DatabaseWrapperMixin, ExportingCursorWrapper
from mssql import base


class DatabaseWrapper(DatabaseWrapperMixin, base.DatabaseWrapper):
    def create_cursor(self, name=None):
        cursor_class = ExportingCursorWrapper(base.CursorWrapper, self.alias, self.vendor)
        return cursor_class(self.connection.cursor(), self)
