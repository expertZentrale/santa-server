import os

from .settings_common import *  # noqa: F401,F403

DEBUG = True
SECRET_KEY = "django-insecure-devcontainer-only"
ALLOWED_HOSTS = ["*"]

DATABASES = {
    "default": {
        "ENGINE": "mssql",
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

SANTA_PUBLIC_BASE_URL = os.getenv("SANTA_PUBLIC_BASE_URL", "http://localhost:8000")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN") or None
