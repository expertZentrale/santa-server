import base64
from datetime import timedelta

from django.contrib.admin.models import DELETION, LogEntry
from django.contrib.auth.models import Permission, User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone

from santa.models import Group, Machine, Policy, Rule, RuleType
from santa.rules import GROUP, MACHINE, effective_rules

from .test_console import SHA_A, SHA_B, ConsoleBase


class ConsoleGroupsTestCase(ConsoleBase):
    def test_pages_load(self):
        for name, args in [("groups", ()), ("group_add", ()), ("group", (self.dev.pk,)), ("machines", ()),
                           ("machine", (self.machine.pk,))]:
            self.assertEqual(self.client.get(reverse(f"console:{name}", args=args)).status_code, 200, name)
        response = self.client.get(reverse("console:group", args=(self.dev.pk,)))
        self.assertContains(response, self.dev.sync_base_url)

    def test_create_and_change_group(self):
        data = {"name": "Design", "client_mode": "LOCKDOWN", "batch_size": 100, "full_sync_interval": 600,
                "allowed_path_regex": "^/opt/tools/\n^/Applications/Figma\\.app/", "removable_media_action": "ALLOW"}
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
        self.client.post(reverse("console:machine_clean_sync", args=(self.machine.pk,)))
        self.machine.refresh_from_db()
        self.assertTrue(self.machine.clean_sync_requested)
        self.assertTrue(LogEntry.objects.filter(object_id=str(self.machine.pk)).exists())

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
                "removable_media_action": "ALLOW"}
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
