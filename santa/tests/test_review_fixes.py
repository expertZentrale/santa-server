"""What Santa decides on a Mac, and the approvals that rely on it (the second review of PR #47)"""
from unittest.mock import patch

from django.core.cache import cache
from django.urls import reverse
from django.utils import timezone

from santa.models import AccessRequest, AccessRequestPackage, Event, Machine, Policy, ReleaseSource, Rule, RuleType
from santa.package_detection import Kind
from santa.progress import run_source_check
from santa.services import decisions_for_machines, decisions_on, resolve_binary_events

from .test_console import SHA_A, ConsoleBase
from .test_package_detection import SHA_REDIS, DetectionBase


class DecisionsTestCase(ConsoleBase):
    def test_a_block_wins_over_an_allow_of_a_package_rule(self):
        source = ReleaseSource.objects.create(name="tools", kind=ReleaseSource.Kind.GITHUB_RELEASE, identifier="o/a")
        Rule.objects.create(rule_type=RuleType.BINARY, identifier=SHA_A, is_global=True, release_source=source)
        Rule.objects.create(rule_type=RuleType.BINARY, identifier=SHA_A, is_global=True, policy=Policy.BLOCKLIST)
        event = self.make_event()
        self.assertEqual(decisions_on([event]), {event.pk: False})

    def test_the_more_specific_scope_wins(self):
        Rule.objects.create(rule_type=RuleType.BINARY, identifier=SHA_A, is_global=True, policy=Policy.BLOCKLIST)
        Rule.objects.create(rule_type=RuleType.BINARY, identifier=SHA_A).groups.add(self.dev)
        on_dev, on_sales = self.make_event(), self.make_event(machine=self.other_machine)
        self.assertEqual(decisions_on([on_dev, on_sales]), {on_dev.pk: True, on_sales.pk: False})

    def test_the_more_specific_rule_type_wins(self):
        Rule.objects.create(rule_type=RuleType.TEAMID, identifier="ABCDE12345", is_global=True)
        Rule.objects.create(rule_type=RuleType.CDHASH, identifier="c" * 40, is_global=True, policy=Policy.BLOCKLIST)
        event = self.make_event(team_id="ABCDE12345", cdhash="c" * 40)
        self.assertEqual(decisions_on([event]), {event.pk: False})
        # a disabled block doesn't count
        Rule.objects.filter(rule_type=RuleType.CDHASH).update(is_enabled=False)
        self.assertEqual(decisions_on([event]), {event.pk: True})

    def test_the_macs_of_a_requester(self):
        Rule.objects.create(rule_type=RuleType.BINARY, identifier=SHA_A).groups.add(self.dev)
        event = self.make_event(machine=self.other_machine)
        self.assertEqual(decisions_for_machines(event, [self.machine, self.other_machine]),
                         {self.machine.pk: True, self.other_machine.pk: False})

    def test_resolving_many_macs(self):
        # more than SQL Server's 2100 parameters
        Machine.objects.bulk_create([
            Machine(machine_id=f"X{n}", serial_number=f"S{n}", hostname=f"h{n}", group=self.dev) for n in range(2200)])
        # SQL Server returns no pks from a bulk insert
        machines = list(Machine.objects.filter(machine_id__startswith="X").order_by("pk"))
        Event.objects.create(machine=machines[-1], group=self.dev, execution_time=timezone.now(),
                             decision="BLOCK_UNKNOWN", file_sha256=SHA_A, file_path="/x", file_name="x")
        self.assertEqual(resolve_binary_events(SHA_A, machines, self.admin), 1)


