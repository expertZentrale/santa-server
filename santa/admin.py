import json
import logging

from django.contrib import admin, messages
from django.contrib.admin.helpers import ACTION_CHECKBOX_NAME
from django.core.exceptions import PermissionDenied
from django.db.models import Count
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils import timezone
from django.utils.html import format_html, format_html_join
from django.utils.safestring import mark_safe
from django.utils.text import slugify

from .auth import delete_sign_in_group, sign_in_group_changed
from .config_io import EXPORT_PERMS, IMPORT_PERMS, ConfigImportError, export_config, import_config
from .forms import AddGroupsForm, AllowEventsForm, ImportConfigForm, RuleAdminForm, TagActionForm, UploadBinaryForm
from .models import (
    AccessRequest,
    AccessRequestPackage,
    Event,
    FileAccessProcess,
    FileAccessRule,
    Group,
    Machine,
    ReleaseSource,
    ReleaseVersion,
    Rule,
    RuleType,
    SavedFilter,
    SignInGroup,
    Tag,
    generate_sync_token,
)
from .profiles import base_profile, group_profile
from .releases import ReleaseError, find_binaries, sync_release_source
from .services import allow_identifier, can_see_sync_token, set_rules_enabled

logger = logging.getLogger(__name__)


def _mobileconfig_response(content, filename):
    response = HttpResponse(content, content_type="application/x-apple-aspen-config")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


