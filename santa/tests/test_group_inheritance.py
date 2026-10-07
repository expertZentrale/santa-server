import json
import plistlib

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from santa.config_io import GROUP_FIELDS, export_config, import_config
from santa.models import FileAccessRule, Group, Machine, Policy, Rule, RuleType
from santa.profiles import group_profile
from santa.rules import GROUP, PARENT, effective_rule_objects

from . import test_sync
from .test_console import SHA_A, ConsoleBase
from .test_file_access import process_formset

FORM_DATA = {"client_mode": "MONITOR", "batch_size": 100, "full_sync_interval": 600,
             "removable_media_action": "ALLOW", "override_file_access_action": "NONE"}


def payload(group):
    return plistlib.loads(group_profile(group))["PayloadContent"][0]


class InheritanceModelTestCase(TestCase):
    def setUp(self):
        self.base = Group.objects.create(name="Base", client_mode="LOCKDOWN", batch_size=50,
                                         removable_media_action="REMOUNT", removable_media_remount_flags="rdonly",
                                         unknown_block_message="Ask the IT")
        self.child = Group.objects.create(name="Developers", parent=self.base)

    def test_every_exported_setting_is_inheritable(self):
        inheritable = {name for names in Group.INHERITABLE_SETTINGS.values() for name in names}
        self.assertEqual(inheritable, set(GROUP_FIELDS) - {"description", "inherit_rules", "inherit_file_access_rules",
                                                            "overridden_settings"})

    def test_effective_settings(self):
        effective = self.child.effective()
        self.assertEqual((effective.client_mode, effective.batch_size, effective.removable_media_policy()),
                         ("LOCKDOWN", 50, {"remount": {"flags": ["rdonly"]}}))
        # the group keeps its own name and token
        self.assertEqual((effective.name, effective.sync_token), ("Developers", self.child.sync_token))
        self.child.overridden_settings = ["client_mode"]
        self.assertEqual(self.child.effective().client_mode, "MONITOR")
        self.assertIs(self.base.effective(), self.base)

    def test_one_level_only(self):
        grandchild = Group(name="Grandchild", parent=self.child)
        with self.assertRaisesMessage(ValidationError, "only one level"):
            grandchild.full_clean()
        self.base.parent = Group.objects.create(name="Other")
        with self.assertRaisesMessage(ValidationError, "cannot be based on another"):
            self.base.full_clean()
        self.base.parent = self.base
        with self.assertRaisesMessage(ValidationError, "based on itself"):
            self.base.full_clean()

    def test_unknown_overrides_are_dropped(self):
        self.child.overridden_settings = ["nope", "client_mode"]
        self.child.full_clean()
        self.assertEqual(self.child.overridden_settings, ["client_mode"])

    def test_profile(self):
        self.assertEqual(payload(self.child)["UnknownBlockMessage"], "Ask the IT")
        self.assertIn(self.child.sync_token, payload(self.child)["SyncBaseURL"])
        self.assertIn(f"group-{self.child.pk}", plistlib.loads(group_profile(self.child))["PayloadIdentifier"])
        self.child.overridden_settings = ["unknown_block_message"]
        self.assertNotIn("UnknownBlockMessage", payload(self.child))

    def test_file_access_rules(self):
        rule = FileAccessRule.objects.create(name="Secrets", paths="/etc/secret")
        rule.groups.set([self.base])
        self.assertIn("Secrets", payload(self.child)["FileAccessPolicy"]["WatchItems"])
        self.child.inherit_file_access_rules = False
        self.assertNotIn("FileAccessPolicy", payload(self.child))


