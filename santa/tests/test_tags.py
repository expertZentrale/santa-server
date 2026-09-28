import json
import uuid
from datetime import datetime, timezone

from django.contrib.admin.helpers import ACTION_CHECKBOX_NAME
from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from santa.config_io import export_config, import_config
from santa.models import Event, Group, Machine, Policy, Rule, RuleType, Tag


class TagTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_superuser("admin", "admin@example.com", "pw")
        cls.dev = Group.objects.create(name="Development")
        cls.homebrew = Tag.objects.create(name="homebrew", description="Installed with brew")
        cls.browser = Tag.objects.create(name="browser")
        cls.colima = Rule.objects.create(rule_type=RuleType.BINARY, identifier="a" * 64, description="colima")
        cls.colima.groups.add(cls.dev)
        cls.colima.tags.add(cls.homebrew)
        cls.chrome = Rule.objects.create(rule_type=RuleType.SIGNINGID, identifier="EQHXZ8M8AV:com.google.Chrome",
                                         is_global=True)
        cls.chrome.tags.add(cls.browser, cls.homebrew)

    def setUp(self):
        self.client.force_login(self.user)

    def changelist(self, **params):
        response = self.client.get(reverse("admin:santa_rule_changelist"), params)
        self.assertEqual(response.status_code, 200)
        return list(response.context["cl"].result_list)

    def test_filter_and_search(self):
        self.assertEqual(self.changelist(tags__id__exact=self.browser.pk), [self.chrome])
        self.assertEqual(sorted(r.pk for r in self.changelist(tags__id__exact=self.homebrew.pk)),
                         sorted([self.colima.pk, self.chrome.pk]))
        # a rule with two matching tags is listed once
        self.assertEqual(sorted(r.pk for r in self.changelist(q="o")), sorted([self.colima.pk, self.chrome.pk]))
        self.assertEqual(self.changelist(q="browser"), [self.chrome])

    def test_tag_admin(self):
        response = self.client.get(reverse("admin:santa_tag_changelist"))
        self.assertContains(response, "2 rule(s)")
        self.assertContains(response, f"?tags__id__exact={self.homebrew.pk}")

    def test_add_new_tag_and_remove(self):
        url = reverse("admin:santa_rule_changelist")
        selected = {ACTION_CHECKBOX_NAME: [self.colima.pk, self.chrome.pk]}
        response = self.client.post(url, {"action": "add_tag", **selected})
        self.assertContains(response, "new_tag")
        self.client.post(url, {"action": "add_tag", "apply": "1", "new_tag": " approved 2026 ", **selected})
        tag = Tag.objects.get(name="approved 2026")
        self.assertEqual(tag.rules.count(), 2)
        self.client.post(url, {"action": "add_tag", "apply": "1", "tag": self.browser.pk, **selected})
        self.assertEqual(self.browser.rules.count(), 2)
        response = self.client.post(url, {"action": "remove_tag", **selected})
        self.assertNotContains(response, "new_tag")
        self.client.post(url, {"action": "remove_tag", "apply": "1", "tag": self.homebrew.pk, **selected})
        self.assertEqual(self.homebrew.rules.count(), 0)

    def test_tag_on_allow(self):
        machine = Machine.objects.create(machine_id="M1", serial_number="C02TEST", group=self.dev)
        event = Event.objects.create(machine=machine, group=self.dev, decision="BLOCK_UNKNOWN",
                                     execution_time=datetime.now(tz=timezone.utc), file_sha256="b" * 64)
        self.client.post(reverse("admin:santa_event_changelist"), {
            "action": "allow_events", ACTION_CHECKBOX_NAME: [event.pk], "apply": "1", "rule_type": RuleType.BINARY,
            "policy": Policy.ALLOWLIST, "scope": "groups", "groups": [self.dev.pk], "tags": [self.homebrew.pk]})
        self.assertEqual(list(Rule.objects.get(identifier="b" * 64).tags.all()), [self.homebrew])

    def test_tags_have_no_effect_on_the_macs(self):
        self.assertNotIn("tags", json.dumps(self.chrome.to_santa()))
        machine_id = str(uuid.uuid4()).upper()
        base = f"/sync/{self.dev.sync_token}"
        self.client.post(f"{base}/preflight/{machine_id}", {"serial_num": "C02SYNC", "santa_version": "2025.8"},
                         content_type="application/json")
        rules = self.client.post(f"{base}/ruledownload/{machine_id}", {}, content_type="application/json").json()
        self.assertEqual(len(rules["rules"]), 2)
        for rule in rules["rules"]:
            self.assertEqual(set(rule), {"identifier", "rule_type", "policy"})

    def test_export_import(self):
        data = export_config()
        self.assertEqual(data["tags"], [{"name": "browser", "description": ""},
                                        {"name": "homebrew", "description": "Installed with brew"}])
        chrome = next(r for r in data["rules"] if r["rule_type"] == "SIGNINGID")
        self.assertEqual(chrome["tags"], ["browser", "homebrew"])
        chrome["tags"] = ["browser", "google"]
        report = import_config(data)
        self.assertIn(("created", "tag", "google"), report["changes"])
        self.assertEqual(sorted(t.name for t in self.chrome.tags.all()), ["browser", "google"])
        # a file without tags keeps them
        for rule in data["rules"]:
            del rule["tags"]
        import_config(data)
        self.assertEqual(sorted(t.name for t in self.chrome.tags.all()), ["browser", "google"])
