import json
import os
import subprocess
import sys

from django.conf import settings
from django.test import SimpleTestCase

# the settings of the devcontainer, read in a new process: no connection is made
READ_SETTINGS = """
import json, django
django.setup()
from django.conf import settings
print(json.dumps({"debug": settings.DEBUG, "hosts": settings.ALLOWED_HOSTS, "oidc": settings.OIDC_RP_CLIENT_ID,
                  "name": settings.SANTA_SERVER_NAME, "email": settings.EMAIL_HOST,
                  "email_tls": settings.EMAIL_USE_TLS,
                  "extra": settings.DATABASES["default"]["OPTIONS"]["extra_params"],
                  "prefix": settings.CACHES["default"]["KEY_PREFIX"]}))
"""
# the variables a developer may set in .devcontainer/.env: left out, so the defaults show
PERSONAL = ("OIDC_", "SANTA_", "EMAIL_", "DEBUG", "ALLOWED_HOSTS", "DB_EXTRA_PARAMS", "CACHE_KEY_PREFIX", "SECRET_KEY")


class DevelopmentSettingsTestCase(SimpleTestCase):
    def dev_settings(self, **env):
        environment = {key: value for key, value in os.environ.items() if not key.startswith(PERSONAL)}
        environment.update({"DJANGO_SETTINGS_MODULE": "santa_server.settings_dev", **env})
        result = subprocess.run([sys.executable, "-c", READ_SETTINGS], env=environment, cwd=settings.BASE_DIR,
                                capture_output=True, text=True, check=True)
        return json.loads(result.stdout.strip().splitlines()[-1])

    def test_defaults_for_development(self):
        values = self.dev_settings()
        self.assertTrue(values["debug"])
        self.assertEqual(values["hosts"], ["*"])
        self.assertEqual(values["extra"], "TrustServerCertificate=yes;")
        self.assertEqual(values["prefix"], "santa_dev")
        self.assertEqual((values["email"], values["email_tls"]), ("mailpit", False))
        self.assertEqual(values["oidc"], "")

    def test_variables_of_the_env_file_apply(self):
        # like the image (settings.py): every variable of the configuration table, winning over the defaults
        values = self.dev_settings(OIDC_CLIENT_ID="santa-dev", SANTA_SERVER_NAME="Santa Dev",
                                   EMAIL_HOST="smtp.example", DEBUG="false")
        self.assertEqual(values["oidc"], "santa-dev")
        self.assertEqual(values["name"], "Santa Dev")
        self.assertEqual(values["email"], "smtp.example")
        self.assertFalse(values["debug"])