class InheritanceSyncTestCase(TestCase):
    # the helpers of the sync tests, without running their tests again
    post, preflight, download_all, full_sync, make_rule = (
        test_sync.SyncTestCase.post, test_sync.SyncTestCase.preflight, test_sync.SyncTestCase.download_all,
        test_sync.SyncTestCase.full_sync, test_sync.SyncTestCase.make_rule)

    def setUp(self):
        self.machine_id = "8B5B8A51-7E0B-4C55-9D0C-2B1F1E3A9C01"
        self.dev = Group.objects.create(name="Development", client_mode="LOCKDOWN", batch_size=5)
        self.child = Group.objects.create(name="Dev tools", parent=self.dev, client_mode="MONITOR",
                                          overridden_settings=["removable_media"], removable_media_action="BLOCK")

    def test_preflight_takes_the_settings_of_the_parent(self):
        preflight = self.preflight(self.child)
        self.assertEqual((preflight["client_mode"], preflight["batch_size"]), ("LOCKDOWN", 5))
        self.assertEqual(preflight["removable_media_policy"], {"block": True})

    def test_rules_of_the_parent(self):
        self.make_rule("a" * 64, Policy.BLOCKLIST, groups=[self.dev])
        self.make_rule("b" * 64, Policy.BLOCKLIST, groups=[self.dev])
        # the group based on it allows more: its own rule wins over the one of the parent
        self.make_rule("b" * 64, Policy.ALLOWLIST, groups=[self.child])
        self.make_rule("c" * 64, Policy.BLOCKLIST, is_global=True)
        _, rules = self.full_sync(self.child)
        self.assertEqual({rule["identifier"][0]: rule["policy"] for rule in rules},
                         {"a": "BLOCKLIST", "b": "ALLOWLIST", "c": "BLOCKLIST"})
        machine = Machine.objects.get(machine_id=self.machine_id)
        levels = {key[-1]: level for key, (level, _) in effective_rule_objects(machine).items()}
        self.assertEqual((levels["a"], levels["b"]), (PARENT, GROUP))
        # a new rule of the parent reaches the Mac at the next sync, without the inherited rules
        self.make_rule("d" * 64, Policy.BLOCKLIST, groups=[self.dev])
        _, rules = self.full_sync(self.child, binary_rule_count=3)
        self.assertEqual([rule["identifier"][0] for rule in rules], ["d"])
        Group.objects.filter(pk=self.child.pk).update(inherit_rules=False)
        _, rules = self.full_sync(self.child, binary_rule_count=4)
        self.assertEqual(sorted((rule["identifier"][0], rule["policy"]) for rule in rules),
                         [("a", "REMOVE"), ("d", "REMOVE")])

    def test_a_mac_rule_still_wins(self):
        self.full_sync(self.child)
        machine = Machine.objects.get(machine_id=self.machine_id)
        self.make_rule("a" * 64, Policy.ALLOWLIST, groups=[self.dev])
        Rule.objects.create(rule_type=RuleType.BINARY, identifier="a" * 64,
                            policy=Policy.BLOCKLIST).machines.set([machine])
        _, rules = self.full_sync(self.child, binary_rule_count=1)
        self.assertEqual([rule["policy"] for rule in rules], ["BLOCKLIST"])


