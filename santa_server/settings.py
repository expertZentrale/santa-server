"""Settings of the container image: everything comes from environment variables (see the README).

To set more (e.g. from a file rendered by a secret store), create santa_server/settings_local.py with
`from .settings import *` and your overrides, and set DJANGO_SETTINGS_MODULE=santa_server.settings_local.
"""
import os

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
ALLOWED_HOSTS = env_list("ALLOWED_HOSTS", ["*"])
CSRF_TRUSTED_ORIGINS = env_list("CSRF_TRUSTED_ORIGINS")
# behind a TLS terminating proxy / ingress that sets X-Forwarded-Proto
if env_bool("TRUST_X_FORWARDED_PROTO"):
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    USE_X_FORWARDED_HOST = True
SESSION_COOKIE_SECURE = CSRF_COOKIE_SECURE = env_bool("SECURE_COOKIES", not DEBUG)

DATABASES = {
    "default": {
        "ENGINE": "mssql",
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
        "BACKEND": "django_prometheus.cache.backends.redis.RedisCache",
        # redis://[user:password@]host:6379/db
        "LOCATION": env("REDIS_URL", required=True),
        "KEY_PREFIX": env("CACHE_KEY_PREFIX", "santa"),
    },
}

LANGUAGE_CODE = env("LANGUAGE_CODE", LANGUAGE_CODE)  # noqa: F405
TIME_ZONE = env("TIME_ZONE", TIME_ZONE)  # noqa: F405

# the URL the Macs use for the sync, e.g. https://santa.example.com
SANTA_PUBLIC_BASE_URL = env("SANTA_PUBLIC_BASE_URL", SANTA_PUBLIC_BASE_URL)  # noqa: F405
GITHUB_TOKEN = env("GITHUB_TOKEN")
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
OIDC_USERNAME_CLAIMS = env_list("OIDC_USERNAME_CLAIMS", OIDC_USERNAME_CLAIMS)  # noqa: F405
OIDC_PROVIDER_NAME = env("OIDC_PROVIDER_NAME", OIDC_PROVIDER_NAME)  # noqa: F405
