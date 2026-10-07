import smtplib
from importlib import import_module
from unittest.mock import patch

from django.contrib.admin.models import LogEntry
from django.contrib.auth.models import Permission, User
from django.core import mail
from django.core.management import call_command
from django.test import override_settings
from django.urls import reverse

from santa import notifications
from santa.models import AccessRequest, ReleaseSource, ReleaseVersion, SignInGroup, UserProfile
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

    @override_settings(EMAIL_NOTIFICATIONS_ENABLED=False)
    def test_switched_off_globally(self):
        self.assertFalse(notifications.enabled())
        access_request = AccessRequest.objects.create(requester=self.user, kind="OTHER", title="Figma",
                                                      justification="x")
        with self.captureOnCommitCallbacks(execute=True):
            notifications.request_created(access_request)
        self.choose(self.approver, new_requests=notifications.DAILY)
        call_command("send_notification_digest", stdout=open("/dev/null", "w"))
        self.assertEqual(mail.outbox, [])

    def test_profile_hides_the_choices_when_off(self):
        self.client.force_login(self.approver)
        for off in ({"EMAIL_HOST": ""}, {"EMAIL_NOTIFICATIONS_ENABLED": False}):
            with self.settings(**off):
                response = self.client.get(reverse("profile"))
                self.assertNotContains(response, "E-mail notifications")
                self.assertNotContains(response, 'name="notify_')
                # saving the profile keeps the choices for when they are on again
                self.choose(self.approver, new_requests=notifications.DAILY)
                self.client.post(reverse("profile"), {"theme": "dark"})
                self.assertEqual(notifications.mode(self.approver, "new_requests"), notifications.DAILY)
        self.assertContains(self.client.get(reverse("profile")), 'name="notify_new_requests"')

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
        # one connection, opened and closed once, each mail on its own
        get_connection.assert_called_once()
        connection = get_connection.return_value
        self.assertEqual((connection.open.call_count, connection.close.call_count), (1, 1))
        sent = [message.to[0] for (messages,), _ in connection.send_messages.call_args_list for message in messages]
        self.assertEqual(sorted(sent), ["admin@example.com", "approver@example.com"])
        self.assertEqual(connection.send_messages.call_count, 2)

    def test_a_refused_address_doesnt_stop_the_others(self):
        access_request = AccessRequest.objects.create(requester=self.user, kind="OTHER", title="Figma",
                                                      justification="x")
        delivered = []

        def send(messages):
            [message] = messages
            if message.to == ["admin@example.com"]:
                raise smtplib.SMTPRecipientsRefused({"admin@example.com": (550, b"5.4.1 Recipient address rejected")})
            delivered.append(message.to[0])
            return 1

        with patch("santa.notifications.get_connection") as get_connection, \
                self.assertLogs("santa.notifications", "WARNING") as logs, self.captureOnCommitCallbacks(execute=True):
            get_connection.return_value.send_messages.side_effect = send
            notifications.request_created(access_request)
        self.assertEqual(delivered, ["approver@example.com"])
        self.assertIn("E-mail to admin@example.com refused", logs.output[0])

    def test_an_error_when_closing_is_only_logged(self):
        access_request = AccessRequest.objects.create(requester=self.user, kind="OTHER", title="Figma",
                                                      justification="x")
        with patch("santa.notifications.get_connection") as get_connection, \
                self.assertLogs("santa.notifications", "ERROR") as logs, self.captureOnCommitCallbacks(execute=True):
            get_connection.return_value.close.side_effect = smtplib.SMTPServerDisconnected("gone")
            notifications.request_created(access_request)
        self.assertEqual(get_connection.return_value.send_messages.call_count, 2)
        self.assertIn("Closing the connection to the mail server failed", logs.output[0])
        # the daily summary (sent in the command, not in a thread) ends normally too
        self.choose(self.approver, new_requests=notifications.DAILY)
        with patch("santa.notifications.get_connection") as get_connection, self.assertLogs("santa.notifications"):
            get_connection.return_value.close.side_effect = OSError("broken pipe")
            call_command("send_notification_digest", stdout=open("/dev/null", "w"))
        self.assertEqual(get_connection.return_value.send_messages.call_count, 1)

    def test_a_failing_mail_server_is_only_logged(self):
        access_request = AccessRequest.objects.create(requester=self.user, kind="OTHER", title="Figma",
                                                      justification="x")
        with patch("santa.notifications.get_connection", side_effect=OSError("connection refused")), \
                self.assertLogs("santa.notifications", "ERROR"), self.captureOnCommitCallbacks(execute=True):
            notifications.request_created(access_request)

    def test_sign_in_group_without_e_mails_and_exceptions(self):
        admins = SignInGroup.objects.create(name="Admins without mailbox", claim_value="admins-1", no_email=True)
        admins.members.add(self.approver)
        access_request = AccessRequest.objects.create(requester=self.user, kind="OTHER", title="Figma",
                                                      justification="x")

        def recipients_of_a_new_request():
            mail.outbox.clear()
            with self.captureOnCommitCallbacks(execute=True):
                notifications.request_created(access_request)
            return recipients()

        self.assertEqual(recipients_of_a_new_request(), ["admin@example.com"])
        self.assertEqual(notifications.available(self.approver), [])
        # an exception in Administration → Users, both ways
        profile = profile_for(self.approver)
        profile.email_override = UserProfile.EmailOverride.ON
        profile.save()
        self.assertEqual(recipients_of_a_new_request(), ["admin@example.com", "approver@example.com"])
        profile.email_override = UserProfile.EmailOverride.OFF
        profile.save()
        admins.members.remove(self.approver)
        self.assertEqual(recipients_of_a_new_request(), ["admin@example.com"])
        # nor in the daily summary
        self.choose(self.approver, new_requests=notifications.DAILY)
        mail.outbox.clear()
        call_command("send_notification_digest", stdout=open("/dev/null", "w"))
        self.assertEqual(mail.outbox, [])

    def test_blocked_user_sees_a_note_in_the_profile(self):
        SignInGroup.objects.create(name="No mail", claim_value="nomail", no_email=True).members.add(self.approver)
        self.client.force_login(self.approver)
        response = self.client.get(reverse("profile"))
        self.assertContains(response, "E-mails are switched off for your account by the administrators.")
        self.assertNotContains(response, 'name="notify_')

    def test_set_in_the_administration(self):
        url = reverse("console:admin_user", args=(self.approver.pk,))
        self.client.post(url, {"is_active": "on", "email_override": "off", "email": "approver@example.com",
                               "is_staff": "on"})
        self.assertEqual(profile_for(self.approver).email_override, "off")
        self.assertIn("email_override", LogEntry.objects.filter(object_id=str(self.approver.pk)).latest("pk")
                      .get_change_message())
        self.client.post(reverse("console:admin_sign_in_group_add"), {
            "name": "Admins", "claim_value": "admins-2", "no_email": "on"})
        self.assertTrue(SignInGroup.objects.get(claim_value="admins-2").no_email)
        self.assertContains(self.client.get(reverse("console:admin_sign_in_groups")), "no e-mails")

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

    def test_defaults_of_the_sign_in_groups(self):
        def fresh_mode(key):
            return notifications.mode(User.objects.get(pk=self.approver.pk), key)

        everyone = SignInGroup.objects.get(claim_value=SignInGroup.EVERYONE)
        everyone.notification_defaults = {"new_requests": "off", "package_auto": "daily"}
        everyone.save()
        everyone.members.add(self.approver)
        self.assertEqual(fresh_mode("new_requests"), notifications.OFF)
        self.assertEqual(fresh_mode("package_auto"), notifications.DAILY)
        self.assertEqual(fresh_mode("package_errors"), notifications.INSTANT)
        # a group of its own wins over everyone, and of several the one with the most e-mails
        quiet = SignInGroup.objects.create(name="Quiet", claim_value="quiet", notification_defaults={
            "new_requests": "daily", "package_auto": "off", "my_requests": "daily"})
        quiet.members.add(self.approver)
        self.assertEqual(fresh_mode("new_requests"), notifications.DAILY)
        self.assertEqual(fresh_mode("package_auto"), notifications.OFF)
        SignInGroup.objects.create(name="Loud", claim_value="loud", notification_defaults={
            "new_requests": "instant"}).members.add(self.approver)
        self.assertEqual(fresh_mode("new_requests"), notifications.INSTANT)
        # no daily summary for my requests: the default of the notification
        self.assertEqual(fresh_mode("my_requests"), notifications.INSTANT)
        # the choice of the user wins
        self.choose(self.approver, package_auto=notifications.INSTANT)
        self.assertEqual(fresh_mode("package_auto"), notifications.INSTANT)

    def test_a_group_default_decides_who_gets_mail(self):
        SignInGroup.objects.create(name="Approvers", claim_value="approvers", notification_defaults={
            "new_requests": "off"}).members.add(self.approver)
        access_request = AccessRequest.objects.create(requester=self.user, kind="OTHER", title="Figma",
                                                      justification="x")
        with self.captureOnCommitCallbacks(execute=True):
            notifications.request_created(access_request)
        self.assertEqual(recipients(), ["admin@example.com"])

    def test_sign_in_group_form_sets_the_defaults(self):
        url = reverse("console:admin_sign_in_group_add")
        self.assertContains(self.client.get(url), 'name="notify_package_auto"')
        self.client.post(url, {"name": "Approvers", "claim_value": "approvers", "notify_new_requests": "daily",
                               "notify_package_auto": ""})
        group = SignInGroup.objects.get(claim_value="approvers")
        self.assertEqual(group.notification_defaults, {"new_requests": "daily"})
        self.assertContains(self.client.get(reverse("console:admin_sign_in_groups")), "e-mail defaults")
        self.client.post(reverse("console:admin_sign_in_group", args=(group.pk,)), {
            "name": "Approvers", "claim_value": "approvers", "notify_new_requests": ""})
        group.refresh_from_db()
        self.assertEqual(group.notification_defaults, {})

    def test_profile_default_choice(self):
        SignInGroup.objects.create(name="Approvers", claim_value="approvers", notification_defaults={
            "new_requests": "daily"}).members.add(self.approver)
        self.choose(self.approver, new_requests=notifications.OFF, package_auto=notifications.INSTANT)
        self.client.force_login(self.approver)
        self.assertContains(self.client.get(reverse("profile")), "Default (Daily summary)")
        self.client.post(reverse("profile"), {"theme": "auto", "notify_new_requests": "",
                                              "notify_package_auto": "instant"})
        profile = UserProfile.objects.get(user=self.approver)
        self.assertEqual(profile.notifications, {"package_auto": "instant"})

    def test_migration_forgets_the_untouched_defaults(self):
        from django.apps import apps
        migration = import_module("santa.migrations.0019_sign_in_group_notification_defaults")
        profile = profile_for(self.approver)
        profile.notifications = {"new_requests": "instant", "package_auto": "off", "package_errors": "daily",
                                 "my_requests": "off"}
        profile.save()
        migration.forget_default_choices(apps, None)
        profile.refresh_from_db()
        self.assertEqual(profile.notifications, {"package_errors": "daily", "my_requests": "off"})

    def test_migration_defaults_match_the_notifications(self):
        migration = import_module("santa.migrations.0019_sign_in_group_notification_defaults")
        self.assertLessEqual(migration.DEFAULTS.items(),
                             {n.key: n.default for n in notifications.NOTIFICATIONS}.items())
