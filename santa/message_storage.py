"""The messages of the console are kept for their user (UserNotification): the bell in the top bar lists them, the
page shows them only for a moment (core.js). Every messages.* call goes through here, nothing else changes."""
from django.contrib.messages import constants
from django.contrib.messages.storage.fallback import FallbackStorage

from .models import UserNotification

LEVELS = {
    constants.SUCCESS: UserNotification.Level.SUCCESS,
    constants.INFO: UserNotification.Level.INFO,
    constants.WARNING: UserNotification.Level.WARNING,
    constants.ERROR: UserNotification.Level.ERROR,
}


class NotificationStorage(FallbackStorage):
    def add(self, level, message, extra_tags=""):
        super().add(level, message, extra_tags)
        user = getattr(self.request, "user", None)
        if level in LEVELS and message and user is not None and user.is_authenticated:
            # the text in the language of the request, as the page shows it
            UserNotification.objects.create(user=user, level=LEVELS[level], text=str(message)[:5000])
            self.request.notifications_added = True
