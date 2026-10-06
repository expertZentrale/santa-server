from datetime import timedelta

from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.management.base import BaseCommand
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from santa import notifications
from santa.models import AccessRequest, ReleaseSource, ReleaseVersion

LOCK_KEY = "santa:send_notification_digest"


class Command(BaseCommand):
    help = "Send the daily summary to the users who chose it for a notification (run it once a day)"

    def add_arguments(self, parser):
        parser.add_argument("--hours", type=int, default=24, help="What happened in the last hours (default 24)")

    def handle(self, *args, **options):
        if not notifications.enabled():
            self.stdout.write("E-mail is not set up (EMAIL_HOST)")
            return
        if not cache.add(LOCK_KEY, "1", timeout=3600):
            self.stderr.write("Another digest is being sent")
            return
        try:
            sent = self.send_all(timezone.now() - timedelta(hours=options["hours"]))
        finally:
            cache.delete(LOCK_KEY)
        self.stdout.write(f"{sent} summaries sent")

    def send_all(self, since):
        versions = list(ReleaseVersion.objects.filter(created_at__gte=since).select_related("source")
                        .order_by("source__name", "-created_at"))
        sections = {
            "new_requests": list(AccessRequest.objects.filter(created_at__gte=since).select_related("requester")),
            "package_pending": [version for version in versions if not version.source.auto_approve],
            "package_auto": [version for version in versions if version.source.auto_approve],
            "package_errors": list(ReleaseSource.objects.filter(is_enabled=True).exclude(last_error="")
                                   .order_by("name")),
        }
        open_requests = AccessRequest.objects.filter(status=AccessRequest.Status.PENDING).count()
        sent = 0
        for user in User.objects.filter(is_active=True, is_staff=True).exclude(email="").order_by("pk"):
            chosen = {notification.key for notification in notifications.available(user)
                      if notification.daily and notifications.mode(user, notification.key) == notifications.DAILY}
            items = {key: sections[key] for key in chosen if sections[key]}
            if not items:
                continue
            notifications.send(user, (_("Daily summary"), {}), "digest", {
                **items, "open_requests": open_requests if "new_requests" in chosen else None,
                "requests_url": notifications.absolute_url("console:requests"),
                "sources_url": notifications.absolute_url("console:sources"),
            })
            sent += 1
        return sent