@admin.register(Group)
class GroupAdmin(admin.ModelAdmin):
    change_list_template = "admin/santa/group/change_list.html"
    list_display = ("name", "client_mode", "machine_count", "rule_count", "sync_url", "mdm_profile")
    search_fields = ("name",)
    readonly_fields = ("sync_url", "mdm_profile", "created_at", "updated_at")
    actions = ["regenerate_sync_token"]
    fieldsets = (
        (None, {
            "fields": ("name", "description", "sync_url", "mdm_profile"),
            "description": "Upload the profile of the group to your MDM as a custom configuration profile and "
                           "assign it to these Macs. Every Mac with this profile belongs to this group. The base "
                           "profile (same for every Mac) is on the group list.",
        }),
        ("Santa configuration", {
            "fields": ("client_mode", "batch_size", "full_sync_interval"),
            "description": "Sent to the Macs at every sync. "
                           "Don't set these keys in the configuration profile as well.",
        }),
        ("Path regexes", {
            "fields": ("allowed_path_regex", "blocked_path_regex"),
            "description": "Several regexes are combined into one for Santa (one per line). "
                           "Rules always win over the path regexes.",
        }),
        ("Advanced options", {
            "fields": ("enable_transitive_rules", "enable_bundles", "enable_all_event_upload"),
        }),
        ("Removable media", {
            "fields": ("removable_media_action", "removable_media_remount_flags", "encrypted_removable_media_action",
                       "encrypted_removable_media_remount_flags"),
            "description": "Sent at every sync, no profile change needed.",
        }),
        ("File access", {
            "fields": ("override_file_access_action",),
            "description": "Sent at every sync. The file access rules are in the profile of the group.",
        }),
        ("Block dialog", {
            "fields": ("event_detail_url", "event_detail_text"),
            "description": "Sent at every sync, no profile change needed.",
        }),
        ("Profile only", {
            "fields": ("unknown_block_message", "banned_block_message", "enable_bad_signature_protection",
                       "file_access_block_message", "on_start_usb_options", "branding_company_name",
                       "branding_company_logo", "branding_company_logo_dark"),
            "description": "These settings are only in the .mobileconfig: after a change, download the profile "
                           "again and replace it in your MDM.",
        }),
        ("Info", {"fields": ("created_at", "updated_at")}),
    )

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(
            _machine_count=Count("machines", distinct=True), _rule_count=Count("rules", distinct=True)
        )

    @admin.display(description="Machines", ordering="_machine_count")
    def machine_count(self, obj):
        return obj._machine_count

    @admin.display(description="Rules", ordering="_rule_count")
    def rule_count(self, obj):
        return obj._rule_count

    # the sync token (in the URL and the profile) is the credential of the sync API: not for read-only viewers
    SECRET_FIELDS = ("sync_url", "mdm_profile")

    def get_list_display(self, request):
        fields = super().get_list_display(request)
        return fields if can_see_sync_token(request.user) else [f for f in fields if f not in self.SECRET_FIELDS]

    def get_fieldsets(self, request, obj=None):
        fieldsets = super().get_fieldsets(request, obj)
        if can_see_sync_token(request.user):
            return fieldsets
        return [(name, {**options, "fields": [f for f in options["fields"] if f not in self.SECRET_FIELDS]})
                for name, options in fieldsets]

    @admin.display(description="SyncBaseURL (configuration profile)")
    def sync_url(self, obj):
        if not obj.pk:
            return "Available after saving"
        return format_html("<code>{}</code>", obj.sync_base_url)

    @admin.display(description="Configuration profile")
    def mdm_profile(self, obj):
        if not obj.pk:
            return "Available after saving"
        url = reverse("admin:santa_group_mobileconfig", args=(obj.pk,))
        return format_html('<a href="{}">Download .mobileconfig</a>', url)

    def get_urls(self):
        return [
            path("<int:pk>/mobileconfig/", self.admin_site.admin_view(self.mobileconfig_view),
                 name="santa_group_mobileconfig"),
            path("base-mobileconfig/", self.admin_site.admin_view(self.base_mobileconfig_view),
                 name="santa_group_base_mobileconfig"),
            path("export/", self.admin_site.admin_view(self.export_view), name="santa_config_export"),
            path("import/", self.admin_site.admin_view(self.import_view), name="santa_config_import"),
        ] + super().get_urls()

    def export_view(self, request):
        if not request.user.has_perms(EXPORT_PERMS):
            raise PermissionDenied
        response = HttpResponse(json.dumps(export_config(), indent=2, ensure_ascii=False),
                                content_type="application/json")
        filename = f"santa-config-{timezone.localdate():%Y-%m-%d}.json"
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        return response

    def import_view(self, request):
        if not request.user.has_perms(IMPORT_PERMS):
            raise PermissionDenied
        form = ImportConfigForm(request.POST or None, request.FILES or None)
        report = errors = None
        if request.method == "POST" and form.is_valid():
            try:
                data = json.load(form.cleaned_data["file"])
            except ValueError as e:
                form.add_error("file", f"Not a JSON file: {e}")
            else:
                try:
                    report = import_config(data, delete_missing=form.cleaned_data["delete_missing"],
                                           dry_run=form.cleaned_data["dry_run"])
                except ConfigImportError as e:
                    errors = e.errors
                else:
                    if not form.cleaned_data["dry_run"]:
                        logger.info("User %s imported a configuration: %s change(s)",
                                    request.user, len(report["changes"]))
                        self.message_user(request, f"Imported: {len(report['changes'])} change(s).",
                                          messages.SUCCESS)
        context = {**self.admin_site.each_context(request), "opts": self.model._meta, "form": form,
                   "report": report, "errors": errors, "title": "Import a configuration"}
        return TemplateResponse(request, "admin/santa/group/import.html", context)

    def mobileconfig_view(self, request, pk):
        group = self.get_object(request, str(pk))
        if group is None or not self.has_change_permission(request, group):
            raise PermissionDenied
        return _mobileconfig_response(group_profile(group), f"santa-{slugify(group.name) or group.pk}.mobileconfig")

    def base_mobileconfig_view(self, request):
        if not self.has_view_permission(request):
            raise PermissionDenied
        return _mobileconfig_response(base_profile(), "santa-base.mobileconfig")

    @admin.action(description="Regenerate the sync token (the configuration profile must be updated!)")
    def regenerate_sync_token(self, request, queryset):
        for group in queryset:
            group.sync_token = generate_sync_token()
            group.save(update_fields=["sync_token", "updated_at"])
            self.log_change(request, group, "Regenerated the sync token")
        self.message_user(request, "New sync URLs generated. "
                                   "Download the profiles again and replace them in your MDM.", messages.WARNING)


