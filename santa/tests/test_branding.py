from django.test import override_settings
from django.urls import reverse

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
        self.assertContains(self.client.get(reverse("admin:index")), icon, html=True)
        self.assertRedirects(self.client.get("/favicon.ico"), "https://intranet.example/icon.png",
                             fetch_redirect_response=False)
