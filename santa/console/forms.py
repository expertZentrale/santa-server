import json

from django import forms
from django.conf import settings
from django.contrib.auth import password_validation
from django.contrib.auth.models import Group as AuthGroup
from django.contrib.auth.models import Permission, User
from django.core.exceptions import ValidationError
from django.forms import formset_factory
from django.utils import timezone
from django.utils.translation import gettext
from django.utils.translation import gettext_lazy as _

from ..catalog import update_identifier_icons
from ..models import (
    AccessRequest,
    Group,
    Machine,
    Policy,
    ReleaseSource,
    Rule,
    RuleType,
    SignInGroup,
    Tag,
    UserProfile,
)

SCOPE_GLOBAL = "global"
SCOPE_GROUPS = "groups"
SCOPE_MACHINES = "machines"

RULE_TYPE_HELP = {
    RuleType.SIGNINGID: _("all versions of this app from this developer"),
    RuleType.BINARY: _("exactly this file, for unsigned tools"),
    RuleType.CDHASH: _("this code signature of the file"),
    RuleType.CERTIFICATE: _("everything signed with this certificate"),
    RuleType.TEAMID: _("everything from this developer"),
}
ALLOW_POLICIES = [(Policy.ALLOWLIST, _("Allow")), (Policy.ALLOWLIST_COMPILER, _("Allow compiler"))]
# labels of the model fields the console forms show (the admin keeps the English field names)
RULE_LABELS = {
    "rule_type": _("Rule type"), "identifier": _("Identifier"), "policy": _("Policy"), "description": _("Comment"),
    "tags": _("Tags"), "groups": _("Groups"), "custom_msg": _("Custom message"), "custom_url": _("Custom URL"),
    "cel_expr": _("CEL expression"),
}


def rule_type_choices(available=None):
    """The rule types with what they cover. A callable: the fields evaluate it in the language of each request."""
    def choices():
        return [(value, f"{label} – {RULE_TYPE_HELP[value]}") for value, label in RuleType.choices
                if available is None or value in available]
    return choices


def parse_new_tags(value):
    """Tags typed as "a, b": the existing ones, and the new ones created"""
    names = list(dict.fromkeys(name.strip()[:100] for name in (value or "").split(",") if name.strip()))
    return [Tag.objects.get_or_create(name=name)[0] for name in names]


class TagsMixin(forms.Form):
    tags = forms.ModelMultipleChoiceField(queryset=Tag.objects.all(), required=False, label=_("Tags"),
                                          widget=forms.SelectMultiple(attrs={"size": 4}),
                                          help_text=_("Only labels to find the rules, no effect on the Macs."))
    new_tags = forms.CharField(required=False, max_length=500, label=_("New tags"),
                               widget=forms.TextInput(attrs={"placeholder": _("comma separated")}))

    def all_tags(self):
        return list(self.cleaned_data.get("tags") or []) + parse_new_tags(self.cleaned_data.get("new_tags"))


class MachinesField(forms.CharField):
    """Macs by serial number or hostname, one per line or comma separated"""

    def __init__(self, **kwargs):
        kwargs.setdefault("required", False)
        kwargs.setdefault("widget", forms.Textarea(attrs={"rows": 2, "placeholder": _("serial numbers or hostnames")}))
        super().__init__(**kwargs)

    def prepare_value(self, value):
        if isinstance(value, (list, tuple)) or hasattr(value, "all"):
            machines = value.all() if hasattr(value, "all") else value
            return "\n".join(m.serial_number if isinstance(m, Machine) else str(m) for m in machines)
        return value

    def clean(self, value):
        names = [name.strip() for name in (super().clean(value) or "").replace(",", "\n").splitlines()
                 if name.strip()]
        machines, unknown = [], []
        for name in dict.fromkeys(names):
            machine = (Machine.objects.filter(serial_number__iexact=name).first()
                       or Machine.objects.filter(hostname__iexact=name).first())
            if machine:
                machines.append(machine)
            else:
                unknown.append(name)
        if unknown:
            raise ValidationError(gettext("Unknown Mac: %(names)s") % {"names": ", ".join(unknown)})
        return machines