@admin.register(Machine)
class MachineAdmin(admin.ModelAdmin):
    list_display = ("serial_number", "hostname", "group", "primary_user", "client_mode", "santa_version",
                    "os_version", "reported_rule_count", "last_postflight_at")
    list_filter = ("group", "client_mode", "santa_version")
    search_fields = ("serial_number", "hostname", "primary_user", "machine_id")
    readonly_fields = [f.name for f in Machine._meta.fields if f.name != "clean_sync_requested"]
    actions = ["request_clean_sync"]

    def has_add_permission(self, request):
        # machines enroll themselves with the sync URL of their group
        return False

    @admin.display(description="Rules on the Mac")
    def reported_rule_count(self, obj):
        return obj.reported_rule_count()

    @admin.action(description="Send a clean sync at the next sync")
    def request_clean_sync(self, request, queryset):
        count = queryset.update(clean_sync_requested=True)
        self.message_user(request, f"{count} machine(s) will do a clean sync.")


@admin.register(Rule)
class RuleAdmin(admin.ModelAdmin):
    form = RuleAdminForm
    change_list_template = "admin/santa/rule/change_list.html"
    list_display = ("identifier_short", "rule_type", "policy", "scope", "is_enabled", "description", "tag_list",
                    "release_source", "created_at")
    list_filter = ("is_enabled", "tags", "policy", "rule_type", "is_global", "groups", "release_source")
    search_fields = ("identifier", "description", "tags__name")
    filter_horizontal = ("groups", "tags")
    autocomplete_fields = ("machines",)
    readonly_fields = ("release_source", "release_version", "created_by", "created_at", "updated_at")
    actions = ["enable_rules", "disable_rules", "add_groups", "add_tag", "remove_tag"]
    fieldsets = (
        (None, {"fields": ("rule_type", "identifier", "policy", "description", "tags")}),
        ("Scope", {"fields": ("is_global", "groups", "machines", "is_enabled")}),
        ("Block message / CEL", {"classes": ("collapse",), "fields": ("custom_msg", "custom_url", "cel_expr")}),
        ("Info", {"fields": ("release_source", "release_version", "created_by", "created_at", "updated_at")}),
    )

    def get_queryset(self, request):
        return (super().get_queryset(request).select_related("release_source")
                                               .prefetch_related("groups", "tags"))

    @admin.display(description="Identifier", ordering="identifier")
    def identifier_short(self, obj):
        return obj.identifier if len(obj.identifier) <= 24 else f"{obj.identifier[:20]}…"

    @admin.display(description="Scope")
    def scope(self, obj):
        if obj.is_global:
            return "All Macs"
        return ", ".join(d.name for d in obj.groups.all()) or "Machines only"

    @admin.display(description="Tags")
    def tag_list(self, obj):
        return ", ".join(tag.name for tag in obj.tags.all())

    def save_model(self, request, obj, form, change):
        if not change:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)

    @admin.action(description="Enable / approve the selected rules")
    def enable_rules(self, request, queryset):
        rules = list(queryset)
        count, _ = set_rules_enabled(queryset, True)
        for rule in rules:
            self.log_change(request, rule, "Enabled")
        self.message_user(request, f"{count} rule(s) enabled.")

    @admin.action(description="Disable the selected rules (removed from the Macs)")
    def disable_rules(self, request, queryset):
        rules = list(queryset)
        count, cancelled = set_rules_enabled(queryset, False)
        for rule in rules:
            self.log_change(request, rule, "Disabled")
        for release_version in cancelled:
            self.message_user(request, f"{release_version}: automatic approval cancelled.", messages.WARNING)
        self.message_user(request, f"{count} rule(s) disabled.")

    @admin.action(description="Add a tag to the selected rules…")
    def add_tag(self, request, queryset):
        return self._tag_action(request, queryset, "add_tag")

    @admin.action(description="Remove a tag from the selected rules…")
    def remove_tag(self, request, queryset):
        return self._tag_action(request, queryset, "remove_tag")

    def _tag_action(self, request, queryset, action):
        form = TagActionForm(request.POST if "apply" in request.POST else None)
        if action == "remove_tag":
            form.fields.pop("new_tag")
        if form.is_bound and form.is_valid():
            tag = form.get_tag()
            rules = list(queryset)
            if action == "add_tag":
                tag.rules.add(*rules)
                self.message_user(request, f"Tag “{tag}” added to {len(rules)} rule(s).")
            else:
                tag.rules.remove(*rules)
                self.message_user(request, f"Tag “{tag}” removed from {len(rules)} rule(s).")
            return None
        context = {**self.admin_site.each_context(request), "opts": self.model._meta, "form": form,
                   "rules": queryset, "action": action, "action_checkbox_name": ACTION_CHECKBOX_NAME,
                   "title": "Add a tag" if action == "add_tag" else "Remove a tag"}
        return render(request, "admin/santa/rule/bulk_action.html", context)

    @admin.action(description="Add groups to the selected rules…")
    def add_groups(self, request, queryset):
        form = AddGroupsForm(request.POST if "apply" in request.POST else None)
        if form.is_bound and form.is_valid():
            groups = list(form.cleaned_data["groups"])
            rules = [rule for rule in queryset.prefetch_related("groups") if not rule.is_global]
            skipped = queryset.filter(is_global=True).count()
            for rule in rules:
                new_groups = [group for group in groups if group not in rule.groups.all()]
                if new_groups:
                    rule.groups.add(*new_groups)
                    self.log_change(request, rule, f"Groups added: {', '.join(g.name for g in new_groups)}")
            self.message_user(request, f"{', '.join(g.name for g in groups)} added to {len(rules)} rule(s). "
                                       "The Macs get the rules at their next sync.")
            if skipped:
                self.message_user(request, f"{skipped} global rule(s) skipped, they already apply to every Mac.",
                                  messages.WARNING)
            return None
        context = {**self.admin_site.each_context(request), "opts": self.model._meta, "form": form,
                   "rules": queryset, "action": "add_groups", "action_checkbox_name": ACTION_CHECKBOX_NAME,
                   "title": "Add groups"}
        return render(request, "admin/santa/rule/bulk_action.html", context)

    def get_urls(self):
        return [
            path("upload/", self.admin_site.admin_view(self.upload_view), name="santa_rule_upload"),
        ] + super().get_urls()

    def upload_view(self, request):
        if not self.has_add_permission(request):
            return redirect("admin:santa_rule_changelist")
        form = UploadBinaryForm(request.POST or None, request.FILES or None)
        found = None
        if request.method == "POST" and form.is_valid():
            upload = form.cleaned_data["file"]
            try:
                found = list(find_binaries(upload, upload.name, form.cleaned_data["binary_pattern"]))
            except ReleaseError as e:
                form.add_error("file", str(e))
            else:
                if not found:
                    form.add_error("file", "No Mach-O executable found in this file.")
                else:
                    created = self._create_rules_from_upload(request, form, found)
                    if created:
                        return redirect("admin:santa_rule_changelist")
        context = {**self.admin_site.each_context(request), "opts": self.model._meta, "form": form,
                   "found": found, "title": "Allow or block a binary by upload"}
        return TemplateResponse(request, "admin/santa/rule/upload.html", context)

    def _create_rules_from_upload(self, request, form, found):
        rule_type = form.cleaned_data["rule_type"]
        identifiers = []
        for path_in_file, info in found:
            candidates = {
                RuleType.BINARY: [info.sha256],
                RuleType.CERTIFICATE: [info.cert_sha256],
                RuleType.TEAMID: [info.team_id],
                RuleType.SIGNINGID: [info.signing_id],
                RuleType.CDHASH: info.cdhashes,
            }[rule_type]
            for identifier in candidates:
                if identifier and (identifier, path_in_file) not in identifiers:
                    identifiers.append((identifier, path_in_file))
        if not identifiers:
            form.add_error("rule_type", f"The file has no {rule_type} (unsigned or ad-hoc signed?). "
                                        "Use a binary rule instead.")
            return 0
        created_count = 0
        seen = set()
        for identifier, path_in_file in identifiers:
            if identifier in seen:
                continue
            seen.add(identifier)
            description = (form.cleaned_data["description"]
                           or f"Upload {form.cleaned_data['file'].name}: {path_in_file}")
            rule, created = allow_identifier(
                rule_type, identifier, form.cleaned_data["policy"],
                form.cleaned_data["scope"] == form.SCOPE_GLOBAL, form.cleaned_data["groups"],
                request.user, description, form.cleaned_data["tags"],
            )
            if created:
                created_count += 1
                self.log_addition(request, rule, "Created from an uploaded file")
            else:
                self.log_change(request, rule, "Scope extended from an uploaded file")
        self.message_user(request, f"{len(seen)} rule(s) saved ({created_count} new).", messages.SUCCESS)
        return len(seen)


