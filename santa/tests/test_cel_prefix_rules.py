"""Team ID rules of signing ID prefixes ("Only signing IDs starting with", a CEL expression): which binaries of the
team they include, and adding the signing ID of an event or a request to them."""
from django.contrib.admin.models import LogEntry
from django.contrib.auth.models import Permission, User
from django.test import SimpleTestCase
from django.urls import reverse

from santa.cel import parse_prefix_expression, prefix_decision, prefix_expression, signing_prefix
from santa.models import AccessRequest, Event, Policy, ReleaseSource, Rule, RuleType
from santa.services import add_signing_prefix, decisions_on, existing_rules, is_allowed, widen_rule

from .test_console import SHA_A, SHA_B, ConsoleBase

TEAM = "ABCDE12345"


class PrefixExpressionTestCase(SimpleTestCase):
    def rule(self, prefixes, rule_type=RuleType.TEAMID):
        return Rule(rule_type=rule_type, identifier=TEAM, policy=Policy.CEL,
                    cel_expr=prefix_expression(TEAM, prefixes))

    def test_round_trip(self):
        expression = prefix_expression(TEAM, ["com.a", "com.b"])
        self.assertEqual(parse_prefix_expression(expression), ["com.a", "com.b"])
        self.assertIsNone(parse_prefix_expression("target.signing_time > 0 ? ALLOWLIST : BLOCKLIST"))

    def test_decision(self):
        rule = self.rule(["com.a"])
        self.assertIs(prefix_decision(rule, f"{TEAM}:com.a.helper"), True)
        self.assertIs(prefix_decision(rule, f"{TEAM}:com.b"), False)
        self.assertIs(prefix_decision(rule, ""), False)
        # not such a rule: unknown
        self.assertIsNone(prefix_decision(self.rule(["com.a"], RuleType.SIGNINGID), f"{TEAM}:com.a"))
        self.assertIsNone(prefix_decision(Rule(rule_type=RuleType.TEAMID, identifier=TEAM,
                                               policy=Policy.ALLOWLIST), f"{TEAM}:com.a"))

    def test_signing_prefix(self):
        rule = self.rule(["com.a"])
        self.assertEqual(signing_prefix(rule, f"{TEAM}:com.b"), "com.b")
        self.assertIsNone(signing_prefix(rule, "OTHER12345:com.b"))
        self.assertIsNone(signing_prefix(rule, f"{TEAM}:com b"))


