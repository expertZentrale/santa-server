from django.urls import reverse

from santa.models import (
    AccessRequest,
    AccessRequestPackage,
    ReleaseSource,
    ReleaseVersion,
    Rule,
    RuleType,
    UserProfile,
)
from santa.users import profile_for

from .test_console import SHA_A, ConsoleBase

NAVIGATE = {"HTTP_SEC_FETCH_MODE": "navigate"}
ROWS = {"HTTP_X_SANTA_ROWS": "1", "HTTP_HX_REQUEST": "true"}
LISTS = ["console:rules", "console:events", "console:sources", "console:requests", "console:machines"]


class ListRowsTestCase(ConsoleBase):
    def setUp(self):
        super().setUp()
        Rule.objects.create(rule_type=RuleType.BINARY, identifier=SHA_A, is_global=True, description="the tool")

    def test_a_browser_gets_the_page_first_and_the_rows_after_it(self):
        url = reverse("console:rules") + "?q=tool"
        response = self.client.get(url, **NAVIGATE)
        self.assertContains(response, 'hx-trigger="load"')
        self.assertContains(response, "data-filter-form")
        self.assertNotContains(response, SHA_A)
        # the real table with grey rows where the rows come, not "no rules"
        self.assertContains(response, 'data-table="rules"')
        self.assertContains(response, '<tr class="skeleton" aria-hidden="true">', count=8)
        self.assertNotContains(response, "No rules for these filters.")
        self.assertIsNone(response.context["page"])
        for header in ("X-Santa-Rows", "HX-Request", "HX-Boosted", "HX-History-Restore-Request", "Sec-Fetch-Mode",
                       "X-Santa-Rerender"):
            self.assertIn(header, response["Vary"])
        response = self.client.get(url, **NAVIGATE, **ROWS)
        self.assertContains(response, SHA_A)
        # only the rows: no page around them, no filters
        self.assertNotContains(response, "<html")
        self.assertNotContains(response, "data-filter-form")
        self.assertIn("X-Santa-Rows", response["Vary"])

    def test_every_list(self):
        for name in LISTS:
            with self.subTest(name):
                url = reverse(name) + "?q=x"
                self.assertContains(self.client.get(url, **NAVIGATE), 'class="skeleton"')
                response = self.client.get(url, **ROWS)
                self.assertNotContains(response, "rows-loading")
                self.assertNotContains(response, 'class="skeleton"')
                self.assertContains(response, "<table")

    def test_scripts_and_browsers_without_javascript_get_everything(self):
        # no Sec-Fetch-Mode: a script, an older browser
        self.assertContains(self.client.get(reverse("console:rules"), {"q": "tool"}), SHA_A)
        # the link of the placeholder for browsers without JavaScript; it is no filter to remember
        response = self.client.get(reverse("console:rules"), {"q": "tool", "rows": "now"}, **NAVIGATE)
        self.assertContains(response, SHA_A)
        self.assertEqual(profile_for(self.admin).last_filters["rules"], "q=tool")


class BoostedPagesTestCase(ConsoleBase):
    """Links of the menu, the filters and the pages of a list load a whole page with htmx (hx-boost)"""
    BOOSTED = {"HTTP_HX_REQUEST": "true", "HTTP_HX_BOOSTED": "true"}

    def test_a_boosted_list_is_the_page_with_its_rows_after_it(self):
        Rule.objects.create(rule_type=RuleType.BINARY, identifier=SHA_A, is_global=True)
        response = self.client.get(reverse("console:rules"), {"q": "x"}, **self.BOOSTED)
        self.assertContains(response, "<html")
        self.assertContains(response, "rows-loading")

    def test_rendered_again_in_place_with_its_rows(self):
        Rule.objects.create(rule_type=RuleType.BINARY, identifier=SHA_A, is_global=True)
        response = self.client.get(reverse("console:rules"), {"q": ""}, HTTP_X_SANTA_RERENDER="1", **self.BOOSTED)
        self.assertNotContains(response, "rows-loading")
        self.assertContains(response, SHA_A)

    def test_a_boosted_detail_is_the_page_not_the_drawer(self):
        rule = Rule.objects.create(rule_type=RuleType.BINARY, identifier=SHA_A, is_global=True)
        response = self.client.get(reverse("console:rule", args=(rule.pk,)), **self.BOOSTED)
        self.assertTemplateUsed(response, "console/rules/form.html")
        response = self.client.get(reverse("console:rule", args=(rule.pk,)), HTTP_HX_REQUEST="true")
        self.assertTemplateUsed(response, "console/rules/drawer_form.html")

    def test_the_menu_and_the_filters_are_boosted(self):
        response = self.client.get(reverse("console:rules"), {"q": "x"})
        # the whole page is boosted (the body), the filters keep the scroll position
        self.assertContains(response, 'hx-boost="true">', count=1)
        self.assertContains(response, 'data-filter-form hx-swap="innerHTML show:none"')
        # theme and language: saved in the background (core.js), the forms are no boosted page loads
        self.assertContains(response, 'class="menu-row" data-preference>', count=2)