class RuleForm(TagsMixin, forms.ModelForm):
    machines = MachinesField(label=_("Macs"))

    class Meta:
        model = Rule
        fields = ("rule_type", "identifier", "policy", "cel_expr", "custom_msg", "custom_url", "is_global", "groups",
                  "machines", "tags", "description", "is_enabled")
        widgets = {
            "groups": forms.CheckboxSelectMultiple,
            "cel_expr": forms.Textarea(attrs={"rows": 3, "class": "mono"}),
            "identifier": forms.TextInput(attrs={"class": "mono", "autocomplete": "off"}),
        }
        labels = RULE_LABELS

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["rule_type"].choices = rule_type_choices()
        self.fields["is_global"].label = _("All Macs (global)")
        self.fields["is_enabled"].label = _("Enabled")
        if self.instance.pk:
            self.initial["machines"] = self.instance.machines.all()

    def clean(self):
        cleaned_data = super().clean()
        if (not cleaned_data.get("is_global") and not cleaned_data.get("groups")
                and not cleaned_data.get("machines")):
            raise ValidationError(gettext("Choose a scope: global, some groups or some Macs."))
        return cleaned_data


class RuleBulkForm(forms.Form):
    ACTIONS = [("enable", _("Enable / approve")), ("disable", _("Disable")), ("add_tag", _("Add tag")),
               ("remove_tag", _("Remove tag")), ("add_groups", _("Add groups")), ("delete", _("Delete"))]

    action = forms.ChoiceField(choices=ACTIONS)
    rules = forms.ModelMultipleChoiceField(queryset=Rule.objects.all())
    tag = forms.CharField(required=False, max_length=100)
    groups = forms.ModelMultipleChoiceField(queryset=Group.objects.all(), required=False)

    def clean(self):
        cleaned_data = super().clean()
        action = cleaned_data.get("action")
        if action in ("add_tag", "remove_tag") and not (cleaned_data.get("tag") or "").strip():
            self.add_error("tag", gettext("Enter a tag."))
        if action == "add_groups" and not cleaned_data.get("groups"):
            self.add_error("groups", gettext("Choose at least one group."))
        return cleaned_data


class UploadBinaryForm(TagsMixin):
    file = forms.FileField(label=_("File"),
                           help_text=_("Mach-O binary, or a zip / tar archive (e.g. a GitHub release asset). "
                                       "The file is only hashed, it is not stored."))
    binary_pattern = forms.CharField(required=False, max_length=200, label=_("Binary pattern"),
                                     help_text=_("Archives only: glob of the files to use, e.g. */bin/colima"))
    rule_type = forms.ChoiceField(choices=rule_type_choices(), initial=RuleType.SIGNINGID, label=_("Rule type"))
    policy = forms.ChoiceField(choices=Policy.choices, initial=Policy.ALLOWLIST, label=_("Policy"))
    is_global = forms.BooleanField(required=False, label=_("All Macs (global)"))
    groups = forms.ModelMultipleChoiceField(queryset=Group.objects.all(), required=False, label=_("Groups"),
                                            widget=forms.CheckboxSelectMultiple)
    description = forms.CharField(max_length=500, required=False, label=_("Comment"))

    def clean(self):
        cleaned_data = super().clean()
        if not cleaned_data.get("is_global") and not cleaned_data.get("groups"):
            self.add_error("groups", gettext("Select at least one group, or choose the global scope."))
        return cleaned_data


class IdentifiersWidget(forms.Textarea):
    """The identifiers of a release source as chips, with catalog suggestions (console.js)"""
    template_name = "console/widgets/identifiers.html"

    def __init__(self, attrs=None):
        super().__init__(attrs={"rows": 3, "class": "mono", **(attrs or {})})

    def get_context(self, name, value, attrs):
        context = super().get_context(name, value, attrs)
        context["widget"]["icons"] = self.icons
        return context

    icons = {}