class PrefixRulesTestCase(ConsoleBase):
    def setUp(self):
        super().setUp()
        self.rule = Rule.objects.create(rule_type=RuleType.TEAMID, identifier=TEAM, policy=Policy.CEL,
                                        cel_expr=prefix_expression(TEAM, ["com.example.b"]), is_global=True)
        self.outside = self.make_event(signing_id=f"{TEAM}:com.example.a", team_id=TEAM)
        self.inside = self.make_event(sha256=SHA_B, signing_id=f"{TEAM}:com.example.b.helper", team_id=TEAM,
                                      file_name="helper")

    def test_which_binaries_it_includes(self):
        [outside] = existing_rules([self.outside])[SHA_A]
        [inside] = existing_rules([self.inside])[SHA_B]
        self.assertEqual((outside["cel"], inside["cel"]), (False, True))
        self.assertFalse(is_allowed([outside]))
        self.assertTrue(is_allowed([inside]))
        self.assertEqual(decisions_on([self.outside, self.inside]), {self.outside.pk: False, self.inside.pk: True})

    def test_the_console_says_the_signing_id_is_not_included(self):
        response = self.client.get(reverse("console:event", args=(self.outside.pk,)), HTTP_HX_REQUEST="true")
        self.assertContains(response, "signing ID not included")
        self.assertContains(response, reverse("console:event_add_signing_id", args=(self.outside.pk,)))
        response = self.client.get(reverse("console:event", args=(self.inside.pk,)), HTTP_HX_REQUEST="true")
        self.assertContains(response, "includes this signing ID")
        response = self.client.get(reverse("console:events"), {"q": "", "rows": "now", "view": "apps"})
        self.assertContains(response, "Team ID rule without this signing ID", count=1)
        # several binaries: the included one is allowed (not ticked), the other one gets its signing ID added to
        # the rule instead of a new rule
        response = self.client.post(reverse("console:events_create_rules"),
                                    {"ids": [self.outside.pk, self.inside.pk]}, headers={"HX-Request": "true"})
        self.assertEqual(response.context["form"]["include"].value(), [])
        self.assertEqual(response.context["form"]["add_signing"].value(), [SHA_A])
        self.assertContains(response, "Instead: add")

    def test_add_the_signing_ids_of_several_events_with_their_scope(self):
        Rule.objects.filter(pk=self.rule.pk).update(is_global=False)
        self.rule.groups.add(self.sales)
        third = self.make_event(sha256="c" * 64, signing_id=f"{TEAM}:org.third", team_id=TEAM, file_name="third")
        unsigned = self.make_event(sha256="d" * 64, file_name="unsigned")
        response = self.client.post(reverse("console:events_create_rules"), {
            "ids": [self.outside.pk, third.pk, unsigned.pk], "apply": "1", "add_signing": [SHA_A, "c" * 64],
            "include": ["d" * 64], "rule_type": RuleType.BINARY, "policy": Policy.ALLOWLIST, "scope": "groups",
            "groups": [self.dev.pk]}, headers={"HX-Request": "true"})
        self.assertEqual(response.status_code, 204)
        self.rule.refresh_from_db()
        self.assertEqual(set(parse_prefix_expression(self.rule.cel_expr)),
                         {"com.example.b", "com.example.a", "org.third"})
        self.assertEqual(set(self.rule.groups.all()), {self.dev, self.sales})
        # resolved: the Mac of the events is in the group added
        for event in (self.outside, third):
            event.refresh_from_db()
            self.assertEqual(event.resolution_rule, self.rule)
        # the binary without a Team ID rule got its own rule, the others none
        self.assertTrue(Rule.objects.filter(rule_type=RuleType.BINARY, identifier="d" * 64).exists())
        self.assertFalse(Rule.objects.filter(rule_type=RuleType.BINARY, identifier=SHA_A).exists())
        self.assertEqual(LogEntry.objects.filter(object_id=str(self.rule.pk)).count(), 2)

    def test_add_the_signing_id_of_an_event(self):
        other = self.make_event(sha256="c" * 64, signing_id=f"{TEAM}:org.other", team_id=TEAM, file_name="other")
        response = self.client.post(reverse("console:event_add_signing_id", args=(self.outside.pk,)),
                                    {"rule": self.rule.pk}, headers={"HX-Request": "true"})
        self.assertEqual(response.status_code, 204)
        self.rule.refresh_from_db()
        self.assertEqual(parse_prefix_expression(self.rule.cel_expr), ["com.example.b", "com.example.a"])
        self.outside.refresh_from_db()
        other.refresh_from_db()
        self.assertEqual(self.outside.resolution_rule, self.rule)
        # another binary of the team stays blocked
        self.assertIsNone(other.resolved_at)
        self.assertTrue(LogEntry.objects.filter(object_id=str(self.rule.pk),
                                                change_message__contains="com.example.a").exists())
        # once is enough
        self.client.post(reverse("console:event_add_signing_id", args=(self.outside.pk,)), {"rule": self.rule.pk})
        self.rule.refresh_from_db()
        self.assertEqual(parse_prefix_expression(self.rule.cel_expr), ["com.example.b", "com.example.a"])

    def test_not_to_a_package_rule_and_not_without_permission(self):
        source = ReleaseSource.objects.create(name="tools", kind=ReleaseSource.Kind.GITHUB_RELEASE,
                                              identifier="o/tools")
        Rule.objects.filter(pk=self.rule.pk).update(release_source=source)
        expression = self.rule.cel_expr
        self.client.post(reverse("console:event_add_signing_id", args=(self.outside.pk,)), {"rule": self.rule.pk})
        self.rule.refresh_from_db()
        self.assertEqual(self.rule.cel_expr, expression)
        viewer = User.objects.create_user("viewer", is_staff=True)
        viewer.user_permissions.add(Permission.objects.get(codename="view_event"))
        self.client.force_login(viewer)
        response = self.client.post(reverse("console:event_add_signing_id", args=(self.outside.pk,)),
                                    {"rule": self.rule.pk})
        self.assertEqual(response.status_code, 403)

    def test_approve_a_request_with_the_signing_id_added(self):
        Rule.objects.filter(pk=self.rule.pk).update(is_global=False)
        self.rule.groups.add(self.sales)
        access_request = AccessRequest.objects.create(requester=self.user, kind="EVENT", event=self.outside,
                                                      machine=self.machine, file_sha256=SHA_A, title="a",
                                                      justification="x")
        response = self.client.get(reverse("console:request", args=(access_request.pk,)))
        self.assertEqual(response.context["approve_form"]["result"].value(), "signing")
        response = self.client.post(reverse("console:request_approve", args=(access_request.pk,)), {
            "result": "signing", "existing_rule": self.rule.pk, "scope": "groups", "groups": [self.dev.pk]})
        access_request.refresh_from_db()
        self.rule.refresh_from_db()
        self.assertEqual(access_request.status, AccessRequest.Status.APPROVED)
        self.assertEqual(access_request.result_rule, self.rule)
        self.assertEqual(parse_prefix_expression(self.rule.cel_expr), ["com.example.b", "com.example.a"])
        self.assertEqual(set(self.rule.groups.all()), {self.dev, self.sales})
        self.assertEqual(Event.objects.get(pk=self.outside.pk).resolution_rule, self.rule)

    def test_without_a_signing_id_it_cannot_be_added(self):
        # a Team ID event without a signing ID: the rule leaves it out, but there is nothing to add
        Event.objects.filter(pk=self.outside.pk).update(signing_id="")
        access_request = AccessRequest.objects.create(requester=self.user, kind="EVENT", event=self.outside,
                                                      machine=self.machine, file_sha256=SHA_A, title="a",
                                                      justification="x")
        form = self.client.get(reverse("console:request", args=(access_request.pk,))).context["approve_form"]
        self.assertNotIn("signing", [value for value, _label in form.fields["result"].choices])
        self.assertNotEqual(form["result"].value(), "signing")

    def test_approve_a_request_with_a_rule_that_includes_it(self):
        access_request = AccessRequest.objects.create(requester=self.user, kind="EVENT", event=self.inside,
                                                      machine=self.machine, file_sha256=SHA_B, title="b",
                                                      justification="x")
        self.client.post(reverse("console:request_approve", args=(access_request.pk,)), {
            "result": "existing", "existing_rule": self.rule.pk})
        access_request.refresh_from_db()
        self.assertEqual(access_request.status, AccessRequest.Status.APPROVED)

    def test_extending_a_rule_that_leaves_the_signing_id_out_is_refused(self):
        access_request = AccessRequest.objects.create(requester=self.user, kind="EVENT", event=self.outside,
                                                      machine=self.machine, file_sha256=SHA_A, title="a",
                                                      justification="x")
        response = self.client.post(reverse("console:request_approve", args=(access_request.pk,)), {
            "result": "extend", "existing_rule": self.rule.pk, "scope": "groups", "groups": [self.dev.pk]})
        self.assertContains(response, "doesn&#x27;t include the signing ID")
        access_request.refresh_from_db()
        self.assertEqual(access_request.status, AccessRequest.Status.PENDING)

    def test_additions_with_stale_copies_of_the_rule_keep_each_other(self):
        first, second = Rule.objects.get(pk=self.rule.pk), Rule.objects.get(pk=self.rule.pk)
        add_signing_prefix(first, f"{TEAM}:com.example.a", self.admin)
        add_signing_prefix(second, f"{TEAM}:org.other", self.admin)
        # and a wider scope with an old copy doesn't write back its expression
        widen_rule(first, True, [], [], self.admin)
        self.rule.refresh_from_db()
        self.assertEqual(set(parse_prefix_expression(self.rule.cel_expr)),
                         {"com.example.b", "com.example.a", "org.other"})

    def test_a_disabled_rule_resolves_no_events(self):
        Rule.objects.filter(pk=self.rule.pk).update(is_enabled=False)
        add_signing_prefix(self.rule, self.outside.signing_id, self.admin)
        self.outside.refresh_from_db()
        self.assertIsNone(self.outside.resolved_at)

    def test_an_expression_of_another_team_is_not_known(self):
        rule = Rule(rule_type=RuleType.TEAMID, identifier=TEAM, policy=Policy.CEL,
                    cel_expr=prefix_expression("OTHER12345", ["com.example.a"]))
        self.assertIsNone(prefix_decision(rule, f"{TEAM}:com.example.a"))

    def test_the_selection_adds_signing_ids_only_where_needed(self):
        # allowed by its own signing ID rule: no prefix for it; without a signing ID: none to add
        Rule.objects.create(rule_type=RuleType.SIGNINGID, identifier=self.outside.signing_id, is_global=True)
        no_signing_id = self.make_event(sha256="e" * 64, team_id=TEAM, file_name="nosig")
        response = self.client.post(reverse("console:events_create_rules"),
                                    {"ids": [self.outside.pk, no_signing_id.pk]}, headers={"HX-Request": "true"})
        self.assertEqual(response.context["form"]["add_signing"].value(), [])
        self.assertEqual(list(response.context["form"].signing_rules), [SHA_A])
        response = self.client.get(reverse("console:event", args=(no_signing_id.pk,)), HTTP_HX_REQUEST="true")
        self.assertNotContains(response, "Add signing ID")
