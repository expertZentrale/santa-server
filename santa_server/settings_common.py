"""Settings shared by every environment.

settings.py (the image) reads the environment variables, settings_dev.py is used in the devcontainer.
Both import this module and only add what differs.
"""
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

INSTALLED_APPS = [
    "django_prometheus",
    # before the admin: its templates (admin/base_site.html) override the admin's
    "santa",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "whitenoise.runserver_nostatic",
    "django.contrib.staticfiles",
    "mozilla_django_oidc",
]

MIDDLEWARE = [
    "django_prometheus.middleware.PrometheusBeforeMiddleware",
    "santa.middleware.HealthCheckMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "santa.middleware.UserLanguageMiddleware",
    "santa.middleware.UserTimeZoneMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "django_prometheus.middleware.PrometheusAfterMiddleware",
]

ROOT_URLCONF = "santa_server.urls"
WSGI_APPLICATION = "santa_server.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "django.template.context_processors.i18n",
                "santa.context_processors.ui",
            ],
        },
    },
]

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# OpenID Connect for everyone, local accounts (e.g. the break-glass superuser) still work
AUTHENTICATION_BACKENDS = [
    "santa.auth.OIDCBackend",
    "django.contrib.auth.backends.ModelBackend",
]
LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "home"
LOGOUT_REDIRECT_URL = "login"

# OpenID Connect. Empty client ID = single sign-on disabled, e.g. in the devcontainer.
OIDC_RP_CLIENT_ID = ""
OIDC_RP_CLIENT_SECRET = ""
OIDC_OP_AUTHORIZATION_ENDPOINT = ""
OIDC_OP_TOKEN_ENDPOINT = ""
# not used (the claims come from the ID token), mozilla-django-oidc requires the setting
OIDC_OP_USER_ENDPOINT = ""
OIDC_OP_JWKS_ENDPOINT = ""
OIDC_RP_SIGN_ALGO = "RS256"
OIDC_RP_SCOPES = "openid email profile"
OIDC_AUTHENTICATION_CALLBACK_URL = "oidc_authentication_callback"
LOGIN_REDIRECT_URL_FAILURE = "/login/?failed=1"
# The role of the administrators (staff access to the console and the admin), in the claim OIDC_ROLES_CLAIM
OIDC_ADMIN_ROLE = "Santa.Admin"
OIDC_ROLES_CLAIM = "roles"
# The groups of the user (object IDs or names, depending on the provider), matched against the sign-in groups
OIDC_GROUPS_CLAIM = "groups"
# the first of these claims that is set is the username
OIDC_USERNAME_CLAIMS = ["preferred_username", "upn", "email"]
# name of the button on the login page
OIDC_PROVIDER_NAME = "single sign-on"
# logo on that button: "microsoft" (the logo and colors of "Sign in with Microsoft"), a URL or path of an image,
# or empty (no logo)
OIDC_PROVIDER_ICON = ""

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# Default language of the console and the request form, when neither the profile nor the browser chooses one
LANGUAGE_CODE = "en"
LANGUAGES = [("en", "English"), ("de", "Deutsch")]
LOCALE_PATHS = [BASE_DIR / "santa" / "locale"]
TEST_RUNNER = "santa.test_runner.EnglishTestRunner"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

# Gunicorn serves the static files through WhiteNoise, no nginx or bucket needed
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}

SESSION_ENGINE = "django.contrib.sessions.backends.cache"
SESSION_CACHE_ALIAS = "default"

# Uploaded binaries are only hashed, never stored. Big ones are spooled to a temp file.
FILE_UPLOAD_MAX_MEMORY_SIZE = 10 * 1024 * 1024
DATA_UPLOAD_MAX_MEMORY_SIZE = 20 * 1024 * 1024
# a compressed sync body may not expand to more than this (DATA_UPLOAD_MAX_MEMORY_SIZE limits the compressed size)
SYNC_MAX_DECOMPRESSED_BYTES = 20 * 1024 * 1024

# Public base URL of the sync endpoints, used for the per-group SyncBaseURL of the configuration profiles
SANTA_PUBLIC_BASE_URL = "http://localhost:8000"

# Release sources
GITHUB_TOKEN = None
RELEASE_MAX_DOWNLOAD_BYTES = 500 * 1024 * 1024
# unpacking archives (releases and uploads): bigger files are skipped, and an archive may not unpack to more in total
RELEASE_MAX_FILE_BYTES = 512 * 1024 * 1024
RELEASE_MAX_UNPACKED_BYTES = 2 * 1024 * 1024 * 1024
RELEASE_HTTP_TIMEOUT = 60

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"plain": {"format": "%(asctime)s %(levelname)s %(name)s %(message)s"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "plain"}},
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {"django.db.backends": {"level": "WARNING"}},
}

# The name in the title, the header and the Django admin, and the favicon: a URL or path, empty = the Santa hat
SANTA_SERVER_NAME = "Santa Server"
SANTA_FAVICON_URL = ""

# E-mail notifications (notifications.py): on with EMAIL_HOST, unless EMAIL_NOTIFICATIONS_ENABLED is false; when off,
# the profile doesn't offer them. Sent right after the change, in a background thread over one connection (the
# request doesn't wait for the mail server), a failure is logged.
EMAIL_NOTIFICATIONS_ENABLED = True
EMAIL_HOST = ""
EMAIL_TIMEOUT = 10
EMAIL_SEND_IN_BACKGROUND = True
DEFAULT_FROM_EMAIL = "santa@localhost"

# Configuration profiles (.mobileconfig) for the MDM. The profile identifiers and UUIDs are derived from the prefix:
# changing it later gives every profile a new identity in the MDM.
SANTA_PROFILE_ORGANIZATION = "Santa Server"
SANTA_PROFILE_IDENTIFIER_PREFIX = "com.example.santa"
# MachineOwner of the group profiles: a variable of the MDM for the primary user, e.g. {{userprincipalname}} (Intune)
# or $EMAIL (Jamf Pro). Santa reports it as primary user, the request form finds the Macs of a user with it.
# Empty = not set.
SANTA_PROFILE_MACHINE_OWNER = ""
