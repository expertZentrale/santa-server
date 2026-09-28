import json
from unittest.mock import patch

from django.contrib.admin.models import LogEntry
from django.urls import reverse

from santa.models import AccessRequest, AccessRequestPackage, Event, Policy, ReleaseSource, Rule, RuleType
from santa.services import machines_for_user

from .test_console import SHA_A, SHA_B, ConsoleBase


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
        # same binary again
        response = self.client.post(reverse("requests:new"), {"kind": "EVENT", "event": event.pk,
                                                              "justification": "again"})
        self.assertContains(response, "already asked")

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