class ReleaseSourceForm(TagsMixin, forms.ModelForm):
    picked = forms.CharField(required=False, widget=forms.HiddenInput)

    class Meta:
        model = ReleaseSource
        fields = ("name", "kind", "identifier", "is_enabled", "version_pattern", "asset_pattern", "binary_pattern",
                  "include_prereleases", "rule_type", "policy", "cel_expr", "custom_msg", "custom_url", "is_global",
                  "groups", "auto_approve", "auto_approve_delay_days", "keep_versions", "tags")
        widgets = {
            "identifier": IdentifiersWidget,
            "groups": forms.CheckboxSelectMultiple,
            "cel_expr": forms.Textarea(attrs={"rows": 3, "class": "mono"}),
        }
        labels = {
            **RULE_LABELS, "name": _("Name"), "version_pattern": _("Version pattern"),
            "asset_pattern": _("Asset pattern"), "binary_pattern": _("Binary pattern"),
            "include_prereleases": _("Include prereleases"), "rule_type": _("Preferred rule type"),
            "auto_approve": _("Auto approve"), "auto_approve_delay_days": _("Auto approve delay (days)"),
            "keep_versions": _("Keep versions"),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["identifier"].widget.icons = self.instance.identifier_icons or {}
        self.fields["identifier"].label = _("Packages")
        self.fields["kind"].label = _("Catalog")
        self.fields["rule_type"].choices = rule_type_choices()
        self.fields["is_global"].label = _("All Macs (global)")
        self.fields["is_enabled"].label = _("Enabled")

    def clean_picked(self):
        """{identifier: {name, icon_url}} of the suggestions chosen in the browser"""
        try:
            data = json.loads(self.cleaned_data["picked"] or "{}")
        except ValueError:
            return {}
        picked = {}
        for identifier, item in (data.items() if isinstance(data, dict) else []):
            if not isinstance(item, dict):
                continue
            picked[str(identifier)[:450]] = {"name": str(item.get("name") or "")[:200],
                                              "icon_url": clean_icon_url(item.get("icon_url"))}
        return picked

    def save(self, commit=True):
        source = super().save(commit=False)
        if "kind" in self.changed_data:
            source.identifier_icons = {}
        update_identifier_icons(source, self.cleaned_data.get("picked"))
        if commit:
            source.save()
            self.save_m2m()
            source.tags.add(*parse_new_tags(self.cleaned_data.get("new_tags")))
        return source


class AllowRowForm(TagsMixin):
    """One binary of the selected events, each with its own rule type, policy, scope and tags

    Only new_tags is shown: it suggests the existing tags, and parse_new_tags reuses them.
    """
    event = forms.IntegerField(widget=forms.HiddenInput)
    include = forms.BooleanField(required=False, initial=True, label=_("Include"))
    rule_type = forms.ChoiceField(choices=rule_type_choices(), label=_("Rule type"))
    policy = forms.ChoiceField(choices=ALLOW_POLICIES, initial=Policy.ALLOWLIST, label=_("Policy"))
    scope = forms.ChoiceField(choices=[(SCOPE_GROUPS, _("Selected groups")), (SCOPE_MACHINES, _("Only these Macs")),
                                       (SCOPE_GLOBAL, _("All Macs"))], initial=SCOPE_GROUPS, label=_("Scope"))
    description = forms.CharField(max_length=500, required=False, label=_("Comment"))


AllowFormSet = formset_factory(AllowRowForm, extra=0)


class AllowSharedForm(forms.Form):
    groups = forms.ModelMultipleChoiceField(queryset=Group.objects.all(), required=False, label=_("Groups"),
                                            widget=forms.CheckboxSelectMultiple,
                                            help_text=_("For the rows with the “Selected groups” scope."))


class RequestEventForm(forms.Form):
    event = forms.ModelChoiceField(queryset=None, widget=forms.RadioSelect, empty_label=None,
                                   label=_("Blocked program"))
    justification = forms.CharField(max_length=1000, widget=forms.Textarea(attrs={"rows": 4}),
                                    label=_("Justification"), help_text=_("Why do you need it?"))

    def __init__(self, *args, events=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["event"].queryset = events
        self.fields["event"].label_from_instance = (
            lambda e: f"{e.file_name or e.file_path} – {e.machine.hostname or e.machine.serial_number}, "
                      f"{timezone.localtime(e.execution_time):%d.%m.%Y %H:%M}")


REQUESTABLE_PACKAGE_KINDS = (
    ReleaseSource.Kind.HOMEBREW_FORMULA, ReleaseSource.Kind.HOMEBREW_CASK, ReleaseSource.Kind.NPM_PACKAGE,
    ReleaseSource.Kind.VSCODE_EXTENSION, ReleaseSource.Kind.JETBRAINS_PLUGIN, ReleaseSource.Kind.GITHUB_RELEASE,
)
MAX_PACKAGES_PER_REQUEST = 20


def clean_icon_url(value):
    # shown in an <img>: only https URLs from the catalogs
    value = str(value or "")
    return value[:800] if value.startswith("https://") else ""


class RequestPackageForm(forms.Form):
    # [{"kind": …, "identifier": …, "name": …, "icon_url": …}], filled by console.js
    packages = forms.CharField(widget=forms.HiddenInput, required=False)
    justification = forms.CharField(max_length=1000, widget=forms.Textarea(attrs={"rows": 4}),
                                    label=_("Justification"), help_text=_("Why do you need them?"))

    def clean_packages(self):
        try:
            data = json.loads(self.cleaned_data["packages"] or "[]")
        except ValueError:
            data = []
        packages = {}
        for item in data if isinstance(data, list) else []:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("kind") or "")
            identifier = str(item.get("identifier") or "").strip()
            if kind not in REQUESTABLE_PACKAGE_KINDS or not identifier:
                continue
            if len(identifier) > 450:
                raise ValidationError(gettext("%(identifier)s…: too long.") % {"identifier": identifier[:50]})
            packages.setdefault((kind, identifier), {
                "kind": kind, "identifier": identifier, "name": str(item.get("name") or "")[:200],
                "icon_url": clean_icon_url(item.get("icon_url")),
            })
        if not packages:
            raise ValidationError(gettext("Add at least one package."))
        if len(packages) > MAX_PACKAGES_PER_REQUEST:
            raise ValidationError(gettext("At most %(max)s packages per request.") % {"max": MAX_PACKAGES_PER_REQUEST})
        return list(packages.values())


class RequestOtherForm(forms.Form):
    title = forms.CharField(max_length=200, label=_("What do you need?"))
    link = forms.URLField(max_length=800, required=False, label=_("Link"),
                          help_text=_("Download or product page, optional"))
    justification = forms.CharField(max_length=1000, widget=forms.Textarea(attrs={"rows": 4}),
                                    label=_("Justification"), help_text=_("Why do you need it?"))


class ApproveEventForm(TagsMixin):
    field_order = ["rule_type", "policy", "scope", "groups", "tags", "new_tags", "note"]

    rule_type = forms.ChoiceField(choices=rule_type_choices(), label=_("Rule type"))
    policy = forms.ChoiceField(choices=ALLOW_POLICIES, initial=Policy.ALLOWLIST, label=_("Policy"))
    scope = forms.ChoiceField(choices=[(SCOPE_MACHINES, _("The Mac of the requester")),
                                       (SCOPE_GROUPS, _("Groups")), (SCOPE_GLOBAL, _("All Macs"))],
                              initial=SCOPE_MACHINES, widget=forms.RadioSelect, label=_("Scope"))
    groups = forms.ModelMultipleChoiceField(queryset=Group.objects.all(), required=False, label=_("Groups"),
                                            widget=forms.CheckboxSelectMultiple)
    note = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}), label=_("Note"),
                           help_text=_("Shown to the user"))

    def __init__(self, *args, available_types=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["rule_type"].choices = rule_type_choices(available_types)

    def clean(self):
        cleaned_data = super().clean()
        if cleaned_data.get("scope") == SCOPE_GROUPS and not cleaned_data.get("groups"):
            self.add_error("groups", gettext("Choose at least one group."))
        return cleaned_data


class ApprovePackagesForm(forms.Form):
    """One row per requested package: approve it or not, into a new or an existing package rule.

    The approved packages with "new package rule" become one new package rule per catalog,
    with all of them as identifiers.
    """
    NEW = "new"

    new_name = forms.CharField(max_length=180, required=False, label=_("Name of the new package rule"),
                               help_text=_("One per catalog; with several catalogs the catalog is added to the name."))
    rule_type = forms.ChoiceField(choices=rule_type_choices(), initial=RuleType.BINARY, required=False,
                                  label=_("Preferred rule type"))
    is_global = forms.BooleanField(required=False, label=_("All Macs (global)"))
    groups = forms.ModelMultipleChoiceField(queryset=Group.objects.all(), required=False, label=_("Groups"),
                                            widget=forms.CheckboxSelectMultiple)
    note = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}), label=_("Note"),
                           help_text=_("Shown to the user"))

    def __init__(self, *args, packages=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.packages = list(packages)
        sources = {}
        for source in ReleaseSource.objects.filter(kind__in={p.kind for p in self.packages}).order_by("name"):
            sources.setdefault(source.kind, []).append(source)
        self.sources = {source.pk: source for items in sources.values() for source in items}
        for package in self.packages:
            self.fields[f"approve_{package.pk}"] = forms.BooleanField(
                required=False, initial=True,
                label=gettext("Approve %(identifier)s") % {"identifier": package.identifier})
            choices = [(self.NEW, gettext("New package rule"))]
            choices += [(str(source.pk), gettext("Add to %(name)s") % {"name": source.name})
                        for source in sources.get(package.kind, [])]
            existing = next((source for source in sources.get(package.kind, [])
                             if package.identifier in source.identifiers), None)
            self.fields[f"target_{package.pk}"] = forms.ChoiceField(
                choices=choices, initial=str(existing.pk) if existing else self.NEW, label=_("Package rule"))

    def rows(self):
        return [(package, self[f"approve_{package.pk}"], self[f"target_{package.pk}"]) for package in self.packages]

    def approved(self):
        """[(package, target source or None for a new one)] of the approved packages"""
        return [(package, self.sources.get(int(self.cleaned_data[f"target_{package.pk}"]))
                 if self.cleaned_data[f"target_{package.pk}"] != self.NEW else None)
                for package in self.packages if self.cleaned_data.get(f"approve_{package.pk}")]

    def new_rule_names(self):
        """{kind: name} of the new package rules"""
        kinds = list(dict.fromkeys(package.kind for package, target in self.approved() if target is None))
        name = (self.cleaned_data.get("new_name") or "").strip()
        if len(kinds) == 1:
            return {kinds[0]: name}
        return {kind: f"{name} ({ReleaseSource.Kind(kind).label})" for kind in kinds}

    def clean(self):
        cleaned_data = super().clean()
        if self.errors:
            return cleaned_data
        approved = self.approved()
        if not approved:
            raise ValidationError(gettext("Approve at least one package, or deny the request."))
        names = self.new_rule_names()
        if names:
            if not (cleaned_data.get("new_name") or "").strip():
                self.add_error("new_name", gettext("Enter a name for the new package rule."))
            for name in names.values():
                if name and ReleaseSource.objects.filter(name=name).exists():
                    self.add_error("new_name", gettext("A package rule “%(name)s” exists already.") % {"name": name})
            if not cleaned_data.get("is_global") and not cleaned_data.get("groups"):
                self.add_error("groups", gettext("Choose at least one group for the new package rule, or all Macs."))
        return cleaned_data


class ApproveOtherForm(forms.Form):
    rule = forms.ModelChoiceField(queryset=Rule.objects.all(), required=False, widget=forms.HiddenInput)
    rule_identifier = forms.CharField(required=False, label=_("Rule"), max_length=256,
                                      help_text=_("Optional: identifier of the rule you created for it"))
    note = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}), label=_("Note"),
                           help_text=_("Shown to the user"))

    def clean(self):
        cleaned_data = super().clean()
        identifier = (cleaned_data.get("rule_identifier") or "").strip()
        if identifier:
            rule = Rule.objects.filter(identifier__iexact=identifier).order_by("pk").first()
            if rule is None:
                self.add_error("rule_identifier", gettext("No rule with this identifier."))
            cleaned_data["rule"] = rule
        return cleaned_data