class RequestReviewTestCase(ConsoleBase):
    def event_request(self, **kwargs):
        event = self.make_event(signing_id="ABCDE12345:com.example.tool", team_id="ABCDE12345")
        return AccessRequest.objects.create(requester=self.user, kind="EVENT", event=event, machine=self.machine,
                                            file_sha256=SHA_A, title="tool", justification="x", **kwargs)

    def test_a_fallback_event_of_another_mac_does_not_count(self):
        # the event of the request was cleaned up; the same binary blocked on a Mac of someone else
        access_request = self.event_request()
        access_request.event.delete()
        self.make_event(machine=self.other_machine)
        rule = Rule.objects.create(rule_type=RuleType.BINARY, identifier=SHA_A)
        rule.groups.add(self.sales)
        access_request.refresh_from_db()
        response = self.client.post(reverse("console:request_approve", args=(access_request.pk,)), {
            "result": "existing", "existing_rule": rule.pk})
        self.assertEqual(response.status_code, 200)
        access_request.refresh_from_db()
        self.assertTrue(access_request.is_pending)
        self.assertIsNone(Event.objects.get().resolved_at)

    def test_a_block_of_the_group_refuses_the_existing_rule(self):
        allow = Rule.objects.create(rule_type=RuleType.TEAMID, identifier="ABCDE12345", is_global=True)
        Rule.objects.create(rule_type=RuleType.BINARY, identifier=SHA_A, policy=Policy.BLOCKLIST).groups.add(self.dev)
        access_request = self.event_request()
        response = self.client.post(reverse("console:request_approve", args=(access_request.pk,)), {
            "result": "existing", "existing_rule": allow.pk})
        self.assertIn("existing_rule", response.context["approve_form"].errors)

    def test_the_rule_of_a_package_rule_is_not_widened(self):
        source = ReleaseSource.objects.create(name="tools", kind=ReleaseSource.Kind.GITHUB_RELEASE, identifier="o/a")
        rule = Rule.objects.create(rule_type=RuleType.TEAMID, identifier="ABCDE12345", release_source=source)
        rule.groups.add(self.sales)
        access_request = self.event_request()
        response = self.client.post(reverse("console:request_approve", args=(access_request.pk,)), {
            "result": "extend", "existing_rule": rule.pk, "scope": "groups", "groups": [self.dev.pk]})
        self.assertContains(response, "comes from the package rule tools")
        self.assertEqual(list(rule.groups.all()), [self.sales])

    def test_approved_while_the_package_rule_is_checked(self):
        source = ReleaseSource.objects.create(name="JS tools", kind="NPM_PACKAGE", identifier="esbuild",
                                              is_global=True, last_checked_at=timezone.now())
        self.addCleanup(cache.delete_many, [f"santa:source-check:{source.pk}",
                                            f"santa:source-check:{source.pk}:cancel"])
        access_request = AccessRequest.objects.create(requester=self.user, kind="PACKAGE", title="vite",
                                                      justification="x")
        vite = AccessRequestPackage.objects.create(access_request=access_request, kind="NPM_PACKAGE",
                                                   identifier="vite")
        with run_source_check(source, self.user), \
                patch("santa.console.views_requests.sync_release_source") as sync:
            response = self.client.post(reverse("console:request_approve", args=(access_request.pk,)), {
                f"approve_{vite.pk}": "on", f"target_{vite.pk}": source.pk}, follow=True)
        sync.assert_not_called()
        self.assertContains(response, "JS tools is being checked right now")
        access_request.refresh_from_db()
        self.assertEqual(access_request.status, AccessRequest.Status.APPROVED)
        source.refresh_from_db()
        self.assertEqual(source.identifiers, ["esbuild", "vite"])


class EventPackagesReviewTestCase(DetectionBase):
    def form_data(self, ids, **data):
        return {"ids": ids, "apply": "1", "approve_0": "on", "kind_0": Kind.HOMEBREW_FORMULA,
                "identifier_0": "redis", "target_0": "new", "new_name": "Datenbanken", "rule_type": RuleType.BINARY,
                "policy": "ALLOWLIST", "groups": [self.dev.pk], "keep_versions": 3, "keep_unit": "VERSIONS",
                "resolve": "on", **data}

    def test_only_the_events_of_the_macs_it_reaches_are_resolved(self):
        on_dev = self.event(SHA_REDIS, "/opt/homebrew/Cellar/redis/8.10.1/bin", "redis-server")
        on_sales = Event.objects.create(machine=self.other_machine, group=self.sales, execution_time=timezone.now(),
                                        decision="BLOCK_UNKNOWN", file_sha256=SHA_REDIS,
                                        file_path="/opt/homebrew/Cellar/redis/8.10.1/bin", file_name="redis-server")

        def sync(source, progress=None, only=None):
            # the package rule is for the group Development only
            Rule.objects.create(rule_type=RuleType.BINARY, identifier=SHA_REDIS,
                                release_source=source).groups.set(source.groups.all())
            return []

        with patch("santa.console.views_event_packages.sync_release_source", side_effect=sync):
            response = self.client.post(reverse("console:events_packages"),
                                        self.form_data([on_sales.pk, on_dev.pk]))
        on_dev.refresh_from_db()
        on_sales.refresh_from_db()
        self.assertIsNotNone(on_dev.resolved_at)
        self.assertIsNone(on_sales.resolved_at)
        self.assertContains(response, "Allowed on some of the Macs only")
        self.assertContains(response, "0 of 1 binary allowed now")

    def test_an_identifier_the_catalog_does_not_take(self):
        event = self.event(SHA_REDIS, "/opt/homebrew/Cellar/redis/8.10.1/bin", "redis-server")
        with patch("santa.console.views_event_packages.sync_release_source") as sync:
            response = self.client.post(reverse("console:events_packages"), self.form_data(
                [event.pk], kind_0=Kind.GITHUB_RELEASE, identifier_0="colima"))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["form"].non_field_errors())
        sync.assert_not_called()
        self.assertFalse(ReleaseSource.objects.exists())
