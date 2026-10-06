from unittest.mock import patch

from django.contrib.auth.models import Permission, User
from django.core import mail
from django.core.management import call_command
from django.test import override_settings
from django.urls import reverse

from santa import notifications
from santa.models import AccessRequest, ReleaseSource, ReleaseVersion
from santa.releases import ReleaseError, sync_release_source
from santa.users import profile_for

from .test_console import ConsoleBase


def recipients():
    return sorted(address for message in mail.outbox for address in message.to)


class NotificationsTestCase(ConsoleBase):
    def setUp(self):
        super().setUp()
        self.approver = User.objects.create_user("approver", "approver@example.com", is_staff=True)
        self.approver.user_permissions.set(Permission.objects.filter(
            codename__in=["view_accessrequest", "change_accessrequest", "view_releasesource"]))
        self.source = ReleaseSource.objects.create(name="Colima", kind=ReleaseSource.Kind.HOMEBREW_FORMULA,
                                                   identifier="colima", auto_approve=False)

    def choose(self, user, **modes):
        profile = profile_for(user)
        profile.notifications = {**profile.notifications, **modes}
        profile.save()

    def test_new_request_goes_to_the_approvers(self):
        self.client.force_login(self.user)
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse("requests:new"), {"kind": "OTHER", "title": "Figma", "justification": "Design"})
        self.assertEqual(recipients(), ["admin@example.com", "approver@example.com"])
        message = mail.outbox[0]
        self.assertEqual(message.subject, "[Santa Server] New request: Figma")
        self.assertIn("Design", message.body)
        access_request = AccessRequest.objects.get()
        self.assertIn(f"http://localhost:8000/console/requests/{access_request.pk}/", message.body)
        self.assertIn("http://localhost:8000/profile/", message.body)

    def test_choices_off_and_daily_are_not_sent_right_away(self):
        self.choose(self.admin, new_requests=notifications.OFF)
        self.choose(self.approver, new_requests=notifications.DAILY)
        access_request = AccessRequest.objects.create(requester=self.user, kind="OTHER", title="Figma",
                                                      justification="x")
        with self.captureOnCommitCallbacks(execute=True):
            notifications.request_created(access_request)
        self.assertEqual(mail.outbox, [])

    def test_decision_goes_to_the_requester_in_their_language(self):
        self.choose(self.user)
        profile = profile_for(self.user)
        profile.language = "de"
        profile.save()
        access_request = AccessRequest.objects.create(requester=self.user, kind="OTHER", title="Figma",
                                                      justification="x")
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse("console:request_deny", args=(access_request.pk,)), {"note": "Use Penpot"})
        self.assertEqual(recipients(), ["jdoe@example.com"])
        self.assertEqual(mail.outbox[0].subject, "[Santa Server] Nicht genehmigt: Figma")
        self.assertIn("Use Penpot", mail.outbox[0].body)

    def test_package_versions_by_auto_approve(self):
        self.approver.user_permissions.add(Permission.objects.get(codename="change_rule"))
        version = ReleaseVersion.objects.create(source=self.source, identifier="colima", version="0.9.1",
                                                binary_count=2)
        with self.captureOnCommitCallbacks(execute=True):
            notifications.versions_found(self.source, [version])
        self.assertEqual(recipients(), ["admin@example.com", "approver@example.com"])
        self.assertEqual(mail.outbox[0].subject, "[Santa Server] New version of Colima to approve")
        self.assertIn("colima 0.9.1 (2 binaries)", mail.outbox[0].body)
        # approved automatically: off by default
        mail.outbox.clear()
        self.source.auto_approve = True
        with self.captureOnCommitCallbacks(execute=True):
            notifications.versions_found(self.source, [version])
        self.assertEqual(mail.outbox, [])

    def test_failing_source_mails_once(self):
        def fail(session, source, identifier):
            raise ReleaseError("formulae.brew.sh is down")

        with patch("santa.releases._sync_identifier", side_effect=fail):
            for _ in range(2):
                with self.captureOnCommitCallbacks(execute=True), self.assertRaises(ReleaseError):
                    sync_release_source(self.source, session=object())
        self.assertEqual(recipients(), ["admin@example.com", "approver@example.com"])
        self.assertIn("formulae.brew.sh is down", mail.outbox[0].body)

    def test_users_without_the_permission_or_address_get_nothing(self):
        self.assertEqual([n.key for n in notifications.available(self.user)], ["my_requests"])
        self.approver.email = ""
        self.approver.save()
        self.assertEqual(notifications.users_for("new_requests"), [self.admin])

    @override_settings(EMAIL_HOST="")
    def test_off_without_email_host(self):
        access_request = AccessRequest.objects.create(requester=self.user, kind="OTHER", title="Figma",
                                                      justification="x")
        with self.captureOnCommitCallbacks(execute=True):
            notifications.request_created(access_request)
        self.assertEqual(mail.outbox, [])
        call_command("send_notification_digest", stdout=open("/dev/null", "w"))
        self.assertEqual(mail.outbox, [])

    def test_daily_summary(self):
        self.choose(self.approver, new_requests=notifications.DAILY, package_errors=notifications.DAILY)
        AccessRequest.objects.create(requester=self.user, kind="OTHER", title="Figma", justification="x")
        self.source.last_error = "formulae.brew.sh is down"
        self.source.save()
        call_command("send_notification_digest", stdout=open("/dev/null", "w"))
        self.assertEqual(recipients(), ["approver@example.com"])
        body = mail.outbox[0].body
        self.assertEqual(mail.outbox[0].subject, "[Santa Server] Daily summary")
        self.assertIn("Figma (Other, jdoe@example.com)", body)
        self.assertIn("1 request is open.", body)
        self.assertIn("Colima: formulae.brew.sh is down", body)
        # nothing new: no mail
        mail.outbox.clear()
        AccessRequest.objects.all().delete()
        self.source.last_error = ""
        self.source.save()
        call_command("send_notification_digest", stdout=open("/dev/null", "w"))
        self.assertEqual(mail.outbox, [])

    def test_daily_summary_by_the_version_and_without_own_requests(self):
        self.choose(self.approver, new_requests=notifications.DAILY, package_pending=notifications.DAILY,
                    package_auto=notifications.DAILY)
        self.approver.user_permissions.add(Permission.objects.get(codename="change_rule"))
        ReleaseVersion.objects.create(source=self.source, identifier="colima", version="0.9.1")
        # switched on after the version was found: it still waits for an approval
        self.source.auto_approve = True
        self.source.save()
        AccessRequest.objects.create(requester=self.approver, kind="OTHER", title="Own request", justification="x")
        call_command("send_notification_digest", stdout=open("/dev/null", "w"))
        body = mail.outbox[0].body
        self.assertIn("Package versions to approve\n- Colima: colima 0.9.1", body)
        self.assertNotIn("Approved package versions", body)
        self.assertNotIn("Own request", body)
        # only their own request: nothing to send
        mail.outbox.clear()
        ReleaseVersion.objects.all().delete()
        call_command("send_notification_digest", stdout=open("/dev/null", "w"))
        self.assertEqual(mail.outbox, [])

    def test_one_connection_in_the_background(self):
        access_request = AccessRequest.objects.create(requester=self.user, kind="OTHER", title="Figma",
                                                      justification="x")
        with patch("santa.notifications.get_connection") as get_connection, \
                patch("santa.notifications.threading.Thread") as thread:
            with self.settings(EMAIL_SEND_IN_BACKGROUND=True), self.captureOnCommitCallbacks(execute=True):
                notifications.request_created(access_request)
            # the request doesn't send: a thread does
            get_connection.assert_not_called()
            thread.call_args.kwargs["target"]()
        connection = get_connection.return_value.__enter__.return_value
        [(messages,), _] = connection.send_messages.call_args
        self.assertEqual(sorted(message.to[0] for message in messages), ["admin@example.com", "approver@example.com"])
        self.assertEqual(connection.send_messages.call_count, 1)

    def test_a_failing_mail_server_is_only_logged(self):
        access_request = AccessRequest.objects.create(requester=self.user, kind="OTHER", title="Figma",
                                                      justification="x")
        with patch("santa.notifications.get_connection", side_effect=OSError("connection refused")), \
                self.assertLogs("santa.notifications", "ERROR"), self.captureOnCommitCallbacks(execute=True):
            notifications.request_created(access_request)

    def test_profile_choices(self):
        self.client.force_login(self.approver)
        response = self.client.get(reverse("profile"))
        self.assertContains(response, 'name="notify_new_requests"')
        self.assertNotContains(response, 'name="notify_package_pending"')
        self.client.post(reverse("profile"), {"theme": "auto", "notify_my_requests": "off",
                                              "notify_new_requests": "daily", "notify_package_auto": "instant",
                                              "notify_package_errors": "off"})
        self.assertEqual(notifications.mode(self.approver, "new_requests"), notifications.DAILY)
        self.assertEqual(notifications.mode(self.approver, "package_auto"), notifications.INSTANT)
        # a requester only has their own requests
        self.client.force_login(self.user)
        response = self.client.get(reverse("profile"))
        self.assertContains(response, 'name="notify_my_requests"')
        self.assertNotContains(response, 'name="notify_new_requests"')
