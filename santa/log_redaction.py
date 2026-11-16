"""What stays out of the logs: the sync tokens (the credential of the sync API, part of its URL), and the probes
and scrapes (/health, /ready, /metrics) that answer fine, every few seconds.

No Django imports: gunicorn.conf.py uses it before Django is set up.
"""
import logging
import re

# up to the end of the token, also without the rest of the path (a malformed or truncated request)
SYNC_TOKEN = re.compile(r"/sync/[^/?#\s\"']+")


def redact_sync_path(text):
    return SYNC_TOKEN.sub("/sync/***", text) if isinstance(text, str) and "/sync/" in text else text


class RedactSyncTokens(logging.Filter):
    """For the Django loggers: django.request ("Not Found: /sync/…") and django.server (runserver)"""

    def filter(self, record):
        message = record.getMessage()
        redacted = redact_sync_path(message)
        if redacted != message:
            record.msg, record.args = redacted, ()
        return True


# the probes of the platform and the Prometheus scrapes: as long as they answer, a line each would bury the requests
QUIET_PATHS = frozenset({"/health", "/ready", "/metrics"})


def is_quiet_request(path, status):
    """A probe or scrape that answered (< 400): not logged. A failing one (e.g. /ready 503) still is."""
    try:
        code = int(str(status).split(maxsplit=1)[0])
    except (ValueError, IndexError):
        return False
    return isinstance(path, str) and (path.rstrip("/") or "/") in QUIET_PATHS and code < 400


class QuietProbes(logging.Filter):
    """For django.server (runserver): the same requests as the access log of gunicorn leaves out"""

    def filter(self, record):
        args = record.args if isinstance(record.args, tuple) else ()
        if len(args) >= 2 and isinstance(args[0], str):
            parts = args[0].split()
            path = parts[1].split("?", 1)[0] if len(parts) > 1 else ""
            return not is_quiet_request(path, args[1])
        return True
