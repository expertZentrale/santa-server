"""Administration: users, roles, sign-in groups, tags and the configuration export / import"""
import json
import logging

from django.contrib import messages
from django.contrib.auth.models import Group as AuthGroup
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.translation import gettext, ngettext
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_POST

from ..auth import (
    ADMIN_GROUP_NAME,
    admin_permissions,
    delete_sign_in_group,
    roles_managed_by_sign_in,
    sign_in_group_changed,
)
from ..config_io import ConfigImportError, export_config, import_config
from ..models import SignInGroup, Tag
from .forms import ConfigImportForm, RoleForm, SignInGroupForm, TagForm, UserForm
from .utils import log_addition, log_change, log_deletion, paginate, require_perms, sort_by, staff_required

logger = logging.getLogger(__name__)

CONFIG_MODELS = ("group", "rule", "releasesource")
EXPORT_PERMS = [f"view_{model}" for model in CONFIG_MODELS]
IMPORT_PERMS = [f"{action}_{model}" for action in ("add", "change", "delete") for model in CONFIG_MODELS]

# (key, label, URL name, permissions to see it)
SECTIONS = [
    ("users", _("Users"), "console:admin_users", ["auth.view_user"]),
    ("roles", _("Roles"), "console:admin_roles", ["auth.view_group"]),
    ("sign_in_groups", _("Sign-in groups"), "console:admin_sign_in_groups", ["view_signingroup"]),
    ("tags", _("Tags"), "console:admin_tags", ["view_tag"]),
    ("config", _("Export / import"), "console:admin_config", EXPORT_PERMS),
]

# the rows of the permission matrix of a role: (app, model, label); other models follow with their own name
PERMISSION_ROWS = [
    ("santa", "rule", _("Execution rules")),
    ("santa", "releasesource", _("Package rules")),
    ("santa", "releaseversion", _("Package versions")),
    ("santa", "event", _("Events")),
    ("santa", "accessrequest", _("Requests")),
    ("santa", "machine", _("Macs")),
    ("santa", "group", _("Groups")),
    ("santa", "tag", _("Tags")),
    ("santa", "signingroup", _("Sign-in groups")),
    ("auth", "user", _("Users")),
    ("auth", "group", _("Roles")),
]
PERMISSION_ACTIONS = [("view", _("View")), ("add", _("Add")), ("change", _("Change")), ("delete", _("Delete"))]
REQUEST_PERMISSION_LABELS = {
    "request_event": _("Request apps blocked on their Macs"),
    "request_package": _("Request packages"),
    "request_other": _("Request other software"),
}


def has_perm(user, perm):
    return user.has_perm(perm if "." in perm else f"santa.{perm}")


def visible_sections(user):
    return [(key, label, url) for key, label, url, perms in SECTIONS
            if all(has_perm(user, perm) for perm in perms)]


def render_section(request, template, section, context):
    return render(request, template, {**context, "section": section, "sections": visible_sections(request.user)})


@staff_required
def administration(request):
    sections = visible_sections(request.user)
    if not sections:
        raise PermissionDenied
    return redirect(sections[0][2])


# Users


def is_sign_in_user(user):
    return not user.has_usable_password()


@staff_required
def users(request):
    require_perms(request, "auth.view_user")
    queryset = User.objects.prefetch_related("groups")
    q = request.GET.get("q", "").strip()
    if q:
        queryset = queryset.filter(Q(username__icontains=q) | Q(first_name__icontains=q)
                                   | Q(last_name__icontains=q) | Q(email__icontains=q))
    if request.GET.get("role", "").isdigit():
        queryset = queryset.filter(groups__id=request.GET["role"])
    if request.GET.get("staff") == "yes":
        queryset = queryset.filter(is_staff=True)
    queryset, sort = sort_by(request, queryset, {"user": "username", "name": ["last_name", "first_name"],
                                                 "login": "last_login"}, "user")
    page = paginate(request, queryset)
    for user in page:
        user.from_sign_in = is_sign_in_user(user)
    return render_section(request, "console/administration/users.html", "users", {
        "page": page, "sort": sort, "params": request.GET, "roles": AuthGroup.objects.order_by("name"),
    })


