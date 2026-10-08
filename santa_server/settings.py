"""Settings of the container image: everything comes from environment variables (see the README).

To set more (e.g. from a file rendered by a secret store), create santa_server/settings_local.py with
`from .settings import *` and your overrides, and set DJANGO_SETTINGS_MODULE=santa_server.settings_local.
"""
import os
from urllib.parse import urlsplit

from django.core.exceptions import ImproperlyConfigured

from .settings_common import *  # noqa: F401,F403


def env(name, default=None, required=False):
    value = os.environ.get(name, "").strip()
    if not value:
        if required:
            raise ImproperlyConfigured(f"The environment variable {name} is required.")
        return default
    return value


def env_bool(name, default=False):
    value = env(name)
    return default if value is None else value.lower() in ("1", "true", "yes", "on")


def env_list(name, default=()):
    value = env(name)
    return list(default) if value is None else [item.strip() for item in value.split(",") if item.strip()]


SECRET_KEY = env("SECRET_KEY", required=True)
DEBUG = env_bool("DEBUG")
# the URL the Macs use for the sync, e.g. https://santa.example.com; also the default host and CSRF origin
SANTA_PUBLIC_BASE_URL = env("SANTA_PUBLIC_BASE_URL", required=True).rstrip("/")
_public_url = urlsplit(SANTA_PUBLIC_BASE_URL)
if _public_url.scheme not in ("http", "https") or not _public_url.hostname:
    raise ImproperlyConfigured("SANTA_PUBLIC_BASE_URL must be an http(s) URL, e.g. https://santa.example.com")
# /health, /ready and /metrics answer before the host check (santa.middleware), for probes and scrapers
ALLOWED_HOSTS = env_list("ALLOWED_HOSTS", [_public_url.hostname])
CSRF_TRUSTED_ORIGINS = env_list("CSRF_TRUSTED_ORIGINS", [f"{_public_url.scheme}://{_public_url.netloc}"])
# behind a TLS terminating proxy / ingress that sets X-Forwarded-Proto
if env_bool("TRUST_X_FORWARDED_PROTO"):
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    USE_X_FORWARDED_HOST = True
SESSION_COOKIE_SECURE = CSRF_COOKIE_SECURE = env_bool("SECURE_COOKIES", not DEBUG)

# Prometheus metrics on /metrics; off: nothing is collected, the plain backends are used
METRICS_ENABLED = env_bool("METRICS_ENABLED", True)
if not METRICS_ENABLED:
    INSTALLED_APPS = [app for app in INSTALLED_APPS if app != "django_prometheus"]  # noqa: F405
    MIDDLEWARE = [name for name in MIDDLEWARE if not name.startswith("django_prometheus.")]  # noqa: F405

DATABASES = {
    "default": {
        # mssql-django, with the metrics of django-prometheus (santa/db/base.py)
        "ENGINE": "santa.db" if METRICS_ENABLED else "mssql",
        "HOST": env("DB_HOST", required=True),
        "PORT": env("DB_PORT", "1433"),
        "NAME": env("DB_NAME", "santa"),
        "USER": env("DB_USER", required=True),
        "PASSWORD": env("DB_PASSWORD", required=True),
        "CONN_MAX_AGE": 60,
        "CONN_HEALTH_CHECKS": True,
        "OPTIONS": {
            "python_driver": "mssql_python",
            # e.g. Encrypt=yes;TrustServerCertificate=no
            "extra_params": env("DB_EXTRA_PARAMS", ""),
        },
    },
}

CACHES = {
    "default": {
        "BACKEND": "django_prometheus.cache.backends.redis.RedisCache" if METRICS_ENABLED
                   else "django.core.cache.backends.redis.RedisCache",
        # redis://[user:password@]host:6379/db
        "LOCATION": env("REDIS_URL", required=True),
        "KEY_PREFIX": env("CACHE_KEY_PREFIX", "santa"),
    },
}

LANGUAGE_CODE = env("LANGUAGE_CODE", LANGUAGE_CODE)  # noqa: F405
TIME_ZONE = env("TIME_ZONE", TIME_ZONE)  # noqa: F405

GITHUB_TOKEN = env("GITHUB_TOKEN")
RELEASE_MAX_DEPENDENCIES = int(env("RELEASE_MAX_DEPENDENCIES", str(RELEASE_MAX_DEPENDENCIES)))  # noqa: F405

EMAIL_NOTIFICATIONS_ENABLED = env_bool("EMAIL_NOTIFICATIONS_ENABLED", True)
EMAIL_HOST = env("EMAIL_HOST", "")
EMAIL_PORT = int(env("EMAIL_PORT", "587"))
EMAIL_HOST_USER = env("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = env_bool("EMAIL_USE_TLS", True)
EMAIL_USE_SSL = env_bool("EMAIL_USE_SSL", False)
if EMAIL_USE_SSL:
    EMAIL_USE_TLS = False
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", f"santa@{_public_url.hostname}")
SANTA_SERVER_NAME = env("SANTA_SERVER_NAME", SANTA_SERVER_NAME)  # noqa: F405
SANTA_FAVICON_URL = env("SANTA_FAVICON_URL", "")
SANTA_CHRISTMAS_THEME = env_bool("SANTA_CHRISTMAS_THEME", False)
SANTA_CHRISTMAS_THEME_FORCE = env_bool("SANTA_CHRISTMAS_THEME_FORCE", False)
SANTA_PROFILE_ORGANIZATION = env("SANTA_PROFILE_ORGANIZATION", SANTA_PROFILE_ORGANIZATION)  # noqa: F405
SANTA_PROFILE_IDENTIFIER_PREFIX = env("SANTA_PROFILE_IDENTIFIER_PREFIX", SANTA_PROFILE_IDENTIFIER_PREFIX)  # noqa: F405
SANTA_PROFILE_MACHINE_OWNER = env("SANTA_PROFILE_MACHINE_OWNER", "")

OIDC_RP_CLIENT_ID = env("OIDC_CLIENT_ID", "")
OIDC_RP_CLIENT_SECRET = env("OIDC_CLIENT_SECRET", "")
OIDC_OP_AUTHORIZATION_ENDPOINT = env("OIDC_AUTHORIZATION_ENDPOINT", "")
OIDC_OP_TOKEN_ENDPOINT = env("OIDC_TOKEN_ENDPOINT", "")
OIDC_OP_JWKS_ENDPOINT = env("OIDC_JWKS_ENDPOINT", "")
OIDC_RP_SCOPES = env("OIDC_SCOPES", OIDC_RP_SCOPES)  # noqa: F405
OIDC_ADMIN_ROLE = env("OIDC_ADMIN_ROLE", OIDC_ADMIN_ROLE)  # noqa: F405
OIDC_ROLES_CLAIM = env("OIDC_ROLES_CLAIM", OIDC_ROLES_CLAIM)  # noqa: F405
OIDC_GROUPS_CLAIM = env("OIDC_GROUPS_CLAIM", OIDC_GROUPS_CLAIM)  # noqa: F405
OIDC_USERNAME_CLAIMS = env_list("OIDC_USERNAME_CLAIMS", OIDC_USERNAME_CLAIMS)  # noqa: F405
OIDC_PROVIDER_NAME = env("OIDC_PROVIDER_NAME", OIDC_PROVIDER_NAME)  # noqa: F405
OIDC_PROVIDER_ICON = env("OIDC_PROVIDER_ICON", "")
