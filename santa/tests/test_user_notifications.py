import json
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.management import call_command
from django.urls import reverse
from django.utils import timezone

from santa.middleware import add_trigger
from santa.models import ReleaseSource, UserNotification
from santa.releases import ReleaseError

from .test_console import ConsoleBase


class UserNotificationTestCase(ConsoleBase):
    def setUp(self):
        super().setUp()
        self.source = ReleaseSource.objects.create(name="colima", kind=ReleaseSource.Kind.GITHUB_RELEASE,
                                                   identifier="abiosoft/colima", is_global=True)

    def check(self, **headers):
        with patch("santa.console.views_sources.sync_release_source", side_effect=ReleaseError("down")):
            return self.client.post(reverse("console:source_check", args=(self.source.pk,)), **headers)

    def test_the_messages_are_kept_for_the_bell(self):
        self.check()
        [notification] = UserNotification.objects.filter(user=self.admin)
        self.assertEqual((notification.level, notification.text), ("error", "colima: down"))
        response = self.client.get(reverse("console:sources"))
        self.assertContains(response, 'data-unread-count aria-hidden="true">1</span>')
        self.assertContains(response, 'aria-label="Notifications, 1 unread"')
        # shown once on the page, for the toast
        self.assertContains(response, "data-toasts")

    def test_in_the_language_of_the_request(self):
        with patch("santa.console.views_sources.sync_release_source", return_value=[]):
            self.client.post(reverse("console:source_check", args=(self.source.pk,)), HTTP_ACCEPT_LANGUAGE="de")
        self.assertEqual(UserNotification.objects.get().text, "colima: aktuell.")

    def test_the_panel_marks_them_as_read(self):
        self.check()
        response = self.client.get(reverse("notifications_panel"))
        self.assertContains(response, "colima: down")
        self.assertFalse(UserNotification.objects.filter(read_at=None).exists())
        self.assertEqual(self.client.get(reverse("notifications_unread")).json(), {"unread": 0})

    def test_htmx_requests_tell_the_page(self):
        response = self.check(HTTP_HX_REQUEST="true")
        self.assertIn("notificationsChanged", response["HX-Trigger"])
        self.assertEqual(add_trigger("drawerSaved", "notificationsChanged"), "drawerSaved, notificationsChanged")
        self.assertEqual(json.loads(add_trigger('{"drawerSaved": {"open": "/x/"}}', "notificationsChanged")),
                         {"drawerSaved": {"open": "/x/"}, "notificationsChanged": None})
        self.assertEqual(add_trigger("", "notificationsChanged"), "notificationsChanged")

    def test_only_the_own_ones(self):
        other = User.objects.create_user("other", is_staff=True)
        foreign = UserNotification.objects.create(user=other, text="not yours")
        own = UserNotification.objects.create(user=self.admin, text="yours", level="success")
        self.assertEqual(self.client.post(reverse("notification_dismiss", args=(foreign.pk,))).status_code, 404)
        response = self.client.get(reverse("notifications"))
        self.assertContains(response, "yours")
        self.assertNotContains(response, "not yours")
        response = self.client.post(reverse("notification_dismiss", args=(own.pk,)), HTTP_HX_REQUEST="true")
        self.assertEqual(response.content, b"")
        self.assertFalse(UserNotification.objects.filter(pk=own.pk).exists())
        self.client.post(reverse("notifications_clear"))
        self.assertEqual(list(UserNotification.objects.values_list("text", flat=True)), ["not yours"])

    def test_the_last_one_removed_shows_the_empty_state(self):
        first = UserNotification.objects.create(user=self.admin, text="first")
        last = UserNotification.objects.create(user=self.admin, text="last")
        response = self.client.post(reverse("notification_dismiss", args=(first.pk,)),
                                    {"in_panel": "1"}, HTTP_HX_REQUEST="true")
        self.assertEqual(response["HX-Retarget"], "[data-notifications-panel]")
        self.assertContains(response, "last")
        response = self.client.post(reverse("notification_dismiss", args=(last.pk,)),
                                    {"in_panel": "1"}, HTTP_HX_REQUEST="true")
        self.assertContains(response, "No notifications.")
        # the full view reloads for its empty state
        other = UserNotification.objects.create(user=self.admin, text="other")
        response = self.client.post(reverse("notification_dismiss", args=(other.pk,)), HTTP_HX_REQUEST="true")
        self.assertEqual(response["HX-Refresh"], "true")

    def test_the_full_view_filters_by_level(self):
        UserNotification.objects.create(user=self.admin, text="it worked", level="success")
        UserNotification.objects.create(user=self.admin, text="it failed", level="error")
        response = self.client.get(reverse("notifications"), {"level": "error"})
        self.assertContains(response, "it failed")
        self.assertNotContains(response, "it worked")
        self.assertFalse(UserNotification.objects.filter(read_at=None).exists())
        self.assertContains(response, '<input type="hidden" name="level" value="error">', html=True)
        # the last one of the level removed: the empty state of the level, also with others left
        failed = UserNotification.objects.get(level="error")
        response = self.client.post(reverse("notification_dismiss", args=(failed.pk,)), {"level": "error"},
                                    HTTP_HX_REQUEST="true")
        self.assertEqual(response["HX-Refresh"], "true")

    def test_requesters_have_the_bell_too(self):
        self.client.force_login(self.user)
        UserNotification.objects.create(user=self.user, text="Request sent")
        self.assertContains(self.client.get(reverse("notifications_panel")), "Request sent")

    def test_anonymous_requests_keep_nothing(self):
        self.client.logout()
        self.client.post(reverse("login"), {"username": "admin", "password": "wrong"})
        self.assertFalse(UserNotification.objects.exists())

    def test_the_old_ones_are_deleted(self):
        old = UserNotification.objects.create(user=self.admin, text="old")
        UserNotification.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(days=31))
        UserNotification.objects.create(user=self.admin, text="new")
        call_command("cleanup_events", stdout=open("/dev/null", "w"))
        self.assertEqual(list(UserNotification.objects.values_list("text", flat=True)), ["new"])