class PreferenceTestCase(ConsoleBase):
    def test_saved_in_the_background(self):
        response = self.client.post(reverse("set_preference"), {"theme": "dark", "next": "/console/rules/"},
                                    HTTP_X_SANTA_PREFERENCE="1")
        self.assertEqual(response.status_code, 204)
        self.assertEqual(UserProfile.objects.get(user=self.admin).theme, "dark")
        response = self.client.post(reverse("set_preference"), {"language": "de"}, HTTP_X_SANTA_PREFERENCE="1")
        self.assertEqual(response.status_code, 204)
        self.assertEqual(UserProfile.objects.get(user=self.admin).language, "de")

    def test_without_javascript_back_to_the_page(self):
        response = self.client.post(reverse("set_preference"), {"theme": "light", "next": "/console/rules/"})
        self.assertRedirects(response, "/console/rules/", fetch_redirect_response=False)

    def test_the_user_menu(self):
        response = self.client.get(reverse("console:rules"), {"q": "x"})
        for icon in ("M4 21c0-4.4", "M12 2.5 4 5.5", "M15 8l4 4-4 4"):
            self.assertContains(response, icon)
        self.assertNotContains(response, "data-snow=")
        with self.settings(SANTA_CHRISTMAS_THEME_FORCE=True):
            response = self.client.get(reverse("console:rules"), {"q": "x"})
        self.assertContains(response, 'data-snow="on"')
        self.assertContains(response, 'data-snow="off"')


class MyRequestsTestCase(ConsoleBase):
    def test_a_package_request_names_its_packages_once(self):
        self.client.force_login(self.user)
        access_request = AccessRequest.objects.create(requester=self.user, kind="PACKAGE", justification="x",
                                                      title="2 packages: esbuild, vite")
        for identifier in ("esbuild", "vite"):
            AccessRequestPackage.objects.create(access_request=access_request, kind="NPM_PACKAGE",
                                                identifier=identifier)
        response = self.client.get(reverse("requests:list"), {"period": "all"})
        self.assertContains(response, "<strong>2 packages</strong>", html=True)
        self.assertNotContains(response, "2 packages: esbuild, vite")
        # every package shows its status, the request none of its own
        self.assertContains(response, '<span class="badge info">Pending</span>', count=2, html=True)


class ProgressTestCase(ConsoleBase):
    def test_the_page_has_the_progress_bar(self):
        self.assertContains(self.client.get(reverse("console:rules"), {"q": "x"}), '<div id="progress"')


class DynamicPagesTestCase(ConsoleBase):
    """Every link and form loads in place (hx-boost on the body), except the pages of another layout"""

    def test_boosted_with_exceptions(self):
        response = self.client.get(reverse("console:rules"), {"q": "x"})
        self.assertContains(response, 'hx-boost="true">', count=1)
        self.assertContains(response, 'role="menuitem" hx-boost="false">')
        self.assertContains(response, 'download data-download hx-boost="false"')
        self.client.logout()
        # the sign-in too; a failed one shows its error in the local account, open
        self.assertNotContains(self.client.get(reverse("login")), 'hx-boost="false"')
        with self.settings(OIDC_RP_CLIENT_ID="console"):
            self.assertContains(self.client.get(reverse("login")), "<details>")
            response = self.client.post(reverse("login"), {"username": "admin", "password": "wrong"})
        self.assertContains(response, "<details open>")

    def test_the_connection_banner(self):
        response = self.client.get(reverse("console:rules"), {"q": "x"})
        self.assertContains(response, '<div id="connection"')
        self.assertContains(response, 'data-lost="Connection to the server lost. Retrying…"')
        # what it checks: no session, no database
        self.client.logout()
        self.assertEqual(self.client.get("/health").status_code, 200)

    def test_upload_binary_in_the_drawer(self):
        response = self.client.get(reverse("console:rule_upload"), HTTP_HX_REQUEST="true")
        self.assertTemplateUsed(response, "console/rules/drawer_upload.html")
        self.assertContains(response, 'hx-encoding="multipart/form-data"')
        self.assertContains(self.client.get(reverse("console:rules"), {"q": "x"}),
                            f'hx-get="{reverse("console:rule_upload")}" hx-target="#drawer"')


class VersionFilterTestCase(ConsoleBase):
    def test_the_version_of_a_package_rule_is_a_filter(self):
        source = ReleaseSource.objects.create(name="Colima", kind=ReleaseSource.Kind.HOMEBREW_FORMULA,
                                              identifier="colima", is_global=True)
        version = ReleaseVersion.objects.create(source=source, identifier="colima", version="0.10.3")
        Rule.objects.create(rule_type=RuleType.BINARY, identifier=SHA_A, is_global=True, release_source=source,
                            release_version=version)
        response = self.client.get(reverse("console:rules"), {"version": version.pk})
        self.assertContains(response, "Package version")
        self.assertContains(response, "Colima 0.10.3")
        # its chip can be removed
        chips = [chip for chip in response.context["bar"]["chips"] if chip["active"]]
        self.assertEqual([chip["facet"].name for chip in chips], ["version"])
        self.assertNotIn("version=", chips[0]["remove_url"])
