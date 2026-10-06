"""Settings of the devcontainer: the settings of the image (settings.py) with defaults for development.

Every environment variable of the configuration table (README) works here as well, e.g. in .devcontainer/.env:
a value there wins over the defaults below. The variables are read when runserver starts.
"""
import os

development_defaults = {
    "SECRET_KEY": "django-insecure-devcontainer-only",
    "DEBUG": "true",
    "ALLOWED_HOSTS": "*",
    "SANTA_PUBLIC_BASE_URL": "http://localhost:8000",
    # the SQL Server of the devcontainer has a self-signed certificate
    "DB_EXTRA_PARAMS": "TrustServerCertificate=yes;",
    "CACHE_KEY_PREFIX": "santa_dev",
    # the Mailpit of the devcontainer: the e-mails are shown on http://localhost:8025
    "EMAIL_HOST": "mailpit",
    "EMAIL_PORT": "1025",
    "EMAIL_USE_TLS": "false",
}
for name, value in development_defaults.items():
    os.environ.setdefault(name, value)

from .settings import *  # noqa: E402,F401,F403

# no manifest in development: the tests (DEBUG=False) must not depend on collectstatic
STORAGES = {
    **STORAGES,  # noqa: F405
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}
