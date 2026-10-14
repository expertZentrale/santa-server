"""Keep the sync tokens out of the logs: they are the credential of the sync API and part of its URL.

No Django imports: gunicorn.conf.py uses it before Django is set up.
"""
import logging
import re

SYNC_TOKEN = re.compile(r"/sync/[^/?#\s]+/")


def redact_sync_path(text):
    return SYNC_TOKEN.sub("/sync/***/", text) if isinstance(text, str) and "/sync/" in text else text


class RedactSyncTokens(logging.Filter):
    """For the Django loggers: django.request ("Not Found: /sync/…") and django.server (runserver)"""

    def filter(self, record):
        message = record.getMessage()
        redacted = redact_sync_path(message)
        if redacted != message:
            record.msg, record.args = redacted, ()
        return True
