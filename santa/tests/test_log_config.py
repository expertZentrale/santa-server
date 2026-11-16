import logging
from unittest.mock import patch

from django.conf import settings
from django.core.checks import run_checks
from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, override_settings

from santa.log_config import configure


class LogConfigTestCase(SimpleTestCase):
    """LOG_LEVEL is applied when Django sets up logging, so a settings module may set it after the import"""

    def configured(self):
        with patch("logging.config.dictConfig") as dict_config:
            configure(settings.LOGGING)
        return dict_config.call_args.args[0]

    def test_the_level(self):
        with override_settings(LOG_LEVEL="warning"):
            config = self.configured()
        self.assertEqual((config["root"]["level"], config["handlers"]["console"]["level"]), ("WARNING", "WARNING"))
        # the settings stay as they are
        self.assertNotIn("level", settings.LOGGING["handlers"]["console"])
        self.assertEqual(config["loggers"], settings.LOGGING["loggers"])

    def test_invalid_level(self):
        with override_settings(LOG_LEVEL="loud"), self.assertRaisesMessage(ImproperlyConfigured, "LOG_LEVEL"):
            self.configured()

    def test_really_configured(self):
        root = logging.getLogger()
        level, handlers = root.level, root.handlers[:]
        self.addCleanup(lambda: (root.setLevel(level), root.handlers.__setitem__(slice(None), handlers)))
        with override_settings(LOG_LEVEL="ERROR"):
            configure(settings.LOGGING)
        self.assertEqual(root.level, logging.ERROR)


class MetricsCheckTestCase(SimpleTestCase):
    def ids(self):
        return [message.id for message in run_checks() if message.id.startswith("santa.")]

    def test_matching(self):
        self.assertEqual(self.ids(), [])

    def test_changed_after_the_import(self):
        installed = "django_prometheus" in settings.INSTALLED_APPS
        with override_settings(METRICS_ENABLED=not installed):
            self.assertEqual(self.ids(), ["santa.W001"])