class InheritanceConsoleTestCase(ConsoleBase):
    def test_create_a_group_based_on_another(self):
        self.dev.unknown_block_message = "Ask the IT"
        self.dev.client_mode = "LOCKDOWN"
        self.dev.save()
        response = self.client.post(reverse("console:group_add"), {
            **FORM_DATA, "name": "Dev tools", "parent": self.dev.pk, "inherit_rules": "on",
            "override_client_mode": "on", "batch_size": "1"})
        # the batch size is inherited: its (invalid) own value is not checked, nor saved
        group = Group.objects.get(name="Dev tools")
        self.assertRedirects(response, reverse("console:group", args=(group.pk,)), fetch_redirect_response=False)
        self.assertEqual((group.parent, group.inherit_rules, group.inherit_file_access_rules),
                         (self.dev, True, False))
        self.assertEqual(group.overridden_settings, ["client_mode"])
        self.assertEqual(group.batch_size, 100)
        self.assertEqual(group.effective().client_mode, "MONITOR")
        response = self.client.get(reverse("console:group", args=(group.pk,)))
        self.assertContains(response, "Inherited from Development.")
        self.assertContains(response, "Ask the IT")
        # back to inherited: the own value stays for later
        self.client.post(reverse("console:group", args=(group.pk,)), {
            **FORM_DATA, "name": "Dev tools", "parent": self.dev.pk, "client_mode": "MONITOR"})
        group.refresh_from_db()
        self.assertEqual((group.overridden_settings, group.client_mode), ([], "MONITOR"))
        self.assertEqual(group.effective().client_mode, "LOCKDOWN")
        # the parent lists it, and is not deleted while it has groups based on it
        self.assertContains(self.client.get(reverse("console:group", args=(self.dev.pk,))), "Dev tools")
        Machine.objects.filter(group=self.dev).update(group=self.sales)
        response = self.client.post(reverse("console:group_delete", args=(self.dev.pk,)), follow=True)
        self.assertContains(response, "Other groups are based on Development")
        self.assertTrue(Group.objects.filter(pk=self.dev.pk).exists())

    def test_parent_choices(self):
        child = Group.objects.create(name="Dev tools", parent=self.dev)
        response = self.client.get(reverse("console:group", args=(self.sales.pk,)))
        self.assertContains(response, f'<option value="{self.dev.pk}">')
        self.assertNotContains(response, f'<option value="{child.pk}">')
        response = self.client.post(reverse("console:group", args=(self.dev.pk,)), {
            **FORM_DATA, "name": "Development", "parent": self.sales.pk})
        self.assertContains(response, "cannot be based on another one itself")

    def test_profile_change_of_the_parent_names_the_groups_based_on_it(self):
        child = Group.objects.create(name="Dev tools", parent=self.dev)
        Group.objects.create(name="Own messages", parent=self.dev, overridden_settings=["unknown_block_message"])
        response = self.client.post(reverse("console:group", args=(self.dev.pk,)), {
            **FORM_DATA, "name": "Development", "unknown_block_message": "Ask the IT"}, follow=True)
        self.assertContains(response, f"The profile of the groups based on it changed too: {child.name}.")

    def test_groups_list_and_mode_filter(self):
        self.dev.client_mode = "LOCKDOWN"
        self.dev.save()
        Group.objects.create(name="Dev tools", parent=self.dev)
        response = self.client.get(reverse("console:groups"), {"mode": "LOCKDOWN"})
        self.assertContains(response, "Dev tools")
        self.assertContains(response, "based on Development")
        self.assertNotContains(response, ">Sales<")

    def test_mac_shows_the_inherited_rules(self):
        child = Group.objects.create(name="Dev tools", parent=self.dev)
        machine = Machine.objects.create(machine_id="M3", serial_number="C02CHILD", group=child)
        rule = Rule.objects.create(rule_type=RuleType.BINARY, identifier=SHA_A, policy=Policy.BLOCKLIST)
        rule.groups.set([self.dev])
        response = self.client.get(reverse("console:machine", args=(machine.pk,)), {"rules": "parent"})
        self.assertContains(response, SHA_A[:20])
        self.assertContains(response, "inherited")

    def test_file_access_change_names_the_groups_based_on_it(self):
        Group.objects.create(name="Dev tools", parent=self.dev)
        response = self.client.post(reverse("console:file_access_rule_add"), {
            "name": "Secrets", "rule_type": "PathsWithAllowedProcesses", "paths": "/etc/secret",
            "groups": [self.dev.pk], "is_enabled": "on", **process_formset({"signing_id": "com.apple.cat",
                                                                             "platform_binary": "on"})}, follow=True)
        self.assertContains(response, "Dev tools, Development")

    def test_existing_rules_of_events_apply_through_the_parent(self):
        from santa.services import existing_rules
        child = Group.objects.create(name="Dev tools", parent=self.dev)
        machine = Machine.objects.create(machine_id="M3", serial_number="C02CHILD", group=child)
        rule = Rule.objects.create(rule_type=RuleType.BINARY, identifier=SHA_A, policy=Policy.ALLOWLIST)
        rule.groups.set([self.dev])
        event = self.make_event(machine=machine)
        self.assertTrue(existing_rules([event])[SHA_A][0]["applies"])
        Group.objects.filter(pk=child.pk).update(inherit_rules=False)
        event.machine.group.refresh_from_db()
        self.assertFalse(existing_rules([event])[SHA_A][0]["applies"])


class InheritanceConfigTestCase(TestCase):
    def test_round_trip(self):
        base = Group.objects.create(name="Base", client_mode="LOCKDOWN")
        Group.objects.create(name="Child", parent=base, inherit_rules=False, overridden_settings=["batch_size"],
                             batch_size=20)
        data = json.loads(json.dumps(export_config()))
        Group.objects.all().delete()
        import_config(data)
        child = Group.objects.get(name="Child")
        self.assertEqual((child.parent.name, child.inherit_rules, child.overridden_settings, child.batch_size),
                         ("Base", False, ["batch_size"], 20))
        # a parent that is itself moved under another group in the file: it loses its parent first
        data["groups"] = [{"name": "Base", "parent": None}, {"name": "Child", "parent": None},
                          {"name": "Base2", "parent": "Child"}]
        import_config(data)
        self.assertEqual(Group.objects.get(name="Base2").parent.name, "Child")
        self.assertIsNone(Group.objects.get(name="Child").parent)

    def test_unknown_parent(self):
        data = json.loads(json.dumps(export_config()))
        data["groups"] = [{"name": "Child", "parent": "Nope"}]
        with self.assertRaisesMessage(Exception, "unknown parent group 'Nope'"):
            import_config(data)


class AdminTestCase(TestCase):
    def test_admin_lists_and_edits_the_parent(self):
        admin = User.objects.create_superuser("admin", "admin@example.com", "pw")
        self.client.force_login(admin)
        base = Group.objects.create(name="Base")
        child = Group.objects.create(name="Child", parent=base)
        response = self.client.get(reverse("admin:santa_group_changelist"), {"_facets": "1"})
        self.assertContains(response, "Child")
        response = self.client.get(reverse("admin:santa_group_change", args=(child.pk,)))
        self.assertContains(response, "Inheritance")
