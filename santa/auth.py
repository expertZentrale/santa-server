"""Sign in with an OpenID Connect provider (Entra ID, Keycloak, Okta, Authentik, …).

The users are matched by the first username claim of OIDC_USERNAME_CLAIMS. The role OIDC_ADMIN_ROLE in the claim
OIDC_ROLES_CLAIM makes them staff and puts them in the "Santa admins" permission group. Without it they can only
use the request form.
"""
import logging

from django.conf import settings
from django.contrib.auth.models import Group as AuthGroup
from django.contrib.auth.models import Permission
from mozilla_django_oidc.auth import OIDCAuthenticationBackend

logger = logging.getLogger(__name__)

ADMIN_GROUP_NAME = "Santa admins"


def admin_group():
    group, created = AuthGroup.objects.get_or_create(name=ADMIN_GROUP_NAME)
    if created:
        group.permissions.set(Permission.objects.filter(content_type__app_label="santa"))
    return group


def username_from_claims(claims):
    for name in settings.OIDC_USERNAME_CLAIMS:
        value = claims.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
    return ""


def roles_from_claims(claims):
    """The roles in OIDC_ROLES_CLAIM, a dotted path for nested claims (Keycloak: realm_access.roles)"""
    value = claims
    for part in settings.OIDC_ROLES_CLAIM.split("."):
        value = value.get(part) if isinstance(value, dict) else None
    if isinstance(value, str):
        return [value]
    return [role for role in value or [] if isinstance(role, str)]


class OIDCBackend(OIDCAuthenticationBackend):
    def authenticate(self, request, **kwargs):
        if not settings.OIDC_RP_CLIENT_ID:
            return None
        return super().authenticate(request, **kwargs)

    def get_userinfo(self, access_token, id_token, payload):
        # the verified ID token has the claims we need (the userinfo endpoint of Entra ID has no roles)
        return payload

    def verify_claims(self, claims):
        return bool(username_from_claims(claims))

    def filter_users_by_claims(self, claims):
        username = username_from_claims(claims)
        return self.UserModel.objects.filter(username__iexact=username)

    def create_user(self, claims):
        user = self.UserModel.objects.create_user(username_from_claims(claims))
        return self.update_user(user, claims)

    def update_user(self, user, claims):
        user.email = (claims.get("email") or username_from_claims(claims))[:254]
        name = (claims.get("name") or "").strip()
        first_name, _, last_name = name.partition(" ")
        user.first_name = claims.get("given_name") or first_name[:150]
        user.last_name = claims.get("family_name") or last_name[:150]
        is_admin = bool(settings.OIDC_ADMIN_ROLE) and settings.OIDC_ADMIN_ROLE in roles_from_claims(claims)
        if user.is_staff != is_admin:
            logger.info("User %s: staff %s from the OIDC roles", user.username, is_admin)
        user.is_staff = is_admin
        user.save()
        if is_admin:
            user.groups.add(admin_group())
        else:
            user.groups.remove(*AuthGroup.objects.filter(name=ADMIN_GROUP_NAME))
        return user