class DenyForm(forms.Form):
    note = forms.CharField(widget=forms.Textarea(attrs={"rows": 2}), help_text=_("Shown to the user"))


class RequestFilterForm(forms.Form):
    status = forms.ChoiceField(choices=[("", _("All"))] + AccessRequest.Status.choices, required=False)


class GroupForm(forms.ModelForm):
    class Meta:
        model = Group
        fields = ("name", "description", "client_mode", "batch_size", "full_sync_interval", "allowed_path_regex",
                  "blocked_path_regex", "enable_transitive_rules", "enable_bundles", "enable_all_event_upload",
                  "block_usb_mount", "remount_usb_mode", "event_detail_url", "event_detail_text",
                  "unknown_block_message", "banned_block_message", "enable_bad_signature_protection")
        widgets = {
            "description": forms.Textarea(attrs={"rows": 2}),
            "client_mode": forms.RadioSelect,
            "allowed_path_regex": forms.Textarea(attrs={"rows": 3, "class": "mono"}),
            "blocked_path_regex": forms.Textarea(attrs={"rows": 3, "class": "mono"}),
            "unknown_block_message": forms.Textarea(attrs={"rows": 2}),
            "banned_block_message": forms.Textarea(attrs={"rows": 2}),
        }
        labels = {
            "name": _("Name"), "description": _("Description"), "client_mode": _("Client mode"),
            "batch_size": _("Batch size"), "full_sync_interval": _("Full sync interval"),
            "allowed_path_regex": _("Allowed path regexes"), "blocked_path_regex": _("Blocked path regexes"),
            "enable_transitive_rules": _("Enable transitive rules"), "enable_bundles": _("Enable bundles"),
            "enable_all_event_upload": _("Enable all event upload"), "block_usb_mount": _("Block USB mass storage"),
            "remount_usb_mode": _("Remount USB with flags"), "event_detail_url": _("Block dialog URL"),
            "event_detail_text": _("Block dialog button text"), "unknown_block_message": _("Unknown block message"),
            "banned_block_message": _("Banned block message"),
            "enable_bad_signature_protection": _("Enable bad signature protection"),
        }

    # the sections of the form page, like in the admin
    SECTIONS = [
        (_("Group"), "", ("name", "description")),
        (_("Santa configuration"), _("Sent to the Macs at every sync."), ("client_mode", "batch_size",
                                                                           "full_sync_interval")),
        (_("Path regexes"), _("One per line, combined into one regex for Santa. Rules always win over the regexes."),
         ("allowed_path_regex", "blocked_path_regex")),
        (_("Advanced"), "", ("enable_transitive_rules", "enable_bundles", "enable_all_event_upload")),
        ("USB", "", ("block_usb_mount", "remount_usb_mode")),
        (_("Block dialog"), _("Sent at every sync, no profile change needed."),
         ("event_detail_url", "event_detail_text")),
        (_("Profile only"), _("Only in the .mobileconfig: after a change, download the profile again and "
                              "replace it in your MDM."),
         ("unknown_block_message", "banned_block_message", "enable_bad_signature_protection")),
    ]

    def sections(self):
        return [(title, help_text, [self[name] for name in names]) for title, help_text, names in self.SECTIONS]


