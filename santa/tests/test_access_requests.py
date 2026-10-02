import json
from datetime import timedelta
from unittest.mock import patch

from django.contrib.admin.models import LogEntry
from django.contrib.auth.models import Permission, User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone

from santa.catalog import Suggestion
from santa.models import AccessRequest, AccessRequestPackage, Event, Policy, ReleaseSource, Rule, RuleType
from santa.services import machines_for_user

from .test_console import SHA_A, SHA_B, ConsoleBase
from .utils import build_macho


class UserRequestTestCase(ConsoleBase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)

    def test_machines_of_the_user(self):
        self.assertEqual(list(machines_for_user(self.user)), [self.machine])
        self.machine.primary_user = "JDoe"
        self.machine.save()
        self.assertEqual(list(machines_for_user(self.user)), [self.machine])

    def test_only_blocks_of_my_macs_are_offered(self):
        mine = self.make_event(file_name="mine")
        self.make_event(sha256=SHA_B, file_name="not-mine", machine=self.other_machine)
        response = self.client.get(reverse("requests:new"), {"sha256": SHA_A})
        self.assertContains(response, "mine")
        self.assertNotContains(response, "not-mine")
        self.assertEqual(response.context["form"].initial["event"], mine)

    def test_request_a_blocked_event(self):
        event = self.make_event(bundle_name="Colima")
        response = self.client.post(reverse("requests:new"), {"kind": "EVENT", "event": event.pk,
                                                              "justification": "I need containers"})
        self.assertRedirects(response, reverse("requests:list"))
        access_request = AccessRequest.objects.get()
        self.assertEqual((access_request.title, access_request.machine, access_request.file_sha256),
                         ("Colima", self.machine, SHA_A))
        # requested: not offered any more
        self.assertNotIn(event, self.client.get(reverse("requests:new")).context["form"].fields["event"].queryset)
        response = self.client.post(reverse("requests:new"), {"kind": "EVENT", "event": event.pk,
                                                              "justification": "again"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(AccessRequest.objects.count(), 1)

    def test_requested_binaries_are_not_offered_again(self):
        event = self.make_event()
        access_request = AccessRequest.objects.create(requester=self.user, kind="EVENT", file_sha256=SHA_A,
                                                      title="colima", justification="x")
        for status in ("PENDING", "APPROVED", "DENIED", "CANCELLED"):
            access_request.status = status
            access_request.save()
            events = self.client.get(reverse("requests:new")).context["form"].fields["event"].queryset
            self.assertEqual(event in events, status == "CANCELLED", status)

    def test_requests_of_others_are_shown_without_the_requester(self):
        self.make_event()
        colleague = User.objects.create_user("colleague@example.com")
        other = AccessRequest.objects.create(requester=colleague, kind="EVENT", file_sha256=SHA_A, title="colima",
                                             justification="x", status=AccessRequest.Status.DENIED)
        AccessRequest.objects.create(requester=colleague, kind="EVENT", file_sha256=SHA_A, title="colima",
                                     justification="x", status=AccessRequest.Status.CANCELLED)
        response = self.client.get(reverse("requests:new"))
        date = timezone.localtime(other.created_at).strftime("%d.%m.%Y")
        self.assertContains(response, f'<span class="badge bad">Requested on {date} · Denied</span>')
        self.assertNotContains(response, "colleague")

    def test_someone_elses_event_cannot_be_requested(self):
        event = self.make_event(machine=self.other_machine)
        response = self.client.post(reverse("requests:new"), {"kind": "EVENT", "event": event.pk,
                                                              "justification": "x"})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(AccessRequest.objects.exists())

    def post_packages(self, packages, justification="I need them"):
        return self.client.post(reverse("requests:new"), {"kind": "PACKAGE", "packages": json.dumps(packages),
                                                          "justification": justification})

    def test_several_packages_in_one_request(self):
        response = self.post_packages([
            {"kind": "VSCODE_EXTENSION", "identifier": " rust-lang.rust-analyzer ", "name": "rust-analyzer",
             "icon_url": "https://cdn.example/ra.png"},
            {"kind": "NPM_PACKAGE", "identifier": "esbuild", "icon_url": "javascript:alert(1)"},
            {"kind": "NPM_PACKAGE", "identifier": "esbuild"},
            {"kind": "URL", "identifier": "https://not.requestable"},
        ])
        self.assertRedirects(response, reverse("requests:list"))
        access_request = AccessRequest.objects.get()
        self.assertEqual(access_request.title, "2 packages: rust-analyzer, esbuild")
        packages = list(access_request.packages.values_list("kind", "identifier", "icon_url"))
        self.assertEqual(packages, [("VSCODE_EXTENSION", "rust-lang.rust-analyzer", "https://cdn.example/ra.png"),
                                    ("NPM_PACKAGE", "esbuild", "")])
        # esbuild is asked for already
        response = self.post_packages([{"kind": "NPM_PACKAGE", "identifier": "esbuild"},
                                       {"kind": "NPM_PACKAGE", "identifier": "vite"}])
        self.assertContains(response, "already asked for esbuild")
        # the chips are shown again after an error
        self.assertContains(response, "vite")
        self.assertContains(self.post_packages([]), "Add at least one package")
        self.assertContains(self.client.get(reverse("requests:list")), "rust-analyzer")

    def test_allowed_packages_cannot_be_requested(self):
        ReleaseSource.objects.create(name="dev tools", kind="NPM_PACKAGE", identifier="vite\nesbuild")
        ReleaseSource.objects.get().groups.set([self.dev])
        response = self.post_packages([{"kind": "NPM_PACKAGE", "identifier": "esbuild"},
                                       {"kind": "NPM_PACKAGE", "identifier": "left-pad"}])
        self.assertContains(response, "esbuild: already allowed on your Mac")
        self.assertFalse(AccessRequest.objects.exists())

    def search(self, kind, *identifiers):
        suggestions = [Suggestion(identifier=identifier) for identifier in identifiers]
        with patch("santa.catalog.search", return_value=suggestions):
            return self.client.get(reverse("requests:catalog_search"), {"kind": kind, "q": "x"})

    def test_catalog_search_marks_allowed_and_requested_packages(self):
        ReleaseSource.objects.create(name="global", kind="NPM_PACKAGE", identifier="esbuild", is_global=True)
        ReleaseSource.objects.create(name="dev", kind="NPM_PACKAGE", identifier="vite").groups.set([self.dev])
        sales = ReleaseSource.objects.create(name="sales", kind="NPM_PACKAGE", identifier="sales-tool")
        sales.groups.set([self.sales])
        ReleaseSource.objects.create(name="off", kind="NPM_PACKAGE", identifier="off-tool", is_global=True,
                                     is_enabled=False)
        ReleaseSource.objects.create(name="blocked", kind="NPM_PACKAGE", identifier="bad-tool", is_global=True,
                                     policy=Policy.BLOCKLIST)
        ReleaseSource.objects.create(name="other kind", kind="HOMEBREW_FORMULA", identifier="left-pad",
                                     is_global=True)
        colleague = User.objects.create_user("colleague@example.com")
        requested = AccessRequest.objects.create(requester=colleague, kind="PACKAGE", title="t", justification="x")
        AccessRequestPackage.objects.create(access_request=requested, kind="NPM_PACKAGE", identifier="left-pad")
        # the same name in another catalog is another package
        AccessRequestPackage.objects.create(access_request=requested, kind="HOMEBREW_FORMULA", identifier="vite")
        response = self.search("NPM_PACKAGE", "esbuild", "vite", "sales-tool", "off-tool", "bad-tool", "left-pad")
        self.assertEqual([s.identifier for s in response.context["suggestions"] if s.requested], ["left-pad"])
        allowed = {s.identifier: s.allowed for s in response.context["suggestions"]}
        self.assertEqual(allowed, {"esbuild": True, "vite": True, "sales-tool": False, "off-tool": False,
                                   "bad-tool": False, "left-pad": False})
        self.assertContains(response, 'data-identifier="esbuild" data-name="" data-icon="" aria-disabled="true"')
        date = timezone.localtime(requested.created_at).strftime("%d.%m.%Y")
        self.assertContains(response, f'<span class="badge info">Requested on {date} · Pending</span>')
        self.assertNotContains(response, "colleague")
        # someone else's request: still selectable
        self.assertContains(response, 'data-identifier="left-pad" data-name="" data-icon="">')
        # no room kept for a missing icon
        self.assertNotContains(response, "icon-space")
        # the own open request: grayed out
        own = AccessRequest.objects.create(requester=self.user, kind="PACKAGE", title="t", justification="x")
        AccessRequestPackage.objects.create(access_request=own, kind="NPM_PACKAGE", identifier="left-pad")
        response = self.search("NPM_PACKAGE", "left-pad")
        self.assertContains(response, 'data-identifier="left-pad" data-name="" data-icon="" aria-disabled="true"')
        self.assertContains(response, "You requested this on")
        own.status = AccessRequest.Status.CANCELLED
        own.save()
        # cancelled requests don't count
        requested.status = AccessRequest.Status.CANCELLED
        requested.save()
        self.assertNotContains(self.search("NPM_PACKAGE", "left-pad"), "Already requested")

    def test_my_requests_by_time_span(self):
        ages = {"today": 0, "last week": 10, "last month": 100, "long ago": 800}
        for title, days in ages.items():
            access_request = AccessRequest.objects.create(requester=self.user, kind="OTHER", title=title,
                                                          justification="x")
            AccessRequest.objects.filter(pk=access_request.pk).update(
                created_at=timezone.now() - timedelta(days=days))
        for period, expected in [(None, ["today"]), ("bogus", ["today"]), ("month", ["today", "last week"]),
                                 ("year", ["today", "last week", "last month"]), ("all", list(ages))]:
            response = self.client.get(reverse("requests:list"), {"period": period} if period else {})
            self.assertEqual([r.title for r in response.context["page"]], expected, period)
        # the last time span is remembered; "reset" goes back to 7 days
        response = self.client.get(reverse("requests:list"), follow=True)
        self.assertEqual([r.title for r in response.context["page"]], list(ages))
        response = self.client.get(reverse("requests:list"), {"reset": "1"}, follow=True)
        self.assertContains(response, '<input type="radio" name="period" value="week" checked>', html=True)
        AccessRequest.objects.filter(title="today").delete()
        self.assertContains(self.client.get(reverse("requests:list")), "anything in this time span")
        # a date range, both days included, and the status
        AccessRequest.objects.filter(title="last week").update(status=AccessRequest.Status.DENIED)
        day = (timezone.localtime() - timedelta(days=10)).date().isoformat()
        response = self.client.get(reverse("requests:list"), {"period": "range", "from": day, "to": day})
        self.assertEqual([r.title for r in response.context["page"]], ["last week"])
        response = self.client.get(reverse("requests:list"), {"period": "all", "status": ["PENDING", "APPROVED"]})
        self.assertEqual([r.title for r in response.context["page"]], ["last month", "long ago"])

    def test_other_requests_and_catalog_search(self):
        self.client.post(reverse("requests:new"), {"kind": "OTHER", "title": "Figma", "justification": "Design",
                                                   "link": "https://figma.example"})
        self.assertEqual(AccessRequest.objects.get().kind, "OTHER")
        with patch("santa.catalog.search", return_value=[]):
            response = self.client.get(reverse("requests:catalog_search"), {"kind": "NPM_PACKAGE", "q": "esbuild"})
        self.assertEqual(response.status_code, 200)

    def test_limit_of_open_requests(self):
        for index in range(20):
            AccessRequest.objects.create(requester=self.user, kind="OTHER", title=f"t{index}", justification="x")
        response = self.client.post(reverse("requests:new"), {"kind": "OTHER", "title": "one more",
                                                              "justification": "x"})
        self.assertContains(response, "open requests already")

    def test_cancel(self):
        access_request = AccessRequest.objects.create(requester=self.user, kind="OTHER", title="t", justification="x")
        other = AccessRequest.objects.create(requester=self.admin, kind="OTHER", title="t", justification="x")
        self.client.post(reverse("requests:cancel", args=(access_request.pk,)))
        self.assertEqual(self.client.post(reverse("requests:cancel", args=(other.pk,))).status_code, 404)
        access_request.refresh_from_db()
        self.assertEqual(access_request.status, AccessRequest.Status.CANCELLED)


class AdminRequestTestCase(ConsoleBase):
    def event_request(self):
        event = self.make_event(signing_id="ABCDE12345:com.example.tool", team_id="ABCDE12345")
        return AccessRequest.objects.create(requester=self.user, kind="EVENT", event=event, machine=self.machine,
                                            file_sha256=SHA_A, title="tool", justification="x")

    def test_detail_page(self):
        access_request = self.event_request()
        response = self.client.get(reverse("console:request", args=(access_request.pk,)))
        self.assertContains(response, "ABCDE12345:com.example.tool")
        self.assertContains(response, "jdoe-mbp")

    def test_approve_event_for_the_mac_of_the_requester(self):
        access_request = self.event_request()
        response = self.client.post(reverse("console:request_approve", args=(access_request.pk,)), {
            "rule_type": RuleType.SIGNINGID, "policy": Policy.ALLOWLIST, "scope": "machines", "new_tags": "request",
            "note": "Enjoy"})
        self.assertRedirects(response, reverse("console:requests"))
        access_request.refresh_from_db()
        rule = access_request.result_rule
        self.assertEqual((access_request.status, access_request.decided_by, access_request.decision_note),
                         (AccessRequest.Status.APPROVED, self.admin, "Enjoy"))
        self.assertEqual((rule.rule_type, rule.identifier), (RuleType.SIGNINGID, "ABCDE12345:com.example.tool"))
        self.assertEqual(list(rule.machines.all()), [self.machine])
        self.assertEqual([t.name for t in rule.tags.all()], ["request"])
        self.assertIsNotNone(Event.objects.get().resolved_at)
        self.assertTrue(LogEntry.objects.filter(object_id=str(access_request.pk)).exists())
        # decided once
        response = self.client.post(reverse("console:request_deny", args=(access_request.pk,)), {"note": "no"})
        self.assertEqual(response.status_code, 404)

    def test_approve_event_after_the_event_was_deleted(self):
        access_request = self.event_request()
        Event.objects.all().delete()
        access_request.refresh_from_db()
        self.client.post(reverse("console:request_approve", args=(access_request.pk,)), {
            "rule_type": RuleType.BINARY, "policy": Policy.ALLOWLIST, "scope": "groups", "groups": [self.dev.pk]})
        self.assertEqual(Rule.objects.get().identifier, SHA_A)

    def package_request(self, *packages):
        access_request = AccessRequest.objects.create(requester=self.user, kind="PACKAGE", title="packages",
                                                      justification="x")
        items = [AccessRequestPackage.objects.create(access_request=access_request, kind=kind, identifier=identifier)
                 for kind, identifier in packages]
        return access_request, items

    def approve(self, access_request, data):
        with patch("santa.console.views_requests.sync_release_source", return_value=[]) as sync:
            response = self.client.post(reverse("console:request_approve", args=(access_request.pk,)), data)
        return response, sync

    def test_approve_some_packages_into_new_rules_per_catalog(self):
        access_request, (ra, python, esbuild, vite) = self.package_request(
            ("VSCODE_EXTENSION", "rust-lang.rust-analyzer"), ("VSCODE_EXTENSION", "ms-python.python"),
            ("NPM_PACKAGE", "esbuild"), ("NPM_PACKAGE", "vite"))
        response = self.client.get(reverse("console:request", args=(access_request.pk,)))
        self.assertContains(response, "Requested by")
        response, sync = self.approve(access_request, {
            f"approve_{ra.pk}": "on", f"target_{ra.pk}": "new", f"approve_{python.pk}": "on",
            f"target_{python.pk}": "new", f"approve_{esbuild.pk}": "on", f"target_{esbuild.pk}": "new",
            f"target_{vite.pk}": "new",
            "new_name": "jdoe tools", "rule_type": RuleType.BINARY, "groups": [self.dev.pk], "note": "vite is not ok"})
        self.assertRedirects(response, reverse("console:requests"))
        vscode = ReleaseSource.objects.get(name="jdoe tools (VS Code extension)")
        npm = ReleaseSource.objects.get(name="jdoe tools (npm package)")
        self.assertEqual(vscode.identifiers, ["rust-lang.rust-analyzer", "ms-python.python"])
        self.assertEqual((npm.identifiers, list(npm.groups.all())), (["esbuild"], [self.dev]))
        self.assertEqual(sync.call_count, 2)
        statuses = dict(access_request.packages.values_list("identifier", "status"))
        self.assertEqual(statuses, {"rust-lang.rust-analyzer": "APPROVED", "ms-python.python": "APPROVED",
                                    "esbuild": "APPROVED", "vite": "DENIED"})
        access_request.refresh_from_db()
        self.assertEqual(access_request.status, AccessRequest.Status.APPROVED)
        self.client.force_login(self.user)
        response = self.client.get(reverse("requests:list"))
        self.assertContains(response, "Not approved")
        self.assertContains(response, "vite is not ok")

    def test_approve_into_an_existing_rule(self):
        source = ReleaseSource.objects.create(name="JS tools", kind="NPM_PACKAGE", identifier="esbuild",
                                              is_global=True)
        access_request, (esbuild, vite) = self.package_request(("NPM_PACKAGE", "esbuild"), ("NPM_PACKAGE", "vite"))
        response = self.client.get(reverse("console:request", args=(access_request.pk,)))
        # esbuild is in JS tools already: preselected
        self.assertEqual(response.context["approve_form"][f"target_{esbuild.pk}"].initial, str(source.pk))
        response, sync = self.approve(access_request, {
            f"approve_{esbuild.pk}": "on", f"target_{esbuild.pk}": source.pk,
            f"approve_{vite.pk}": "on", f"target_{vite.pk}": source.pk})
        self.assertRedirects(response, reverse("console:requests"))
        source.refresh_from_db()
        self.assertEqual(source.identifiers, ["esbuild", "vite"])
        sync.assert_called_once_with(source)
        self.assertEqual(ReleaseSource.objects.count(), 1)

    def test_approve_needs_a_package_and_a_scope(self):
        access_request, (esbuild,) = self.package_request(("NPM_PACKAGE", "esbuild"))
        response, _ = self.approve(access_request, {f"target_{esbuild.pk}": "new"})
        self.assertContains(response, "Approve at least one package")
        response, _ = self.approve(access_request, {f"approve_{esbuild.pk}": "on", f"target_{esbuild.pk}": "new",
                                                    "new_name": "x"})
        self.assertContains(response, "Choose at least one group")
        self.assertFalse(ReleaseSource.objects.exists())

    def other_request(self):
        return AccessRequest.objects.create(requester=self.user, kind="OTHER", title="Figma", justification="design")

    def test_approve_other_with_a_new_rule(self):
        access_request = self.other_request()
        response = self.client.get(reverse("console:request", args=(access_request.pk,)))
        self.assertContains(response, 'enctype="multipart/form-data"')
        self.assertEqual(response.context["approve_form"]["package_name"].initial, "Figma")
        response, _ = self.approve(access_request, {
            "result": "rule", "rule_type": RuleType.TEAMID, "identifier": "t8w5s5s6ra", "policy": Policy.ALLOWLIST,
            "scope": "machines", "package_target": "new", "package_kind": "GITHUB_RELEASE",
            "package_rule_type": RuleType.BINARY, "new_tags": "design", "note": "enjoy"})
        self.assertRedirects(response, reverse("console:requests"))
        rule = Rule.objects.get(rule_type=RuleType.TEAMID)
        # normalized like on the rule form, for the Mac of the requester
        self.assertEqual((rule.identifier, list(rule.machines.all())), ("T8W5S5S6RA", [self.machine]))
        self.assertEqual([tag.name for tag in rule.tags.all()], ["design"])
        access_request.refresh_from_db()
        self.assertEqual((access_request.status, access_request.result_rule), (AccessRequest.Status.APPROVED, rule))

    def test_approve_other_with_an_uploaded_app(self):
        access_request = self.other_request()
        upload = SimpleUploadedFile("figma", build_macho(identifier="com.figma.agent", team_id="T8W5S5S6RA"))
        response, _ = self.approve(access_request, {
            "result": "rule", "rule_type": RuleType.SIGNINGID, "file": upload, "policy": Policy.ALLOWLIST,
            "scope": "groups", "groups": [self.dev.pk], "package_target": "new", "package_kind": "GITHUB_RELEASE",
            "package_rule_type": RuleType.BINARY})
        self.assertRedirects(response, reverse("console:requests"))
        rule = Rule.objects.get()
        self.assertEqual((rule.identifier, list(rule.groups.all())), ("T8W5S5S6RA:com.figma.agent", [self.dev]))

    def test_approve_other_needs_an_identifier(self):
        access_request = self.other_request()
        response, _ = self.approve(access_request, {
            "result": "rule", "rule_type": RuleType.TEAMID, "policy": Policy.ALLOWLIST, "scope": "machines",
            "package_target": "new", "package_kind": "GITHUB_RELEASE", "package_rule_type": RuleType.BINARY})
        self.assertContains(response, "Enter the identifier, or upload the app.")
        self.assertFalse(Rule.objects.exists())

    def test_approve_other_with_a_new_package_rule(self):
        access_request = self.other_request()
        response, sync = self.approve(access_request, {
            "result": "package", "rule_type": RuleType.SIGNINGID, "policy": Policy.ALLOWLIST, "scope": "machines",
            "package_target": "new", "package_kind": "GITHUB_RELEASE", "package_identifiers": "abiosoft/colima\n",
            "package_name": "Colima", "package_rule_type": RuleType.BINARY, "package_groups": [self.dev.pk]})
        self.assertRedirects(response, reverse("console:requests"))
        source = ReleaseSource.objects.get(name="Colima")
        self.assertEqual((source.identifiers, list(source.groups.all()), source.auto_approve),
                         (["abiosoft/colima"], [self.dev], False))
        sync.assert_called_once_with(source)
        access_request.refresh_from_db()
        self.assertEqual(access_request.result_source, source)

    def test_approve_other_into_an_existing_package_rule(self):
        source = ReleaseSource.objects.create(name="JS tools", kind="NPM_PACKAGE", identifier="esbuild",
                                              is_global=True)
        access_request = self.other_request()
        response, _ = self.approve(access_request, {
            "result": "package", "rule_type": RuleType.SIGNINGID, "policy": Policy.ALLOWLIST, "scope": "machines",
            "package_target": source.pk, "package_kind": "GITHUB_RELEASE", "package_identifiers": "vite",
            "package_rule_type": RuleType.BINARY})
        self.assertRedirects(response, reverse("console:requests"))
        source.refresh_from_db()
        self.assertEqual(source.identifiers, ["esbuild", "vite"])

    def test_approve_other_with_an_invalid_package(self):
        access_request = self.other_request()
        response, _ = self.approve(access_request, {
            "result": "package", "rule_type": RuleType.SIGNINGID, "policy": Policy.ALLOWLIST, "scope": "machines",
            "package_target": "new", "package_kind": "GITHUB_RELEASE", "package_identifiers": "not-a-repo",
            "package_name": "Broken", "package_rule_type": RuleType.BINARY, "package_is_global": "on"})
        self.assertContains(response, "use the owner/repo format")
        access_request.refresh_from_db()
        self.assertTrue(access_request.is_pending)

    def test_approve_other_with_an_existing_rule_or_none(self):
        rule = Rule.objects.create(rule_type=RuleType.BINARY, identifier=SHA_A, is_global=True)
        access_request = self.other_request()
        self.approve(access_request, {"result": "existing", "rule_identifier": SHA_A.upper(),
                                      "rule_type": RuleType.SIGNINGID, "policy": Policy.ALLOWLIST,
                                      "scope": "machines", "package_target": "new", "package_kind": "URL",
                                      "package_rule_type": RuleType.BINARY})
        access_request.refresh_from_db()
        self.assertEqual(access_request.result_rule, rule)
        other = self.other_request()
        other.title = "Something else"
        other.save()
        self.approve(other, {"result": "none", "rule_type": RuleType.SIGNINGID, "policy": Policy.ALLOWLIST,
                             "scope": "machines", "package_target": "new", "package_kind": "URL",
                             "package_rule_type": RuleType.BINARY})
        other.refresh_from_db()
        self.assertEqual((other.status, other.result_rule), (AccessRequest.Status.APPROVED, None))

    def test_approve_other_needs_the_rule_permissions(self):
        approver = User.objects.create_user("approver", is_staff=True)
        approver.user_permissions.set(Permission.objects.filter(codename__in=["view_accessrequest",
                                                                              "change_accessrequest"]))
        self.client.force_login(approver)
        access_request = self.other_request()
        response, _ = self.approve(access_request, {
            "result": "rule", "rule_type": RuleType.TEAMID, "identifier": "T8W5S5S6RA", "policy": Policy.ALLOWLIST,
            "scope": "machines", "package_target": "new", "package_kind": "GITHUB_RELEASE",
            "package_rule_type": RuleType.BINARY})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Rule.objects.exists())

    def test_deny_all_packages(self):
        access_request, (esbuild,) = self.package_request(("NPM_PACKAGE", "esbuild"))
        self.client.post(reverse("console:request_deny", args=(access_request.pk,)), {"note": "no"})
        esbuild.refresh_from_db()
        self.assertEqual(esbuild.status, "DENIED")

    def test_deny(self):
        access_request = AccessRequest.objects.create(requester=self.user, kind="OTHER", title="t", justification="x")
        response = self.client.post(reverse("console:request_deny", args=(access_request.pk,)), {"note": ""})
        self.assertEqual(response.status_code, 200)
        self.client.post(reverse("console:request_deny", args=(access_request.pk,)), {"note": "Use the other tool"})
        access_request.refresh_from_db()
        self.assertEqual(access_request.status, AccessRequest.Status.DENIED)
        self.client.force_login(self.user)
        self.assertContains(self.client.get(reverse("requests:list")), "Use the other tool")

    def test_users_cannot_decide(self):
        access_request = AccessRequest.objects.create(requester=self.user, kind="OTHER", title="t", justification="x")
        self.client.force_login(self.user)
        response = self.client.post(reverse("console:request_approve", args=(access_request.pk,)), {})
        self.assertEqual(response.status_code, 403)


class RequestPermissionsTestCase(ConsoleBase):
    def setUp(self):
        super().setUp()
        self.user.groups.clear()
        self.client.force_login(self.user)

    def test_no_request_permission(self):
        response = self.client.get(reverse("requests:new"))
        self.assertEqual(response.status_code, 403)
        self.assertContains(response, "You can't make requests", status_code=403)
        self.assertNotContains(self.client.get(reverse("requests:list")), reverse("requests:new"))
        self.assertEqual(self.client.get(reverse("requests:catalog_search"), {"kind": "NPM_PACKAGE", "q": "x"})
                         .status_code, 403)

    def test_only_the_allowed_kinds(self):
        self.user.user_permissions.add(Permission.objects.get(codename="request_other"))
        response = self.client.get(reverse("requests:new"), {"kind": "PACKAGE"})
        self.assertEqual(response.context["kind"], "OTHER")
        self.assertEqual([value for value, _ in response.context["kinds"]], ["OTHER"])
        response = self.client.post(reverse("requests:new"), {"kind": "PACKAGE", "justification": "x",
                                                              "packages": "[]"})
        self.assertEqual(response.status_code, 403)
        response = self.client.post(reverse("requests:new"), {"kind": "OTHER", "title": "Figma",
                                                              "justification": "for the designs"})
        self.assertRedirects(response, reverse("requests:list"))
        self.assertTrue(AccessRequest.objects.filter(requester=self.user, kind="OTHER").exists())
