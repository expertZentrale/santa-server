import json
import plistlib

from django.contrib.admin.models import ADDITION, DELETION, LogEntry
from django.contrib.auth.models import Permission, User
from django.core.exceptions import ValidationError
from django.urls import reverse

from santa.config_io import ConfigImportError, export_config, import_config
from santa.models import FileAccessProcess, FileAccessRule, FileAccessRuleType, Group
from santa.profiles import file_access_policy, group_profile

from .test_console import ConsoleBase

HTMX = {"HTTP_HX_REQUEST": "true"}


def process_formset(*processes, prefix="processes"):
    """The POST data of the process formset: new rows only"""
    data = {f"{prefix}-TOTAL_FORMS": str(len(processes)), f"{prefix}-INITIAL_FORMS": "0",
            f"{prefix}-MIN_NUM_FORMS": "0", f"{prefix}-MAX_NUM_FORMS": "1000"}
    for index, process in enumerate(processes):
        data.update({f"{prefix}-{index}-{key}": value for key, value in process.items()})
    return data


class FileAccessTestCase(ConsoleBase):
    def setUp(self):
        super().setUp()
        self.ssh = FileAccessRule.objects.create(name="SSH-keys", paths="/Users/*/.ssh/id_rsa",
                                                 path_prefixes="/Users/*/.ssh/keys/", audit_only=False,
                                                 block_message="Only ssh may read the keys.")
        self.ssh.groups.add(self.dev)
        self.ssh.processes.create(signing_id="com.openssh.ssh", platform_binary=True)
        self.ssh.processes.create(team_id="EQHXZ8M8AV", signing_id="com.google.Chrome")

    def test_validation(self):
        rule = FileAccessRule(name="SSH keys", paths="relative/path\n/Users/**/x",
                              block_message="x" * 2049)
        with self.assertRaises(ValidationError) as cm:
            rule.full_clean()
        # Santa ignores rules with spaces in their name, and longer block messages
        self.assertEqual(set(cm.exception.message_dict), {"name", "paths", "block_message"})
        with self.assertRaises(ValidationError) as cm:
            FileAccessRule(name="a" * 65, paths="/etc/hosts").full_clean()
        self.assertIn("name", cm.exception.message_dict)
        FileAccessRule(name="Corp:ssh_keys-1.0", paths="/etc/hosts").full_clean()
        with self.assertRaises(ValidationError) as cm:
            FileAccessRule(name="empty").full_clean()
        self.assertIn("At least one path.", cm.exception.message_dict["paths"])
        for fields, error_field in [({"signing_id": "com.example.tool"}, "signing_id"),
                                    ({"team_id": "short"}, "team_id"), ({"cdhash": "xyz"}, "cdhash"),
                                    ({"binary_path": "usr/bin/ssh"}, "binary_path")]:
            with self.assertRaises(ValidationError) as cm:
                FileAccessProcess(rule=self.ssh, **fields).full_clean()
            self.assertIn(error_field, cm.exception.message_dict)
        with self.assertRaises(ValidationError):
            FileAccessProcess(rule=self.ssh).full_clean()
        process = FileAccessProcess(rule=self.ssh, team_id="eqhxz8m8av", cdhash="A" * 40)
        process.full_clean()
        self.assertEqual((process.team_id, process.cdhash), ("EQHXZ8M8AV", "a" * 40))

    def test_profile(self):
        Group.objects.filter(pk=self.dev.pk).update(file_access_block_message="Ask the IT")
        self.dev.refresh_from_db()
        payload, = plistlib.loads(group_profile(self.dev))["PayloadContent"]
        policy = payload["FileAccessPolicy"]
        self.assertEqual(payload["FileAccessBlockMessage"], "Ask the IT")
        item = policy["WatchItems"]["SSH-keys"]
        self.assertEqual(item["Paths"], [{"Path": "/Users/*/.ssh/id_rsa", "IsPrefix": False},
                                         {"Path": "/Users/*/.ssh/keys/", "IsPrefix": True}])
        self.assertEqual(item["Options"], {"RuleType": "PathsWithAllowedProcesses", "AllowReadAccess": False,
                                           "AuditOnly": False, "EnableSilentMode": False, "EnableSilentTTYMode": False,
                                           "BlockMessage": "Only ssh may read the keys."})
        self.assertEqual(item["Processes"], [{"SigningID": "com.openssh.ssh", "PlatformBinary": True},
                                             {"SigningID": "com.google.Chrome", "TeamID": "EQHXZ8M8AV"}])
        # only the rules of the group, the global ones, and only the enabled ones
        payload, = plistlib.loads(group_profile(self.sales))["PayloadContent"]
        self.assertNotIn("FileAccessPolicy", payload)
        FileAccessRule.objects.create(name="Global", paths="/etc/hosts", is_global=True)
        FileAccessRule.objects.create(name="Off", paths="/etc/hosts", is_global=True, is_enabled=False)
        self.assertEqual(list(file_access_policy(self.sales)["WatchItems"]), ["Global"])
        # the version follows the rules
        version = file_access_policy(self.dev)["Version"]
        self.assertEqual(file_access_policy(self.dev)["Version"], version)
        self.ssh.processes.first().delete()
        self.assertNotEqual(file_access_policy(self.dev)["Version"], version)

    def test_list_and_filters(self):
        FileAccessRule.objects.create(name="Cookies", path_prefixes="/Users/*/Library/Cookies/", is_global=True)
        url = reverse("console:file_access_rules")
        response = self.client.get(url)
        self.assertContains(response, "SSH-keys")
        rule_url = reverse("console:file_access_rule", args=(self.ssh.pk,))
        self.assertContains(response, f'href="{rule_url}" hx-get="{rule_url}" hx-target="#drawer"')
        names = [rule.name for rule in self.client.get(url, {"group": self.sales.pk}).context["page"]]
        self.assertEqual(names, ["Cookies"])
        # several groups join the groups: the processes are still counted once
        self.ssh.groups.add(self.sales)
        page = self.client.get(url, {"group": [self.dev.pk, self.sales.pk]}).context["page"]
        self.assertEqual({rule.name: rule.process_count for rule in page}, {"Cookies": 0, "SSH-keys": 2})
        names = [rule.name for rule in self.client.get(url, {"mode": "block"}).context["page"]]
        self.assertEqual(names, ["SSH-keys"])
        names = [rule.name for rule in self.client.get(url, {"q": "cookies"}).context["page"]]
        self.assertEqual(names, ["Cookies"])
        # the group shows how many rules its profile has (its own and the global one)
        self.assertContains(self.client.get(reverse("console:group", args=(self.dev.pk,))), "2 in the profile")

    def test_create_in_the_drawer(self):
        self.assertContains(self.client.get(reverse("console:file_access_rule_add"), **HTMX), "data-formset-add")
        data = {"name": "Browser-cookies", "rule_type": FileAccessRuleType.PATHS_WITH_ALLOWED_PROCESSES,
                "path_prefixes": "/Users/*/Library/Cookies/", "audit_only": "on", "is_enabled": "on",
                "groups": [self.sales.pk],
                **process_formset({"team_id": "EQHXZ8M8AV", "signing_id": "com.google.Chrome"},
                                  {"signing_id": "com.apple.Safari", "platform_binary": "on"})}
        response = self.client.post(reverse("console:file_access_rule_add"), data, **HTMX)
        self.assertEqual(response.status_code, 204, getattr(response, "context", None) and
                         [response.context["form"].errors, response.context["formset"].errors])
        rule = FileAccessRule.objects.get(name="Browser-cookies")
        self.assertEqual(rule.processes.count(), 2)
        self.assertEqual(list(rule.groups.all()), [self.sales])
        self.assertTrue(LogEntry.objects.filter(object_id=str(rule.pk), action_flag=ADDITION).exists())
        response = self.client.get(reverse("console:file_access_rules"))
        self.assertContains(response, "Download the configuration profile of these groups again")
        self.assertContains(response, "Sales")

    def test_change_and_remove_a_process(self):
        url = reverse("console:file_access_rule", args=(self.ssh.pk,))
        first, second = self.ssh.processes.all()
        data = {"name": "SSH-keys", "rule_type": self.ssh.rule_type, "paths": self.ssh.paths, "is_enabled": "on",
                "groups": [self.dev.pk],
                "processes-TOTAL_FORMS": "2", "processes-INITIAL_FORMS": "2", "processes-MIN_NUM_FORMS": "0",
                "processes-MAX_NUM_FORMS": "1000",
                "processes-0-id": first.pk, "processes-0-rule": self.ssh.pk,
                "processes-0-signing_id": "com.openssh.ssh", "processes-0-platform_binary": "on",
                "processes-1-id": second.pk, "processes-1-rule": self.ssh.pk,
                "processes-1-team_id": "EQHXZ8M8AV", "processes-1-signing_id": "com.google.Chrome",
                "processes-1-DELETE": "on"}
        response = self.client.post(url, data)
        self.assertRedirects(response, reverse("console:file_access_rules"))
        self.ssh.refresh_from_db()
        self.assertEqual(list(self.ssh.processes.all()), [first])
        # an unticked box is off
        self.assertFalse(self.ssh.audit_only)
        self.assertIn("Processes changed",
                      LogEntry.objects.filter(object_id=str(self.ssh.pk)).latest("pk").change_message)

    def test_processes_rule_types_need_a_process(self):
        data = {"name": "Curl", "rule_type": FileAccessRuleType.PROCESSES_WITH_DENIED_PATHS,
                "path_prefixes": "/Users/", "is_global": "on", **process_formset()}
        response = self.client.post(reverse("console:file_access_rule_add"), data)
        self.assertContains(response, "This rule type needs at least one process.")
        response = self.client.post(reverse("console:file_access_rule_add"),
                                    {**data, **process_formset({"binary_path": "/usr/bin/curl"})})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(FileAccessRule.objects.get(name="Curl").processes.get().binary_path, "/usr/bin/curl")

    def test_delete(self):
        response = self.client.post(reverse("console:file_access_rule_delete", args=(self.ssh.pk,)), follow=True)
        self.assertContains(response, "Development")
        self.assertFalse(FileAccessRule.objects.exists())
        self.assertFalse(FileAccessProcess.objects.exists())
        self.assertTrue(LogEntry.objects.filter(object_repr="SSH-keys", action_flag=DELETION).exists())

    def test_permissions(self):
        viewer = User.objects.create_user("viewer", is_staff=True)
        viewer.user_permissions.set(Permission.objects.filter(codename="view_fileaccessrule"))
        self.client.force_login(viewer)
        url = reverse("console:file_access_rule", args=(self.ssh.pk,))
        response = self.client.get(url)
        self.assertContains(response, "disabled")
        self.assertNotContains(response, "file-access-delete-form")
        self.assertEqual(self.client.post(url, {"name": "x"}).status_code, 403)
        self.assertEqual(self.client.post(reverse("console:file_access_rule_delete", args=(self.ssh.pk,))).status_code,
                         403)
        nobody = User.objects.create_user("nobody", is_staff=True)
        self.client.force_login(nobody)
        self.assertEqual(self.client.get(reverse("console:file_access_rules")).status_code, 403)

    def test_history(self):
        response = self.client.get(reverse("console:history", args=("fileaccessrule", self.ssh.pk)), **HTMX)
        self.assertContains(response, 'class="drawer-panel')

    def test_export_and_import(self):
        data = json.loads(json.dumps(export_config()))
        exported = data["file_access_rules"][0]
        self.assertEqual((exported["name"], exported["groups"], len(exported["processes"])),
                         ("SSH-keys", ["Development"], 2))
        FileAccessRule.objects.all().delete()
        report = import_config(data)
        self.assertIn(("created", "file access rule", "SSH-keys"), report["changes"])
        rule = FileAccessRule.objects.get()
        self.assertEqual((rule.block_message, list(rule.groups.all()), rule.processes.count()),
                         ("Only ssh may read the keys.", [self.dev], 2))
        # idempotent
        self.assertFalse([change for change in import_config(data)["changes"] if change[1] == "file access rule"])
        # an overlong process field is an error of the file, nothing is saved
        broken = json.loads(json.dumps(data))
        broken["file_access_rules"][0]["processes"][0]["signing_id"] = "x" * 301
        with self.assertRaises(ConfigImportError):
            import_config(broken)
        # an old file without file access rules deletes none of them
        del data["file_access_rules"]
        import_config(data, delete_missing=True)
        self.assertTrue(FileAccessRule.objects.exists())