class ProfileForm(forms.ModelForm):
    class Meta:
        model = UserProfile
        fields = ("theme", "language")
        widgets = {"theme": forms.RadioSelect}
        labels = {"theme": _("Theme"), "language": _("Language")}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["language"].choices = [("", _("Language of the browser"))] + list(settings.LANGUAGES)


# Administration


class RolesField(forms.ModelMultipleChoiceField):
    def __init__(self, **kwargs):
        kwargs.setdefault("queryset", AuthGroup.objects.order_by("name"))
        kwargs.setdefault("required", False)
        kwargs.setdefault("widget", forms.CheckboxSelectMultiple)
        super().__init__(**kwargs)


class UserForm(forms.ModelForm):
    """A user of the console. Staff and the password only for the local accounts: the sign-in sets the others."""

    roles = RolesField(label=_("Roles"))
    password1 = forms.CharField(label=_("Password"), required=False, strip=False,
                                widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}))
    password2 = forms.CharField(label=_("Password again"), required=False, strip=False,
                                widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}))

    class Meta:
        model = User
        fields = ("username", "first_name", "last_name", "email", "is_active", "is_staff")
        labels = {
            "username": _("Username"), "first_name": _("First name"), "last_name": _("Last name"),
            "email": _("E-mail"), "is_active": _("Active"), "is_staff": _("Console access"),
        }
        help_texts = {
            "username": _("For a local account, e.g. break-glass-admin. Users of the sign-in are created at their "
                          "first sign-in."),
            "is_active": _("Inactive users can't sign in."),
            "is_staff": _("Can open the console, with the permissions of the roles."),
        }

    def __init__(self, *args, managed_roles=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.managed_roles = set(managed_roles)
        user = self.instance
        self.is_local = user.pk is None or user.has_usable_password()
        self.fields["roles"].queryset = AuthGroup.objects.exclude(
            pk__in=[role.pk for role in self.managed_roles]).order_by("name")
        if user.pk:
            self.initial["roles"] = [role for role in user.groups.all() if role not in self.managed_roles]
            self.fields["username"].disabled = True
            self.fields["password1"].help_text = _("Leave empty to keep the password.")
        else:
            self.fields["password1"].required = self.fields["password2"].required = True
        if not self.is_local:
            # the sign-in sets them at every sign-in
            for name in ("first_name", "last_name", "email", "is_staff", "password1", "password2"):
                del self.fields[name]

    def clean(self):
        cleaned_data = super().clean()
        password = cleaned_data.get("password1")
        if password or cleaned_data.get("password2"):
            if password != cleaned_data.get("password2"):
                self.add_error("password2", gettext("The two passwords are different."))
            else:
                try:
                    password_validation.validate_password(password, self.instance)
                except ValidationError as e:
                    self.add_error("password1", e)
        return cleaned_data

    def save(self, commit=True):
        user = super().save(commit=False)
        if self.cleaned_data.get("password1"):
            user.set_password(self.cleaned_data["password1"])
        user.save()
        kept = [role for role in user.groups.all() if role in self.managed_roles]
        user.groups.set([*kept, *self.cleaned_data["roles"]])
        return user


class RoleForm(forms.ModelForm):
    permissions = forms.ModelMultipleChoiceField(queryset=Permission.objects.none(), required=False,
                                                 widget=forms.CheckboxSelectMultiple)

    class Meta:
        model = AuthGroup
        fields = ("name", "permissions")
        labels = {"name": _("Name")}

    def __init__(self, *args, permissions=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["permissions"].queryset = permissions


class SignInGroupForm(forms.ModelForm):
    roles = RolesField(label=_("Roles"), help_text=_("The members get these roles"))

    class Meta:
        model = SignInGroup
        fields = ("name", "claim_value", "console_access", "roles")
        labels = {"name": _("Name"), "claim_value": _("Value in the groups claim"),
                  "console_access": _("Console access")}
        widgets = {"claim_value": forms.TextInput(attrs={"class": "mono", "autocomplete": "off"})}


class TagForm(forms.ModelForm):
    class Meta:
        model = Tag
        fields = ("name", "description")
        labels = {"name": _("Name"), "description": _("Description")}


class ConfigImportForm(forms.Form):
    file = forms.FileField(label=_("File"), help_text=_("JSON file of “Export configuration”, e.g. from the test "
                                                        "server, or of the export_config command."))
    delete_missing = forms.BooleanField(
        required=False, label=_("Delete what is not in the file"),
        help_text=_("Deletes the manual rules and package rules that are not in the file."))
    dry_run = forms.BooleanField(required=False, initial=True, label=_("Dry run"),
                                 help_text=_("Only show what would change. Uncheck to import."))