class ExecutingUserFilter(admin.AllValuesFieldListFilter):
    """The users who ran something, only those of the selected group if one is.

    A field list filter, not a SimpleListFilter: the "Show counts" of a SimpleListFilter aggregate over a subquery,
    which SQL Server refuses.
    """
    max_choices = 200

    def __init__(self, field, request, params, model, model_admin, field_path):
        super().__init__(field, request, params, model, model_admin, field_path)
        self.title = "user"
        events = model_admin.get_queryset(request).exclude(executing_user="")
        group_id = request.GET.get("group__id__exact")
        if group_id and group_id.isdigit():
            events = events.filter(group_id=group_id)
        users = events.values_list("executing_user", flat=True).distinct().order_by("executing_user")
        self.lookup_choices = list(users[:self.max_choices])


@admin.register(Event)
class EventAdmin(admin.ModelAdmin):
    change_list_template = "admin/santa/event/change_list.html"
    list_display = ("execution_time", "decision_badge", "file_name", "machine", "group", "executing_user",
                    "signing_id", "resolved")
    list_filter = (("resolved_at", admin.EmptyFieldListFilter), "group", ("executing_user", ExecutingUserFilter),
                   "decision", "execution_time")
    search_fields = ("file_name", "file_path", "file_sha256", "signing_id", "team_id", "machine__serial_number",
                     "machine__hostname", "executing_user", "bundle_id")
    list_select_related = ("machine", "group")
    date_hierarchy = "execution_time"
    actions = ["allow_events", "mark_resolved"]
    readonly_fields = [f.name for f in Event._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    @admin.display(description="Decision", ordering="decision")
    def decision_badge(self, obj):
        color = "#ba2121" if obj.is_blocked else "#447e9b"
        return format_html('<strong style="color:{}">{}</strong>', color, obj.decision)

    @admin.display(description="Resolved", boolean=True, ordering="resolved_at")
    def resolved(self, obj):
        return obj.resolved_at is not None

    @admin.action(description="Mark as resolved (no rule)")
    def mark_resolved(self, request, queryset):
        count = queryset.filter(resolved_at__isnull=True).update(resolved_at=timezone.now(), resolved_by=request.user)
        self.message_user(request, f"{count} event(s) marked as resolved.")

    @admin.action(description="Allow the selected binaries…", permissions=["view"])
    def allow_events(self, request, queryset):
        if not request.user.has_perm("santa.add_rule"):
            self.message_user(request, "You are not allowed to create rules.", messages.ERROR)
            return None
        events = list(queryset.select_related("group"))
        if "apply" in request.POST:
            form = AllowEventsForm(request.POST)
            if form.is_valid():
                return self._allow(request, form, events)
        else:
            form = AllowEventsForm(initial={"groups": list({e.group_id for e in events})})
        context = {**self.admin_site.each_context(request), "opts": self.model._meta, "form": form,
                   "events": events, "action_checkbox_name": ACTION_CHECKBOX_NAME,
                   "title": "Allow the selected binaries"}
        return render(request, "admin/santa/event/allow.html", context)

    def _allow(self, request, form, events):
        rule_type = form.cleaned_data["rule_type"]
        identifiers = {}
        skipped = 0
        for event in events:
            identifier = event.identifier_for(rule_type)
            if identifier:
                identifiers.setdefault(identifier, event)
            else:
                skipped += 1
        created_count = 0
        for identifier, event in identifiers.items():
            description = form.cleaned_data["description"] or f"{event.file_name} ({event.file_path})"
            rule, created = allow_identifier(
                rule_type, identifier, form.cleaned_data["policy"],
                form.cleaned_data["scope"] == form.SCOPE_GLOBAL, form.cleaned_data["groups"],
                request.user, description, form.cleaned_data["tags"],
            )
            if created:
                created_count += 1
                self.log_addition(request, rule, f"Created from event {event.pk}")
            else:
                self.log_change(request, rule, f"Scope extended from event {event.pk}")
        self.message_user(request, f"{len(identifiers)} rule(s) saved ({created_count} new). "
                                   "The Macs get them at their next sync.", messages.SUCCESS)
        if skipped:
            self.message_user(request, f"{skipped} event(s) skipped: no {rule_type} (unsigned binary?).",
                              messages.WARNING)
        return None


class ReleaseVersionInline(admin.TabularInline):
    model = ReleaseVersion
    extra = 0
    can_delete = False
    fields = ("identifier", "version", "published_at", "binary_count", "rules_link", "approval", "created_at")
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False

    @admin.display(description="Approval")
    def approval(self, obj):
        if obj.auto_enable_pending:
            return f"automatic on {timezone.localtime(obj.auto_enable_at):%Y-%m-%d %H:%M}"
        enabled = obj.rules.filter(is_enabled=True).count()
        total = obj.rules.count()
        if enabled == total:
            return "enabled"
        return "waiting for approval" if not enabled else f"{enabled} of {total} enabled"

    @admin.display(description="Rules")
    def rules_link(self, obj):
        url = reverse("admin:santa_rule_changelist") + f"?release_version__id__exact={obj.pk}"
        return format_html('<a href="{}">{} rule(s)</a>', url, obj.rules.count())


@admin.register(ReleaseSource)
class ReleaseSourceAdmin(admin.ModelAdmin):
    list_display = ("name", "kind", "identifier_list", "rule_type", "policy", "latest_version", "auto_approve",
                    "is_enabled", "last_checked_at", "status")
    list_filter = ("kind", "rule_type", "policy", "is_enabled", "auto_approve")
    search_fields = ("name", "identifier")
    filter_horizontal = ("groups", "tags")
    readonly_fields = ("last_checked_at", "last_error", "created_at", "updated_at")
    inlines = [ReleaseVersionInline]
    actions = ["check_now"]
    fieldsets = (
        (None, {"fields": ("name", "kind", "identifier", "is_enabled")}),
        ("Which versions and files", {"fields": ("version_pattern", "asset_pattern", "binary_pattern",
                                                 "include_prereleases")}),
        ("Rules", {"fields": ("rule_type", "policy", "custom_msg", "custom_url", "cel_expr")}),
        ("Scope", {"fields": ("is_global", "groups", "auto_approve", "auto_approve_delay_days", "keep_versions",
                              "tags")}),
        ("Status", {"fields": ("last_checked_at", "last_error", "created_at", "updated_at")}),
    )

    @admin.display(description="Identifiers", ordering="identifier")
    def identifier_list(self, obj):
        return format_html_join(mark_safe("<br>"), "{}", ((identifier,) for identifier in obj.identifiers))

    @admin.display(description="Latest version")
    def latest_version(self, obj):
        version = obj.versions.order_by("-created_at").first()
        if not version:
            return "-"
        return f"{version.identifier} {version.version}" if obj.has_several_identifiers else version.version

    @admin.display(description="OK", boolean=True)
    def status(self, obj):
        if obj.last_checked_at is None:
            return None
        return not obj.last_error

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        source = form.instance
        if not source.is_global and not source.groups.exists():
            self.message_user(request, f"{source}: no group and not global, its rules apply to no Mac.",
                              messages.WARNING)

    @admin.action(description="Check for a new release now")
    def check_now(self, request, queryset):
        for source in queryset:
            try:
                release_versions = sync_release_source(source)
            except ReleaseError as e:
                self.message_user(request, f"{source}: {e}", messages.ERROR)
                release_versions = e.new_versions
                if not release_versions:
                    continue
            for release_version in release_versions:
                message = (f"{release_version}: new version, "
                           f"{release_version.binary_count} binary hash(es)")
                if release_version.auto_enable_pending:
                    enable_at = timezone.localtime(release_version.auto_enable_at)
                    message += f", enabled automatically on {enable_at:%Y-%m-%d %H:%M}"
                elif not source.auto_approve:
                    message += ", waiting for approval"
                self.message_user(request, message + ".", messages.SUCCESS)
            if not release_versions:
                self.message_user(request, f"{source}: already up to date.")


class FileAccessProcessInline(admin.TabularInline):
    model = FileAccessProcess
    extra = 0


@admin.register(FileAccessRule)
class FileAccessRuleAdmin(admin.ModelAdmin):
    list_display = ("name", "rule_type", "audit_only", "is_global", "is_enabled", "updated_at")
    list_filter = ("rule_type", "audit_only", "is_global", "is_enabled")
    search_fields = ("name", "description", "paths", "path_prefixes")
    filter_horizontal = ("groups",)
    readonly_fields = ("created_at", "updated_at")
    inlines = [FileAccessProcessInline]
    fieldsets = (
        (None, {
            "fields": ("name", "description", "is_enabled"),
            "description": "In the configuration profile of the groups (FileAccessPolicy): after a change, download "
                           "their profiles again and replace them in your MDM.",
        }),
        ("Rule", {"fields": ("rule_type", "paths", "path_prefixes", "allow_read_access", "audit_only")}),
        ("Block dialog", {"fields": ("block_message", "event_detail_url", "event_detail_text", "enable_silent_mode",
                                     "enable_silent_tty_mode")}),
        ("Scope", {"fields": ("is_global", "groups")}),
        ("Info", {"fields": ("created_at", "updated_at")}),
    )


@admin.register(Tag)
class TagAdmin(admin.ModelAdmin):
    list_display = ("name", "description", "rules_link")
    search_fields = ("name", "description")

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(_rule_count=Count("rules"))

    @admin.display(description="Rules", ordering="_rule_count")
    def rules_link(self, obj):
        url = reverse("admin:santa_rule_changelist") + f"?tags__id__exact={obj.pk}"
        return format_html('<a href="{}">{} rule(s)</a>', url, obj._rule_count)


@admin.register(SignInGroup)
class SignInGroupAdmin(admin.ModelAdmin):
    list_display = ("name", "claim_value", "console_access")
    list_filter = ("console_access",)
    search_fields = ("name", "claim_value")
    filter_horizontal = ("roles",)

    def save_related(self, request, form, formsets, change):
        old_roles = list(form.instance.roles.all()) if change else []
        super().save_related(request, form, formsets, change)
        # the members get the new roles now, not only at their next sign-in
        sign_in_group_changed(form.instance, old_roles)

    def delete_model(self, request, obj):
        delete_sign_in_group(obj)

    def delete_queryset(self, request, queryset):
        for obj in queryset:
            delete_sign_in_group(obj)


@admin.register(SavedFilter)
class SavedFilterAdmin(admin.ModelAdmin):
    list_display = ("name", "page", "user", "created_at")
    list_filter = ("page",)
    search_fields = ("name", "user__username")
    raw_id_fields = ("user",)


class AccessRequestPackageInline(admin.TabularInline):
    model = AccessRequestPackage
    extra = 0
    fields = ("kind", "identifier", "name", "status", "result_source")
    raw_id_fields = ("result_source",)


@admin.register(AccessRequest)
class AccessRequestAdmin(admin.ModelAdmin):
    inlines = [AccessRequestPackageInline]
    list_display = ("title", "kind", "requester", "machine", "status", "created_at", "decided_by")
    list_filter = ("status", "kind")
    search_fields = ("title", "justification", "requester__username", "packages__identifier", "file_sha256")
    list_select_related = ("requester", "machine", "decided_by")
    raw_id_fields = ("event", "machine", "result_rule", "result_source")
    readonly_fields = ("requester", "created_at", "decided_by", "decided_at")

    def has_add_permission(self, request):
        # the users file them with the request form
        return False
