"""gunicorn settings of the image (gunicorn reads ./gunicorn.conf.py from /code by itself).

Workers, threads, worker class, timeout, max requests, the access log and the log level come from the Django
settings (GUNICORN_*, LOG_LEVEL): environment variables (the configuration table of the README) or the settings
module of the deployment, whatever its name (DJANGO_SETTINGS_MODULE). Only that module is imported here, not
django.setup(): no apps, no database. Bind, the heartbeat and metrics directories, the graceful timeout and the
control socket are fixed here. Flags on the command line still win over this file.
"""
import os
import shutil
import sys

from gunicorn.glogging import Logger

from santa.log_redaction import RedactSyncTokens, is_quiet_request, redact_sync_path

# as santa_server/wsgi.py: the workers load the same settings
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "santa_server.settings")

from django.conf import settings  # noqa: E402
from django.core.exceptions import ImproperlyConfigured  # noqa: E402

LOG_LEVELS = ("debug", "info", "warning", "error", "critical")


def setting(name):
    try:
        return getattr(settings, name)
    except ImproperlyConfigured as error:
        # a missing or invalid variable: one line, not a traceback of the master
        sys.exit(str(error))


def setting_int(name, minimum=1):
    """A whole number of the settings; settings.py checks the variables, this one what a settings module sets"""
    value = setting(name)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        sys.exit(f"{name} must be a whole number >= {minimum}, not {value!r}")
    return value


def setting_log_level():
    value = str(setting("LOG_LEVEL")).strip().lower()
    if value not in LOG_LEVELS:
        sys.exit(f"LOG_LEVEL must be one of {', '.join(LOG_LEVELS)}, not {value!r}")
    return value


bind = "0.0.0.0:8000"
# gthread: a long request (a package rule check, an upload) holds one thread, the worker keeps answering with the
# others and its heartbeat goes on. gevent / eventlet don't fit: the SQL Server driver would block their event loop.
worker_class = setting("GUNICORN_WORKER_CLASS") or "gthread"
workers = setting_int("GUNICORN_WORKERS")
# gunicorn uses gthread anyway when threads > 1, so the sync worker gets one thread unless more are asked for
threads = (setting_int("GUNICORN_THREADS") if setting("GUNICORN_THREADS") is not None
           else 1 if worker_class == "sync" else 4)
timeout = setting_int("GUNICORN_TIMEOUT")
# a new worker after about this many requests, so a slowly growing worker never reaches the memory limit (0 = never)
max_requests = setting_int("GUNICORN_MAX_REQUESTS", minimum=0)
max_requests_jitter = max_requests // 10
# below the 30 s that Kubernetes waits after SIGTERM
graceful_timeout = 25
# the heartbeat of the workers in memory, not on the overlay disk of the container, which can stall
worker_tmp_dir = "/dev/shm"
# the control socket (gunicorn 25+) would go into the home directory, the image user has none; the container is
# managed with signals by the platform
control_socket_disable = True

# the error log of gunicorn (worker starts, timeouts) at the level of the server's own logs
loglevel = setting_log_level()
# one line per request; the probes and scrapes that answer fine are left out (SyncTokenRedactingLogger.access)
accesslog = "-" if setting("GUNICORN_ACCESS_LOG") else None
# the PID of the worker as <pid> (to find a restarted one) and the duration in ms
access_log_format = '%(p)s %(h)s %(u)s %(t)s "%(r)s" %(s)s %(b)s %(M)sms "%(a)s"'


class SyncTokenRedactingLogger(Logger):
    """The sync URLs contain the token of the group, a secret: never in the access or error log. The probes and
    scrapes that answer fine don't get a line either."""

    def setup(self, cfg):
        super().setup(cfg)
        self.error_log.addFilter(RedactSyncTokens())

    def access(self, resp, req, environ, request_time):
        if is_quiet_request(environ.get("PATH_INFO"), getattr(resp, "status", "")):
            return
        super().access(resp, req, environ, request_time)

    def atoms(self, resp, req, environ, request_time):
        atoms = super().atoms(resp, req, environ, request_time)
        for key in ("r", "U", "{raw_uri}e", "{path_info}e", "{request_uri}e"):
            if key in atoms:
                atoms[key] = redact_sync_path(atoms[key])
        return atoms


logger_class = SyncTokenRedactingLogger


def metrics_enabled():
    """As the workers run: settings.py installs django_prometheus by METRICS_ENABLED while it is imported, a module
    of the deployment that changes the setting afterwards doesn't change the apps (santa.checks warns)"""
    return bool(setting("METRICS_ENABLED")) and "django_prometheus" in setting("INSTALLED_APPS")


# Each worker keeps its own metrics, and a scrape reaches only one of them: their counters would seem to jump back
# and forth. In this directory every worker writes its values, and /metrics sums them up (prometheus_client's
# multiprocess mode, django_prometheus' export uses it by itself).
METRICS_DIR = "/dev/shm/santa-metrics"


def on_starting(server):
    """In the master, before the workers start (they get the variable): an empty directory, nothing of a last run"""
    if not metrics_enabled():
        return
    directory = os.environ.setdefault("PROMETHEUS_MULTIPROC_DIR", METRICS_DIR)
    shutil.rmtree(directory, ignore_errors=True)
    os.makedirs(directory)


def child_exit(server, worker):
    """A worker ended (restarted after max_requests, or stopped): its gauges go, its counters stay in the sums"""
    if os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        from prometheus_client import multiprocess

        multiprocess.mark_process_dead(worker.pid)


def post_worker_init(worker):
    """SIGTERM (a new version is rolled out, the server scaled down): the package checks running in this worker stop
    after their current package and end within the graceful timeout, with their versions, notifications and
    messages, instead of being killed in the middle (santa.progress.SHUTTING_DOWN). Then gunicorn's own handler."""
    import signal

    from santa.progress import SHUTTING_DOWN

    handler = signal.getsignal(signal.SIGTERM)

    def stop(signum, frame):
        SHUTTING_DOWN.set()
        if callable(handler):
            handler(signum, frame)

    signal.signal(signal.SIGTERM, stop)
    # as gunicorn sets it: the signal doesn't interrupt the system calls of running requests
    signal.siginterrupt(signal.SIGTERM, False)
