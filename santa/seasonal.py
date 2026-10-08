"""The Christmas theme: the console, the request form and the e-mails in December.

SANTA_CHRISTMAS_THEME switches it on (December only), SANTA_CHRISTMAS_THEME_FORCE shows it all year.
"""
from django.conf import settings
from django.utils import timezone


def christmas_active(now=None):
    if settings.SANTA_CHRISTMAS_THEME_FORCE:
        return True
    return settings.SANTA_CHRISTMAS_THEME and timezone.localdate(now).month == 12
