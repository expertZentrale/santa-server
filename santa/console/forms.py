import base64
import json
import re

from django import forms
from django.conf import settings
from django.contrib.auth import password_validation
from django.contrib.auth.models import Group as AuthGroup
from django.contrib.auth.models import Permission, User
from django.core.exceptions import ValidationError
from django.utils import timezone
from django.utils.text import format_lazy
from django.utils.translation import gettext
from django.utils.translation import gettext_lazy as _

from ..catalog import update_identifier_icons
from ..models import (
    BRANDING_LOGO_MAX_BYTES,
    BRANDING_LOGO_TYPES,
    AccessRequest,
    FileAccessProcess,
    FileAccessRule,
    FileAccessRuleType,
    Group,
    Machine,
    OnStartUSBOption,
    Policy,
    ReleaseSource,
    RemovableMediaAction,
    Rule,
    RuleType,
    SignInGroup,
    Tag,
    UserProfile,
    validate_logo_url,
)
from ..users import time_zone_names
from ..validators import validate_identifier

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
# rules from events can also block; CEL needs an expression per rule, that is the rule form
EVENT_POLICIES = [*ALLOW_POLICIES, (Policy.BLOCKLIST, _("Block")), (Policy.SILENT_BLOCKLIST, _("Block silently")),
                  (Policy.CEL, Policy.CEL.label)]
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
                                          widget=forms.SelectMultiple(attrs={
                                              "size": 4, "data-placeholder": _("Add a tag, Enter creates a new one"),
                                              "data-remove-label": _("Remove")}),
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

    def has_changed(self, initial, data):
        # the initial value is a queryset, the data a text: compare the Macs
        try:
            new = {machine.pk for machine in self.clean(data)}
        except ValidationError:
            return True
        old = {machine.pk for machine in (initial.all() if hasattr(initial, "all") else initial or [])}
        return new != old


# Suggestions for the CEL field (cel.js): fields and global functions start a value, functions follow one (".").
# They only complete: Santa evaluates the expression, so fields of newer
# Santa versions work without being listed. Fields: https://northpole.dev/features/binary-authorization/
CEL_SUGGESTIONS = [
    ("target.signing_id", "field", _("TEAMID:bundle.id of the binary (cached)")),
    ("target.team_id", "field", _("Team ID of the binary (cached)")),
    ("target.signing_time", "field", _("Signing time, as timestamp (cached)")),
    ("target.secure_signing_time", "field", _("Signing time from a trusted time stamp (cached)")),
    ("target.is_platform_binary", "field", _("Part of macOS (cached)")),
    ("path", "field", _("Path of the binary (not cached, slower)")),
    ("args", "field", _("Arguments, a list (not cached, slower)")),
    ("envs", "field", _("Environment variables, a map (not cached, slower)")),
    ("euid", "field", _("Effective user ID (not cached, slower)")),
    ("cwd", "field", _("Working directory (not cached, slower)")),
    ("ancestors", "field", _("Parent processes, each with path, signing_id, team_id, cdhash, args (not cached)")),
    ("ALLOWLIST", "result", _("Allow")),
    ("BLOCKLIST", "result", _("Block")),
    ('startsWith("")', "function", _("String starts with")),
    ('endsWith("")', "function", _("String ends with")),
    ('contains("")', "function", _("String contains")),
    ('matches("")', "function", _("Regular expression (RE2)")),
    ("size()", "function", _("Length of a string or list")),
    ("exists(x, )", "function", _("Any element of a list matches")),
    ("all(x, )", "function", _("Every element of a list matches")),
    ('timestamp("")', "global", _("Time, e.g. timestamp(\"2025-01-01T00:00:00Z\")")),
    ('duration("")', "global", _("Duration, e.g. duration(\"24h\")")),
    (" in []", "function", _("Value in a list")),
]


def cel_suggestions_json():
    return json.dumps([{"text": text, "kind": kind, "help": str(help_text)}
                       for text, kind, help_text in CEL_SUGGESTIONS])


# "Only signing IDs starting with": a Team ID rule with this CEL expression (Santa has no wildcards)
SIGNING_PREFIX_RE = re.compile(r"^[A-Za-z0-9._-]+$")
PREFIX_EXPRESSION_RE = re.compile(
    r'^\(target\.signing_id\.startsWith\("[A-Z0-9]{10}:[A-Za-z0-9._-]+"\)'
    r'(?: \|\| target\.signing_id\.startsWith\("[A-Z0-9]{10}:[A-Za-z0-9._-]+"\))*\) \? ALLOWLIST : BLOCKLIST$')


