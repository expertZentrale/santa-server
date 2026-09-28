import json
import time
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from django.contrib.auth.models import Group as AuthGroup
from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse

from santa.auth import ADMIN_GROUP_NAME, OIDCBackend

TENANT = "https://login.example"
OIDC_SETTINGS = {
    "OIDC_RP_CLIENT_ID": "client-id",
    "OIDC_RP_CLIENT_SECRET": "client-secret",
    "OIDC_OP_AUTHORIZATION_ENDPOINT": f"{TENANT}/authorize",
    "OIDC_OP_TOKEN_ENDPOINT": f"{TENANT}/token",
    "OIDC_OP_JWKS_ENDPOINT": f"{TENANT}/keys",
}


class ClaimsTestCase(TestCase):
    def claims(self, **kwargs):
        return {"preferred_username": "J.Doe@Example.com", "name": "Jane Doe", "given_name": "Jane",
                "family_name": "Doe", **kwargs}

    def test_user_without_role(self):
        backend = OIDCBackend()
        user = backend.create_user(self.claims())
        self.assertEqual(user.username, "j.doe@example.com")
        self.assertEqual((user.first_name, user.last_name), ("Jane", "Doe"))
        self.assertFalse(user.is_staff)
        self.assertEqual(list(backend.filter_users_by_claims(self.claims())), [user])

    def test_admin_role_gives_and_removes_staff(self):
        backend = OIDCBackend()
        user = backend.create_user(self.claims(roles=["Santa.Admin"]))
        self.assertTrue(user.is_staff)
        self.assertTrue(user.has_perm("santa.add_rule"))
        self.assertTrue(AuthGroup.objects.get(name=ADMIN_GROUP_NAME).permissions.exists())
        user = backend.update_user(User.objects.get(pk=user.pk), self.claims(roles=[]))
        self.assertFalse(user.is_staff)
        self.assertFalse(user.groups.exists())

    def test_configurable_claims(self):
        # e.g. Keycloak: the realm roles are nested, the username is in another claim
        claims = {"sub": "1", "login": "jane", "realm_access": {"roles": ["santa-admin"]}}
        with self.settings(OIDC_USERNAME_CLAIMS=["login"], OIDC_ROLES_CLAIM="realm_access.roles",
                           OIDC_ADMIN_ROLE="santa-admin"):
            user = OIDCBackend().create_user(claims)
        self.assertEqual(user.username, "jane")
        self.assertTrue(user.is_staff)

    def test_claims_without_username_are_refused(self):
        self.assertFalse(OIDCBackend().verify_claims({"name": "x"}))

    def test_disabled_without_client_id(self):
        self.assertIsNone(OIDCBackend().authenticate(None))


@override_settings(**OIDC_SETTINGS)
class OIDCFlowTestCase(TestCase):
    """The whole code flow of mozilla-django-oidc on this Django version, with a signed ID token"""

    def setUp(self):
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key()))
        self.jwks = {"keys": [{**jwk, "kid": "k1", "alg": "RS256", "use": "sig"}]}

    def id_token(self, nonce, **claims):
        payload = {"iss": TENANT, "aud": "client-id", "iat": int(time.time()), "exp": int(time.time()) + 300,
                   "nonce": nonce, "preferred_username": "admin@example.com", "name": "Ada Admin",
                   "roles": ["Santa.Admin"], **claims}
        return jwt.encode(payload, self.key, algorithm="RS256", headers={"kid": "k1"})

    def test_sign_in(self):
        response = self.client.get(reverse("oidc_authentication_init"), {"next": "/console/rules/"})
        self.assertEqual(response.status_code, 302)
        query = parse_qs(urlparse(response["Location"]).query)
        self.assertEqual(query["client_id"], ["client-id"])
        state, nonce = query["state"][0], query["nonce"][0]

        token_response = MagicMock(status_code=200)
        token_response.json.return_value = {"id_token": self.id_token(nonce), "access_token": "access"}
        jwks_response = MagicMock(status_code=200)
        jwks_response.json.return_value = self.jwks
        with patch("mozilla_django_oidc.auth.requests") as http:
            http.post.return_value = token_response
            http.get.return_value = jwks_response
            response = self.client.get(reverse("oidc_authentication_callback"), {"code": "abc", "state": state})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/console/rules/")
        user = User.objects.get(username="admin@example.com")
        self.assertTrue(user.is_staff)
        self.assertEqual(int(self.client.session["_auth_user_id"]), user.pk)
        self.assertEqual(self.client.get(reverse("console:rules")).status_code, 200)

    def test_wrong_nonce_is_refused(self):
        response = self.client.get(reverse("oidc_authentication_init"))
        state = parse_qs(urlparse(response["Location"]).query)["state"][0]
        token_response = MagicMock(status_code=200)
        token_response.json.return_value = {"id_token": self.id_token("other"), "access_token": "access"}
        jwks_response = MagicMock(status_code=200)
        jwks_response.json.return_value = self.jwks
        with patch("mozilla_django_oidc.auth.requests") as http:
            http.post.return_value = token_response
            http.get.return_value = jwks_response
            response = self.client.get(reverse("oidc_authentication_callback"), {"code": "abc", "state": state})
        # SuspiciousOperation: Django answers 400, nobody is signed in
        self.assertEqual(response.status_code, 400)
        self.assertFalse(User.objects.exists())
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_login_page(self):
        response = self.client.get(reverse("login"))
        self.assertContains(response, "Sign in with single sign-on")
