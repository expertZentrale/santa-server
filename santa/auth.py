"""Sign in with an OpenID Connect provider (Entra ID, Keycloak, Okta, Authentik, …).

The users are matched by the first username claim of OIDC_USERNAME_CLAIMS. The role OIDC_ADMIN_ROLE in the claim
OIDC_ROLES_CLAIM makes them staff and puts them in the "Santa admins" permission group.

The groups in OIDC_GROUPS_CLAIM that an admin added as sign-in groups give their members roles (permission groups),
and the console when the sign-in group has console_access. The other groups of the claim are ignored.
"""
import logging

from django.conf import settings
from django.contrib.auth.models import Group as AuthGroup
from django.contrib.auth.models import Permission
from django.db.models import Q
from mozilla_django_oidc.auth import OIDCAuthenticationBackend

from .models import SignInGroup
from .users import profile_for

logger = logging.getLogger(__name__)

ADMIN_GROUP_NAME = "Santa admins"
REQUESTERS_GROUP_NAME = "Santa requesters"


def admin_permissions():
    """Everything of Santa, plus the users and the roles"""
    return Permission.objects.filter(Q(content_type__app_label="santa")
                                     | Q(content_type__app_label="auth", content_type__model__in=["user", "group"]))


def admin_group():
    group, created = AuthGroup.objects.get_or_create(name=ADMIN_GROUP_NAME)
    if created:
        group.permissions.set(admin_permissions())
    return group


def username_from_claims(claims):
    for name in settings.OIDC_USERNAME_CLAIMS:
        value = claims.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
    return ""


def claim_strings(claims, path):
    """The strings of a claim, a dotted path for nested claims (Keycloak: realm_access.roles)"""
    value = claims
    for part in path.split(".") if path else []:
        value = value.get(part) if isinstance(value, dict) else None
    if not path or isinstance(value, dict):
        return []
    if isinstance(value, str):
        return [value]
    return [item for item in value or [] if isinstance(item, str)]


def roles_from_claims(claims):
    return claim_strings(claims, settings.OIDC_ROLES_CLAIM)


def groups_from_claims(claims):
    path = settings.OIDC_GROUPS_CLAIM
    if path and path in (claims.get("_claim_names") or {}):
        # Entra ID: too many groups, the token only points to the Graph API
        logger.warning("The ID token has too many groups (%s is an overage reference): the sign-in groups are "
                       "ignored. Let the provider send only the groups assigned to the application.", path)
        return []
    return claim_strings(claims, path)


def matching_sign_in_groups(values):
    """The sign-in groups of these claim values, and the one for everyone"""
    values = list(dict.fromkeys([*values, SignInGroup.EVERYONE]))
    groups = []
    # SQL Server: at most 2100 parameters per query
    for start in range(0, len(values), 1000):
        groups += SignInGroup.objects.filter(claim_value__in=values[start:start + 1000])
    return groups


def roles_managed_by_sign_in():
    """The roles that the sign-in sets: "Santa admins" and the roles of the sign-in groups"""
    return set(AuthGroup.objects.filter(Q(name=ADMIN_GROUP_NAME) | Q(sign_in_groups__isnull=False)).distinct())


def apply_sign_in_roles(user, also_managed=()):
    """Set the staff flag and the roles that come from the sign-in: OIDC_ADMIN_ROLE and the sign-in groups the user
    had at the last sign-in. Roles no sign-in group grants are assigned by hand, they are left alone.

    also_managed: roles a sign-in group just stopped granting.
    """
    groups = list(user.sign_in_groups.prefetch_related("roles"))
    has_admin_role = profile_for(user).has_admin_role
    granted = {role for group in groups for role in group.roles.all()}
    if has_admin_role:
        granted.add(admin_group())
    removed = (roles_managed_by_sign_in() | set(also_managed)) - granted
    user.groups.remove(*removed)
    user.groups.add(*granted)
    is_staff = has_admin_role or any(group.console_access for group in groups)
    if user.is_staff != is_staff:
        logger.info("User %s: staff %s from the sign-in", user.username, is_staff)
        user.is_staff = is_staff
        user.save(update_fields=["is_staff"])


def sign_in_group_changed(sign_in_group, old_roles=()):
    """Apply a changed sign-in group to its members right away, not only at their next sign-in"""
    for user in sign_in_group.members.all():
        apply_sign_in_roles(user, also_managed=old_roles)


def delete_sign_in_group(sign_in_group):
    members = list(sign_in_group.members.all())
    old_roles = list(sign_in_group.roles.all())
    sign_in_group.delete()
    for user in members:
        apply_sign_in_roles(user, also_managed=old_roles)


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
        user.save()
        profile = profile_for(user)
        admin_role = settings.OIDC_ADMIN_ROLE
        profile.has_admin_role = bool(admin_role) and admin_role in roles_from_claims(claims)
        profile.save(update_fields=["has_admin_role"])
        user.sign_in_groups.set(matching_sign_in_groups(groups_from_claims(claims)))
        apply_sign_in_roles(user)
        return user
