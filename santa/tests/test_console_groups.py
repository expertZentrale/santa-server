import base64
from datetime import timedelta

from django.contrib.admin.models import DELETION, LogEntry
from django.contrib.auth.models import Permission, User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from santa.models import Group, Machine, Policy, Rule, RuleType, SavedFilter
from santa.rules import GROUP, MACHINE, effective_rules

from .test_console import SHA_A, SHA_B, ConsoleBase


class ConsoleGroupsTestCase(ConsoleBase):
    def test_pages_load(self):
        for name, args in [("groups", ()), ("group_add", ()), ("group", (self.dev.pk,)), ("machines", ()),
                           ("machine", (self.machine.pk,))]:
            self.assertEqual(self.client.get(reverse(f"console:{name}", args=args)).status_code, 200, name)
        response = self.client.get(reverse("console:group", args=(self.dev.pk,)))
        self.assertContains(response, self.dev.sync_base_url)

    def test_search_and_filter_groups(self):
        self.sales.client_mode = "LOCKDOWN"
        self.sales.description = "Field team"
        self.sales.save()
        response = self.client.get(reverse("console:groups"), {"q": "field"})
        self.assertEqual([group.name for group in response.context["page"]], ["Sales"])
        response = self.client.get(reverse("console:groups"), {"q": "", "mode": "MONITOR"})
        self.assertEqual([group.name for group in response.context["page"]], ["Development"])
        # the default view
        SavedFilter.objects.create(user=self.admin, page="groups", name="Monitor", query="q=&mode=MONITOR",
                                   is_default=True)
        self.assertRedirects(self.client.get(reverse("console:groups")),
                             reverse("console:groups") + "?q=&mode=MONITOR", fetch_redirect_response=False)

    def test_create_and_change_group(self):
        data = {"name": "Design", "client_mode": "LOCKDOWN", "batch_size": 100, "full_sync_interval": 600,
                "allowed_path_regex": "^/opt/tools/\n^/Applications/Figma\\.app/", "removable_media_action": "ALLOW",
                "override_file_access_action": "NONE"}
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

    def test_read_only_staff_cannot_see_the_sync_token(self):
        viewer = User.objects.create_user("viewer", is_staff=True)
        viewer.user_permissions.set(Permission.objects.filter(codename="view_group"))
        self.client.force_login(viewer)
        response = self.client.get(reverse("console:group", args=(self.dev.pk,)))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, self.dev.sync_token)
        self.assertNotContains(response, reverse("console:group_profile", args=(self.dev.pk,)))
        self.assertNotContains(self.client.get(reverse("console:groups")), self.dev.sync_token)
        self.assertEqual(self.client.get(reverse("console:group_profile", args=(self.dev.pk,))).status_code, 403)
        # the base profile has no secret
        self.assertEqual(self.client.get(reverse("console:base_profile")).status_code, 200)
        # the admin: no SyncBaseURL, no profile download
        response = self.client.get(reverse("admin:santa_group_change", args=(self.dev.pk,)))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, self.dev.sync_token)
        self.assertNotContains(self.client.get(reverse("admin:santa_group_changelist")), self.dev.sync_token)
        self.assertEqual(self.client.get(reverse("admin:santa_group_mobileconfig", args=(self.dev.pk,))).status_code,
                         403)

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
        response = self.client.get(reverse("console:machine", args=(self.machine.pk,)))
        self.assertContains(response, "Full clean sync")
        self.client.post(reverse("console:machine_clean_sync", args=(self.machine.pk,)))
        self.machine.refresh_from_db()
        self.assertEqual((self.machine.clean_sync_requested, self.machine.clean_sync_all), (True, False))
        self.assertTrue(LogEntry.objects.filter(object_id=str(self.machine.pk)).exists())
        response = self.client.post(reverse("console:machine_clean_sync", args=(self.machine.pk,)), {"all": "1"},
                                    follow=True)
        self.assertContains(response, "full clean sync at the next sync")
        self.machine.refresh_from_db()
        self.assertEqual((self.machine.clean_sync_requested, self.machine.clean_sync_all), (True, True))
        self.assertContains(self.client.get(reverse("console:machine", args=(self.machine.pk,))),
                            "Full clean sync pending")
        self.assertTrue(LogEntry.objects.filter(object_id=str(self.machine.pk),
                                                change_message="Full clean sync requested").exists())

    def test_machine_lists_every_rule_that_reaches_it(self):
        Rule.objects.create(rule_type=RuleType.BINARY, identifier=SHA_A, is_global=True, description="for all")
        Rule.objects.create(rule_type=RuleType.BINARY, identifier=SHA_B, description="dev rule").groups.add(self.dev)
        Rule.objects.create(rule_type=RuleType.BINARY, identifier="c" * 64,
                            description="sales rule").groups.add(self.sales)
        # the same identifier for this Mac only: it wins over the global rule
        mine = Rule.objects.create(rule_type=RuleType.BINARY, identifier=SHA_A, policy=Policy.BLOCKLIST,
                                   description="blocked here")
        mine.machines.add(self.machine)
        url = reverse("console:machine", args=(self.machine.pk,))
        response = self.client.get(url)
        rules = {rule.description: rule for rule in response.context["page"]}
        self.assertEqual(set(rules), {"blocked here", "dev rule"})
        self.assertEqual(rules["blocked here"].scope_level, MACHINE)
        self.assertEqual(rules["dev rule"].scope_level, GROUP)
        self.assertContains(response, f'name="rules" value="{mine.pk}"')
        self.assertEqual([r.description for r in self.client.get(url, {"rules": "group"}).context["page"]],
                         ["dev rule"])
        self.assertEqual([r.description for r in self.client.get(url, {"q": "block"}).context["page"]],
                         ["blocked here"])

    def test_remove_mac_keeps_group_scope_and_deletes_orphans(self):
        shared = Rule.objects.create(rule_type=RuleType.BINARY, identifier=SHA_A, description="shared")
        shared.groups.add(self.dev)
        shared.machines.add(self.machine)
        only_here = Rule.objects.create(rule_type=RuleType.BINARY, identifier=SHA_B, description="only here")
        only_here.machines.add(self.machine)
        elsewhere = Rule.objects.create(rule_type=RuleType.BINARY, identifier="c" * 64)
        elsewhere.machines.add(self.other_machine)
        url = reverse("console:machine_rules_remove", args=(self.machine.pk,))
        response = self.client.post(url, {"rules": [shared.pk, only_here.pk, elsewhere.pk]})
        self.assertRedirects(response, reverse("console:machine", args=(self.machine.pk,)))
        self.assertEqual(list(shared.machines.all()), [])
        self.assertEqual(list(shared.groups.all()), [self.dev])
        self.assertFalse(Rule.objects.filter(pk=only_here.pk).exists())
        # a rule that doesn't list this Mac is not touched
        self.assertEqual(list(elsewhere.machines.all()), [self.other_machine])
        self.assertIn(SHA_A, {rule["identifier"] for rule in effective_rules(self.machine).values()})
        self.assertTrue(LogEntry.objects.filter(object_id=str(shared.pk), change_message__contains="Removed").exists())
        self.assertTrue(LogEntry.objects.filter(object_id=str(only_here.pk), action_flag=DELETION).exists())

    def test_remove_mac_needs_permissions(self):
        only_here = Rule.objects.create(rule_type=RuleType.BINARY, identifier=SHA_B)
        only_here.machines.add(self.machine)
        url = reverse("console:machine_rules_remove", args=(self.machine.pk,))
        viewer = User.objects.create_user("viewer", is_staff=True)
        viewer.user_permissions.set(Permission.objects.filter(codename__in=["view_machine", "view_rule"]))
        self.client.force_login(viewer)
        self.assertEqual(self.client.post(url, {"rules": [only_here.pk]}).status_code, 403)
        # may change but not delete rules: the rule only this Mac has is kept
        viewer.user_permissions.add(Permission.objects.get(codename="change_rule"))
        self.client.post(url, {"rules": [only_here.pk]})
        self.assertEqual(list(only_here.machines.all()), [self.machine])

    def test_new_rule_from_the_mac_page(self):
        response = self.client.get(reverse("console:rule_add"), {"machine": self.machine.pk})
        self.assertContains(response, "C02TEST")

    def test_removable_media_and_branding(self):
        url = reverse("console:group", args=(self.dev.pk,))
        data = {"name": self.dev.name, "client_mode": "MONITOR", "batch_size": 100, "full_sync_interval": 600,
                "removable_media_action": "REMOUNT", "removable_media_remount_flags": "rdonly,noexec",
                "override_file_access_action": "NONE",
                "encrypted_removable_media_action": "ALLOW", "on_start_usb_options": "ForceRemount",
                "branding_company_name": "Example Corp"}
        png = b"\x89PNG\r\n\x1a\n" + b"\0" * 20
        response = self.client.post(url, {**data, "branding_company_logo_file": SimpleUploadedFile("logo.png", png),
                                          "branding_company_logo_dark_url": "file:///Library/Example/dark.png"},
                                    follow=True)
        self.assertContains(response, "download the profile again")
        self.dev.refresh_from_db()
        self.assertEqual((self.dev.removable_media_action, self.dev.removable_media_remount_flags,
                          self.dev.encrypted_removable_media_action, self.dev.on_start_usb_options),
                         ("REMOUNT", "rdonly,noexec", "ALLOW", "ForceRemount"))
        self.assertEqual(self.dev.branding_company_logo, "data:image/png;base64," + base64.b64encode(png).decode())
        self.assertEqual(self.dev.branding_company_logo_dark, "file:///Library/Example/dark.png")
        # the form shows the logo, saving without a new one keeps it
        response = self.client.get(url)
        self.assertContains(response, f'src="{self.dev.branding_company_logo}"')
        self.assertContains(response, 'value="file:///Library/Example/dark.png"')
        self.client.post(url, {**data, "branding_company_logo_dark_url": "file:///Library/Example/dark.png"})
        self.dev.refresh_from_db()
        self.assertTrue(self.dev.branding_company_logo.startswith("data:image/png"))
        # removed, and an emptied file URL removes it too
        self.client.post(url, {**data, "branding_company_logo_clear": "on"})
        self.dev.refresh_from_db()
        self.assertEqual((self.dev.branding_company_logo, self.dev.branding_company_logo_dark), ("", ""))

    def test_logo_upload_is_checked(self):
        url = reverse("console:group", args=(self.dev.pk,))
        data = {"name": self.dev.name, "client_mode": "MONITOR", "batch_size": 100, "full_sync_interval": 600,
                "removable_media_action": "ALLOW", "override_file_access_action": "NONE"}
        response = self.client.post(url, {**data, "branding_company_logo_file": SimpleUploadedFile(
            "logo.svg", b"<svg onload='alert(1)'/>")})
        self.assertContains(response, "Only PNG or JPEG images.")
        response = self.client.post(url, {**data, "branding_company_logo_file": SimpleUploadedFile(
            "logo.png", b"\x89PNG\r\n\x1a\n" + b"\0" * (300 * 1024))})
        self.assertContains(response, "The image is too large")
        response = self.client.post(url, {**data, "branding_company_logo_url": "https://example.com/logo.png"})
        self.assertContains(response, "A file:///… URL")
        response = self.client.post(url, {**data, "removable_media_action": "REMOUNT"})
        self.assertContains(response, "Remounting needs at least one flag.")
        self.dev.refresh_from_db()
        self.assertEqual(self.dev.branding_company_logo, "")


