"""E-mail notifications: who gets which mail, right after the change or in the daily summary.

Every user chooses per notification in the profile (UserProfile.notifications): off, immediately or daily (the
summary of the command send_notification_digest). Without a choice, the default of their sign-in groups applies
(SignInGroup.notification_defaults), else the default of the notification. Only the notifications the permissions
of the user allow are offered and sent. Without EMAIL_HOST, or with EMAIL_NOTIFICATIONS_ENABLED=false, nothing is
sent or offered.
"""
import logging
import smtplib
import threading
from dataclasses import dataclass

from django.conf import settings
from django.contrib.auth.models import User
from django.core.mail import EmailMessage, get_connection
from django.db import transaction
from django.db.models import Q
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import translation
from django.utils.translation import gettext_lazy as _

from .models import AccessRequest, SignInGroup, UserProfile
from .users import profile_for

logger = logging.getLogger(__name__)

OFF, INSTANT, DAILY = "off", "instant", "daily"
MODE_LABELS = {OFF: _("Off"), INSTANT: _("Immediately"), DAILY: _("Daily summary")}
# when sign-in groups disagree, the one with the most mail wins
RANK = {OFF: 0, DAILY: 1, INSTANT: 2}


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
    # personal and expected soon: no daily summary
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
                 _("A package rule has problems checking for new versions (once, when they start)."),
                 perms=("santa.view_releasesource",)),
]
BY_KEY = {notification.key: notification for notification in NOTIFICATIONS}


def enabled():
    """Off without a mail server, or switched off with EMAIL_NOTIFICATIONS_ENABLED=false"""
    return settings.EMAIL_NOTIFICATIONS_ENABLED and bool(settings.EMAIL_HOST)


def blocked(user):
    """No e-mails for this user: set by the administrators, or a sign-in group without e-mails"""
    override = profile_for(user).email_override
    if override:
        return override == UserProfile.EmailOverride.OFF
    return user.sign_in_groups.filter(no_email=True).exists()


def available(user):
    """The notifications the user may get"""
    if not user.is_active or blocked(user):
        return []
    return [notification for notification in NOTIFICATIONS
            if (user.is_staff or not notification.staff) and user.has_perms(notification.perms)]


def sign_in_groups(user):
    """Cached on the user: users_for() and the daily summary ask for every notification"""
    if not hasattr(user, "_santa_sign_in_groups"):
        user._santa_sign_in_groups = list(user.sign_in_groups.all())
    return user._santa_sign_in_groups


def group_default(user, key):
    """The default of the sign-in groups of the user, None without one. A group of its own wins over *."""
    notification = BY_KEY[key]
    values = {True: [], False: []}
    for group in sign_in_groups(user):
        value = group.notification_defaults.get(key)
        if value in notification.modes:
            values[group.claim_value == SignInGroup.EVERYONE].append(value)
    chosen = values[False] or values[True]
    return max(chosen, key=RANK.get) if chosen else None


def default_mode(user, key):
    """The mode without a choice of the user"""
    return group_default(user, key) or BY_KEY[key].default


def mode(user, key):
    value = profile_for(user).notifications.get(key)
    return value if value in BY_KEY[key].modes else default_mode(user, key)


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


def message(user, subject, template, context):
    """One mail, in the language of the user. subject: (lazy text, its values)."""
    language = profile_for(user).language or settings.LANGUAGE_CODE
    with translation.override(language):
        body = render_to_string(f"email/{template}.txt", {
            **context, "user": user, "server_name": settings.SANTA_SERVER_NAME,
            "profile_url": absolute_url("profile"),
        })
        text, values = subject
        subject = f"[{settings.SANTA_SERVER_NAME}] {str(text) % values}"
    return EmailMessage(subject, body, to=[user.email])


def send_messages(messages, background=None):
    """Send over one connection: an unreachable server costs one timeout, not one per mail.

    Each mail on its own: an address the server refuses (e.g. 550, unknown or not allowed) is logged, the others still
    get theirs. In the background (EMAIL_SEND_IN_BACKGROUND), the request doesn't wait for the mail server; the thread
    doesn't touch the database. A failure is logged, it never breaks the change that caused it.
    """
    if not messages:
        return

    def run():
        try:
            connection = get_connection()
            connection.open()
        except Exception:
            logger.exception("Mail server not reachable, %s e-mails not sent: %s", len(messages), messages[0].subject)
            return
        try:
            for index, message in enumerate(messages):
                try:
                    connection.send_messages([message])
                except smtplib.SMTPRecipientsRefused as e:
                    logger.warning("E-mail to %s refused: %s", ", ".join(message.to), e.recipients)
                except smtplib.SMTPServerDisconnected:
                    logger.exception("Mail server closed the connection, %s e-mails not sent: %s",
                                     len(messages) - index, message.subject)
                    break
                except Exception:
                    logger.exception("E-mail to %s failed: %s", ", ".join(message.to), message.subject)
        finally:
            # the mails are out: an error while saying goodbye to the server is logged, never raised to the caller
            try:
                connection.close()
            except Exception:
                logger.exception("Closing the connection to the mail server failed")

    if settings.EMAIL_SEND_IN_BACKGROUND if background is None else background:
        threading.Thread(target=run, name="santa-mail", daemon=True).start()
    else:
        run()


def notify(key, users, subject, template, context, exclude=None):
    """Send right after the transaction to the users who chose "immediately".

    users: a list, or a function that returns it (evaluated after the commit); subject: (lazy text, its values).
    """
    if not enabled():
        return

    def deliver():
        # the recipients and the texts here, with the database; only the sending in the background
        send_messages([message(user, subject, template, context)
                       for user in (users() if callable(users) else users)
                       if user != exclude and user.email and BY_KEY[key] in available(user)
                       and mode(user, key) == INSTANT])

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
           (_("Package rule %(name)s has errors"), {"name": source.name}), "source_failed",
           {"source": source, "url": absolute_url("console:source", source.pk)})
