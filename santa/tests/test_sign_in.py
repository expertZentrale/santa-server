from django.test import TestCase, override_settings
from django.urls import reverse


@override_settings(OIDC_RP_CLIENT_ID="santa", OIDC_PROVIDER_NAME="Microsoft")
class SignInButtonTestCase(TestCase):
    def test_without_logo(self):
        response = self.client.get(reverse("login"))
        self.assertContains(response, 'class="button wide primary"')
        self.assertContains(response, "Sign in with Microsoft")
        self.assertNotContains(response, "sign-in-logo")

    @override_settings(OIDC_PROVIDER_ICON="microsoft")
    def test_microsoft(self):
        response = self.client.get(reverse("login"))
        self.assertContains(response, 'class="button wide sign-in sign-in-microsoft"')
        self.assertContains(response, 'fill="#f25022"')
        self.assertContains(response, "Sign in with Microsoft")

    @override_settings(OIDC_PROVIDER_ICON="https://idp.example/logo.svg", OIDC_PROVIDER_NAME="Example ID")
    def test_logo_by_url(self):
        response = self.client.get(reverse("login"))
        self.assertContains(response, 'class="button wide sign-in"')
        self.assertContains(response, '<img class="sign-in-logo" src="https://idp.example/logo.svg" alt="" width="21" '
                                      'height="21" referrerpolicy="no-referrer">', html=True)