class GroupCountsTestCase(ConsoleBase):
    def test_the_counts_of_a_group_with_macs_and_rules(self):
        # counted apart: joined, the Macs and rules multiplied each other (seconds with thousands of rules)
        Machine.objects.create(machine_id="M3", serial_number="C02THIRD", group=self.dev,
                               last_postflight_at=timezone.now())
        for identifier in (SHA_A, SHA_B, "c" * 64):
            Rule.objects.create(rule_type=RuleType.BINARY, identifier=identifier).groups.add(self.dev)
        response = self.client.get(reverse("console:groups"), {"q": "", "sort": "-rules"})
        rows = {group.name: (group.machine_count, group.rule_count, bool(group.last_sync))
                for group in response.context["page"]}
        self.assertEqual(rows["Development"], (2, 3, True))
        self.assertEqual(rows["Sales"], (1, 0, False))
        self.assertEqual(next(iter(response.context["page"])).name, "Development")


class MachinesBulkTestCase(ConsoleBase):
    def bulk(self, action, machines=None, follow=True, **data):
        machines = machines or [self.machine, self.other_machine]
        return self.client.post(reverse("console:machines_bulk"), {
            "action": action, "machines": [machine.pk for machine in machines], "next": reverse("console:machines"),
            **data}, follow=follow)

    def test_the_list_offers_the_actions(self):
        response = self.client.get(reverse("console:machines"), {"q": ""})
        self.assertContains(response, 'name="machines" value="%s"' % self.machine.pk)
        self.assertContains(response, '<option value="clean_sync_all">Full clean sync</option>', html=True)
        # the clean sync of a row is a form of its own, not one inside the selection
        self.assertContains(response, 'form="mac-clean-sync-form"')

    def test_clean_sync_and_full_clean_sync(self):
        Machine.objects.filter(pk=self.machine.pk).update(synced_rules={"BINARY:" + SHA_A: "x"})
        response = self.bulk("clean_sync")
        self.assertContains(response, "2 Macs: clean sync at their next sync.")
        self.assertEqual(set(Machine.objects.values_list("clean_sync_requested", "clean_sync_all")), {(True, False)})
        response = self.bulk("clean_sync_all", machines=[self.machine])
        self.assertContains(response, "1 Mac: full clean sync at its next sync, the transitive rules too.")
        self.machine.refresh_from_db()
        self.assertTrue(self.machine.clean_sync_all)
        # the rule ledger stays as it is
        self.assertEqual(self.machine.synced_rules, {"BINARY:" + SHA_A: "x"})
        self.assertEqual(LogEntry.objects.filter(object_id=str(self.other_machine.pk)).count(), 1)

    def test_change_the_mode(self):
        response = self.bulk("set_mode", mode="LOCKDOWN", until_0="7d")
        self.assertContains(response, "2 Macs: Lockdown from their next sync on.")
        for machine in Machine.objects.all():
            self.assertEqual(machine.client_mode_override, "LOCKDOWN")
            self.assertAlmostEqual(machine.client_mode_override_until, timezone.now() + timedelta(days=7),
                                   delta=timedelta(minutes=1))
        # as the group: no end
        response = self.bulk("set_mode", mode="", until_0="1d")
        self.assertContains(response, "2 Macs: the mode of their group from their next sync on.")
        self.assertEqual(set(Machine.objects.values_list("client_mode_override", "client_mode_override_until")),
                         {("", None)})
        past = (timezone.localtime() - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")
        response = self.bulk("set_mode", mode="MONITOR", until_0="date", until_1=past)
        self.assertContains(response, "The date must be in the future.")
        self.assertEqual(set(Machine.objects.values_list("client_mode_override", flat=True)), {""})

    @override_settings(DATA_UPLOAD_MAX_NUMBER_FIELDS=None)
    def test_more_macs_than_sql_server_parameters(self):
        # SQL Server takes at most 2100 parameters per query: the selection is checked and changed in chunks
        # (Django's default limit of 1000 form fields is lifted here, a deployment can raise it)
        Machine.objects.bulk_create(Machine(machine_id=f"BULK-{i}", serial_number=f"S{i}", group=self.machine.group)
                                    for i in range(2200))
        machines = list(Machine.objects.all())
        response = self.bulk("clean_sync", machines=machines)
        self.assertContains(response, f"{len(machines)} Macs: clean sync at their next sync.")
        self.assertEqual(Machine.objects.filter(clean_sync_requested=True).count(), len(machines))
        self.bulk("set_mode", machines=machines, mode="MONITOR", until_0="1d")
        self.assertEqual(Machine.objects.filter(client_mode_override="MONITOR").count(), len(machines))

    def test_permissions(self):
        viewer = User.objects.create_user("viewer", is_staff=True)
        viewer.user_permissions.add(Permission.objects.get(codename="view_machine"))
        self.client.force_login(viewer)
        self.assertNotContains(self.client.get(reverse("console:machines"), {"q": ""}), "data-bulkbar")
        self.assertEqual(self.bulk("clean_sync", follow=False).status_code, 403)
        self.assertFalse(Machine.objects.filter(clean_sync_requested=True).exists())
