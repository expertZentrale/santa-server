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
            self.stdout.write("E-mail notifications are off (EMAIL_HOST, EMAIL_NOTIFICATIONS_ENABLED)")
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
                        .prefetch_related("rules").order_by("source__name", "-created_at"))
        # from the version, not from auto_approve of the source today: it may have changed since
        approved = [version for version in versions
                    if version.auto_enable_pending or any(rule.is_enabled for rule in version.rules.all())]
        sections = {
            "new_requests": list(AccessRequest.objects.filter(created_at__gte=since).select_related("requester")),
            "package_pending": [version for version in versions if version not in approved],
            "package_auto": approved,
            "package_errors": list(ReleaseSource.objects.filter(is_enabled=True).exclude(last_error="")
                                   .order_by("name")),
        }
        open_requests = AccessRequest.objects.filter(status=AccessRequest.Status.PENDING).count()
        messages = []
        for user in User.objects.filter(is_active=True, is_staff=True).exclude(email="").order_by("pk"):
            chosen = {notification.key for notification in notifications.available(user)
                      if notification.daily and notifications.mode(user, notification.key) == notifications.DAILY}
            mine = {**sections, "new_requests": [access_request for access_request in sections["new_requests"]
                                                 if access_request.requester_id != user.pk]}
            items = {key: mine[key] for key in chosen if mine[key]}
            if not items:
                continue
            messages.append(notifications.message(user, (_("Daily summary"), {}), "digest", {
                **items, "open_requests": open_requests if "new_requests" in chosen else None,
                "requests_url": notifications.absolute_url("console:requests"),
                "sources_url": notifications.absolute_url("console:sources"),
            }))
        # the scheduler waits, no thread
        notifications.send_messages(messages, background=False)
        return len(messages)
