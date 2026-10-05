import hashlib
from datetime import datetime, timezone

from django.contrib.admin.helpers import ACTION_CHECKBOX_NAME
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from santa.forms import AllowEventsForm, UploadBinaryForm
from santa.models import Event, FileAccessRule, Group, Machine, Policy, Rule, RuleType, SavedFilter, Tag

from .utils import build_macho


class AdminTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_superuser("admin", "admin@example.com", "pw")
        cls.dev = Group.objects.create(name="Development")
        cls.sales = Group.objects.create(name="Sales")
        cls.machine = Machine.objects.create(machine_id="M1", serial_number="C02TEST", group=cls.dev)

    def setUp(self):
        self.client.force_login(self.user)

    def make_event(self, sha256, group=None, **kwargs):
        return Event.objects.create(machine=self.machine, group=group or self.dev, decision="BLOCK_UNKNOWN",
                                    execution_time=datetime.now(tz=timezone.utc), file_sha256=sha256,
                                    file_name="colima", file_path="/opt/homebrew/bin/colima", **kwargs)

    def test_admin_pages(self):
        self.make_event("a" * 64, executing_user="jdoe")
        tag = Tag.objects.create(name="homebrew")
        rule = Rule.objects.create(rule_type=RuleType.BINARY, identifier="a" * 64)
        rule.groups.add(self.dev)
        rule.tags.add(tag)
        SavedFilter.objects.create(user=User.objects.first(), page="rules", name="Global", query="scope=global")
        file_access = FileAccessRule.objects.create(name="SSH keys", paths="/Users/*/.ssh/id_rsa")
        file_access.processes.create(signing_id="com.openssh.ssh", platform_binary=True)
        for name in ("group", "machine", "rule", "event", "releasesource", "tag", "accessrequest", "signingroup",
                     "savedfilter", "fileaccessrule"):
            for params in ({}, {"_facets": "True"}):
                response = self.client.get(reverse(f"admin:santa_{name}_changelist"), params)
                self.assertEqual(response.status_code, 200, (name, params))
        response = self.client.get(reverse("admin:santa_group_change", args=(self.dev.pk,)))
        self.assertContains(response, self.dev.sync_base_url)
        self.assertEqual(self.client.get(reverse("admin:santa_rule_upload")).status_code, 200)

    def test_allow_events_for_group(self):
        event = self.make_event("a" * 64)
        other = self.make_event("a" * 64, group=self.sales)
        url = reverse("admin:santa_event_changelist")
        response = self.client.post(url, {"action": "allow_events", ACTION_CHECKBOX_NAME: [event.pk]})
        self.assertContains(response, "Create the rules")
        response = self.client.post(url, {"action": "allow_events", ACTION_CHECKBOX_NAME: [event.pk], "apply": "1",
                                          "rule_type": RuleType.BINARY, "policy": Policy.ALLOWLIST,
                                          "scope": "groups", "groups": [self.dev.pk]})
        self.assertEqual(response.status_code, 302)
        rule = Rule.objects.get()
        self.assertEqual(rule.identifier, "a" * 64)
        self.assertEqual(list(rule.groups.all()), [self.dev])
        self.assertEqual(rule.created_by, self.user)
        event.refresh_from_db()
        other.refresh_from_db()
        self.assertEqual(event.resolution_rule, rule)
        self.assertIsNone(other.resolved_at)

        # allowing it for Sales too widens the existing rule
        self.client.post(url, {"action": "allow_events", ACTION_CHECKBOX_NAME: [other.pk], "apply": "1",
                               "rule_type": RuleType.BINARY, "policy": Policy.ALLOWLIST,
                               "scope": "groups", "groups": [self.sales.pk]})
        self.assertEqual(Rule.objects.count(), 1)
        self.assertEqual(set(rule.groups.all()), {self.dev, self.sales})

    def test_allow_events_by_signing_id_skips_unsigned(self):
        signed = self.make_event("a" * 64, signing_id="ABCDE12345:com.example.tool")
        unsigned = self.make_event("b" * 64)
        response = self.client.post(reverse("admin:santa_event_changelist"), {
            "action": "allow_events", ACTION_CHECKBOX_NAME: [signed.pk, unsigned.pk], "apply": "1",
            "rule_type": RuleType.SIGNINGID, "policy": Policy.ALLOWLIST, "scope": "global"}, follow=True)
        self.assertContains(response, "1 event(s) skipped")
        rule = Rule.objects.get()
        self.assertEqual(rule.rule_type, RuleType.SIGNINGID)
        self.assertTrue(rule.is_global)

    def test_upload_binary(self):
        binary = build_macho()
        response = self.client.post(reverse("admin:santa_rule_upload"), {
            "file": SimpleUploadedFile("tool", binary), "rule_type": RuleType.BINARY, "policy": Policy.ALLOWLIST,
            "scope": "groups", "groups": [self.dev.pk]})
        self.assertEqual(response.status_code, 302)
        rule = Rule.objects.get()
        self.assertEqual(rule.identifier, hashlib.sha256(binary).hexdigest())

    def test_upload_signing_id(self):
        response = self.client.post(reverse("admin:santa_rule_upload"), {
            "file": SimpleUploadedFile("tool", build_macho()), "rule_type": RuleType.SIGNINGID,
            "policy": Policy.ALLOWLIST, "scope": "global"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Rule.objects.get().identifier, "ABCDE12345:com.example.tool")

    def test_upload_unsigned_team_id_fails(self):
        response = self.client.post(reverse("admin:santa_rule_upload"), {
            "file": SimpleUploadedFile("tool", build_macho(signed=False)), "rule_type": RuleType.TEAMID,
            "policy": Policy.ALLOWLIST, "scope": "global"})
        self.assertContains(response, "The file has no TEAMID")
        self.assertFalse(Rule.objects.exists())

    def test_group_validation(self):
        group = Group(name="Design", allowed_path_regex="^/ok/\n^/broken/(", removable_media_action="REMOUNT",
                      removable_media_remount_flags="rdonly,fast", encrypted_removable_media_remount_flags="rdonly",
                      branding_company_logo="https://example.com/logo.png")
        with self.assertRaises(ValidationError) as cm:
            group.full_clean()
        errors = cm.exception.message_dict
        self.assertIn("Line 2 (^/broken/()", errors["allowed_path_regex"][0])
        self.assertIn("Unknown flag(s): fast", errors["removable_media_remount_flags"][0])
        self.assertIn("Only used with “Remount with flags”", errors["encrypted_removable_media_remount_flags"][0])
        self.assertIn("file:///", errors["branding_company_logo"][0])
        group = Group(name="Design", removable_media_action="REMOUNT", branding_company_logo="file:///Library/l.png")
        with self.assertRaises(ValidationError) as cm:
            group.full_clean()
        self.assertEqual(list(cm.exception.message_dict), ["removable_media_remount_flags"])

    def test_group_form_shows_descriptions(self):
        response = self.client.get(reverse("admin:santa_group_change", args=(self.dev.pk,)))
        self.assertContains(response, "transitive rules")
        self.assertContains(response, "One regular expression per line")

    def test_signing_id_is_the_default_rule_type(self):
        self.assertEqual(Rule().rule_type, RuleType.SIGNINGID)
        self.assertEqual(AllowEventsForm().fields["rule_type"].initial, RuleType.SIGNINGID)
        self.assertEqual(UploadBinaryForm().fields["rule_type"].initial, RuleType.SIGNINGID)
        response = self.client.get(reverse("admin:santa_rule_add"))
        self.assertContains(response, '<option value="SIGNINGID" selected>')

    def test_manual_rule_validation(self):
        url = reverse("admin:santa_rule_add")
        data = {"rule_type": RuleType.TEAMID, "identifier": "abcde12345", "policy": Policy.ALLOWLIST,
                "is_enabled": "on", "groups": [self.dev.pk]}
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Rule.objects.get().identifier, "ABCDE12345")
        response = self.client.post(url, {**data, "identifier": "not-a-team-id"})
        self.assertContains(response, "10 character Team ID")
        response = self.client.post(url, {**data, "identifier": "ZYXWV98765", "groups": []})
        self.assertContains(response, "Choose a scope")
