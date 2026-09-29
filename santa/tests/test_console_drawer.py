from django.contrib.admin.models import LogEntry
from django.contrib.auth.models import Permission, User
from django.urls import reverse

from santa.models import AccessRequest, Policy, ReleaseSource, ReleaseVersion, Rule, RuleType

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

    def test_save_in_the_drawer_refreshes_the_page(self):
        url = reverse("console:rule", args=(self.rule.pk,))
        data = {"rule_type": RuleType.BINARY, "identifier": SHA_A, "policy": Policy.ALLOWLIST, "is_global": "on",
                "is_enabled": "on"}
        response = self.client.post(url, {**data, "description": "new text"}, **HTMX)
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response["HX-Refresh"], "true")
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
        self.assertEqual(response["HX-Refresh"], "true")
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