def prefix_expression(team_id, prefixes):
    tests = " || ".join(f'target.signing_id.startsWith("{team_id}:{prefix}")' for prefix in prefixes)
    return f"({tests}) ? ALLOWLIST : BLOCKLIST"


def parse_prefix_expression(expression):
    """The prefixes of an expression written by prefix_expression(), else None"""
    if not PREFIX_EXPRESSION_RE.match(expression or ""):
        return None
    return re.findall(r'startsWith\("[A-Z0-9]{10}:([A-Za-z0-9._-]+)"\)', expression)


def signing_prefixes_field():
    return forms.CharField(
        required=False, label=_("Only signing IDs starting with"),
        widget=forms.Textarea(attrs={"rows": 2, "class": "mono", "placeholder": "com.microsoft.teams2"}),
        help_text=_("One bundle ID prefix per line. Writes the CEL expression: the binaries of this team whose "
                    "signing ID starts with one of them are allowed, the others are blocked, also in monitor mode. "
                    "Rules for a signing ID or binary still win."))


def clean_signing_prefixes(form, value):
    """The prefixes of the field as a list; an error on the field for invalid ones"""
    prefixes = [line.strip() for line in (value or "").splitlines() if line.strip()]
    invalid = [prefix for prefix in prefixes if not SIGNING_PREFIX_RE.match(prefix)]
    if invalid:
        form.add_error("signing_prefixes", gettext("Only letters, digits, dots, hyphens and underscores: "
                                                   "%(prefixes)s") % {"prefixes": ", ".join(invalid)})
        return []
    return prefixes


class RuleForm(TagsMixin, forms.ModelForm):
    machines = MachinesField(label=_("Macs"))
    signing_prefixes = signing_prefixes_field()

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
        self.fields["cel_expr"].widget.attrs["data-cel-suggestions"] = cel_suggestions_json()
        if self.instance.pk:
            self.initial["machines"] = self.instance.machines.all()
            prefixes = parse_prefix_expression(self.instance.cel_expr)
            if prefixes:
                self.initial["signing_prefixes"] = "\n".join(prefixes)

    def clean(self):
        cleaned_data = super().clean()
        if (not cleaned_data.get("is_global") and not cleaned_data.get("groups")
                and not cleaned_data.get("machines")):
            raise ValidationError(gettext("Choose a scope: global, some groups or some Macs."))
        if cleaned_data.get("rule_type") == RuleType.TEAMID and cleaned_data.get("policy") == Policy.CEL:
            prefixes = clean_signing_prefixes(self, cleaned_data.get("signing_prefixes"))
        else:
            prefixes = []
        if prefixes:
            expression = cleaned_data.get("cel_expr") or ""
            if expression and parse_prefix_expression(expression) is None:
                # never overwrite an expression written by hand
                self.add_error("signing_prefixes", gettext("Clear the CEL expression or the prefixes."))
            else:
                cleaned_data["cel_expr"] = prefix_expression(cleaned_data.get("identifier", ""), prefixes)
        return cleaned_data


class RuleBulkForm(forms.Form):
    ACTIONS = [("enable", _("Enable / approve")), ("disable", _("Disable")), ("add_tag", _("Add tag")),
               ("remove_tag", _("Remove tag")), ("set_policy", _("Set policy")), ("add_groups", _("Add groups")),
               ("delete", _("Delete"))]
    # CEL needs an expression per rule, not a bulk change
    POLICIES = [(value, label) for value, label in Policy.choices if value != Policy.CEL]

    action = forms.ChoiceField(choices=ACTIONS)
    rules = forms.ModelMultipleChoiceField(queryset=Rule.objects.all())
    tag = forms.CharField(required=False, max_length=100)
    groups = forms.ModelMultipleChoiceField(queryset=Group.objects.all(), required=False)
    policy = forms.ChoiceField(choices=POLICIES, required=False)

    def clean(self):
        cleaned_data = super().clean()
        action = cleaned_data.get("action")
        if action == "set_policy" and not cleaned_data.get("policy"):
            self.add_error("policy", gettext("Choose a policy."))
        if action in ("add_tag", "remove_tag") and not (cleaned_data.get("tag") or "").strip():
            self.add_error("tag", gettext("Enter a tag."))
        if action == "add_groups" and not cleaned_data.get("groups"):
            self.add_error("groups", gettext("Choose at least one group."))
        return cleaned_data