@staff_required
def user_form(request, pk=None):
    user = get_object_or_404(User, pk=pk) if pk else None
    require_perms(request, "auth.view_user", "auth.change_user" if user else "auth.add_user")
    if user and user.is_superuser and not request.user.is_superuser:
        raise PermissionDenied
    managed = roles_managed_by_sign_in() if user and is_sign_in_user(user) else set()
    form = UserForm(request.POST or None, instance=user, managed_roles=managed)
    if request.method == "POST" and form.is_valid():
        if user == request.user and not form.cleaned_data["is_active"]:
            form.add_error("is_active", gettext("You can't deactivate your own account."))
        else:
            created = user is None
            user = form.save()
            if created:
                log_addition(request.user, user)
            else:
                log_change(request.user, user, f"Changed in the console: {', '.join(form.changed_data)}")
            messages.success(request, gettext("User %(user)s saved.") % {"user": user})
            return redirect("console:admin_users")
    sign_in_roles = []
    if user and managed:
        # which sign-in group gives each role, to explain why it can't be changed here
        groups = list(user.sign_in_groups.prefetch_related("roles"))
        for role in user.groups.all():
            if role in managed:
                sources = [group.name for group in groups if role in group.roles.all()]
                sign_in_roles.append((role, sources))
    return render_section(request, "console/administration/user_form.html", "users", {
        "form": form, "edited_user": user, "sign_in_roles": sign_in_roles,
        "sign_in_groups": user.sign_in_groups.all() if user else [],
        "from_sign_in": bool(user and is_sign_in_user(user)),
    })


# Roles


def permission_matrix(form):
    """The permissions of the role form as rows of view / add / change / delete, and the request permissions"""
    permissions = {(perm.content_type.app_label, perm.codename): perm
                   for perm in form.fields["permissions"].queryset.select_related("content_type")}
    selected = {str(value) for value in (form["permissions"].value() or [])}

    def cell(app, codename):
        perm = permissions.pop((app, codename), None)
        return perm and {"pk": perm.pk, "checked": str(perm.pk) in selected, "name": perm.name}

    requests = [(label, cell("santa", codename)) for codename, label in REQUEST_PERMISSION_LABELS.items()]
    rows = []
    models = [*PERMISSION_ROWS]
    listed = {(app, model) for app, model, _label in models}
    for perm in sorted(permissions.values(), key=lambda perm: perm.content_type.model):
        key = (perm.content_type.app_label, perm.content_type.model)
        if key not in listed:
            listed.add(key)
            models.append((*key, perm.content_type.model_class()._meta.verbose_name_plural.capitalize()))
    for app, model, label in models:
        cells = [cell(app, f"{action}_{model}") for action, _action_label in PERMISSION_ACTIONS]
        if any(cells):
            rows.append((label, cells))
    return rows, requests


@staff_required
def roles(request):
    require_perms(request, "auth.view_group")
    queryset = AuthGroup.objects.annotate(user_count=Count("user", distinct=True),
                                          permission_count=Count("permissions", distinct=True)) \
                                .prefetch_related("sign_in_groups").order_by("name")
    return render_section(request, "console/administration/roles.html", "roles", {"roles": queryset})


@staff_required
def role_form(request, pk=None):
    role = get_object_or_404(AuthGroup, pk=pk) if pk else None
    require_perms(request, "auth.view_group", "auth.change_group" if role else "auth.add_group")
    form = RoleForm(request.POST or None, instance=role, permissions=admin_permissions())
    if request.method == "POST" and form.is_valid():
        created = role is None
        role = form.save()
        if created:
            log_addition(request.user, role)
        else:
            log_change(request.user, role, f"Changed in the console: {', '.join(form.changed_data)}")
        messages.success(request, gettext("Role %(role)s saved.") % {"role": role})
        return redirect("console:admin_roles")
    rows, request_permissions = permission_matrix(form)
    return render_section(request, "console/administration/role_form.html", "roles", {
        "form": form, "role": role, "rows": rows, "request_permissions": request_permissions,
        "actions": PERMISSION_ACTIONS, "is_protected": role is not None and role.name == ADMIN_GROUP_NAME,
        "members": role.user_set.order_by("username")[:50] if role else [],
        "sign_in_groups": role.sign_in_groups.all() if role else [],
    })


@staff_required
@require_POST
def role_delete(request, pk):
    require_perms(request, "auth.delete_group")
    role = get_object_or_404(AuthGroup, pk=pk)
    if role.name == ADMIN_GROUP_NAME:
        messages.error(request, gettext("The role %(role)s is given by the admin role of the sign-in, it can't be "
                                        "deleted.") % {"role": role})
        return redirect("console:admin_role", pk=role.pk)
    log_deletion(request.user, role)
    role.delete()
    messages.success(request, gettext("Role %(role)s deleted.") % {"role": role})
    return redirect("console:admin_roles")


# Sign-in groups


@staff_required
def sign_in_groups(request):
    require_perms(request, "view_signingroup")
    queryset = SignInGroup.objects.annotate(member_count=Count("members", distinct=True)) \
                                  .prefetch_related("roles").order_by("name")
    return render_section(request, "console/administration/sign_in_groups.html", "sign_in_groups", {
        "sign_in_groups": queryset, "everyone": SignInGroup.EVERYONE,
    })


