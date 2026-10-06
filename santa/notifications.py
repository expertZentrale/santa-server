"""E-mail notifications: who gets which mail, right after the change or in the daily summary.

Every user chooses per notification in the profile (UserProfile.notifications): off, immediately or daily (the
summary of the command send_notification_digest). Only the notifications the permissions of the user allow are
offered and sent. Without EMAIL_HOST nothing is sent.
"""
import logging
from dataclasses import dataclass

from django.conf import settings
from django.contrib.auth.models import User
from django.core.mail import send_mail
from django.db import transaction
from django.db.models import Q
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import translation
from django.utils.translation import gettext_lazy as _

from .models import AccessRequest
from .users import profile_for

logger = logging.getLogger(__name__)

OFF, INSTANT, DAILY = "off", "instant", "daily"
MODE_LABELS = {OFF: _("Off"), INSTANT: _("Immediately"), DAILY: _("Daily summary")}


@dataclass(frozen=True)
class Notification:
    key: str
    label: str
    help_text: str
    # all of them are needed; staff: only users of the console
    perms: tuple = ()
    staff: bool = True
    default: str = INSTANT
    # can be part of the daily summary
    daily: bool = True

    @property
    def modes(self):
        return [OFF, INSTANT, DAILY] if self.daily else [OFF, INSTANT]


NOTIFICATIONS = [
    Notification("my_requests", _("My requests"), _("Your request was approved or denied."),
                 staff=False, daily=False),
    Notification("new_requests", _("New requests"), _("A user asks for software."),
                 perms=("santa.change_accessrequest",)),
    Notification("package_pending", _("Package versions to approve"),
                 _("A package rule found a new version that waits for an approval."),
                 perms=("santa.view_releasesource", "santa.change_rule")),
    Notification("package_auto", _("Approved package versions"),
                 _("A package rule found a new version that is approved automatically."),
                 perms=("santa.view_releasesource",), default=OFF),
    Notification("package_errors", _("Package rule errors"),
                 _("A package rule can't be checked for new versions any more (once, when it starts failing)."),
                 perms=("santa.view_releasesource",)),
]
BY_KEY = {notification.key: notification for notification in NOTIFICATIONS}


def enabled():
    return bool(settings.EMAIL_HOST)


def available(user):
    """The notifications the user may get"""
    if not user.is_active:
        return []
    return [notification for notification in NOTIFICATIONS
            if (user.is_staff or not notification.staff) and user.has_perms(notification.perms)]


def mode(user, key):
    notification = BY_KEY[key]
    value = profile_for(user).notifications.get(key, notification.default)
    return value if value in notification.modes else notification.default


def absolute_url(name, *args):
    return settings.SANTA_PUBLIC_BASE_URL + reverse(name, args=args)


def users_for(key):
    """The users with an e-mail address who may get this notification (the mode is checked when sending)"""
    notification = BY_KEY[key]
    users = User.objects.filter(is_active=True).exclude(email="")
    if notification.staff:
        users = users.filter(is_staff=True)
    if notification.perms:
        # narrowed by the first permission in the database, has_perms() checks all of them
        app_label, codename = notification.perms[0].split(".")
        users = users.filter(
            Q(is_superuser=True)
            | Q(user_permissions__codename=codename, user_permissions__content_type__app_label=app_label)
            | Q(groups__permissions__codename=codename, groups__permissions__content_type__app_label=app_label)
        ).distinct()
    return [user for user in users if notification in available(user)]


def send(user, subject, template, context):
    """One mail, in the language of the user. A failure is logged, it never breaks the change that caused it.

    subject: (lazy text, its values), translated in the language of the user.
    """
    language = profile_for(user).language or settings.LANGUAGE_CODE
    with translation.override(language):
        body = render_to_string(f"email/{template}.txt", {
            **context, "user": user, "server_name": settings.SANTA_SERVER_NAME,
            "profile_url": absolute_url("profile"),
        })
        text, values = subject
        subject = f"[{settings.SANTA_SERVER_NAME}] {str(text) % values}"
    try:
        send_mail(subject, body, None, [user.email])
    except Exception:
        logger.exception("E-mail to %s failed: %s", user.email, subject)


def notify(key, users, subject, template, context, exclude=None):
    """Send right after the transaction to the users who chose "immediately".

    users: a list, or a function that returns it (evaluated after the commit); subject: (lazy text, its values).
    """
    if not enabled():
        return

    def deliver():
        for user in (users() if callable(users) else users):
            if user == exclude or not user.email or BY_KEY[key] not in available(user):
                continue
            if mode(user, key) == INSTANT:
                send(user, subject, template, context)

    transaction.on_commit(deliver)


# The changes that send a mail


def request_created(access_request):
    notify("new_requests", lambda: users_for("new_requests"),
           (_("New request: %(title)s"), {"title": access_request.title}), "new_request",
           {"access_request": access_request, "url": absolute_url("console:request", access_request.pk)},
           exclude=access_request.requester)


DECIDED_SUBJECTS = {AccessRequest.Status.APPROVED: _("Approved: %(title)s"),
                    AccessRequest.Status.DENIED: _("Not approved: %(title)s")}


def request_decided(access_request):
    if access_request.status not in DECIDED_SUBJECTS:
        return
    notify("my_requests", [access_request.requester],
           (DECIDED_SUBJECTS[access_request.status], {"title": access_request.title}), "request_decided",
           {"access_request": access_request, "url": absolute_url("requests:list")})


def versions_found(source, versions):
    """New versions of a package rule: to approve, or approved automatically (now or after the delay)"""
    if not versions:
        return
    if source.auto_approve:
        key, subject = "package_auto", _("New version of %(name)s approved")
    else:
        key, subject = "package_pending", _("New version of %(name)s to approve")
    notify(key, lambda: users_for(key), (subject, {"name": source.name}), "versions_found",
           {"source": source, "versions": versions, "pending": not source.auto_approve,
            "url": absolute_url("console:source", source.pk)})


def source_failed(source):
    notify("package_errors", lambda: users_for("package_errors"),
           (_("Package rule %(name)s fails"), {"name": source.name}), "source_failed",
           {"source": source, "url": absolute_url("console:source", source.pk)})
