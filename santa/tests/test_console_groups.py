from datetime import timedelta

from django.contrib.admin.models import LogEntry
from django.urls import reverse
from django.utils import timezone

from santa.models import Group, Machine, Rule, RuleType

from .test_console import SHA_A, ConsoleBase


class ConsoleGroupsTestCase(ConsoleBase):
    def test_pages_load(self):
        for name, args in [("groups", ()), ("group_add", ()), ("group", (self.dev.pk,)), ("machines", ()),
                           ("machine", (self.machine.pk,))]:
            self.assertEqual(self.client.get(reverse(f"console:{name}", args=args)).status_code, 200, name)
        response = self.client.get(reverse("console:group", args=(self.dev.pk,)))
        self.assertContains(response, self.dev.sync_base_url)

    def test_create_and_change_group(self):
        data = {"name": "Design", "client_mode": "LOCKDOWN", "batch_size": 100, "full_sync_interval": 600,
                "allowed_path_regex": "^/opt/tools/\n^/Applications/Figma\\.app/"}
        response = self.client.post(reverse("console:group_add"), data)
        group = Group.objects.get(name="Design")
        self.assertRedirects(response, reverse("console:group", args=(group.pk,)))
        self.assertEqual(group.allowed_path_regex_for_santa(), "(?:^/opt/tools/)|(?:^/Applications/Figma\\.app/)")
        response = self.client.post(reverse("console:group", args=(group.pk,)),
                                    {**data, "unknown_block_message": "Ask the IT"}, follow=True)
        self.assertContains(response, "download the profile again")
        response = self.client.post(reverse("console:group", args=(group.pk,)), {**data, "blocked_path_regex": "("})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Line 1")
        self.assertEqual(LogEntry.objects.filter(object_id=str(group.pk)).count(), 2)

    def test_regenerate_token_and_profile(self):
        token = self.dev.sync_token
        self.client.post(reverse("console:group_regenerate_token", args=(self.dev.pk,)))
        self.dev.refresh_from_db()
        self.assertNotEqual(self.dev.sync_token, token)
        response = self.client.get(reverse("console:group_profile", args=(self.dev.pk,)))
        self.assertEqual(response["Content-Type"], "application/x-apple-aspen-config")
        self.assertIn(self.dev.sync_token.encode(), response.content)
        self.assertEqual(self.client.get(reverse("console:base_profile")).status_code, 200)

    def test_group_with_macs_is_not_deleted(self):
        self.client.post(reverse("console:group_delete", args=(self.dev.pk,)))
        self.assertTrue(Group.objects.filter(pk=self.dev.pk).exists())
        empty = Group.objects.create(name="Empty")
        self.client.post(reverse("console:group_delete", args=(empty.pk,)))
        self.assertFalse(Group.objects.filter(pk=empty.pk).exists())

    def test_non_staff_cannot_see_the_sync_url(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(reverse("console:group", args=(self.dev.pk,))).status_code, 403)
        self.assertEqual(self.client.get(reverse("console:group_profile", args=(self.dev.pk,))).status_code, 403)

    def test_machine_filters_and_detail(self):
        Machine.objects.filter(pk=self.other_machine.pk).update(last_postflight_at=timezone.now())
        Machine.objects.filter(pk=self.machine.pk).update(last_postflight_at=timezone.now() - timedelta(days=5))
        response = self.client.get(reverse("console:machines"), {"status": "stale"})
        self.assertContains(response, "jdoe-mbp")
        self.assertNotContains(response, ">other<")
        rule = Rule.objects.create(rule_type=RuleType.BINARY, identifier=SHA_A, description="only for jdoe")
        rule.machines.add(self.machine)
        self.make_event(file_name="blocked-here")
        response = self.client.get(reverse("console:machine", args=(self.machine.pk,)))
        self.assertContains(response, "only for jdoe")
        self.assertContains(response, "blocked-here")
        self.assertContains(response, "1 should be on the Mac, 0 confirmed")
        response = self.client.get(reverse("console:events"), {"machine": self.other_machine.pk})
        self.assertEqual(list(response.context["page"]), [])

    def test_clean_sync(self):
        self.client.post(reverse("console:machine_clean_sync", args=(self.machine.pk,)))
        self.machine.refresh_from_db()
        self.assertTrue(self.machine.clean_sync_requested)
        self.assertTrue(LogEntry.objects.filter(object_id=str(self.machine.pk)).exists())
