import json
import time

from django.conf import settings
from django.db import connection
from django.http import HttpResponse
from django.utils import timezone, translation
from django_prometheus.exports import ExportToDjangoView
from prometheus_client import Histogram

from .users import profile_for, time_zone_for


class HealthCheckMiddleware:
    """/health, /ready and /metrics answer before the host check: probes and scrapers use the pod IP.

    /metrics only here (no URL), so METRICS_ENABLED=false makes it a 404.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path == "/health":
            return HttpResponse("ok", content_type="text/plain")
        if request.path == "/ready":
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT 1")
            except Exception:
                return HttpResponse("database unavailable", status=503, content_type="text/plain")
            return HttpResponse("ok", content_type="text/plain")
        if request.path == "/metrics" and settings.METRICS_ENABLED:
            return ExportToDjangoView(request)
        return self.get_response(request)


VIEW_LABELS = ["view", "method", "rows"]
view_db_queries = Histogram("santa_view_db_queries", "Database queries of a request, by view", VIEW_LABELS,
                            buckets=(1, 2, 5, 10, 15, 20, 30, 50, 100, 200))
view_db_seconds = Histogram("santa_view_db_seconds", "Time of the database queries of a request, by view", VIEW_LABELS,
                            buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5))
view_db_connect_seconds = Histogram("santa_view_db_connect_seconds",
                                    "Time to open a new database connection in a request, by view", VIEW_LABELS,
                                    buckets=(0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10))


class ViewMetricsMiddleware:
    """The queries of a request by view: django-prometheus counts them only per database.

    rows: the request of the rows of a list (console/_rows.html), the page itself is "0".
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        queries = [0, 0.0]

        def timed(execute, sql, params, many, context):
            started = time.perf_counter()
            try:
                return execute(sql, params, many, context)
            finally:
                queries[0] += 1
                queries[1] += time.perf_counter() - started

        # set by santa.db when the request opens a connection
        connection.connect_seconds = None
        with connection.execute_wrapper(timed):
            response = self.get_response(request)
        match = getattr(request, "resolver_match", None)
        if match is not None:
            labels = (match.view_name, request.method, "1" if request.headers.get("X-Santa-Rows") == "1" else "0")
            view_db_queries.labels(*labels).observe(queries[0])
            view_db_seconds.labels(*labels).observe(queries[1])
            if connection.connect_seconds is not None:
                view_db_connect_seconds.labels(*labels).observe(connection.connect_seconds)
        return response


class UserLanguageMiddleware:
    """The language chosen in the profile wins over the one of the browser (LocaleMiddleware)"""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated:
            language = profile_for(user).language
            if language:
                translation.activate(language)
                request.LANGUAGE_CODE = language
        return self.get_response(request)


class UserTimeZoneMiddleware:
    """Times in the time zone of the profile, or of the browser, instead of TIME_ZONE of the server"""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        zone = time_zone_for(request)
        if zone:
            timezone.activate(zone)
        else:
            timezone.deactivate()
        return self.get_response(request)


class NotificationTriggerMiddleware:
    """A request that added a message (message_storage.py) tells the page with htmx: the bell counts again.

    An HX-Trigger of the view (drawerSaved, also as JSON with details) is kept.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if getattr(request, "notifications_added", False) and request.headers.get("HX-Request") == "true":
            response["HX-Trigger"] = add_trigger(response.get("HX-Trigger", ""), "notificationsChanged")
        return response


def add_trigger(header, name):
    """An event added to an HX-Trigger header: a name, names with commas, or JSON"""
    header = header.strip()
    if not header:
        return name
    if header.startswith("{"):
        events = json.loads(header)
        events.setdefault(name, None)
        return json.dumps(events)
    return f"{header}, {name}"
