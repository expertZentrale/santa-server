from django.test import override_settings
from django.urls import reverse

from ..models import Group
from .test_console import ConsoleBase


class BrandingTestCase(ConsoleBase):
    def test_default_name_and_favicon(self):
        response = self.client.get(reverse("console:rules"))
        self.assertContains(response, "· Santa Server</title>")
        self.assertContains(response, '<a class="brand" href="/">Santa Server</a>', html=True)
        self.assertContains(response, '<link rel="icon" href="/static/santa/favicon.svg">', html=True)
        self.assertContains(response, 'rel="apple-touch-icon"')
        self.assertRedirects(self.client.get("/favicon.ico"), "/static/santa/favicon.svg",
                             fetch_redirect_response=False)

    @override_settings(SANTA_SERVER_NAME="Mac Allowlist", SANTA_FAVICON_URL="https://intranet.example/icon.png")
    def test_own_name_and_favicon(self):
        icon = '<link rel="icon" href="https://intranet.example/icon.png">'
        response = self.client.get(reverse("console:rules"))
        self.assertContains(response, "· Mac Allowlist</title>")
        self.assertNotContains(response, "Santa Server")
        self.assertContains(response, icon, html=True)
        # the hat doesn't stay as the icon of the home screen
        self.assertNotContains(response, 'rel="apple-touch-icon"')
        self.client.logout()
        self.assertContains(self.client.get(reverse("login")), icon, html=True)
        self.client.force_login(self.admin)
        response = self.client.get(reverse("admin:index"))
        self.assertContains(response, icon, html=True)
        self.assertContains(response, '<div id="site-name"><a href="/admin/">Mac Allowlist</a></div>', html=True)
        self.assertContains(response, "| Mac Allowlist</title>")
        self.assertRedirects(self.client.get("/favicon.ico"), "https://intranet.example/icon.png",
                             fetch_redirect_response=False)


class CompanyBrandingTestCase(ConsoleBase):
    LOGO = "data:image/png;base64,iVBORw0KGgo="
    HINT = "Not shown in Santa: the group has a company logo."

    def test_the_company_name_is_hidden_by_a_logo(self):
        url = reverse("console:group", args=(self.dev.pk,))
        response = self.client.get(url)
        self.assertContains(response, "only when no company logo is set")
        self.assertContains(response, "The company name only shows without a logo.")
        self.assertNotContains(response, self.HINT)
        self.dev.branding_company_name = "Example Corp"
        self.dev.save()
        self.assertNotContains(self.client.get(url), self.HINT)
        for field in ("branding_company_logo", "branding_company_logo_dark"):
            setattr(self.dev, field, self.LOGO)
            self.dev.save()
            self.assertContains(self.client.get(url), self.HINT)
            setattr(self.dev, field, "")
        # a logo without a name: nothing hidden
        self.dev.branding_company_name = ""
        self.dev.branding_company_logo = self.LOGO
        self.dev.save()
        self.assertNotContains(self.client.get(url), self.HINT)

    def test_inherited_branding_hides_the_company_name(self):
        self.dev.branding_company_name = "Example Corp"
        self.dev.branding_company_logo = self.LOGO
        self.dev.save()
        child = Group.objects.create(name="Dev tools", parent=self.dev)
        url = reverse("console:group", args=(child.pk,))
        self.assertContains(self.client.get(url), self.HINT)
        # an own name with the inherited logo: still hidden
        child.overridden_settings = ["branding_company_name"]
        child.branding_company_name = "Dev tools Corp"
        child.save()
        self.assertContains(self.client.get(url), self.HINT)
        # an own empty logo: the name shows
        child.overridden_settings = ["branding_company_name", "branding_company_logo"]
        child.save()
        self.assertNotContains(self.client.get(url), self.HINT)
