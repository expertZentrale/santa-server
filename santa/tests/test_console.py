import json
from datetime import timedelta
from unittest.mock import patch

from django.contrib.admin.models import ADDITION, CHANGE, LogEntry
from django.contrib.auth.models import Group as AuthGroup
from django.contrib.auth.models import Permission, User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from santa.auth import REQUESTERS_GROUP_NAME
from santa.console.forms import CEL_SUGGESTIONS
from santa.models import Event, Group, Machine, Policy, ReleaseSource, ReleaseVersion, Rule, RuleType, Tag
from santa.services import existing_rules, is_allowed

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


class ConsolePaginationTestCase(ConsoleBase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        Rule.objects.bulk_create(Rule(rule_type=RuleType.BINARY, identifier=f"{i:064x}", is_global=True)
                                 for i in range(60))

    def test_page_size_is_chosen_and_remembered(self):
        response = self.client.get(reverse("console:rules"))
        self.assertEqual(len(response.context["page"]), 50)
        response = self.client.get(reverse("console:rules"), {"per_page": 25, "policy": "ALLOWLIST"})
        self.assertEqual(len(response.context["page"]), 25)
        # the links of the page sizes keep the filters and go back to the first page
        self.assertContains(response, 'href="?per_page=100&amp;policy=ALLOWLIST"')
        self.assertContains(response, '<a href="?per_page=25&amp;policy=ALLOWLIST" aria-current="true">25</a>',
                            html=True)
        # every list keeps the size of the session (the rules come back with their last filter)
        response = self.client.get(reverse("console:rules"), follow=True)
        self.assertEqual(response.redirect_chain, [("/console/rules/?policy=ALLOWLIST", 302)])
        self.assertEqual(response.context["page"].paginator.per_page, 25)
        self.assertEqual(self.client.get(reverse("console:machines")).context["page"].paginator.per_page, 25)

    def test_unknown_page_size_is_ignored(self):
        for value in ("7", "abc", "100000"):
            response = self.client.get(reverse("console:rules"), {"per_page": value})
            self.assertEqual(response.context["page"].paginator.per_page, 50, value)

    def test_page_size_choice_is_shown_on_a_single_page(self):
        response = self.client.get(reverse("console:rules"), {"per_page": 100})
        self.assertEqual(response.context["page"].paginator.num_pages, 1)
        self.assertContains(response, "Per page")
        self.assertContains(response, 'aria-disabled="true"', count=2)

    def test_drawer_tools(self):
        rule = Rule.objects.first()
        response = self.client.get(reverse("console:rule", args=[rule.pk]), headers={"HX-Request": "true"})
        self.assertContains(response, 'class="icon-button" data-drawer-close title="Close" aria-label="Close"')
        self.assertContains(response, 'aria-label="Open as page"')


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

    def post_team_rule(self, pk=None, **data):
        url = reverse("console:rule", args=(pk,)) if pk else reverse("console:rule_add")
        return self.client.post(url, {"rule_type": RuleType.TEAMID, "identifier": "UBF8T346G9", "policy": Policy.CEL,
                                      "is_global": "on", "is_enabled": "on", **data})

    def test_signing_id_prefixes(self):
        response = self.post_team_rule(signing_prefixes="com.microsoft.teams2\ncom.microsoft.teams2.helper\n")
        self.assertEqual(response.status_code, 302)
        rule = Rule.objects.get()
        self.assertEqual(rule.cel_expr, '(target.signing_id.startsWith("UBF8T346G9:com.microsoft.teams2") || '
                                        'target.signing_id.startsWith("UBF8T346G9:com.microsoft.teams2.helper")) '
                                        '? ALLOWLIST : BLOCKLIST')
        # editing shows the prefixes again, and a changed list writes the expression again
        response = self.client.get(reverse("console:rule", args=(rule.pk,)))
        self.assertEqual(response.context["form"]["signing_prefixes"].value(),
                         "com.microsoft.teams2\ncom.microsoft.teams2.helper")
        self.post_team_rule(rule.pk, signing_prefixes="com.microsoft.teams2", cel_expr=rule.cel_expr)
        rule.refresh_from_db()
        self.assertEqual(rule.cel_expr,
                         '(target.signing_id.startsWith("UBF8T346G9:com.microsoft.teams2")) ? ALLOWLIST : BLOCKLIST')

    def test_signing_id_prefixes_are_checked(self):
        response = self.post_team_rule(signing_prefixes='com.x") || true || ("')
        self.assertContains(response, "Only letters, digits")
        # an expression written by hand is never replaced
        response = self.post_team_rule(signing_prefixes="com.microsoft.teams2", cel_expr="args.size() < 3")
        self.assertContains(response, "Clear the CEL expression or the prefixes.")
        self.assertFalse(Rule.objects.exists())
        # without prefixes the expression stays as written, also with fields this list doesn't know
        self.post_team_rule(cel_expr='target.new_field == "x" ? ALLOWLIST : BLOCKLIST')
        self.assertEqual(Rule.objects.get().cel_expr, 'target.new_field == "x" ? ALLOWLIST : BLOCKLIST')
        # other rule types ignore the prefixes
        self.client.post(reverse("console:rule_add"), {
            "rule_type": RuleType.SIGNINGID, "identifier": "UBF8T346G9:com.x", "policy": Policy.ALLOWLIST,
            "is_global": "on", "is_enabled": "on", "signing_prefixes": "com.y"})
        self.assertEqual(Rule.objects.get(rule_type=RuleType.SIGNINGID).cel_expr, "")

    def test_cel_suggestions(self):
        response = self.client.get(reverse("console:rule_add"))
        # the hint of existing rules swaps only itself, not the drawer of the form (that emptied the drawer)
        self.assertContains(response, 'hx-include="closest form" hx-target="this"')
        self.assertContains(response, "data-cel-suggestions=")
        self.assertContains(response, "target.signing_id")
        self.assertContains(self.client.get(reverse("console:source_add")), "data-cel-suggestions=")

    def test_list_shows_the_full_identifier(self):
        long_id = "UBF8T346G9:com.microsoft.teams2.notificationcenter.helper"
        Rule.objects.create(rule_type=RuleType.SIGNINGID, identifier=long_id, is_global=True, description="teams")
        response = self.client.get(reverse("console:rules"))
        self.assertContains(response, '<span class="muted">UBF8T346G9:</span>' + long_id.split(":")[1])
        self.assertContains(response, 'data-table="rules" data-optional="comment created"')
        self.assertContains(response, '<th data-col="identifier"')
        self.assertContains(response, '<td data-col="created"')

    def test_export_for_read_only_users(self):
        # the export needs the same permission as the list, not the one to add rules
        viewer = User.objects.create_user("reader", is_staff=True)
        viewer.user_permissions.add(Permission.objects.get(codename="view_rule"))
        self.client.force_login(viewer)
        response = self.client.get(reverse("console:rules"))
        self.assertContains(response, reverse("console:rules_export"))
        self.assertNotContains(response, reverse("console:rule_upload"))
        self.assertEqual(self.client.get(reverse("console:rules_export")).status_code, 200)

    def test_cel_global_functions(self):
        kinds = {text: kind for text, kind, _help in CEL_SUGGESTIONS}
        # timestamp() and duration() start a value, startsWith() follows one
        self.assertEqual((kinds['timestamp("")'], kinds['duration("")'], kinds['startsWith("")']),
                         ("global", "global", "function"))

    def test_export_csv(self):
        Rule.objects.create(rule_type=RuleType.BINARY, identifier=SHA_A, is_global=True, description="=HYPERLINK()")
        blocked = Rule.objects.create(rule_type=RuleType.TEAMID, identifier="ABCDE12345", policy=Policy.BLOCKLIST)
        blocked.groups.add(self.dev)
        Rule.objects.bulk_create(Rule(rule_type=RuleType.BINARY, identifier=f"{i:064x}", is_global=True)
                                 for i in range(60))
        response = self.client.get(reverse("console:rules_export"))
        self.assertEqual(response["Content-Type"], "text/csv; charset=utf-8")
        content = response.content.decode("utf-8")
        self.assertTrue(content.startswith("﻿Type,Identifier,Policy"))
        # all pages, and no formulas for the spreadsheet
        self.assertEqual(len(content.strip().splitlines()), 63)
        self.assertIn("'=HYPERLINK()", content)
        # the filters of the list
        content = self.client.get(reverse("console:rules_export"), {"policy": Policy.BLOCKLIST}).content.decode()
        self.assertEqual(len(content.strip().splitlines()), 2)
        self.assertIn("ABCDE12345,Block,Development", content)
        self.client.force_login(User.objects.create_user("viewer", is_staff=True))
        self.assertEqual(self.client.get(reverse("console:rules_export")).status_code, 403)

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

    def test_bulk_set_policy(self):
        manual = Rule.objects.create(rule_type=RuleType.SIGNINGID, identifier="ABCDE12345:com.example.tool",
                                     is_global=True, policy=Policy.CEL, cel_expr="ALLOWLIST")
        source = ReleaseSource.objects.create(name="colima", kind=ReleaseSource.Kind.HOMEBREW_FORMULA,
                                              identifier="colima", is_global=True)
        package = Rule.objects.create(rule_type=RuleType.BINARY, identifier=SHA_A, release_source=source,
                                      is_global=True)
        response = self.client.post(reverse("console:rules_bulk"), {
            "action": "set_policy", "policy": Policy.SILENT_BLOCKLIST, "rules": [manual.pk, package.pk]}, follow=True)
        self.assertContains(response, "1 rule set to “Block silently”.")
        self.assertContains(response, "change the policy there")
        manual.refresh_from_db()
        package.refresh_from_db()
        self.assertEqual((manual.policy, manual.cel_expr), (Policy.SILENT_BLOCKLIST, ""))
        self.assertEqual(package.policy, Policy.ALLOWLIST)
        self.assertTrue(LogEntry.objects.filter(object_id=str(manual.pk), change_message__contains="Policy").exists())
        # CEL needs an expression, it is not offered
        response = self.client.post(reverse("console:rules_bulk"), {
            "action": "set_policy", "policy": Policy.CEL, "rules": [manual.pk]}, follow=True)
        manual.refresh_from_db()
        self.assertEqual(manual.policy, Policy.SILENT_BLOCKLIST)
        self.assertNotContains(self.client.get(reverse("console:rules")), '<option value="CEL">', html=False)

    def test_rule_form(self):
        response = self.client.get(reverse("console:rule_add"))
        # Santa shows the message and the URL only in the block dialog, a silent block has none
        self.assertContains(response, 'data-show-when="policy=BLOCKLIST|CEL"')
        # the tags become chips (console/tags.js), with the translated texts
        self.assertContains(response, 'data-placeholder="Add a tag, Enter creates a new one"')

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
                                                auto_enable_pending=True, binary_count=1)
        rule = Rule.objects.create(rule_type=RuleType.BINARY, identifier=SHA_A, release_source=source,
                                   release_version=version, is_global=True, is_enabled=False)
        self.client.post(reverse("console:version_approve", args=(version.pk,)))
        rule.refresh_from_db()
        version.refresh_from_db()
        self.assertTrue(rule.is_enabled)
        self.assertFalse(version.auto_enable_pending)
        # an enabled version can only be disabled, a disabled one only approved
        approve_url = reverse("console:version_approve", args=(version.pk,))
        disable_url = reverse("console:version_disable", args=(version.pk,))
        response = self.client.get(reverse("console:source", args=(source.pk,)))
        self.assertContains(response, disable_url)
        self.assertNotContains(response, approve_url)
        self.client.post(disable_url)
        response = self.client.get(reverse("console:source", args=(source.pk,)))
        self.assertContains(response, approve_url)
        self.assertNotContains(response, disable_url)
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

    def test_unknown_in_monitor_mode_is_listed_with_the_blocks(self):
        # monitor mode: unknown binaries run (ALLOW_UNKNOWN), lockdown would block them
        unknown = self.make_event(sha256=SHA_B, file_name="unknown-tool", decision="ALLOW_UNKNOWN")
        self.make_event(decision="BLOCK_UNKNOWN")
        self.make_event(sha256="c" * 64, file_name="allowed-tool", decision="ALLOW_BINARY")
        response = self.client.get(reverse("console:events"))
        rows = {row["file_sha256"]: row for row in response.context["page"]}
        self.assertEqual(set(rows), {SHA_A, SHA_B})
        self.assertEqual((rows[SHA_A]["block_count"], rows[SHA_B]["block_count"]), (1, 0))
        self.assertContains(response, "Monitor: blocked in lockdown", count=1)
        self.assertNotContains(response, "allowed-tool")
        self.assertEqual(
            self.client.get(reverse("console:event", args=(unknown.pk,))).context["event"].blocked_in_lockdown, True)
        # counted as open blocks of the Mac and its group
        machine = self.client.get(reverse("console:machine", args=(self.machine.pk,)))
        self.assertEqual(machine.context["open_blocks"], 2)
        self.assertEqual(self.client.get(reverse("console:group", args=(self.dev.pk,))).context["open_blocks"], 2)
        # selected and resolved like a block
        self.client.post(reverse("console:events_resolve"), {"shas": [SHA_B]})
        unknown.refresh_from_db()
        self.assertIsNotNone(unknown.resolved_at)
        # live updates of the list
        new = self.make_event(sha256="d" * 64, file_name="new-unknown", decision="ALLOW_UNKNOWN")
        response = self.client.get(reverse("console:event_updates"), {"after": new.pk - 1})
        self.assertEqual([row["file_sha256"] for row in response.context["rows"]], ["d" * 64])

    def test_live_updates(self):
        first = self.make_event()
        url = reverse("console:event_updates")
        self.assertEqual(self.client.get(url, {"after": first.pk}).status_code, 204)
        new = self.make_event(sha256=SHA_B, file_name="brand-new")
        self.make_event(file_name="colima", minutes_ago=1)
        allowed = self.make_event(sha256="c" * 64, file_name="allowed", decision="ALLOW_BINARY")
        # "All events": every new event, with its key and the new position
        response = self.client.get(url, {"after": first.pk, "view": "all"})
        self.assertContains(response, f'data-max-pk="{allowed.pk}"')
        self.assertContains(response, f'data-key="{new.pk}"')
        self.assertContains(response, f'data-key="{allowed.pk}"')
        # the filters of the list apply
        response = self.client.get(url, {"after": first.pk, "view": "all", "group": self.sales.pk})
        self.assertNotContains(response, "data-key")
        # "Blocked apps": the rows of the binaries with new blocks, with all their blocks
        response = self.client.get(url, {"after": first.pk})
        rows = {row["file_sha256"]: row for row in response.context["rows"]}
        self.assertEqual(set(rows), {SHA_A, SHA_B})
        self.assertEqual(rows[SHA_A]["event_count"], 2)
        self.assertContains(response, f'data-key="{SHA_B}"')
        # the list polls it, in the default order new rows are added on top
        response = self.client.get(reverse("console:events"), {"view": "all"})
        self.assertContains(response, "data-live-updates")
        self.assertTrue(response.context["live_insert"])
        self.assertFalse(self.client.get(reverse("console:events"), {"sort": "program"}).context["live_insert"])
        self.client.force_login(User.objects.create_user("viewer", is_staff=True))
        self.assertEqual(self.client.get(url, {"after": first.pk}).status_code, 403)

    def test_relations_open_in_the_drawer(self):
        event = self.make_event(signing_id="ABCDE12345:com.example.a", team_id="ABCDE12345")
        rule = Rule.objects.create(rule_type=RuleType.SIGNINGID, identifier="ABCDE12345:com.example.a", is_global=True)
        rule_url = reverse("console:rule", args=(rule.pk,))
        machine_url = reverse("console:machine", args=(self.machine.pk,))
        group_url = reverse("console:group", args=(self.dev.pk,))
        response = self.client.get(reverse("console:event", args=(event.pk,)), headers={"HX-Request": "true"})
        for url in (rule_url, machine_url, group_url):
            self.assertContains(response, f'href="{url}" hx-get="{url}" hx-target="#drawer"')
        response = self.client.post(reverse("console:events_create_rules"), {"ids": [event.pk]},
                                    headers={"HX-Request": "true"})
        self.assertContains(response, f'href="{rule_url}" hx-get="{rule_url}" hx-target="#drawer"')
        # the Mac and the group as drawers, and as pages without JS
        for url in (machine_url, group_url):
            self.assertNotContains(self.client.get(url, headers={"HX-Request": "true"}), "<html")
            self.assertContains(self.client.get(url), "<html")
        response = self.client.get(machine_url, headers={"HX-Request": "true"})
        self.assertContains(response, f'data-drawer-url="{machine_url}"')

    def test_group_saves_in_the_drawer(self):
        data = {"name": "Development", "description": "", "client_mode": "MONITOR", "batch_size": 100,
                "full_sync_interval": 600, "removable_media_action": "ALLOW",
                "override_file_access_action": "NONE"}
        response = self.client.post(reverse("console:group", args=(self.dev.pk,)), data,
                                    headers={"HX-Request": "true"})
        if response.status_code == 200:
            self.fail(response.context["form"].errors)
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response["HX-Trigger"], "drawerSaved")

    def create_rules(self, events, htmx=True, **data):
        data = {"ids": [e.pk for e in events], "apply": "1", "policy": Policy.ALLOWLIST, "scope": "groups",
                "groups": [self.dev.pk], "rule_type": "", **data}
        headers = {"HX-Request": "true"} if htmx else {}
        return self.client.post(reverse("console:events_create_rules"), data, headers=headers)

    def test_create_rules_for_several_binaries(self):
        a = self.make_event(signing_id="ABCDE12345:com.example.a", team_id="ABCDE12345")
        other = self.make_event(machine=self.other_machine, signing_id="ABCDE12345:com.example.a",
                                team_id="ABCDE12345")
        b = self.make_event(sha256=SHA_B, file_name="unsigned", minutes_ago=5)
        # the bulk bar opens the form in the drawer
        response = self.client.post(reverse("console:events_create_rules"), {"shas": [SHA_A], "ids": [b.pk]},
                                    headers={"HX-Request": "true"})
        self.assertNotContains(response, "<html")
        self.assertContains(response, "Suggested per binary")
        self.assertEqual(len(response.context["binaries"]), 2)
        # suggested per binary: a signing ID rule and a binary rule, both on the Macs of their events
        response = self.create_rules([a, other, b], scope="machines", include=[SHA_A, SHA_B], new_tags="cli")
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response["HX-Trigger"], "drawerSaved")
        signed = Rule.objects.get(rule_type=RuleType.SIGNINGID)
        self.assertEqual({m.pk for m in signed.machines.all()}, {self.machine.pk, self.other_machine.pk})
        self.assertFalse(signed.groups.exists())
        unsigned = Rule.objects.get(rule_type=RuleType.BINARY)
        self.assertEqual([t.name for t in unsigned.tags.all()], ["cli"])
        self.assertEqual(unsigned.description, "unsigned (/opt/bin/unsigned)")
        self.assertFalse(Event.objects.filter(resolved_at__isnull=True).exists())

    def test_rule_type_falls_back_and_the_rest_stays_in_the_drawer(self):
        a = self.make_event(signing_id="ABCDE12345:com.example.a", team_id="ABCDE12345")
        b = self.make_event(sha256=SHA_B, file_name="unsigned", minutes_ago=5)
        c = self.make_event(sha256="c" * 64, file_name="left out", minutes_ago=10)
        # a signing ID for all: the unsigned binary gets a binary rule; the unchecked one stays in the drawer
        response = self.create_rules([a, b, c], rule_type=RuleType.SIGNINGID, include=[SHA_A, SHA_B])
        self.assertEqual(response.status_code, 200)
        self.assertEqual({r.rule_type for r in Rule.objects.all()}, {RuleType.SIGNINGID, RuleType.BINARY})
        self.assertEqual([binary["event"] for binary in response.context["binaries"]], [c])
        self.assertContains(response, "1 binary left")
        self.assertContains(response, "data-refresh-on-close")
        # the second decision for the rest: block it silently
        response = self.create_rules([c], rule_type=RuleType.BINARY, include=["c" * 64],
                                     policy=Policy.SILENT_BLOCKLIST, scope="global", saved="1")
        self.assertEqual(response.status_code, 204)
        blocked = Rule.objects.get(identifier="c" * 64)
        self.assertEqual((blocked.policy, blocked.is_global), (Policy.SILENT_BLOCKLIST, True))
        c.refresh_from_db()
        self.assertEqual(c.resolution_rule, blocked)

    def test_create_rules_errors(self):
        a = self.make_event()
        response = self.create_rules([a], groups=[], include=[SHA_A])
        self.assertContains(response, "Choose at least one group.")
        response = self.create_rules([a], include=[])
        self.assertContains(response, "Choose at least one binary.")
        response = self.create_rules([a], include=[SHA_A], policy=Policy.CEL)
        self.assertContains(response, "Required for the CEL policy.")
        self.assertFalse(Rule.objects.exists())

    def test_create_cel_rules_from_events(self):
        a = self.make_event()
        b = self.make_event(sha256=SHA_B, file_name="other", minutes_ago=5)
        expression = "target.signing_time >= timestamp('2025-01-01T00:00:00Z') ? ALLOWLIST : BLOCKLIST"
        response = self.create_rules([a, b], include=[SHA_A, SHA_B], policy=Policy.CEL, cel_expr=f" {expression} ")
        self.assertEqual(response.status_code, 204)
        self.assertEqual(sorted(Rule.objects.values_list("policy", "cel_expr")), [(Policy.CEL, expression)] * 2)
        # another expression for the same binary is another rule, the same one widens the rule
        one = {"include": [SHA_A], "rule_type": RuleType.BINARY, "policy": Policy.CEL}
        self.create_rules([a], cel_expr="ALLOWLIST", scope="global", **one)
        self.create_rules([a], cel_expr=expression, groups=[self.sales.pk], **one)
        rules = Rule.objects.filter(identifier=SHA_A).order_by("pk")
        self.assertEqual([r.cel_expr for r in rules], [expression, "ALLOWLIST"])
        self.assertEqual({g.name for g in rules[0].groups.all()}, {"Development", "Sales"})
        # an expression left over from a CEL choice is dropped for other policies
        self.create_rules([b], include=[SHA_B], rule_type=RuleType.BINARY, policy=Policy.BLOCKLIST,
                          cel_expr="ALLOWLIST", scope="global")
        self.assertEqual(Rule.objects.get(identifier=SHA_B, policy=Policy.BLOCKLIST).cel_expr, "")

    def test_team_id_rules_with_signing_id_prefixes_from_events(self):
        a = self.make_event(signing_id="ABCDE12345:com.example.a", team_id="ABCDE12345")
        b = self.make_event(sha256=SHA_B, file_name="other team", minutes_ago=5,
                            signing_id="ZYXWV98765:com.other.b", team_id="ZYXWV98765")
        unsigned = self.make_event(sha256="c" * 64, file_name="unsigned", minutes_ago=10)
        prefixes = {"rule_type": RuleType.TEAMID, "policy": Policy.CEL, "signing_prefixes": "com.example\ncom.other"}
        # the binaries share no Team ID: the expression is written per binary
        response = self.client.post(reverse("console:events_create_rules"), {"ids": [a.pk, b.pk]},
                                    headers={"HX-Request": "true"})
        self.assertNotContains(response, "data-team-id")
        response = self.create_rules([a, b, unsigned], include=[SHA_A, SHA_B, "c" * 64], **prefixes)
        self.assertContains(response, "No Team ID, so no signing ID prefixes: unsigned")
        response = self.create_rules([a, b], include=[SHA_A, SHA_B], signing_prefixes="com.ex ample",
                                     rule_type=RuleType.TEAMID, policy=Policy.CEL)
        self.assertContains(response, "Only letters, digits, dots, hyphens and underscores")
        response = self.create_rules([a, b], include=[SHA_A, SHA_B], cel_expr="ALLOWLIST", **prefixes)
        self.assertContains(response, "Clear the CEL expression or the prefixes.")
        self.assertFalse(Rule.objects.exists())
        response = self.create_rules([a, b], include=[SHA_A, SHA_B], **prefixes)
        self.assertEqual(response.status_code, 204)
        self.assertEqual(dict(Rule.objects.values_list("identifier", "cel_expr")), {
            team: f'(target.signing_id.startsWith("{team}:com.example") || '
                  f'target.signing_id.startsWith("{team}:com.other")) ? ALLOWLIST : BLOCKLIST'
            for team in ("ABCDE12345", "ZYXWV98765")})
        # one binary: the drawer knows its Team ID and shows the expression while typing
        response = self.client.get(reverse("console:event", args=(unsigned.pk,)), HTTP_HX_REQUEST="true")
        self.assertNotContains(response, "data-team-id")
        response = self.client.get(reverse("console:event", args=(a.pk,)), HTTP_HX_REQUEST="true")
        self.assertContains(response, 'data-team-id="ABCDE12345"')
        self.assertContains(response, 'name="signing_prefixes"')

    def test_create_cel_rule_from_the_drawer(self):
        event = self.make_event()
        response = self.client.get(reverse("console:event", args=(event.pk,)), HTTP_HX_REQUEST="true")
        self.assertContains(response, 'value="CEL"')
        self.assertContains(response, "data-cel-suggestions")
        self.client.post(reverse("console:event_create_rule", args=(event.pk,)), {
            "rule_type": RuleType.BINARY, "policy": Policy.CEL, "cel_expr": "ALLOWLIST", "scope": "machines",
            "include": SHA_A})
        self.assertEqual(Rule.objects.get().cel_expr, "ALLOWLIST")

    def test_blocked_app_row_links_the_rule_and_shows_the_whole_path(self):
        path = "/Applications/Microsoft Teams.app/Contents/XPCServices/com.microsoft.teams2.notificationcenter.xpc"
        Event.objects.create(machine=self.machine, group=self.dev, execution_time=timezone.now(),
                             decision="BLOCK_UNKNOWN", file_sha256=SHA_A, file_name="teams", file_path=path)
        rule = Rule.objects.create(rule_type=RuleType.BINARY, identifier=SHA_A, policy=Policy.BLOCKLIST,
                                   is_global=True)
        response = self.client.get(reverse("console:events"))
        rule_url = reverse("console:rule", args=(rule.pk,))
        self.assertContains(response, f'href="{rule_url}" hx-get="{rule_url}" hx-target="#drawer"')
        self.assertContains(response, f">{path}</small>")
        self.assertContains(response, '<td class="row-actions">')

    def test_create_rules_without_js(self):
        a = self.make_event()
        response = self.client.post(reverse("console:events_create_rules"), {"ids": [a.pk]})
        self.assertContains(response, "<html")
        self.assertContains(response, 'value="BLOCKLIST"')
        response = self.create_rules([a], htmx=False, include=[SHA_A], rule_type=RuleType.BINARY)
        self.assertRedirects(response, reverse("console:events"))
        self.assertTrue(Rule.objects.exists())

    def test_create_rule_from_the_drawer(self):
        event = self.make_event()
        response = self.client.get(reverse("console:event", args=(event.pk,)), HTTP_HX_REQUEST="true")
        self.assertContains(response, "Create rule")
        self.assertContains(response, 'value="SILENT_BLOCKLIST"')
        self.assertNotContains(response, "Suggested per binary")
        self.assertNotContains(response, "<html")
        # in the action bar, which stays at the bottom of the drawer like in the other forms
        self.assertContains(response, '<div class="actions"><button class="button primary">Create rule</button></div>',
                            html=True)
        self.client.post(reverse("console:event_create_rule", args=(event.pk,)), {
            "rule_type": RuleType.BINARY, "policy": Policy.BLOCKLIST, "scope": "machines", "include": SHA_A,
            "next": "https://evil/"})
        rule = Rule.objects.get()
        self.assertEqual(list(rule.machines.all()), [self.machine])
        self.assertEqual(rule.policy, Policy.BLOCKLIST)

    def test_existing_rules(self):
        signed = self.make_event(signing_id="ABCDE12345:com.example.a", team_id="ABCDE12345")
        other = self.make_event(sha256=SHA_B, machine=self.other_machine, team_id="ABCDE12345")
        allow = Rule.objects.create(rule_type=RuleType.SIGNINGID, identifier="ABCDE12345:com.example.a",
                                    is_global=True)
        team = Rule.objects.create(rule_type=RuleType.TEAMID, identifier="ABCDE12345", policy=Policy.BLOCKLIST)
        team.groups.add(self.dev)
        disabled = Rule.objects.create(rule_type=RuleType.BINARY, identifier=SHA_B, is_global=True, is_enabled=False)
        matches = existing_rules([signed, other])
        # the most specific rule type first, "applies" for the Macs of the events
        self.assertEqual([(m["rule"], m["applies"]) for m in matches[SHA_A]], [(allow, True), (team, True)])
        self.assertEqual([(m["rule"], m["applies"]) for m in matches[SHA_B]], [(disabled, False), (team, False)])
        self.assertTrue(is_allowed(matches[SHA_A]))
        self.assertFalse(is_allowed(matches[SHA_B]))
        # many events: chunked for SQL Server
        many = [Event(machine=self.machine, group=self.dev, execution_time=timezone.now(), decision="BLOCK_UNKNOWN",
                      file_sha256=f"{i:064x}", signing_id=f"ABCDE12345:com.example.{i}") for i in range(1500)]
        self.assertEqual(len(existing_rules(many)), 1500)

    def test_existing_rules_are_shown(self):
        event = self.make_event(signing_id="ABCDE12345:com.example.a", team_id="ABCDE12345")
        unsigned = self.make_event(sha256=SHA_B, file_name="unsigned")
        Rule.objects.create(rule_type=RuleType.SIGNINGID, identifier="ABCDE12345:com.example.a", is_global=True)
        response = self.client.get(reverse("console:event", args=(event.pk,)), HTTP_HX_REQUEST="true")
        self.assertContains(response, "There are rules for this binary already")
        # several binaries: the allowed one is shown, but not checked
        response = self.client.post(reverse("console:events_create_rules"), {"ids": [event.pk, unsigned.pk]},
                                    headers={"HX-Request": "true"})
        self.assertContains(response, "Already:")
        self.assertEqual(response.context["form"]["include"].value(), [SHA_B])
        self.assertContains(self.client.get(reverse("console:events")), "Rule exists")
        # "New rule": the rules with the typed identifier
        url = reverse("console:rules_existing")
        response = self.client.get(url, {"rule_type": RuleType.SIGNINGID, "identifier": "abcde12345:com.example.a"})
        self.assertContains(response, "There is a rule for this identifier already")
        self.assertNotContains(self.client.get(url, {"rule_type": RuleType.TEAMID, "identifier": "ABCDE12345"}),
                               "already")
        self.client.force_login(User.objects.create_user("viewer", is_staff=True))
        self.assertEqual(self.client.get(url, {"rule_type": RuleType.TEAMID, "identifier": "x"}).status_code, 403)

    def test_groups_are_the_default_scope(self):
        event = self.make_event()
        response = self.client.get(reverse("console:event", args=(event.pk,)), HTTP_HX_REQUEST="true")
        self.assertEqual(response.context["form"]["scope"].value(), "groups")
        response = self.client.post(reverse("console:events_create_rules"), {"ids": [event.pk]})
        self.assertEqual(response.context["form"]["scope"].value(), "groups")

    def test_the_drawer_follows_the_selection(self):
        a = self.make_event(signing_id="ABCDE12345:com.example.a", team_id="ABCDE12345")
        b = self.make_event(sha256=SHA_B, file_name="unsigned")
        c = self.make_event(sha256="c" * 64, file_name="third")
        # b was unchecked by hand, c is new in the selection: checked; the entered values stay
        response = self.create_rules([a, b, c], refresh="1", known=[SHA_A, SHA_B], include=[SHA_A],
                                     policy=Policy.BLOCKLIST, new_tags="later", apply="")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Rule.objects.exists())
        form = response.context["form"]
        self.assertEqual(sorted(form["include"].value()), sorted([SHA_A, "c" * 64]))
        self.assertEqual(form["policy"].value(), Policy.BLOCKLIST)
        self.assertEqual(form["new_tags"].value(), "later")
        self.assertFalse(form.errors)
        # nothing selected anymore
        response = self.client.post(reverse("console:events_create_rules"), {"refresh": "1"},
                                    headers={"HX-Request": "true"})
        self.assertContains(response, "Select events in the list.")

    def test_selected_apps_follow_the_resolved_filter(self):
        event = self.make_event()
        Event.objects.filter(pk=event.pk).update(resolved_at=timezone.now())
        response = self.client.post(reverse("console:events_create_rules"), {"shas": [SHA_A]})
        self.assertRedirects(response, reverse("console:events"))
        response = self.client.post(reverse("console:events_create_rules"), {"shas": [SHA_A], "resolved": "all"})
        self.assertEqual(len(response.context["binaries"]), 1)

    def test_status_filter_with_several_values(self):
        self.make_event(SHA_A)
        resolved = self.make_event(SHA_B, file_name="docker")
        Event.objects.filter(pk=resolved.pk).update(resolved_at=timezone.now())
        url = reverse("console:events")

        def names(**params):
            response = self.client.get(url, {"view": "all", "q": "", **params})
            return sorted(event.file_name for event in response.context["page"]), response.context["resolved"]

        # open by default, before the form was sent
        response = self.client.get(url, {"view": "all"})
        self.assertEqual([event.file_name for event in response.context["page"]], ["colima"])
        self.assertEqual(names(resolved="open"), (["colima"], "open"))
        self.assertEqual(names(resolved="resolved"), (["docker"], "resolved"))
        # both, none (the × of the chip), and the old "all" of saved views
        for params in ({"resolved": ["open", "resolved"]}, {"resolved": ""}, {"resolved": "all"}):
            self.assertEqual(names(**params), (["colima", "docker"], "all"), params)

    def test_mark_resolved(self):
        self.make_event()
        self.make_event(machine=self.other_machine)
        response = self.client.post(reverse("console:events_resolve"), {"shas": [SHA_A], "next": "https://evil/"})
        self.assertEqual(response.url, reverse("console:events"))
        self.assertFalse(Event.objects.filter(resolved_at__isnull=True).exists())
