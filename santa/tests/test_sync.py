import gzip
import json
import uuid
import zlib

from django.conf import settings
from django.test import TestCase

from santa.models import Event, Group, Machine, Policy, RemovableMediaAction, Rule, RuleType

SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64


class SyncTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.dev = Group.objects.create(name="Development", client_mode="LOCKDOWN", batch_size=5)
        cls.sales = Group.objects.create(name="Sales")

    def setUp(self):
        self.machine_id = str(uuid.uuid4()).upper()

    def post(self, stage, data, group=None, machine_id=None, compress=False):
        group = group or self.dev
        body = json.dumps(data).encode()
        headers = {}
        if compress:
            body = zlib.compress(body)
            headers["HTTP_CONTENT_ENCODING"] = "deflate"
        return self.client.post(f"/sync/{group.sync_token}/{stage}/{machine_id or self.machine_id}",
                                body, content_type="application/json", **headers)

    def preflight(self, group=None, **extra):
        data = {"serial_num": "C02TEST", "hostname": "mac-1", "os_version": "15.6", "os_build": "24G84",
                "santa_version": "2025.8", "client_mode": "MONITOR", "primary_user": "jdoe"}
        data.update(extra)
        response = self.post("preflight", data, group)
        self.assertEqual(response.status_code, 200)
        return response.json()

    def download_all(self, group=None):
        rules = []
        cursor = None
        while True:
            response = self.post("ruledownload", {"cursor": cursor} if cursor else {}, group)
            self.assertEqual(response.status_code, 200)
            data = response.json()
            rules.extend(data["rules"])
            cursor = data.get("cursor")
            if not cursor:
                return rules

    def full_sync(self, group=None, **preflight_extra):
        preflight = self.preflight(group, **preflight_extra)
        rules = self.download_all(group)
        response = self.post("postflight", {"rules_received": len(rules), "rules_processed": len(rules)}, group)
        self.assertEqual(response.status_code, 200)
        return preflight, rules

    def make_rule(self, identifier, policy=Policy.ALLOWLIST, groups=(), is_global=False, **kwargs):
        rule = Rule.objects.create(rule_type=kwargs.pop("rule_type", RuleType.BINARY), identifier=identifier,
                                   policy=policy, is_global=is_global, **kwargs)
        rule.groups.set(groups)
        return rule

    def test_unknown_token(self):
        response = self.client.post(f"/sync/nope/preflight/{self.machine_id}", "{}", content_type="application/json")
        self.assertEqual(response.status_code, 404)

    def test_get_not_allowed(self):
        response = self.client.get(f"/sync/{self.dev.sync_token}/preflight/{self.machine_id}")
        self.assertEqual(response.status_code, 405)

    def test_preflight_enrolls_machine_with_group_config(self):
        response = self.preflight()
        self.assertEqual(response["client_mode"], "LOCKDOWN")
        self.assertEqual(response["sync_type"], "CLEAN")
        self.assertEqual(response["batch_size"], 5)
        self.assertEqual(response["allowed_path_regex"], "(?!)")
        machine = Machine.objects.get(machine_id=self.machine_id)
        self.assertEqual(machine.group, self.dev)
        self.assertEqual(machine.serial_number, "C02TEST")
        self.assertEqual(machine.primary_user, "jdoe")

    def test_multiple_path_regexes_are_combined(self):
        self.dev.allowed_path_regex = "^/opt/tools/bin/\n\n  ^/Applications/Internal\\.app/  \n"
        self.dev.blocked_path_regex = "^/Users/[^/]+/Downloads/"
        self.dev.save()
        response = self.preflight()
        self.assertEqual(response["allowed_path_regex"], "(?:^/opt/tools/bin/)|(?:^/Applications/Internal\\.app/)")
        self.assertEqual(response["blocked_path_regex"], "^/Users/[^/]+/Downloads/")

    def test_block_dialog_url(self):
        self.assertNotIn("event_detail_url", self.preflight())
        self.dev.event_detail_url = "https://tickets.example.com/new?sha=%file_sha%"
        self.dev.event_detail_text = "Request access"
        self.dev.save()
        response = self.preflight()
        self.assertEqual(response["event_detail_url"], "https://tickets.example.com/new?sha=%file_sha%")
        self.assertEqual(response["event_detail_text"], "Request access")

    def test_removable_media(self):
        response = self.preflight()
        self.assertEqual(response["removable_media_policy"], {"allow": True})
        self.assertIs(response["block_usb_mount"], False)
        self.assertNotIn("encrypted_removable_media_policy", response)
        self.dev.removable_media_action = RemovableMediaAction.BLOCK
        self.dev.save()
        response = self.preflight()
        self.assertEqual(response["removable_media_policy"], {"block": True})
        # older Santa versions only know these keys
        self.assertIs(response["block_usb_mount"], True)
        self.assertEqual(response["remount_usb_mode"], [])

    def test_file_access_override(self):
        self.assertEqual(self.preflight()["override_file_access_action"], "NONE")
        self.dev.override_file_access_action = "AUDIT_ONLY"
        self.dev.save()
        self.assertEqual(self.preflight()["override_file_access_action"], "AUDIT_ONLY")

    def test_removable_media_remount(self):
        self.dev.removable_media_action = RemovableMediaAction.REMOUNT
        self.dev.removable_media_remount_flags = "rdonly, noexec"
        self.dev.encrypted_removable_media_action = RemovableMediaAction.ALLOW
        self.dev.save()
        response = self.preflight()
        self.assertEqual(response["removable_media_policy"], {"remount": {"flags": ["rdonly", "noexec"]}})
        self.assertIs(response["block_usb_mount"], True)
        self.assertEqual(response["remount_usb_mode"], ["rdonly", "noexec"])
        self.assertEqual(response["encrypted_removable_media_policy"], {"allow": True})

    def test_compressed_body(self):
        response = self.post("preflight", {"serial_num": "C02ZLIB", "santa_version": "2025.8"}, compress=True)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(Machine.objects.filter(serial_number="C02ZLIB").exists())

    def test_gzip_body(self):
        body = gzip.compress(json.dumps({"serial_num": "C02GZIP", "santa_version": "2025.8"}).encode())
        response = self.client.post(f"/sync/{self.dev.sync_token}/preflight/{self.machine_id}", body,
                                    content_type="application/json", HTTP_CONTENT_ENCODING="gzip")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(Machine.objects.filter(serial_number="C02GZIP").exists())

    def test_decompression_bomb_is_refused(self):
        # a few KB that would expand to 5 MB, above the limit
        bomb = {"serial_num": "C02BOMB", "padding": " " * (5 * 1024 * 1024)}
        with self.settings(SYNC_MAX_DECOMPRESSED_BYTES=1024 * 1024):
            response = self.post("preflight", bomb, compress=True)
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Machine.objects.filter(serial_number="C02BOMB").exists())
        body = gzip.compress(json.dumps(bomb).encode())
        with self.settings(SYNC_MAX_DECOMPRESSED_BYTES=1024 * 1024):
            response = self.client.post(f"/sync/{self.dev.sync_token}/preflight/{self.machine_id}", body,
                                        content_type="application/json", HTTP_CONTENT_ENCODING="gzip")
        self.assertEqual(response.status_code, 400)

    def test_old_santa_gets_clean_sync_flag(self):
        response = self.preflight(santa_version="2023.9")
        self.assertIs(response["clean_sync"], True)
        self.assertNotIn("sync_type", response)

    def test_missing_serial_number(self):
        response = self.post("preflight", {"santa_version": "2025.8"})
        self.assertEqual(response.status_code, 400)

    def test_unknown_machine_rule_download(self):
        response = self.post("ruledownload", {})
        self.assertEqual(response.status_code, 400)

    def test_rule_scopes(self):
        self.make_rule(SHA_A, is_global=True)
        self.make_rule(SHA_B, groups=[self.dev])
        self.make_rule(SHA_C, groups=[self.sales])
        self.make_rule("d" * 64, groups=[self.dev], is_enabled=False)
        _, rules = self.full_sync()
        self.assertEqual(sorted(r["identifier"] for r in rules), [SHA_A, SHA_B])
        self.assertEqual(rules[0], {"identifier": SHA_A, "rule_type": "BINARY", "policy": "ALLOWLIST"})

    def test_machine_scope_and_precedence(self):
        self.preflight()
        machine = Machine.objects.get(machine_id=self.machine_id)
        self.make_rule(SHA_A, is_global=True, policy=Policy.BLOCKLIST, custom_msg="Nope")
        self.make_rule(SHA_A, groups=[self.dev], policy=Policy.ALLOWLIST)
        self.make_rule(SHA_B, groups=[self.dev], policy=Policy.ALLOWLIST)
        self.make_rule(SHA_B, groups=[self.dev], policy=Policy.BLOCKLIST)
        self.make_rule(SHA_C, policy=Policy.BLOCKLIST, groups=[self.dev])
        allow_on_machine = self.make_rule(SHA_C, policy=Policy.ALLOWLIST)
        allow_on_machine.machines.add(machine)
        _, rules = self.full_sync()
        policies = {r["identifier"]: r["policy"] for r in rules}
        # group beats global, block beats allow on the same scope, machine beats group
        self.assertEqual(policies, {SHA_A: "ALLOWLIST", SHA_B: "BLOCKLIST", SHA_C: "ALLOWLIST"})

    def test_incremental_sync_and_removal(self):
        rule_a = self.make_rule(SHA_A, groups=[self.dev])
        self.make_rule(SHA_B, groups=[self.dev])
        _, rules = self.full_sync()
        self.assertEqual(len(rules), 2)

        # nothing changed
        preflight, rules = self.full_sync(binary_rule_count=2)
        self.assertEqual(preflight["sync_type"], "NORMAL")
        self.assertEqual(rules, [])

        # a disabled rule is removed, a changed rule is sent again
        rule_a.is_enabled = False
        rule_a.save()
        rule_b = Rule.objects.get(identifier=SHA_B)
        rule_b.policy = Policy.BLOCKLIST
        rule_b.custom_msg = "Blocked by IT"
        rule_b.save()
        _, rules = self.full_sync(binary_rule_count=2)
        self.assertEqual(sorted(rules, key=lambda r: r["identifier"]), [
            {"identifier": SHA_A, "rule_type": "BINARY", "policy": "REMOVE"},
            {"identifier": SHA_B, "rule_type": "BINARY", "policy": "BLOCKLIST", "custom_msg": "Blocked by IT"},
        ])
        machine = Machine.objects.get(machine_id=self.machine_id)
        self.assertEqual(list(machine.synced_rules), [f"BINARY:{SHA_B}"])

    def test_uncommitted_session_is_sent_again(self):
        self.make_rule(SHA_A, groups=[self.dev])
        self.full_sync()
        self.make_rule(SHA_B, groups=[self.dev])
        self.preflight(binary_rule_count=1)
        self.assertEqual(len(self.download_all()), 1)
        # no postflight, the next session sends the rule again
        _, rules = self.full_sync(binary_rule_count=1)
        self.assertEqual([r["identifier"] for r in rules], [SHA_B])

    def test_pagination(self):
        identifiers = [f"{i:064x}" for i in range(12)]
        for identifier in identifiers:
            self.make_rule(identifier, is_global=True)
        self.preflight()
        response = self.post("ruledownload", {}).json()
        self.assertEqual(len(response["rules"]), 5)
        self.assertIn("cursor", response)
        rules = response["rules"]
        while "cursor" in response:
            response = self.post("ruledownload", {"cursor": response["cursor"]}).json()
            rules.extend(response["rules"])
        self.assertEqual(sorted(r["identifier"] for r in rules), identifiers)

    def test_clean_sync_when_client_lost_its_rules(self):
        self.make_rule(SHA_A, groups=[self.dev])
        self.full_sync()
        preflight = self.preflight()  # every rule count is 0
        self.assertEqual(preflight["sync_type"], "CLEAN")

    def test_clean_sync_requested_by_admin(self):
        self.make_rule(SHA_A, groups=[self.dev])
        self.full_sync()
        Machine.objects.filter(machine_id=self.machine_id).update(clean_sync_requested=True)
        self.assertEqual(self.preflight(binary_rule_count=1)["sync_type"], "CLEAN")
        self.assertEqual(self.preflight(binary_rule_count=1)["sync_type"], "NORMAL")

    def test_group_change(self):
        self.make_rule(SHA_A, groups=[self.dev])
        self.make_rule(SHA_C, groups=[self.sales])
        self.full_sync()
        preflight, rules = self.full_sync(group=self.sales, binary_rule_count=1)
        self.assertEqual(preflight["sync_type"], "CLEAN")
        self.assertEqual(preflight["client_mode"], "MONITOR")
        self.assertEqual([r["identifier"] for r in rules], [SHA_C])
        # the old group URL does not work anymore for this machine
        with self.assertLogs("santa.sync_views", "WARNING") as logs:
            response = self.post("ruledownload", {}, group=self.dev)
        self.assertEqual(response.status_code, 400)
        # the reason is logged, but the response does not tell which group the machine belongs to
        self.assertEqual(response.json(), {"error": "bad request"})
        self.assertIn(f"belongs to group {self.sales.pk}", logs.output[0])

    def test_event_upload(self):
        self.preflight()
        event = {
            "file_sha256": SHA_A.upper(), "file_path": "/opt/homebrew/bin", "file_name": "colima",
            "executing_user": "jdoe", "execution_time": 1758800000.123, "decision": "BLOCK_UNKNOWN",
            "signing_chain": [{"sha256": SHA_B, "cn": "Developer ID Application: Example"}],
            "team_id": "ABCDE12345", "signing_id": "ABCDE12345:com.example.tool", "cdhash": "1" * 40,
        }
        response = self.post("eventupload", {"events": [event, {**event, "execution_time": 1758800001}]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {})
        # uploaded again after a failure
        self.post("eventupload", {"events": [event]})
        self.assertEqual(Event.objects.count(), 2)
        stored = Event.objects.order_by("execution_time").first()
        self.assertEqual(stored.file_sha256, SHA_A)
        self.assertEqual(stored.cert_sha256, SHA_B)
        self.assertEqual(stored.group, self.dev)
        self.assertTrue(stored.is_blocked)

    def test_health(self):
        self.assertEqual(self.client.get("/health", HTTP_HOST="10.0.0.1").status_code, 200)
        self.assertEqual(self.client.get("/ready").status_code, 200)

    def test_probes_answer_before_the_host_check(self):
        # probes and Prometheus use the pod IP, not the public host name
        with self.settings(ALLOWED_HOSTS=["santa.example.com"]):
            for path in ("/health", "/ready"):
                self.assertEqual(self.client.get(path, HTTP_HOST="10.0.0.1").status_code, 200, path)
            metrics = self.client.get("/metrics", HTTP_HOST="10.0.0.1")
            if settings.METRICS_ENABLED:
                self.assertEqual(metrics.status_code, 200)
                self.assertIn(b"django_http_requests", metrics.content)
            else:
                # switched off in the environment (METRICS_ENABLED=false): an ordinary path, the host check refuses it
                self.assertEqual(metrics.status_code, 400)
            self.assertEqual(self.client.get("/login/", HTTP_HOST="10.0.0.1").status_code, 400)
