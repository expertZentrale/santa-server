"""The logging setup of Django (LOGGING_CONFIG): LOGGING at the level LOG_LEVEL.

Read when Django sets up logging, after the whole settings module: a deployment's module that sets LOG_LEVEL after
`from santa_server.settings import *` counts too, not only the environment variable.
"""
import copy
import logging.config

LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


def configure(logging_settings):
    from django.conf import settings
    from django.core.exceptions import ImproperlyConfigured

    level = str(getattr(settings, "LOG_LEVEL", "INFO")).strip().upper()
    if level not in LEVELS:
        raise ImproperlyConfigured(f"LOG_LEVEL must be one of {', '.join(LEVELS)}, not {level!r}")
    # a copy: the dict of the settings stays as it is
    config = copy.deepcopy(logging_settings)
    config.setdefault("root", {})["level"] = level
    # also on the handler: the "django" loggers have a level of their own (INFO) and would pass it otherwise
    if "console" in config.get("handlers", {}):
        config["handlers"]["console"]["level"] = level
    logging.config.dictConfig(config)
