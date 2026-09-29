import json
from datetime import timedelta
from unittest.mock import patch

from django.contrib.admin.models import ADDITION, CHANGE, LogEntry
from django.contrib.auth.models import Group as AuthGroup
from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from santa.auth import REQUESTERS_GROUP_NAME
from santa.models import Event, Group, Machine, Policy, ReleaseSource, ReleaseVersion, Rule, RuleType, Tag

from .test_catalog import LOCMEM

SHA_A = "a" * 64
SHA_B = "b" * 64


@override_settings(CACHES=LOCMEM)
class ConsoleBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_superuser("admin", "admin@example.com", "pw")
        cls.user = User.objects.create_user("jdoe@example.com", "jdoe@example.com", "pw")
        # the role the sign-in gives everyone (migration 0012)
        cls.user.groups.add(AuthGroup.objects.get(name=REQUESTERS_GROUP_NAME))
        cls.dev = Group.objects.create(name="Development")
        cls.sales = Group.objects.create(name="Sales")
        cls.machine = Machine.objects.create(machine_id="M1", serial_number="C02TEST", hostname="jdoe-mbp",
                                             group=cls.dev, primary_user="jdoe@example.com")
        cls.other_machine = Machine.objects.create(machine_id="M2", serial_number="C02OTHER", hostname="other",
                                                   group=cls.sales, primary_user="someone")

    def setUp(self):
        self.client.force_login(self.admin)
        patcher = patch("santa.catalog.lookup", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def make_event(self, sha256=SHA_A, machine=None, minutes_ago=0, **kwargs):
        machine = machine or self.machine
        kwargs.setdefault("decision", "BLOCK_UNKNOWN")
        kwargs.setdefault("file_name", "colima")
        return Event.objects.create(machine=machine, group=machine.group,
                                    execution_time=timezone.now() - timedelta(minutes=minutes_ago),
                                    file_sha256=sha256, file_path=f"/opt/bin/{kwargs['file_name']}", **kwargs)


class ConsolePagesTestCase(ConsoleBase):
    def test_pages_load(self):
        event = self.make_event(signing_id="ABCDE12345:com.example.tool", team_id="ABCDE12345")
        rule = Rule.objects.create(rule_type=RuleType.BINARY, identifier=SHA_A, is_global=True)
        source = ReleaseSource.objects.create(name="colima", kind=ReleaseSource.Kind.HOMEBREW_FORMULA,
                                              identifier="colima\nlima", is_global=True)
        ReleaseVersion.objects.create(source=source, identifier="colima", version="0.9.0")
        for name, args in [("events", ()), ("rules", ()), ("rule_add", ()), ("rule_upload", ()), ("rule", (rule.pk,)),
                           ("sources", ()), ("source_add", ()), ("source", (source.pk,)),
                           ("source_edit", (source.pk,)), ("event", (event.pk,)), ("requests", ())]:
            response = self.client.get(reverse(f"console:{name}", args=args))
            self.assertEqual(response.status_code, 200, name)
        self.assertEqual(self.client.get(reverse("console:events"), {"view": "all"}).status_code, 200)
        self.assertEqual(self.client.get(reverse("home")).url, reverse("console:events"))

    def test_non_staff_users_only_get_the_request_form(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(reverse("console:rules")).status_code, 403)
        self.assertEqual(self.client.get(reverse("console:catalog_search")).status_code, 403)
        self.assertEqual(self.client.get(reverse("home")).url, reverse("requests:list"))
        self.client.logout()
        self.assertEqual(self.client.get(reverse("console:events")).status_code, 302)

    def test_staff_without_permissions(self):
        staff = User.objects.create_user("viewer", is_staff=True)
        self.client.force_login(staff)
        self.assertEqual(self.client.get(reverse("console:rules")).status_code, 403)


class ConsoleRulesTestCase(ConsoleBase):
    def test_create_rule(self):
        tag = Tag.objects.create(name="dev tools")
        response = self.client.post(reverse("console:rule_add"), {
            "rule_type": RuleType.SIGNINGID, "identifier": "ABCDE12345:com.example.tool", "policy": Policy.CEL,
            "cel_expr": "args.size() < 3", "groups": [self.dev.pk], "machines": "c02other",
            "tags": [tag.pk], "new_tags": "cli, reviewed", "is_enabled": "on", "description": "tool",
        })
        self.assertEqual(response.status_code, 302)
        rule = Rule.objects.get()
        self.assertEqual(rule.created_by, self.admin)
        self.assertEqual(list(rule.machines.all()), [self.other_machine])
        self.assertEqual({t.name for t in rule.tags.all()}, {"dev tools", "cli", "reviewed"})
        self.assertEqual(LogEntry.objects.get().action_flag, ADDITION)

    def test_rule_needs_a_scope_and_a_valid_identifier(self):
        response = self.client.post(reverse("console:rule_add"), {
            "rule_type": RuleType.BINARY, "identifier": "nope", "policy": Policy.ALLOWLIST, "machines": "unknown"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Unknown Mac")
        self.assertContains(response, "SHA-256")
        self.assertFalse(Rule.objects.exists())

    def test_filters_and_tabs(self):
        Rule.objects.create(rule_type=RuleType.BINARY, identifier=SHA_A, is_global=True, description="alpha")
        Rule.objects.create(rule_type=RuleType.TEAMID, identifier="ABCDE12345", is_global=True)
        response = self.client.get(reverse("console:rules"), {"type": RuleType.TEAMID})
        self.assertContains(response, "ABCDE12345")
        self.assertNotContains(response, "alpha")
        response = self.client.get(reverse("console:rules"), {"q": "alpha", "scope": "global"})
        self.assertContains(response, "alpha")

    def test_toggle_and_bulk(self):
        rules = [Rule.objects.create(rule_type=RuleType.BINARY, identifier=c * 64, is_global=True) for c in "ab"]
        response = self.client.post(reverse("console:rule_toggle", args=(rules[0].pk,)), HTTP_HX_REQUEST="true")
        self.assertContains(response, f'id="rule-{rules[0].pk}"')
        rules[0].refresh_from_db()
        self.assertFalse(rules[0].is_enabled)
        self.client.post(reverse("console:rules_bulk"), {"action": "add_tag", "tag": "new tag",
                                                         "rules": [r.pk for r in rules]})
        self.assertEqual(Tag.objects.get().rules.count(), 2)
        self.client.post(reverse("console:rules_bulk"), {"action": "enable", "rules": [r.pk for r in rules]})
        self.assertEqual(Rule.objects.filter(is_enabled=True).count(), 2)
        self.client.post(reverse("console:rules_bulk"), {"action": "delete", "rules": [rules[1].pk]})
        self.assertEqual(Rule.objects.count(), 1)
        self.assertTrue(LogEntry.objects.filter(action_flag=CHANGE, object_id=str(rules[0].pk)).exists())

    def test_package_rules_are_read_only(self):
        source = ReleaseSource.objects.create(name="colima", kind=ReleaseSource.Kind.HOMEBREW_FORMULA,
                                              identifier="colima", is_global=True)
        version = ReleaseVersion.objects.create(source=source, identifier="colima", version="1")
        rule = Rule.objects.create(rule_type=RuleType.BINARY, identifier=SHA_A, release_source=source,
                                   release_version=version, is_global=True)
        response = self.client.post(reverse("console:rule", args=(rule.pk,)), {"identifier": SHA_B})
        self.assertContains(response, "Change the package rule")
        rule.refresh_from_db()
        self.assertEqual(rule.identifier, SHA_A)


class ConsoleSourcesTestCase(ConsoleBase):
    def post_source(self, **data):
        return self.client.post(reverse("console:source_add"), {
            "name": "editors", "kind": ReleaseSource.Kind.VSCODE_EXTENSION, "rule_type": RuleType.BINARY,
            "policy": Policy.ALLOWLIST, "groups": [self.dev.pk], "auto_approve": "on", "is_enabled": "on",
            "auto_approve_delay_days": 0, "keep_versions": 3, **data})

    def test_create_with_picked_icons(self):
        picked = {"rust-lang.rust-analyzer": {"name": "rust-analyzer", "icon_url": "https://cdn.example/i.png"},
                  "evil.one": {"name": "x", "icon_url": "javascript:alert(1)"}}
        response = self.post_source(identifier="rust-lang.rust-analyzer\nevil.one", picked=json.dumps(picked),
                                    new_tags="editor")
        source = ReleaseSource.objects.get()
        self.assertRedirects(response, reverse("console:source", args=(source.pk,)))
        self.assertEqual(source.identifiers, ["rust-lang.rust-analyzer", "evil.one"])
        self.assertEqual(source.identifier_icons["rust-lang.rust-analyzer"]["icon_url"], "https://cdn.example/i.png")
        self.assertEqual(source.identifier_icons["evil.one"]["icon_url"], "")
        self.assertEqual([t.name for t in source.tags.all()], ["editor"])
        response = self.client.get(reverse("console:source_edit", args=(source.pk,)))
        self.assertContains(response, 'src="https://cdn.example/i.png"')
        # no placeholder picture for the identifier without icon, no icons in the list
        self.assertNotContains(response, "glyph")

        def icons(url):
            # the pictures on the page, without the one of the user in the header
            content = self.client.get(url).content.decode()
            return content.count("<img") - content.count('class="avatar')

        self.assertEqual(icons(reverse("console:sources")), 0)
        self.assertEqual(icons(reverse("console:source", args=(source.pk,))), 1)

    def test_validation(self):
        response = self.post_source(identifier="", version_pattern="(")
        self.assertContains(response, "Invalid regex")
        self.assertFalse(ReleaseSource.objects.exists())

    def test_catalog_search(self):
        with patch("santa.catalog.search", return_value=[]) as search:
            response = self.client.get(reverse("console:catalog_search"),
                                       {"kind": ReleaseSource.Kind.NPM_PACKAGE, "q": "esbuild"})
        self.assertContains(response, "Nothing found")
        search.assert_called_once_with(ReleaseSource.Kind.NPM_PACKAGE, "esbuild")

    def test_check_and_approve_version(self):
        source = ReleaseSource.objects.create(name="colima", kind=ReleaseSource.Kind.HOMEBREW_FORMULA,
                                              identifier="colima", is_global=True, auto_approve=False)
        version = ReleaseVersion.objects.create(source=source, identifier="colima", version="1",
                                                auto_enable_pending=True)
        rule = Rule.objects.create(rule_type=RuleType.BINARY, identifier=SHA_A, release_source=source,
                                   release_version=version, is_global=True, is_enabled=False)
        self.client.post(reverse("console:version_approve", args=(version.pk,)))
        rule.refresh_from_db()
        version.refresh_from_db()
        self.assertTrue(rule.is_enabled)
        self.assertFalse(version.auto_enable_pending)
        with patch("santa.console.views_sources.sync_release_source", return_value=[]) as sync:
            response = self.client.post(reverse("console:source_check", args=(source.pk,)), follow=True)
        sync.assert_called_once()
        self.assertContains(response, "up to date")


class ConsoleEventsTestCase(ConsoleBase):
    def test_blocked_apps_are_grouped_by_binary(self):
        self.make_event(executing_user="jdoe")
        self.make_event(machine=self.other_machine, executing_user="someone")
        self.make_event(sha256=SHA_B, file_name="other-tool", decision="ALLOW_BINARY")
        response = self.client.get(reverse("console:events"))
        rows = list(response.context["page"])
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["event_count"], rows[0]["machine_count"], rows[0]["user_count"]), (2, 2, 2))
        self.assertNotContains(response, "other-tool")

    def test_live_rows(self):
        first = self.make_event()
        url = reverse("console:event_rows")
        self.assertEqual(self.client.get(url, {"after": first.pk}).status_code, 204)
        new = self.make_event(sha256=SHA_B, file_name="brand-new")
        response = self.client.get(url, {"after": first.pk})
        self.assertContains(response, "brand-new")
        self.assertContains(response, f"after={new.pk}")
        self.assertContains(response, 'hx-swap-oob="true"')
        # the filters of the list apply to the new rows too
        self.assertEqual(self.client.get(url, {"after": first.pk, "group": self.sales.pk}).status_code, 204)
        response = self.client.get(reverse("console:new_events_count"), {"after": first.pk})
        self.assertContains(response, "1 new block")

    def test_allow_several_binaries_with_their_own_tags(self):
        a = self.make_event(signing_id="ABCDE12345:com.example.a", team_id="ABCDE12345")
        self.make_event(machine=self.other_machine, signing_id="ABCDE12345:com.example.a", team_id="ABCDE12345")
        b = self.make_event(sha256=SHA_B, file_name="unsigned", minutes_ago=5)
        url = reverse("console:events_allow")
        response = self.client.post(url, {"shas": [SHA_A], "ids": [b.pk]})
        self.assertEqual(len(response.context["formset"].forms), 2)
        rows = {form.initial["event"]: form for form in response.context["formset"]}
        self.assertEqual(rows[b.pk].initial["rule_type"], RuleType.BINARY)
        self.assertNotIn(RuleType.SIGNINGID, dict(rows[b.pk].fields["rule_type"].choices))
        event_ids = [e.pk for e in response.context["events"]]
        data = {"ids": event_ids, "apply": "1", "groups": [self.dev.pk],
                "rows-TOTAL_FORMS": 2, "rows-INITIAL_FORMS": 2}
        for index, (event, rule_type, scope, tags) in enumerate([
                (a, RuleType.SIGNINGID, "machines", "signed"), (b, RuleType.BINARY, "groups", "unsigned, cli")]):
            data.update({f"rows-{index}-event": event.pk, f"rows-{index}-include": "on",
                         f"rows-{index}-rule_type": rule_type, f"rows-{index}-policy": Policy.ALLOWLIST,
                         f"rows-{index}-scope": scope, f"rows-{index}-new_tags": tags,
                         f"rows-{index}-description": ""})
        response = self.client.post(url, data)
        self.assertRedirects(response, reverse("console:events"))
        signed = Rule.objects.get(rule_type=RuleType.SIGNINGID)
        self.assertEqual({m.pk for m in signed.machines.all()}, {self.machine.pk, self.other_machine.pk})
        self.assertFalse(signed.groups.exists())
        self.assertEqual([t.name for t in signed.tags.all()], ["signed"])
        unsigned = Rule.objects.get(rule_type=RuleType.BINARY)
        self.assertEqual(list(unsigned.groups.all()), [self.dev])
        self.assertEqual({t.name for t in unsigned.tags.all()}, {"unsigned", "cli"})
        # the events are resolved, on both Macs for the machine scoped rule
        self.assertFalse(Event.objects.filter(resolved_at__isnull=True).exists())

    def test_apply_to_all_offers_every_rule_type(self):
        # the first row is an unsigned binary: the "apply to all" select still has the signing ID
        event = self.make_event(sha256=SHA_B, file_name="unsigned")
        response = self.client.post(reverse("console:events_allow"), {"ids": [event.pk]})
        self.assertContains(response, '<select data-apply="rule_type"')
        select = response.content.decode().split('data-apply="rule_type"', 1)[1].split("</select>", 1)[0]
        self.assertIn(f'value="{RuleType.SIGNINGID}"', select)

    def test_create_rule_from_the_drawer(self):
        event = self.make_event()
        response = self.client.get(reverse("console:event", args=(event.pk,)), HTTP_HX_REQUEST="true")
        self.assertContains(response, "Create rule")
        self.assertNotContains(response, "<html")
        self.client.post(reverse("console:event_allow", args=(event.pk,)), {
            "rule_type": RuleType.BINARY, "policy": Policy.ALLOWLIST, "scope": "machines", "next": "https://evil/"})
        rule = Rule.objects.get()
        self.assertEqual(list(rule.machines.all()), [self.machine])

    def test_selected_apps_follow_the_resolved_filter(self):
        event = self.make_event()
        Event.objects.filter(pk=event.pk).update(resolved_at=timezone.now())
        response = self.client.post(reverse("console:events_allow"), {"shas": [SHA_A]})
        self.assertRedirects(response, reverse("console:events"))
        response = self.client.post(reverse("console:events_allow"), {"shas": [SHA_A], "resolved": "all"})
        self.assertEqual(len(response.context["formset"].forms), 1)

    def test_mark_resolved(self):
        self.make_event()
        self.make_event(machine=self.other_machine)
        response = self.client.post(reverse("console:events_resolve"), {"shas": [SHA_A], "next": "https://evil/"})
        self.assertEqual(response.url, reverse("console:events"))
        self.assertFalse(Event.objects.filter(resolved_at__isnull=True).exists())
