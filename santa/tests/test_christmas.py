from datetime import datetime
from unittest.mock import patch

from django.core import mail
from django.test import SimpleTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from santa import notifications
from santa.models import AccessRequest
from santa.seasonal import christmas_active

from .test_console import ConsoleBase

DECEMBER = timezone.make_aware(datetime(2026, 12, 3, 12))
NOVEMBER = timezone.make_aware(datetime(2026, 11, 28, 12))


class ChristmasActiveTestCase(SimpleTestCase):
    def test_off_by_default(self):
        self.assertFalse(christmas_active(DECEMBER))

    @override_settings(SANTA_CHRISTMAS_THEME=True)
    def test_on_in_december_only(self):
        self.assertTrue(christmas_active(DECEMBER))
        self.assertFalse(christmas_active(NOVEMBER))

    @override_settings(SANTA_CHRISTMAS_THEME_FORCE=True)
    def test_forced_all_year(self):
        self.assertTrue(christmas_active(NOVEMBER))


@override_settings(SANTA_CHRISTMAS_THEME_FORCE=True, EMAIL_HOST="smtp.example.com", EMAIL_SEND_IN_BACKGROUND=False)
class ChristmasThemeTestCase(ConsoleBase):
    def test_console_and_request_form(self):
        response = self.client.get(reverse("console:rules"))
        self.assertContains(response, 'data-season="christmas"')
        self.assertContains(response, "christmas.css")
        self.assertContains(response, 'data-snow="off"')
        self.assertContains(response, 'data-lights="off"')
        self.client.force_login(self.user)
        response = self.client.get(reverse("requests:new"), {"kind": "OTHER"})
        self.assertContains(response, "Send to the North Pole")

    def test_request_sent_message(self):
        self.client.force_login(self.user)
        response = self.client.post(reverse("requests:new"), {"kind": "OTHER", "title": "Figma",
                                                              "justification": "design"}, follow=True)
        self.assertContains(response, "Your wish is on its way to the North Pole")

    def test_e_mails(self):
        self.user.email = "jdoe@example.com"
        self.user.save()
        access_request = AccessRequest.objects.create(requester=self.user, kind="OTHER", title="Figma",
                                                      justification="design", status=AccessRequest.Status.APPROVED)
        with self.captureOnCommitCallbacks(execute=True):
            notifications.request_decided(access_request)
        [message] = mail.outbox
        self.assertIn("🎁", message.subject)
        self.assertTrue(message.body.startswith("Ho ho ho!"))
        self.assertIn("🎁 An early present from your IT team.", message.body)
        self.assertIn("Merry Christmas and happy holidays!", message.body)
        # denied: a lump of coal
        access_request.status = AccessRequest.Status.DENIED
        access_request.save()
        with self.captureOnCommitCallbacks(execute=True):
            notifications.request_decided(access_request)
        message = mail.outbox[1]
        self.assertIn("🪨", message.subject)
        self.assertIn("🪨 This time only a lump of coal.", message.body)
        self.assertNotIn("🎁", message.body)


class NoChristmasTestCase(ConsoleBase):
    @patch("santa.seasonal.timezone.localdate", return_value=DECEMBER.date())
    def test_nothing_without_the_setting(self, _localdate):
        response = self.client.get(reverse("console:rules"))
        self.assertNotContains(response, "data-season")
        self.assertNotContains(response, "christmas.css")
