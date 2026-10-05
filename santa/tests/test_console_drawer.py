import json

from django.contrib.admin.models import LogEntry
from django.contrib.auth.models import Group as AuthGroup
from django.contrib.auth.models import Permission, User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from santa.config_io import export_config
from santa.models import AccessRequest, Group, Policy, ReleaseSource, ReleaseVersion, Rule, RuleType, Tag

from .test_console import SHA_A, ConsoleBase

HTMX = {"HTTP_HX_REQUEST": "true"}


class DrawerTestCase(ConsoleBase):
    """The details and forms open in the side drawer (htmx), and as a page without JS"""

    def setUp(self):
        super().setUp()
        self.rule = Rule.objects.create(rule_type=RuleType.BINARY, identifier=SHA_A, is_global=True,
                                        description="old text")
        self.source = ReleaseSource.objects.create(name="Colima", kind="GITHUB_RELEASE", identifier="abiosoft/colima",
                                                   is_global=True)
        self.request_other = AccessRequest.objects.create(requester=self.user, kind="OTHER", title="Figma",
                                                          justification="design")

    def assertDrawer(self, response, text):
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "<html")
        self.assertContains(response, 'class="drawer-panel')
        self.assertContains(response, text)

    def test_drawer_and_page(self):
        for url, text in [(reverse("console:rule", args=(self.rule.pk,)), "old text"),
                          (reverse("console:rule_add"), "New execution rule"),
                          (reverse("console:source", args=(self.source.pk,)), "abiosoft/colima"),
                          (reverse("console:source_edit", args=(self.source.pk,)), "data-source-form"),
                          (reverse("console:source_add"), "New package rule"),
                          (reverse("console:request", args=(self.request_other.pk,)), "Figma"),
                          (reverse("console:history", args=("rule", self.rule.pk)), "No changes recorded.")]:
            self.assertDrawer(self.client.get(url, **HTMX), text)
            page = self.client.get(url)
            self.assertContains(page, "<html")
            self.assertContains(page, text)
        # the forms in the drawer post into the drawer
        self.assertContains(self.client.get(reverse("console:rule", args=(self.rule.pk,)), **HTMX),
                            'hx-target="#drawer"')

    def test_links_open_the_drawer(self):
        rule_url = reverse("console:rule", args=(self.rule.pk,))
        self.assertContains(self.client.get(reverse("console:rules")),
                            f'href="{rule_url}" hx-get="{rule_url}" hx-target="#drawer"')
        source_url = reverse("console:source", args=(self.source.pk,))
        self.assertContains(self.client.get(reverse("console:sources")),
                            f'href="{source_url}" hx-get="{source_url}" hx-target="#drawer"')
        request_url = reverse("console:request", args=(self.request_other.pk,))
        self.assertContains(self.client.get(reverse("console:requests")),
                            f'href="{request_url}" hx-get="{request_url}" hx-target="#drawer"')

    def test_save_in_the_drawer_tells_console_js(self):
        url = reverse("console:rule", args=(self.rule.pk,))
        data = {"rule_type": RuleType.BINARY, "identifier": SHA_A, "policy": Policy.ALLOWLIST, "is_global": "on",
                "is_enabled": "on"}
        response = self.client.post(url, {**data, "description": "new text"}, **HTMX)
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response["HX-Trigger"], "drawerSaved")
        self.rule.refresh_from_db()
        self.assertEqual(self.rule.description, "new text")
        # without htmx: the redirect as before
        response = self.client.post(url, {**data, "description": "page"})
        self.assertRedirects(response, reverse("console:rules"))

    def test_errors_stay_in_the_drawer(self):
        response = self.client.post(reverse("console:rule_add"), {"rule_type": RuleType.BINARY, "identifier": "x",
                                                                   "policy": Policy.ALLOWLIST}, **HTMX)
        self.assertDrawer(response, 'class="field has-error')
        response = self.client.post(reverse("console:request_deny", args=(self.request_other.pk,)), {"note": ""},
                                    **HTMX)
        self.assertDrawer(response, "Figma")

    def test_approve_and_deny_in_the_drawer(self):
        response = self.client.post(reverse("console:request_deny", args=(self.request_other.pk,)),
                                    {"note": "no"}, **HTMX)
        self.assertEqual(response["HX-Trigger"], "drawerSaved")
        self.request_other.refresh_from_db()
        self.assertEqual(self.request_other.status, AccessRequest.Status.DENIED)

    def test_package_rule_actions_answer_with_the_drawer(self):
        version = ReleaseVersion.objects.create(source=self.source, identifier="abiosoft/colima", version="v1",
                                                binary_count=1)
        Rule.objects.create(rule_type=RuleType.BINARY, identifier="b" * 64, release_source=self.source,
                            release_version=version, is_enabled=False, is_global=True)
        response = self.client.post(reverse("console:version_approve", args=(version.pk,)), **HTMX)
        self.assertDrawer(response, "1 rule enabled.")
        self.assertTrue(version.rules.get().is_enabled)
        self.assertRedirects(self.client.post(reverse("console:version_disable", args=(version.pk,))),
                             reverse("console:source", args=(self.source.pk,)))

    def test_history(self):
        self.client.post(reverse("console:rule", args=(self.rule.pk,)), {
            "rule_type": RuleType.BINARY, "identifier": SHA_A, "policy": Policy.ALLOWLIST, "is_global": "on",
            "is_enabled": "on", "description": "changed"})
        other = Rule.objects.create(rule_type=RuleType.BINARY, identifier="c" * 64, is_global=True)
        LogEntry.objects.log_actions(self.admin.pk, Rule.objects.filter(pk=other.pk), 2, change_message="other rule")
        response = self.client.get(reverse("console:history", args=("rule", self.rule.pk)), **HTMX)
        # only what really changed (the Macs field compares the Macs, not its text)
        self.assertDrawer(response, "Changed in the console: description<")
        self.assertNotContains(response, "other rule")
        # back to the rule in the drawer
        self.assertContains(response, f'hx-get="{reverse("console:rule", args=(self.rule.pk,))}"')
        for model, pk in [("group", self.dev.pk), ("machine", self.machine.pk), ("releasesource", self.source.pk),
                          ("user", self.user.pk)]:
            self.assertEqual(self.client.get(reverse("console:history", args=(model, pk))).status_code, 200, model)
        self.assertEqual(self.client.get(reverse("console:history", args=("event", 1))).status_code, 404)

    def test_history_needs_the_view_permission(self):
        viewer = User.objects.create_user("viewer", is_staff=True)
        viewer.user_permissions.set(Permission.objects.filter(codename="view_rule"))
        self.client.force_login(viewer)
        self.assertEqual(self.client.get(reverse("console:history", args=("rule", self.rule.pk))).status_code, 200)
        self.assertEqual(self.client.get(reverse("console:history", args=("group", self.dev.pk))).status_code, 403)
        self.assertEqual(self.client.get(reverse("console:history", args=("user", self.user.pk))).status_code, 403)

    def test_administration_and_groups_in_the_drawer(self):
        role = AuthGroup.objects.create(name="Viewers")
        tag = Tag.objects.create(name="team")
        for url, text in [(reverse("console:admin_user", args=(self.user.pk,)), self.user.username),
                          (reverse("console:admin_user_add"), "New local account"),
                          (reverse("console:admin_role", args=(role.pk,)), "Viewers"),
                          (reverse("console:admin_role_add"), "New role"),
                          (reverse("console:admin_sign_in_group_add"), "New sign-in group"),
                          (reverse("console:admin_tag", args=(tag.pk,)), "team"),
                          (reverse("console:admin_tag_add"), "New tag"),
                          (reverse("console:admin_config"), "Export configuration"),
                          (reverse("console:admin_config_import"), 'hx-encoding="multipart/form-data"'),
                          (reverse("console:group_add"), "New group")]:
            self.assertDrawer(self.client.get(url, **HTMX), text)
            page = self.client.get(url)
            self.assertContains(page, "<html")
        # the forms of the drawer post into it, "Cancel" closes it
        response = self.client.get(reverse("console:admin_tag", args=(tag.pk,)), **HTMX)
        self.assertContains(response, 'hx-target="#drawer"')
        self.assertContains(response, "data-drawer-close")

    def test_administration_links_open_the_drawer(self):
        role = AuthGroup.objects.create(name="Viewers")
        tag = Tag.objects.create(name="team")
        for list_name, url in [("admin_users", reverse("console:admin_user", args=(self.user.pk,))),
                               ("admin_users", reverse("console:admin_user_add")),
                               ("admin_roles", reverse("console:admin_role", args=(role.pk,))),
                               ("admin_roles", reverse("console:admin_role_add")),
                               ("admin_sign_in_groups", reverse("console:admin_sign_in_group_add")),
                               ("admin_tags", reverse("console:admin_tag", args=(tag.pk,))),
                               ("admin_tags", reverse("console:admin_tag_add")),
                               ("admin_config", reverse("console:admin_config_import")),
                               ("groups", reverse("console:group_add")),
                               ("groups", reverse("console:admin_config"))]:
            self.assertContains(self.client.get(reverse(f"console:{list_name}")),
                                f'href="{url}" hx-get="{url}" hx-target="#drawer"', msg_prefix=list_name)

    def test_saving_administration_in_the_drawer(self):
        response = self.client.post(reverse("console:admin_tag_add"), {"name": "new-tag"}, **HTMX)
        self.assertEqual(response["HX-Trigger"], "drawerSaved")
        response = self.client.post(reverse("console:admin_role_add"), {"name": "Auditors"}, **HTMX)
        self.assertEqual(response["HX-Trigger"], "drawerSaved")
        self.assertRedirects(self.client.post(reverse("console:admin_tag_add"), {"name": "page-tag"}),
                             reverse("console:admin_tags"))

    def test_new_group_opens_in_the_drawer(self):
        data = {"name": "Design", "client_mode": "MONITOR", "batch_size": 50, "full_sync_interval": 600}
        response = self.client.post(reverse("console:group_add"), data, **HTMX)
        group = Group.objects.get(name="Design")
        url = reverse("console:group", args=(group.pk,))
        self.assertEqual(json.loads(response["HX-Trigger"]), {"drawerSaved": {"open": url}})
        # the message comes with the group in the drawer
        self.assertDrawer(self.client.get(url, **HTMX), "Download its configuration profile below")

    def test_import_in_the_drawer(self):
        upload = SimpleUploadedFile("config.json", json.dumps(export_config()).encode(),
                                    content_type="application/json")
        response = self.client.post(reverse("console:admin_config_import"), {"file": upload}, **HTMX)
        self.assertDrawer(response, "Imported")
        # the lists behind the drawer show the import after it closes
        self.assertContains(response, "data-refresh-on-close")

    def test_delete_rule(self):
        url = reverse("console:rule", args=(self.rule.pk,))
        self.assertContains(self.client.get(url, **HTMX), reverse("console:rule_delete", args=(self.rule.pk,)))
        response = self.client.post(reverse("console:rule_delete", args=(self.rule.pk,)), **HTMX)
        self.assertEqual(response["HX-Trigger"], "drawerSaved")
        self.assertFalse(Rule.objects.filter(pk=self.rule.pk).exists())
        self.assertTrue(LogEntry.objects.filter(object_repr=str(self.rule), action_flag=3).exists())
        # the rules of a package rule are disabled there, not deleted
        version = ReleaseVersion.objects.create(source=self.source, identifier="abiosoft/colima", version="v1")
        package_rule = Rule.objects.create(rule_type=RuleType.BINARY, identifier="b" * 64, is_global=True,
                                           release_source=self.source, release_version=version)
        self.assertEqual(self.client.post(reverse("console:rule_delete", args=(package_rule.pk,))).status_code, 404)
        # without the permission
        viewer = User.objects.create_user("viewer", is_staff=True)
        viewer.user_permissions.set(Permission.objects.filter(codename__in=["view_rule", "change_rule"]))
        self.client.force_login(viewer)
        other = Rule.objects.create(rule_type=RuleType.BINARY, identifier="c" * 64, is_global=True)
        self.assertNotContains(self.client.get(reverse("console:rule", args=(other.pk,))), "rule-delete-form")
        self.assertEqual(self.client.post(reverse("console:rule_delete", args=(other.pk,))).status_code, 403)
