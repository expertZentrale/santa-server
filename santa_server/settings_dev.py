import os

from .settings_common import *  # noqa: F401,F403

DEBUG = True
SECRET_KEY = "django-insecure-devcontainer-only"
ALLOWED_HOSTS = ["*"]

DATABASES = {
    "default": {
        # mssql-django with the database metrics of django-prometheus (santa/db/base.py)
        "ENGINE": "santa.db",
        "HOST": os.getenv("DB_HOST", "mssql"),
        "PORT": os.getenv("DB_PORT", "1433"),
        "USER": os.getenv("DB_USER", "sa"),
        "PASSWORD": os.getenv("DB_PASSWORD", ""),
        "NAME": os.getenv("DB_NAME", "santa"),
        "OPTIONS": {
            "python_driver": "mssql_python",
            "extra_params": "TrustServerCertificate=yes;",
        },
    },
}

CACHES = {
    "default": {
        "BACKEND": "django_prometheus.cache.backends.redis.RedisCache",
        "LOCATION": os.getenv("REDIS_URL", "redis://redis:6379/1"),
        "KEY_PREFIX": "santa_dev",
    },
}

# no manifest in development: the tests (DEBUG=False) must not depend on collectstatic
STORAGES = {
    **STORAGES,  # noqa: F405
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}

# like the image: TIME_ZONE / LANGUAGE_CODE in .devcontainer/.env or containerEnv of devcontainer.json
TIME_ZONE = os.getenv("TIME_ZONE", TIME_ZONE)  # noqa: F405
LANGUAGE_CODE = os.getenv("LANGUAGE_CODE", LANGUAGE_CODE)  # noqa: F405

SANTA_PUBLIC_BASE_URL = os.getenv("SANTA_PUBLIC_BASE_URL", "http://localhost:8000")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN") or None

# e-mail notifications are on, the mails are printed in the log of runserver (the tests collect them in memory)
EMAIL_HOST = "localhost"
EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
