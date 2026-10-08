import io
import json
import tempfile
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import CommandError, call_command
from django.test import TestCase
from django.urls import reverse

from santa.config_io import ConfigImportError, apply_choices, export_config, import_config
from santa.models import Group, Machine, Policy, ReleaseSource, ReleaseVersion, Rule, RuleType

SHA_A = "a" * 64
SHA_B = "b" * 64
CEL_EXPR = "target.signing_time >= timestamp('2025-01-01T00:00:00Z')"


class ConfigIOTestCase(TestCase):
    def setUp(self):
        self.dev = Group.objects.create(name="Development", client_mode="LOCKDOWN",
                                        allowed_path_regex="^/opt/tools/\n^/Applications/Internal\\.app/")
        self.sales = Group.objects.create(name="Sales", unknown_block_message="Ask the IT",
                                          removable_media_action="REMOUNT", removable_media_remount_flags="rdonly",
                                          encrypted_removable_media_action="ALLOW", on_start_usb_options="Unmount",
                                          branding_company_name="Example Corp",
                                          branding_company_logo="data:image/png;base64,iVBORw0KGgo=")
        self.machine = Machine.objects.create(machine_id="M1", serial_number="C02TEST", group=self.dev)
        source = ReleaseSource.objects.create(name="colima", kind=ReleaseSource.Kind.GITHUB_RELEASE,
                                              identifier="abiosoft/colima\nlima-vm/lima", asset_pattern="Darwin",
                                              version_pattern="^v1\\.", rule_type=RuleType.SIGNINGID,
                                              policy=Policy.CEL, cel_expr=CEL_EXPR, custom_msg="Ask the IT",
                                              keep_versions=2, keep_unit=ReleaseSource.KeepUnit.MONTHS,
                                              approve_kept_versions=True)
        source.groups.add(self.dev)
        version = ReleaseVersion.objects.create(source=source, version="v1")
        Rule.objects.create(rule_type=RuleType.BINARY, identifier="c" * 64, release_source=source,
                            release_version=version)
        rule = Rule.objects.create(rule_type=RuleType.BINARY, identifier=SHA_A, description="tool A")
        rule.groups.set([self.dev, self.sales])
        Rule.objects.create(rule_type=RuleType.TEAMID, identifier="EQHXZ8M8AV", is_global=True)
        cel = Rule.objects.create(rule_type=RuleType.SIGNINGID, identifier="EQHXZ8M8AV:com.google.Chrome",
                                  policy=Policy.CEL, cel_expr=CEL_EXPR)
        cel.groups.add(self.sales)
        machine_rule = Rule.objects.create(rule_type=RuleType.BINARY, identifier=SHA_B, policy=Policy.BLOCKLIST)
        machine_rule.machines.add(self.machine)

    def wipe(self):
        Rule.objects.all().delete()
        ReleaseSource.objects.all().delete()
        Machine.objects.all().delete()
        Group.objects.all().delete()

    def test_export(self):
        data = export_config()
        self.assertEqual(data["format"], "santa-server-config")
        self.assertNotIn(self.dev.sync_token, json.dumps(data))
        self.assertEqual([g["name"] for g in data["groups"]], ["Development", "Sales"])
        self.assertEqual(data["release_sources"][0]["groups"], ["Development"])
        # the release source rules are rebuilt on the target
        self.assertEqual(len(data["rules"]), 4)
        rule_a = next(r for r in data["rules"] if r["identifier"] == SHA_A)
        self.assertEqual(rule_a["groups"], ["Development", "Sales"])
        rule_b = next(r for r in data["rules"] if r["identifier"] == SHA_B)
        self.assertEqual(rule_b["machines"], ["C02TEST"])

    def test_round_trip_to_an_empty_server(self):
        data = json.loads(json.dumps(export_config()))
        self.wipe()
        report = import_config(data)
        self.assertEqual(len(report["changes"]), 2 + 1 + 4)
        # the machine does not exist on the new server
        self.assertTrue(any("C02TEST unknown" in w for w in report["warnings"]))
        dev = Group.objects.get(name="Development")
        self.assertEqual(dev.client_mode, "LOCKDOWN")
        self.assertEqual(dev.allowed_path_regex, "^/opt/tools/\n^/Applications/Internal\\.app/")
        sales = Group.objects.get(name="Sales")
        self.assertEqual((sales.unknown_block_message, sales.removable_media_action,
                          sales.removable_media_remount_flags, sales.encrypted_removable_media_action,
                          sales.on_start_usb_options, sales.branding_company_name, sales.branding_company_logo),
                         ("Ask the IT", "REMOUNT", "rdonly", "ALLOW", "Unmount", "Example Corp",
                          "data:image/png;base64,iVBORw0KGgo="))
        rule = Rule.objects.get(identifier=SHA_A)
        self.assertEqual({g.name for g in rule.groups.all()}, {"Development", "Sales"})
        self.assertEqual(Rule.objects.get(policy=Policy.CEL).cel_expr, CEL_EXPR)
        self.assertEqual(list(ReleaseSource.objects.get().groups.all()), [dev])
        source = ReleaseSource.objects.get()
        self.assertEqual(source.identifiers, ["abiosoft/colima", "lima-vm/lima"])
        self.assertEqual((source.version_pattern, source.rule_type, source.policy, source.cel_expr, source.custom_msg),
                         ("^v1\\.", RuleType.SIGNINGID, Policy.CEL, CEL_EXPR, "Ask the IT"))
        self.assertEqual((source.keep_versions, source.keep_unit, source.approve_kept_versions,
                          source.include_dependencies), (2, ReleaseSource.KeepUnit.MONTHS, True, False))

    def test_choices_of_the_preview(self):
        data = json.loads(json.dumps(export_config()))
        sales = next(index for index, item in enumerate(data["groups"]) if item["name"] == "Sales")
        rule_a = next(index for index, item in enumerate(data["rules"]) if item["identifier"] == SHA_A)
        cel = next(index for index, item in enumerate(data["rules"]) if item["policy"] == Policy.CEL)
        edited, keep, warnings = apply_choices(
            data, skip={f"rules:{cel}"}, disabled={f"rules:{rule_a}", "release_sources:0"},
            names={f"groups:{sales}": "Sales EU", "release_sources:0": "Colima tools"})
        # a renamed group keeps its references
        self.assertIn("Sales EU", next(item for item in edited["rules"] if item["identifier"] == SHA_A)["groups"])
        self.assertEqual(edited["release_sources"][0]["name"], "Colima tools")
        self.assertFalse(edited["release_sources"][0]["is_enabled"])
        self.assertTrue(all(item["is_enabled"] != (item["identifier"] == SHA_A) for item in edited["rules"]))
        self.assertNotIn(Policy.CEL, [item["policy"] for item in edited["rules"]])
        # skipped: not deleted by delete_missing either
        self.assertEqual(keep, {"rule:SIGNINGID|EQHXZ8M8AV:com.google.Chrome|CEL"})
        self.assertEqual(warnings, [])
        report = import_config(edited, delete_missing=True, keep=keep)
        self.assertTrue(Rule.objects.filter(policy=Policy.CEL).exists())
        # a skipped item has no status: it is not in the file any more
        self.assertNotIn(f"rules:{cel}", report["items"])
        self.assertTrue(Group.objects.filter(name="Sales EU").exists())
        self.assertFalse(Rule.objects.get(identifier=SHA_A).is_enabled)

    def test_a_skipped_item_keeps_its_name(self):
        data = json.loads(json.dumps(export_config()))
        edited, keep, _warnings = apply_choices(data, skip={"release_sources:0"},
                                                names={"release_sources:0": "Renamed"})
        self.assertEqual(keep, {"source:colima"})
        import_config(edited, delete_missing=True, keep=keep)
        # not deleted, although renamed in the preview and not in the file any more
        self.assertTrue(ReleaseSource.objects.filter(name="colima").exists())

    def test_a_skipped_group_the_target_does_not_have(self):
        data = json.loads(json.dumps(export_config()))
        data["groups"].append({**data["groups"][0], "name": "New", "parent": None})
        data["rules"][0]["groups"] = ["New"]
        edited, _keep, warnings = apply_choices(data, skip={f"groups:{len(data['groups']) - 1}"})
        self.assertEqual(edited["rules"][0]["groups"], [])
        self.assertEqual(len(warnings), 1)
        # without the warning it would be an error: the group is unknown on the target
        import_config(edited, dry_run=True)

    def test_import_of_the_old_usb_settings(self):
        data = json.loads(json.dumps(export_config()))
        for group in data["groups"]:
            for field in ("removable_media_action", "removable_media_remount_flags"):
                del group[field]
        data["groups"][0].update({"block_usb_mount": True, "remount_usb_mode": ""})
        data["groups"][1].update({"block_usb_mount": True, "remount_usb_mode": "rdonly,noexec"})
        import_config(data)
        self.assertEqual(Group.objects.get(name="Development").removable_media_action, "BLOCK")
        sales = Group.objects.get(name="Sales")
        self.assertEqual((sales.removable_media_action, sales.removable_media_remount_flags),
                         ("REMOUNT", "rdonly,noexec"))

    def test_import_keeps_the_sync_tokens(self):
        token = self.dev.sync_token
        data = export_config()
        data["groups"][0]["client_mode"] = "MONITOR"
        data["groups"].append({"name": "Design"})
        import_config(data)
        self.dev.refresh_from_db()
        self.assertEqual(self.dev.sync_token, token)
        self.assertEqual(self.dev.client_mode, "MONITOR")
        self.assertTrue(Group.objects.get(name="Design").sync_token)

    def test_import_is_idempotent(self):
        data = export_config()
        self.assertEqual(import_config(data)["changes"], [])
        self.assertEqual(Rule.objects.count(), 5)

    def test_scope_changes_are_applied(self):
        data = export_config()
        rule_a = next(r for r in data["rules"] if r["identifier"] == SHA_A)
        rule_a["groups"] = ["Sales"]
        report = import_config(data)
        self.assertEqual(len(report["changes"]), 1)
        self.assertEqual([g.name for g in Rule.objects.get(identifier=SHA_A).groups.all()], ["Sales"])

    def test_dry_run(self):
        data = export_config()
        data["groups"].append({"name": "Design"})
        data["rules"] = []
        report = import_config(data, delete_missing=True, dry_run=True)
        self.assertIn(("created", "group", "Design"), report["changes"])
        self.assertEqual(sum(1 for action, _, _ in report["changes"] if action == "deleted"), 4)
        self.assertFalse(Group.objects.filter(name="Design").exists())
        self.assertEqual(Rule.objects.count(), 5)

    def test_delete_missing(self):
        data = export_config()
        data["rules"] = [r for r in data["rules"] if r["identifier"] != SHA_A]
        data["release_sources"] = []
        import_config(data, delete_missing=True)
        self.assertFalse(Rule.objects.filter(identifier=SHA_A).exists())
        self.assertFalse(ReleaseSource.objects.exists())
        # a group is never deleted
        self.assertEqual(Group.objects.count(), 2)

    def test_errors_import_nothing(self):
        data = export_config()
        data["groups"].append({"name": "Design"})
        data["groups"][0]["allowed_path_regex"] = "^/broken/("
        data["rules"].append({"rule_type": "TEAMID", "identifier": "not a team id", "policy": "ALLOWLIST"})
        data["rules"].append({"rule_type": "BINARY", "identifier": SHA_B, "policy": "ALLOWLIST",
                              "groups": ["Unknown"]})
        with self.assertRaises(ConfigImportError) as cm:
            import_config(data)
        errors = "\n".join(cm.exception.errors)
        self.assertIn("allowed_path_regex", errors)
        self.assertIn("10 character Team ID", errors)
        self.assertIn("unknown group 'Unknown'", errors)
        self.assertFalse(Group.objects.filter(name="Design").exists())

    def test_wrong_file(self):
        with self.assertRaises(ConfigImportError):
            import_config({"groups": []})

    def test_commands(self):
        with tempfile.NamedTemporaryFile("w+", suffix=".json") as f:
            call_command("export_config", "-o", f.name, stderr=io.StringIO())
            self.wipe()
            out = io.StringIO()
            call_command("import_config", f.name, "--dry-run", stdout=out, stderr=io.StringIO())
            self.assertIn("Dry run, nothing saved: 7 change(s)", out.getvalue())
            self.assertFalse(Group.objects.exists())
            f.seek(0)
            with patch("sys.stdin", f):
                call_command("import_config", "-", stdout=io.StringIO(), stderr=io.StringIO())
        self.assertEqual(Group.objects.count(), 2)
        with self.assertRaises(CommandError):
            call_command("import_config", "/does/not/exist.json")

    def test_admin(self):
        self.client.force_login(User.objects.create_superuser("admin", "admin@example.com", "pw"))
        response = self.client.get(reverse("admin:santa_config_export"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("santa-config-", response["Content-Disposition"])
        data = json.loads(response.content)
        data["groups"].append({"name": "Design"})
        upload = SimpleUploadedFile("config.json", json.dumps(data).encode())
        url = reverse("admin:santa_config_import")
        response = self.client.post(url, {"file": upload, "dry_run": "on"})
        self.assertContains(response, "Dry run: nothing saved")
        self.assertFalse(Group.objects.filter(name="Design").exists())
        upload = SimpleUploadedFile("config.json", json.dumps(data).encode())
        response = self.client.post(url, {"file": upload})
        self.assertContains(response, "Imported")
        self.assertTrue(Group.objects.filter(name="Design").exists())

    def test_admin_import_needs_permissions(self):
        user = User.objects.create_user("viewer", password="pw", is_staff=True)
        self.client.force_login(user)
        self.assertEqual(self.client.get(reverse("admin:santa_config_import")).status_code, 403)