class FileDropInput(forms.FileInput):
    """A file input as a drop zone: the input covers the zone, so a file can be dropped on it (console/upload.js)"""
    template_name = "console/widgets/file_drop.html"


class UploadBinaryForm(TagsMixin):
    file = forms.FileField(label=_("File"), widget=FileDropInput,
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
    """The identifiers of a release source as chips, with catalog suggestions (console/suggestions.js)"""
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
        self.fields["cel_expr"].widget.attrs["data-cel-suggestions"] = cel_suggestions_json()
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


class EventRuleForm(TagsMixin):
    """One rule decision for the binaries of the selected events (one binary: the drawer of an event)

    rows: the events grouped by binary. With several binaries the rule type can stay "suggested per binary",
    and a type a binary doesn't have falls back to its suggestion (the view decides, see rule_type_for).
    """
    field_order = ["rule_type", "policy", "signing_prefixes", "cel_expr", "scope", "groups", "tags", "new_tags",
                   "description", "include"]

    rule_type = forms.ChoiceField(label=_("Rule type"), required=False)
    policy = forms.ChoiceField(choices=EVENT_POLICIES, initial=Policy.ALLOWLIST, label=_("Policy"))
    signing_prefixes = signing_prefixes_field()
    cel_expr = forms.CharField(required=False, label=_("CEL expression"),
                               widget=forms.Textarea(attrs={"rows": 3, "class": "mono"}))
    scope = forms.ChoiceField(choices=[(SCOPE_MACHINES, _("The Macs of these events")), (SCOPE_GROUPS, _("Groups")),
                                       (SCOPE_GLOBAL, _("All Macs"))],
                              initial=SCOPE_GROUPS, widget=forms.RadioSelect, label=_("Scope"))
    groups = forms.ModelMultipleChoiceField(queryset=Group.objects.all(), required=False, label=_("Groups"),
                                            widget=forms.CheckboxSelectMultiple)
    description = forms.CharField(max_length=500, required=False, label=_("Comment"),
                                  help_text=_("Empty: the name and path of each binary."))
    include = forms.MultipleChoiceField(label=_("Binaries"), widget=forms.CheckboxSelectMultiple,
                                        error_messages={"required": _("Choose at least one binary.")})

    def __init__(self, *args, rows, available_types=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.rows = rows
        self.several = len(rows) > 1
        types = rule_type_choices(available_types)
        if self.several:
            self.fields["rule_type"].choices = lambda: [("", _("Suggested per binary")), *types()]
        else:
            self.fields["rule_type"].choices = types
            self.fields["rule_type"].required = True
        if len({event.machine_id for row in rows for event in row}) == 1:
            self.fields["scope"].choices = [(SCOPE_MACHINES, _("This Mac")), (SCOPE_GROUPS, _("Groups")),
                                            (SCOPE_GLOBAL, _("All Macs"))]
        self.fields["cel_expr"].widget.attrs["data-cel-suggestions"] = cel_suggestions_json()
        # a binary is its SHA-256: the ids of its events change with every new event
        self.fields["include"].choices = [(row[0].file_sha256, row[0].file_name or row[0].file_sha256) for row in rows]

    def clean(self):
        cleaned_data = super().clean()
        if cleaned_data.get("scope") == SCOPE_GROUPS and not cleaned_data.get("groups"):
            self.add_error("groups", gettext("Choose at least one group."))
        cel = cleaned_data.get("policy") == Policy.CEL
        cel_expr = (cleaned_data.get("cel_expr") or "").strip()
        prefixes = []
        if cel and cleaned_data.get("rule_type") == RuleType.TEAMID:
            prefixes = clean_signing_prefixes(self, cleaned_data.get("signing_prefixes"))
        if prefixes:
            # the expression is written per binary, from its own Team ID (see cel_expr_for)
            if cel_expr and parse_prefix_expression(cel_expr) is None:
                self.add_error("signing_prefixes", gettext("Clear the CEL expression or the prefixes."))
            chosen = set(cleaned_data.get("include") or [])
            unsigned = [row[0].file_name or row[0].file_sha256 for row in self.rows
                        if row[0].file_sha256 in chosen and not row[0].team_id]
            if unsigned:
                self.add_error("include", gettext("No Team ID, so no signing ID prefixes: %(binaries)s") % {
                    "binaries": ", ".join(unsigned)})
        elif cel and not cel_expr:
            self.add_error("cel_expr", gettext("Required for the CEL policy."))
        # only the CEL policy uses them: a leftover of another choice is dropped
        cleaned_data["cel_expr"] = cel_expr if cel else ""
        cleaned_data["signing_prefixes"] = prefixes
        return cleaned_data

    @property
    def team_id(self):
        """The Team ID of the binaries if they all have the same one: the expression can be shown while typing"""
        teams = {row[0].team_id for row in self.rows}
        return teams.pop() if len(teams) == 1 else ""

    def cel_expr_for(self, event):
        prefixes = self.cleaned_data["signing_prefixes"]
        return prefix_expression(event.team_id, prefixes) if prefixes else self.cleaned_data["cel_expr"]

    def included_rows(self):
        chosen = set(self.cleaned_data.get("include") or [])
        return [row for row in self.rows if row[0].file_sha256 in chosen]


class RequestEventForm(forms.Form):
    event = forms.ModelChoiceField(queryset=None, widget=forms.RadioSelect, empty_label=None,
                                   label=_("Blocked program"))
    justification = forms.CharField(max_length=1000, widget=forms.Textarea(attrs={"rows": 4}),
                                    label=_("Justification"), help_text=_("Why do you need it?"))

    def __init__(self, *args, events=None, requested=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["event"].queryset = events
        # {sha256: request of someone else}, shown as a badge next to the binary
        self.requested = requested or {}
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
    # [{"kind": …, "identifier": …, "name": …, "icon_url": …}], filled by console/suggestions.js
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
                              initial=SCOPE_GROUPS, widget=forms.RadioSelect, label=_("Scope"))
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


class ApproveOtherForm(TagsMixin):
    """Approve a request for other software: create the rule for it right here, a manual rule or a package rule"""

    RULE, PACKAGE, EXISTING, NONE = "rule", "package", "existing", "none"
    NEW = "new"

    result = forms.ChoiceField(
        choices=[(RULE, _("New execution rule")), (PACKAGE, _("Package rule")),
                 (EXISTING, _("Existing execution rule")), (NONE, _("No rule"))],
        initial=RULE, widget=forms.RadioSelect, label=_("Approve with"))

    # a new execution rule
    rule_type = forms.ChoiceField(choices=rule_type_choices(), initial=RuleType.SIGNINGID, label=_("Rule type"))
    identifier = forms.CharField(required=False, max_length=256, label=_("Identifier"),
                                 widget=forms.TextInput(attrs={"class": "mono", "autocomplete": "off"}),
                                 help_text=_("Or upload the app below: the server reads the identifier from it."))
    file = forms.FileField(required=False, label=_("File"), widget=FileDropInput,
                           help_text=_("Mach-O binary, or a zip / tar archive. Every executable in it gets a rule. "
                                       "The file is only hashed, it is not stored."))
    binary_pattern = forms.CharField(required=False, max_length=200, label=_("Binary pattern"),
                                     help_text=_("Archives only: glob of the files to use, e.g. */bin/colima"))
    policy = forms.ChoiceField(choices=ALLOW_POLICIES, initial=Policy.ALLOWLIST, label=_("Policy"))
    scope = forms.ChoiceField(choices=[(SCOPE_MACHINES, _("The Macs of the requester")),
                                       (SCOPE_GROUPS, _("Groups")), (SCOPE_GLOBAL, _("All Macs"))],
                              initial=SCOPE_GROUPS, widget=forms.RadioSelect, label=_("Scope"))
    groups = forms.ModelMultipleChoiceField(queryset=Group.objects.all(), required=False, label=_("Groups"),
                                            widget=forms.CheckboxSelectMultiple)

    # a package rule: a new one, or more identifiers for an existing one
    package_target = forms.ChoiceField(label=_("Package rule"))
    package_kind = forms.ChoiceField(choices=ReleaseSource.Kind.choices, initial=ReleaseSource.Kind.GITHUB_RELEASE,
                                     label=_("Catalog"))
    package_identifiers = forms.CharField(
        required=False, label=_("Packages"), widget=forms.Textarea(attrs={"rows": 2, "class": "mono"}),
        help_text=_("One per line. GitHub: owner/repo · Homebrew: formula or cask name · URL: the full URL · "
                    "npm: package name · VS Code: publisher.name · JetBrains: plugin ID"))
    package_name = forms.CharField(required=False, max_length=180, label=_("Name of the new package rule"))
    package_rule_type = forms.ChoiceField(choices=rule_type_choices(), initial=RuleType.BINARY,
                                          label=_("Preferred rule type"))
    package_auto_approve = forms.BooleanField(required=False, initial=True, label=_("Auto approve"),
                                              help_text=_("Enable the rules of new releases automatically."))
    package_is_global = forms.BooleanField(required=False, label=_("All Macs (global)"))
    package_groups = forms.ModelMultipleChoiceField(queryset=Group.objects.all(), required=False, label=_("Groups"),
                                                    widget=forms.CheckboxSelectMultiple)

    # an existing rule
    # with the permission to view the rules, a search (_approve_other.html, suggestions.js)
    rule_identifier = forms.CharField(required=False, label=_("Rule"), max_length=256,
                                      help_text=_("Identifier of the rule you created for it"),
                                      widget=forms.TextInput(attrs={"class": "mono", "autocomplete": "off"}))
    # the rule picked from the suggestions: one identifier can have several rules (scopes)
    rule = forms.ModelChoiceField(queryset=Rule.objects.all(), required=False, widget=forms.HiddenInput)

    note = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}), label=_("Note"),
                           help_text=_("Shown to the user"))

    field_order = ["result", "rule_type", "identifier", "file", "binary_pattern", "policy", "scope", "groups",
                   "tags", "new_tags", "package_target", "package_kind", "package_identifiers", "package_name",
                   "package_rule_type", "package_auto_approve", "package_is_global", "package_groups",
                   "rule_identifier", "rule", "note"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["rule_type"].choices = rule_type_choices()
        self.fields["package_rule_type"].choices = rule_type_choices()
        self.sources = {str(source.pk): source for source in ReleaseSource.objects.order_by("name")}
        self.fields["package_target"].choices = [(self.NEW, gettext("New package rule"))] + [
            (pk, gettext("Add to %(name)s") % {"name": f"{source.name} ({source.get_kind_display()})"})
            for pk, source in self.sources.items()]
        self.fields["package_target"].initial = self.NEW

    def clean(self):
        cleaned_data = super().clean()
        result = cleaned_data.get("result")
        if result == self.RULE:
            identifier = (cleaned_data.get("identifier") or "").strip()
            cleaned_data["identifier"] = identifier
            if not identifier and not cleaned_data.get("file"):
                self.add_error("identifier", gettext("Enter the identifier, or upload the app."))
            elif identifier and cleaned_data.get("rule_type"):
                try:
                    cleaned_data["identifier"] = validate_identifier(cleaned_data["rule_type"], identifier)
                except ValidationError as e:
                    self.add_error("identifier", e)
            if cleaned_data.get("scope") == SCOPE_GROUPS and not cleaned_data.get("groups"):
                self.add_error("groups", gettext("Choose at least one group."))
        elif result == self.PACKAGE:
            identifiers = [line.strip() for line in (cleaned_data.get("package_identifiers") or "").splitlines()
                           if line.strip()]
            cleaned_data["package_identifiers"] = identifiers
            if not identifiers:
                self.add_error("package_identifiers", gettext("Enter at least one identifier."))
            target = cleaned_data.get("package_target")
            cleaned_data["package_source"] = self.sources.get(target)
            if target == self.NEW:
                if not (cleaned_data.get("package_name") or "").strip():
                    self.add_error("package_name", gettext("Enter a name for the new package rule."))
                if not cleaned_data.get("package_is_global") and not cleaned_data.get("package_groups"):
                    self.add_error("package_groups",
                                   gettext("Choose at least one group for the new package rule, or all Macs."))
        elif result == self.EXISTING:
            identifier = (cleaned_data.get("rule_identifier") or "").strip()
            picked = cleaned_data.get("rule")
            if picked and picked.identifier.lower() == identifier.lower():
                rule = picked
            elif identifier:
                # typed by hand: the first rule with the identifier
                rule = Rule.objects.filter(identifier__iexact=identifier).order_by("pk").first()
            else:
                rule = None
            if rule is None:
                self.add_error("rule_identifier", gettext("No rule with this identifier."))
            cleaned_data["rule"] = rule
        return cleaned_data


class DenyForm(forms.Form):
    note = forms.CharField(widget=forms.Textarea(attrs={"rows": 2}), help_text=_("Shown to the user"))


class RequestFilterForm(forms.Form):
    status = forms.ChoiceField(choices=[("", _("All"))] + AccessRequest.Status.choices, required=False)


class LogoField:
    """A logo of the group form: an upload (stored as data: URL), or a file:/// URL of an image on the Macs"""

    is_logo = True

    def __init__(self, form, name):
        self.name = name
        self.label = form.LOGO_LABELS[name]
        self.help_text = Group._meta.get_field(name).help_text
        self.file = form[f"{name}_file"]
        self.url = form[f"{name}_url"]
        self.clear = form[f"{name}_clear"]
        value = getattr(form.instance, name)
        self.preview = value if value.startswith("data:") else ""
        self.errors = [*self.file.errors, *self.url.errors]


class GroupForm(forms.ModelForm):
    class Meta:
        model = Group
        fields = ("name", "description", "client_mode", "batch_size", "full_sync_interval", "allowed_path_regex",
                  "blocked_path_regex", "enable_transitive_rules", "enable_bundles", "enable_all_event_upload",
                  "removable_media_action", "removable_media_remount_flags", "encrypted_removable_media_action",
                  "encrypted_removable_media_remount_flags", "override_file_access_action", "event_detail_url",
                  "event_detail_text", "unknown_block_message", "banned_block_message",
                  "enable_bad_signature_protection", "file_access_block_message", "on_start_usb_options",
                  "branding_company_name")
        widgets = {
            "description": forms.Textarea(attrs={"rows": 2}),
            "client_mode": forms.RadioSelect,
            "allowed_path_regex": forms.Textarea(attrs={"rows": 3, "class": "mono"}),
            "blocked_path_regex": forms.Textarea(attrs={"rows": 3, "class": "mono"}),
            "unknown_block_message": forms.Textarea(attrs={"rows": 2}),
            "banned_block_message": forms.Textarea(attrs={"rows": 2}),
            "file_access_block_message": forms.Textarea(attrs={"rows": 2}),
        }
        labels = {
            "name": _("Name"), "description": _("Description"), "client_mode": _("Client mode"),
            "batch_size": _("Batch size"), "full_sync_interval": _("Full sync interval"),
            "allowed_path_regex": _("Allowed path regexes"), "blocked_path_regex": _("Blocked path regexes"),
            "enable_transitive_rules": _("Enable transitive rules"), "enable_bundles": _("Enable bundles"),
            "enable_all_event_upload": _("Enable all event upload"),
            "removable_media_action": _("Removable media"), "removable_media_remount_flags": _("Remount flags"),
            "encrypted_removable_media_action": _("Encrypted removable media"),
            "encrypted_removable_media_remount_flags": _("Remount flags of encrypted media"),
            "event_detail_url": _("Block dialog URL"),
            "event_detail_text": _("Block dialog button text"), "unknown_block_message": _("Unknown block message"),
            "banned_block_message": _("Banned block message"),
            "enable_bad_signature_protection": _("Enable bad signature protection"),
            "override_file_access_action": _("File access override"),
            "file_access_block_message": _("File access block message"),
            "on_start_usb_options": _("Removable media mounted when Santa starts"),
            "branding_company_name": _("Company name"),
        }

    LOGO_LABELS = {"branding_company_logo": _("Company logo"),
                   "branding_company_logo_dark": _("Company logo in dark mode")}

    # the sections of the form page, like in the admin
    SECTIONS = [
        (_("Group"), "", ("name", "description")),
        (_("Santa configuration"), _("Sent to the Macs at every sync."), ("client_mode", "batch_size",
                                                                           "full_sync_interval")),
        (_("Path regexes"), _("One per line, combined into one regex for Santa. Rules always win over the regexes."),
         ("allowed_path_regex", "blocked_path_regex")),
        (_("Advanced"), "", ("enable_transitive_rules", "enable_bundles", "enable_all_event_upload")),
        (_("Removable media"), _("Sent at every sync, no profile change needed."),
         ("removable_media_action", "removable_media_remount_flags", "encrypted_removable_media_action",
          "encrypted_removable_media_remount_flags")),
        (_("File access"), _("The file access rules are in the profile of the group, the override is sent at "
                             "every sync."),
         ("override_file_access_action",)),
        (_("Block dialog"), _("Sent at every sync, no profile change needed."),
         ("event_detail_url", "event_detail_text")),
        (_("Profile only"), _("Only in the .mobileconfig: after a change, download the profile again and "
                              "replace it in your MDM."),
         ("unknown_block_message", "banned_block_message", "enable_bad_signature_protection",
          "file_access_block_message", "on_start_usb_options")),
        (_("Branding"), _("Profile only, Santa 2026.1 and newer: after a change, download the profile again and "
                          "replace it in your MDM."),
         ("branding_company_name", "branding_company_logo", "branding_company_logo_dark")),
    ]
    # a change of these needs a new profile (the logo fields: _file, _url, _clear)
    PROFILE_FIELDS = ("unknown_block_message", "banned_block_message", "enable_bad_signature_protection",
                      "file_access_block_message", "on_start_usb_options", "branding_company_name",
                      "branding_company_logo")
    SHOW_WHEN = {
        "removable_media_remount_flags": "removable_media_action=REMOUNT",
        "encrypted_removable_media_remount_flags": "encrypted_removable_media_action=REMOUNT",
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["encrypted_removable_media_action"].choices = [
            ("", _("The same as removable media")), *RemovableMediaAction.choices]
        self.fields["on_start_usb_options"].choices = [("", _("Leave them")), *OnStartUSBOption.choices]
        for name in self.LOGO_LABELS:
            value = getattr(self.instance, name)
            self.fields[f"{name}_file"] = forms.FileField(
                required=False, label=_("Upload an image"),
                help_text=format_lazy(_("PNG or JPEG, at most {size} KB. It goes into the profile."),
                                      size=BRANDING_LOGO_MAX_BYTES // 1024),
                widget=FileDropInput(attrs={"accept": ",".join(BRANDING_LOGO_TYPES)}))
            self.fields[f"{name}_url"] = forms.CharField(
                required=False, label=_("Or an image on the Macs"),
                initial=value if value.startswith("file://") else "",
                widget=forms.TextInput(attrs={"placeholder": "file:///Library/Company/logo.png", "class": "mono"}))
            self.fields[f"{name}_clear"] = forms.BooleanField(required=False, label=_("Remove the logo"))

    def clean(self):
        cleaned = super().clean()
        for name in self.LOGO_LABELS:
            upload = cleaned.get(f"{name}_file")
            url = cleaned.get(f"{name}_url", "").strip()
            current = getattr(self.instance, name)
            if upload:
                try:
                    value = logo_data_url(upload)
                except ValidationError as e:
                    self.add_error(f"{name}_file", e)
                    continue
            elif url:
                try:
                    validate_logo_url(url)
                except ValidationError as e:
                    self.add_error(f"{name}_url", e)
                    continue
                value = url
            elif cleaned.get(f"{name}_clear") or current.startswith("file://"):
                # an emptied URL removes it too
                value = ""
            else:
                value = current
            setattr(self.instance, name, value)
        return cleaned

    def profile_changed(self):
        return any(name.startswith(self.PROFILE_FIELDS) for name in self.changed_data)

    def sections(self):
        def field(name):
            return LogoField(self, name) if name in self.LOGO_LABELS else self[name]
        return [(title, help_text, [(field(name), self.SHOW_WHEN.get(name, "")) for name in names])
                for title, help_text, names in self.SECTIONS]


def logo_data_url(upload):
    """An uploaded logo as data: URL, for the profile: only PNG and JPEG (by their first bytes), size limited"""
    content = upload.read(BRANDING_LOGO_MAX_BYTES + 1)
    if len(content) > BRANDING_LOGO_MAX_BYTES:
        raise ValidationError(gettext("The image is too large (at most %(size)s KB).")
                              % {"size": BRANDING_LOGO_MAX_BYTES // 1024})
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        kind = "image/png"
    elif content.startswith(b"\xff\xd8\xff"):
        kind = "image/jpeg"
    else:
        raise ValidationError(gettext("Only PNG or JPEG images."))
    return f"data:{kind};base64,{base64.b64encode(content).decode()}"


class ProfileForm(forms.ModelForm):
    class Meta:
        model = UserProfile
        fields = ("theme", "language", "time_zone")
        widgets = {"theme": forms.RadioSelect}
        labels = {"theme": _("Theme"), "language": _("Language"), "time_zone": _("Time zone")}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["language"].choices = [("", _("Language of the browser"))] + list(settings.LANGUAGES)
        self.fields["time_zone"] = forms.ChoiceField(
            choices=[("", _("Time zone of the browser"))] + [(name, name) for name in sorted(time_zone_names())],
            required=False, label=_("Time zone"))


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


class FileAccessRuleForm(forms.ModelForm):
    class Meta:
        model = FileAccessRule
        fields = ("name", "description", "is_enabled", "rule_type", "paths", "path_prefixes", "allow_read_access",
                  "audit_only", "block_message", "event_detail_url", "event_detail_text", "enable_silent_mode",
                  "enable_silent_tty_mode", "is_global", "groups")
        widgets = {
            "description": forms.Textarea(attrs={"rows": 2}),
            "rule_type": forms.RadioSelect,
            "paths": forms.Textarea(attrs={"rows": 3, "class": "mono"}),
            "path_prefixes": forms.Textarea(attrs={"rows": 3, "class": "mono"}),
            "block_message": forms.Textarea(attrs={"rows": 2}),
            "groups": forms.CheckboxSelectMultiple,
        }
        labels = {
            "name": _("Name"), "description": _("Description"), "is_enabled": _("Enabled"),
            "rule_type": _("Rule type"), "paths": _("Paths"), "path_prefixes": _("Paths with everything below them"),
            "allow_read_access": _("Allow reading"), "audit_only": _("Audit only"),
            "block_message": _("Block message"), "event_detail_url": _("Block dialog URL"),
            "event_detail_text": _("Block dialog button text"), "enable_silent_mode": _("Silent"),
            "enable_silent_tty_mode": _("Silent in the terminal"), "is_global": _("All groups"),
            "groups": _("Groups"),
        }


class FileAccessProcessForm(forms.ModelForm):
    class Meta:
        model = FileAccessProcess
        fields = ("signing_id", "team_id", "platform_binary", "binary_path", "cdhash", "certificate_sha256")
        labels = {
            "signing_id": _("Signing ID"), "team_id": _("Team ID"), "platform_binary": _("Platform binary"),
            "binary_path": _("Binary path"), "cdhash": _("CDHash"), "certificate_sha256": _("Certificate SHA-256"),
        }
        widgets = {name: forms.TextInput(attrs={"class": "mono"})
                   for name in ("signing_id", "team_id", "binary_path", "cdhash", "certificate_sha256")}


class BaseFileAccessProcessFormSet(forms.BaseInlineFormSet):
    def clean(self):
        super().clean()
        if any(self.errors):
            return
        # an empty extra row has no cleaned_data
        count = sum(1 for form in self.forms if form.cleaned_data and not form.cleaned_data.get("DELETE"))
        rule_type = self.instance.rule_type
        if count == 0 and rule_type in (FileAccessRuleType.PROCESSES_WITH_ALLOWED_PATHS,
                                        FileAccessRuleType.PROCESSES_WITH_DENIED_PATHS):
            raise ValidationError(gettext("This rule type needs at least one process."))


FileAccessProcessFormSet = forms.inlineformset_factory(
    FileAccessRule, FileAccessProcess, form=FileAccessProcessForm, formset=BaseFileAccessProcessFormSet, extra=1,
    can_delete=True)


class ConfigImportForm(forms.Form):
    file = forms.FileField(label=_("File"), widget=FileDropInput(attrs={"accept": ".json,application/json"}),
                           help_text=_("JSON file of “Export configuration”, e.g. from the test "
                                       "server, or of the export_config command."))
    delete_missing = forms.BooleanField(
        required=False, label=_("Delete what is not in the file"),
        help_text=_("Deletes the manual rules, package rules and file access rules that are not in the file."))
    dry_run = forms.BooleanField(required=False, initial=True, label=_("Dry run"),
                                 help_text=_("Only show what would change. Uncheck to import."))
