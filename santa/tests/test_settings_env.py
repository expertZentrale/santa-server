import importlib.util
from pathlib import Path
from unittest.mock import patch

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase

SETTINGS_FILE = Path(__file__).resolve().parents[2] / "santa_server" / "settings.py"
REQUIRED = {"SECRET_KEY": "secret", "DB_HOST": "db", "DB_USER": "santa", "DB_PASSWORD": "pw",
            "REDIS_URL": "redis://redis:6379/0"}


def load_settings(environ):
    """Import santa_server/settings.py (the settings of the image) with these environment variables only"""
    spec = importlib.util.spec_from_file_location("santa_server._settings_under_test", SETTINGS_FILE)
    module = importlib.util.module_from_spec(spec)
    with patch.dict("os.environ", environ, clear=True):
        spec.loader.exec_module(module)
    return module


class ImageSettingsTestCase(SimpleTestCase):
    def test_required_variables(self):
        with self.assertRaisesMessage(ImproperlyConfigured, "SECRET_KEY"):
            load_settings({key: value for key, value in REQUIRED.items() if key != "SECRET_KEY"})
        with self.assertRaisesMessage(ImproperlyConfigured, "DB_PASSWORD"):
            load_settings({**REQUIRED, "DB_PASSWORD": "  "})

    def test_defaults(self):
        settings = load_settings(REQUIRED)
        self.assertFalse(settings.DEBUG)
        self.assertTrue(settings.SESSION_COOKIE_SECURE)
        self.assertFalse(hasattr(settings, "SECURE_PROXY_SSL_HEADER"))
        self.assertEqual(settings.ALLOWED_HOSTS, ["*"])
        self.assertEqual((settings.LANGUAGE_CODE, settings.TIME_ZONE), ("en", "UTC"))
        self.assertEqual(settings.SANTA_PROFILE_MACHINE_OWNER, "")
        self.assertEqual(settings.DATABASES["default"]["PORT"], "1433")
        self.assertEqual(settings.OIDC_RP_CLIENT_ID, "")

    def test_variables(self):
        settings = load_settings({
            **REQUIRED, "DEBUG": "true", "ALLOWED_HOSTS": "santa.example.com, localhost",
            "CSRF_TRUSTED_ORIGINS": "https://santa.example.com", "TRUST_X_FORWARDED_PROTO": "1",
            "SECURE_COOKIES": "yes", "LANGUAGE_CODE": "de", "TIME_ZONE": "Europe/Paris",
            "SANTA_PROFILE_MACHINE_OWNER": "$EMAIL", "SANTA_PROFILE_IDENTIFIER_PREFIX": "org.example.santa",
            "OIDC_CLIENT_ID": "client", "OIDC_TOKEN_ENDPOINT": "https://idp.example.com/token",
            "OIDC_ROLES_CLAIM": "realm_access.roles", "OIDC_USERNAME_CLAIMS": "email, sub",
        })
        self.assertTrue(settings.DEBUG)
        self.assertEqual(settings.ALLOWED_HOSTS, ["santa.example.com", "localhost"])
        self.assertEqual(settings.SECURE_PROXY_SSL_HEADER, ("HTTP_X_FORWARDED_PROTO", "https"))
        self.assertTrue(settings.CSRF_COOKIE_SECURE)
        self.assertEqual((settings.LANGUAGE_CODE, settings.TIME_ZONE), ("de", "Europe/Paris"))
        self.assertEqual(settings.SANTA_PROFILE_MACHINE_OWNER, "$EMAIL")
        self.assertEqual(settings.SANTA_PROFILE_IDENTIFIER_PREFIX, "org.example.santa")
        self.assertEqual((settings.OIDC_RP_CLIENT_ID, settings.OIDC_OP_TOKEN_ENDPOINT),
                         ("client", "https://idp.example.com/token"))
        self.assertEqual(settings.OIDC_USERNAME_CLAIMS, ["email", "sub"])
        self.assertEqual(settings.OIDC_ROLES_CLAIM, "realm_access.roles")