@staff_required
def sign_in_group_form(request, pk=None):
    sign_in_group = get_object_or_404(SignInGroup, pk=pk) if pk else None
    require_perms(request, "view_signingroup", "change_signingroup" if sign_in_group else "add_signingroup")
    old_roles = list(sign_in_group.roles.all()) if sign_in_group else []
    form = SignInGroupForm(request.POST or None, instance=sign_in_group)
    if request.method == "POST" and form.is_valid():
        created = sign_in_group is None
        sign_in_group = form.save()
        sign_in_group_changed(sign_in_group, old_roles)
        if created:
            log_addition(request.user, sign_in_group)
        else:
            log_change(request.user, sign_in_group, f"Changed in the console: {', '.join(form.changed_data)}")
        messages.success(request, gettext("Sign-in group %(group)s saved. It applies to its members now, new "
                                          "members get it at their next sign-in.") % {"group": sign_in_group})
        return redirect("console:admin_sign_in_groups")
    return render_section(request, "console/administration/sign_in_group_form.html", "sign_in_groups", {
        "form": form, "sign_in_group": sign_in_group,
        "members": sign_in_group.members.order_by("username")[:50] if sign_in_group else [],
    })


@staff_required
@require_POST
def sign_in_group_delete(request, pk):
    require_perms(request, "delete_signingroup")
    sign_in_group = get_object_or_404(SignInGroup, pk=pk)
    log_deletion(request.user, sign_in_group)
    delete_sign_in_group(sign_in_group)
    messages.success(request, gettext("Sign-in group %(group)s deleted, its members lost its roles.")
                     % {"group": sign_in_group})
    return redirect("console:admin_sign_in_groups")


# Tags


@staff_required
def tags(request):
    require_perms(request, "view_tag")
    queryset = Tag.objects.annotate(rule_count=Count("rules", distinct=True),
                                    source_count=Count("release_sources", distinct=True))
    q = request.GET.get("q", "").strip()
    if q:
        queryset = queryset.filter(Q(name__icontains=q) | Q(description__icontains=q))
    queryset, sort = sort_by(request, queryset, {"name": "name", "rules": "rule_count",
                                                 "packages": "source_count"}, "name")
    return render_section(request, "console/administration/tags.html", "tags", {
        "page": paginate(request, queryset), "sort": sort, "params": request.GET,
    })


@staff_required
def tag_form(request, pk=None):
    tag = get_object_or_404(Tag, pk=pk) if pk else None
    require_perms(request, "view_tag", "change_tag" if tag else "add_tag")
    form = TagForm(request.POST or None, instance=tag)
    if request.method == "POST" and form.is_valid():
        created = tag is None
        tag = form.save()
        if created:
            log_addition(request.user, tag)
        else:
            log_change(request.user, tag, f"Changed in the console: {', '.join(form.changed_data)}")
        messages.success(request, gettext("Tag %(tag)s saved.") % {"tag": tag})
        return redirect("console:admin_tags")
    return render_section(request, "console/administration/tag_form.html", "tags", {"form": form, "tag": tag})


@staff_required
@require_POST
def tag_delete(request, pk):
    require_perms(request, "delete_tag")
    tag = get_object_or_404(Tag, pk=pk)
    log_deletion(request.user, tag)
    tag.delete()
    messages.success(request, gettext("Tag %(tag)s deleted. The rules stay, only the label is gone.")
                     % {"tag": tag})
    return redirect("console:admin_tags")


# Export / import


@staff_required
def config(request):
    require_perms(request, *EXPORT_PERMS)
    can_import = request.user.has_perms([f"santa.{perm}" for perm in IMPORT_PERMS])
    form = ConfigImportForm(request.POST or None, request.FILES or None)
    report = errors = None
    if request.method == "POST":
        if not can_import:
            raise PermissionDenied
        if form.is_valid():
            try:
                data = json.load(form.cleaned_data["file"])
            except ValueError as e:
                form.add_error("file", gettext("Not a JSON file: %(error)s") % {"error": e})
            else:
                dry_run = form.cleaned_data["dry_run"]
                try:
                    report = import_config(data, delete_missing=form.cleaned_data["delete_missing"], dry_run=dry_run)
                except ConfigImportError as e:
                    errors = e.errors
                else:
                    if not dry_run:
                        logger.info("User %s imported a configuration: %s change(s)",
                                    request.user, len(report["changes"]))
                        count = len(report["changes"])
                        messages.success(request, ngettext("Imported: %(count)s change.",
                                                           "Imported: %(count)s changes.", count) % {"count": count})
    return render_section(request, "console/administration/config.html", "config", {
        "form": form, "can_import": can_import, "report": report, "errors": errors,
        "dry_run": form.is_bound and form.is_valid() and form.cleaned_data["dry_run"],
    })


@staff_required
def config_export(request):
    require_perms(request, *EXPORT_PERMS)
    response = HttpResponse(json.dumps(export_config(), indent=2, ensure_ascii=False), content_type="application/json")
    response["Content-Disposition"] = f'attachment; filename="santa-config-{timezone.localdate():%Y-%m-%d}.json"'
    return response
