import io
from datetime import timedelta

from django.contrib.admin.models import DELETION, LogEntry
from django.core.management import call_command
from django.urls import reverse
from django.utils import timezone

from santa.models import AccessRequest, Policy, Rule, RuleType
from santa.services import widen_rule

from .test_access_requests import AdminRequestBase
from .test_console import ConsoleBase
from .test_sync import SHA_A, SHA_B, SyncBase


class ExpiredRulesSyncTestCase(SyncBase):
    def test_an_expired_rule_is_removed_from_the_macs(self):
        rule = self.make_rule(SHA_A, is_global=True, expires_at=timezone.now() + timedelta(hours=1))
        _preflight, rules = self.full_sync()
        self.assertIn(SHA_A, [item["identifier"] for item in rules])
        Rule.objects.filter(pk=rule.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
        # the Mac reports its rule (else it is a clean sync)
        _preflight, rules = self.full_sync(binary_rule_count=1)
        self.assertIn({"identifier": SHA_A, "rule_type": "BINARY", "policy": "REMOVE"}, rules)


class CleanupExpiredRulesTestCase(ConsoleBase):
    def test_deletes_the_expired_rules_with_an_entry_in_their_history(self):
        expired = Rule.objects.create(rule_type=RuleType.BINARY, identifier=SHA_A, is_global=True,
                                      created_by=self.admin, expires_at=timezone.now() - timedelta(minutes=5))
        Rule.objects.create(rule_type=RuleType.BINARY, identifier=SHA_B, is_global=True,
                            expires_at=timezone.now() + timedelta(days=1))
        Rule.objects.create(rule_type=RuleType.TEAMID, identifier="ABCDE12345", is_global=True)
        output = io.StringIO()
        call_command("cleanup_expired_rules", stdout=output)
        self.assertIn("1 expired rule(s) deleted", output.getvalue())
        self.assertEqual(sorted(Rule.objects.values_list("identifier", flat=True)), ["ABCDE12345", SHA_B])
        entry = LogEntry.objects.get(object_id=str(expired.pk), action_flag=DELETION)
        self.assertEqual((entry.user, entry.change_message), (self.admin, "Expired"))


class RuleExpiryConsoleTestCase(ConsoleBase):
    def test_the_rule_form_sets_an_end(self):
        before = timezone.now()
        response = self.client.post(reverse("console:rule_add"), {
            "rule_type": RuleType.BINARY, "identifier": SHA_A, "policy": Policy.ALLOWLIST, "is_global": "on",
            "is_enabled": "on", "expires_at_0": "7d"})
        self.assertEqual(response.status_code, 302)
        rule = Rule.objects.get()
        self.assertGreater(rule.expires_at, before + timedelta(days=6, hours=23))
        # never: the end goes
        self.client.post(reverse("console:rule", args=[rule.pk]), {
            "rule_type": RuleType.BINARY, "identifier": SHA_A, "policy": Policy.ALLOWLIST, "is_global": "on",
            "is_enabled": "on", "expires_at_0": ""})
        rule.refresh_from_db()
        self.assertIsNone(rule.expires_at)

    def test_bulk_set_and_remove_only_for_manual_rules(self):
        manual = Rule.objects.create(rule_type=RuleType.BINARY, identifier=SHA_A, is_global=True)
        date = (timezone.localtime() + timedelta(days=3)).replace(second=0, microsecond=0)
        self.client.post(reverse("console:rules_bulk"), {
            "action": "set_expiry", "rules": [manual.pk], "expires_0": "date",
            "expires_1": date.strftime("%Y-%m-%dT%H:%M")})
        manual.refresh_from_db()
        self.assertEqual(manual.expires_at, date)
        page = self.client.get(reverse("console:rules") + "?rows=now&expiry=expires").content.decode()
        self.assertIn(SHA_A, page)
        self.client.post(reverse("console:rules_bulk"), {"action": "clear_expiry", "rules": [manual.pk]})
        manual.refresh_from_db()
        self.assertIsNone(manual.expires_at)

    def test_bulk_set_needs_how_long(self):
        rule = Rule.objects.create(rule_type=RuleType.BINARY, identifier=SHA_A, is_global=True)
        self.client.post(reverse("console:rules_bulk"), {"action": "set_expiry", "rules": [rule.pk],
                                                         "expires_0": ""})
        rule.refresh_from_db()
        self.assertIsNone(rule.expires_at)

    def test_another_bulk_action_ignores_the_hidden_expiry(self):
        rule = Rule.objects.create(rule_type=RuleType.BINARY, identifier=SHA_A, is_global=True)
        # "until a date" chosen, but no date, then another action
        self.client.post(reverse("console:rules_bulk"), {"action": "disable", "rules": [rule.pk],
                                                         "expires_0": "date", "expires_1": ""})
        rule.refresh_from_db()
        self.assertFalse(rule.is_enabled)


class ApprovalExpiryTestCase(AdminRequestBase):
    def test_a_new_rule_for_a_week(self):
        access_request = self.event_request()
        self.client.post(reverse("console:request_approve", args=(access_request.pk,)), {
            "rule_type": RuleType.SIGNINGID, "policy": Policy.ALLOWLIST, "scope": "machines", "expires_at_0": "7d"})
        access_request.refresh_from_db()
        self.assertEqual(access_request.status, AccessRequest.Status.APPROVED)
        self.assertGreater(access_request.result_rule.expires_at, timezone.now() + timedelta(days=6))

    def test_an_existing_rule_keeps_the_later_end(self):
        later = timezone.now() + timedelta(days=30)
        sooner = timezone.now() + timedelta(days=1)
        rule = Rule.objects.create(rule_type=RuleType.TEAMID, identifier="ABCDE12345", is_global=True)
        # for good: an approval for a time doesn't give it an end
        widen_rule(rule, False, [], [], self.admin, expires_at=sooner)
        self.assertIsNone(rule.expires_at)
        rule.expires_at = sooner
        rule.save()
        widen_rule(rule, False, [], [], self.admin, expires_at=later)
        self.assertEqual(rule.expires_at, later)
        widen_rule(rule, False, [], [], self.admin, expires_at=sooner)
        self.assertEqual(rule.expires_at, later)
        # an approval for good takes the end away
        widen_rule(rule, False, [], [], self.admin)
        rule.refresh_from_db()
        self.assertIsNone(rule.expires_at)
