"""Export and import the configuration (groups, release sources, rules) as JSON, e.g. from dev to live.

Everything is matched by natural keys, never by primary key:
- groups, tags and release sources by name (also the parent of a group based on another one),
- manual rules by (rule type, identifier, policy),
- machine scopes by serial number (machines are never created).

The sync tokens are never exported: a group keeps its token (and so its configuration profile) on the target,
a new group gets a new one. The rules of the release sources are not exported either, the target builds them
by checking the sources itself.
"""
import copy

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .models import FileAccessProcess, FileAccessRule, Group, Machine, ReleaseSource, Rule, Tag

FORMAT = "santa-server-config"
# the models in the file: exporting needs their view permission, importing add / change / delete
CONFIG_MODELS = ("group", "rule", "releasesource", "fileaccessrule")
EXPORT_PERMS = [f"santa.view_{model}" for model in CONFIG_MODELS]
IMPORT_PERMS = [f"santa.{action}_{model}" for action in ("add", "change", "delete") for model in CONFIG_MODELS]
VERSION = 1

GROUP_FIELDS = (
    "description", "client_mode", "batch_size", "full_sync_interval", "allowed_path_regex", "blocked_path_regex",
    "enable_bundles", "enable_transitive_rules", "enable_all_event_upload", "removable_media_action",
    "removable_media_remount_flags", "encrypted_removable_media_action", "encrypted_removable_media_remount_flags",
    "event_detail_url", "event_detail_text", "unknown_block_message", "banned_block_message",
    "enable_bad_signature_protection", "on_start_usb_options", "branding_company_name", "branding_company_logo",
    "branding_company_logo_dark", "override_file_access_action", "file_access_block_message", "inherit_rules",
    "inherit_file_access_rules", "overridden_settings",
)
RELEASE_SOURCE_FIELDS = (
    "kind", "identifier", "version_pattern", "asset_pattern", "binary_pattern", "include_prereleases", "rule_type",
    "policy", "custom_msg", "custom_url", "cel_expr", "is_global", "auto_approve", "auto_approve_delay_days",
    "keep_versions", "keep_unit", "approve_kept_versions", "include_dependencies", "is_enabled",
)
FILE_ACCESS_RULE_FIELDS = (
    "description", "rule_type", "paths", "path_prefixes", "allow_read_access", "audit_only", "block_message",
    "event_detail_url", "event_detail_text", "enable_silent_mode", "enable_silent_tty_mode", "is_global", "is_enabled",
)
FILE_ACCESS_PROCESS_FIELDS = ("signing_id", "team_id", "platform_binary", "binary_path", "cdhash",
                              "certificate_sha256")
RULE_KEY_FIELDS = ("rule_type", "identifier", "policy")
RULE_FIELDS = ("custom_msg", "custom_url", "cel_expr", "description", "is_global", "is_enabled")


def legacy_usb(item):
    """Files of older versions have block_usb_mount / remount_usb_mode instead of removable_media_action"""
    if "block_usb_mount" not in item or "removable_media_action" in item:
        return item
    flags = item.get("remount_usb_mode", "") if item["block_usb_mount"] else ""
    action = ("REMOUNT" if flags else "BLOCK") if item["block_usb_mount"] else "ALLOW"
    return {**item, "removable_media_action": action, "removable_media_remount_flags": flags}


# The import preview (console): every item of the file by its list and index, e.g. "rules:3"
PREVIEW_LISTS = ("groups", "release_sources", "rules", "file_access_rules")
RENAMABLE_LISTS = ("groups", "release_sources", "file_access_rules")
SWITCHABLE_LISTS = ("release_sources", "rules", "file_access_rules")


def item_token(list_name, index):
    return f"{list_name}:{index}"


def deletion_key(list_name, item):
    """What "delete what is not in the file" matches an item of the file by, or None (groups are never deleted)"""
    if list_name == "release_sources":
        return f"source:{item.get('name')}"
    if list_name == "file_access_rules":
        return f"file_access:{item.get('name')}"
    if list_name == "rules":
        return "rule:" + "|".join(str(item.get(field)) for field in RULE_KEY_FIELDS)
    return None


def apply_choices(data, skip=(), disabled=None, names=None, keep=()):
    """The file as changed in the import preview. Returns (data, keep, warnings).

    skip: tokens of the items left out; they are not deleted by delete_missing either (their keys go into keep).
    disabled: tokens of the rules, package rules and file access rules imported disabled, None = as in the file.
    names: {token: new name} of groups, package rules and file access rules; a group keeps its references.
    keep: the keys of deletions not to do. Every item gets its original token as "_token", for the report.
    """
    data = copy.deepcopy(data)
    # a skipped item keeps its name: it is protected from delete_missing by the name it has on the target
    names = {token: name for token, name in (names or {}).items() if token not in skip}
    keep = set(keep)
    warnings = []
    for list_name in PREVIEW_LISTS:
        for index, item in enumerate(data.get(list_name) or []):
            item["_token"] = item_token(list_name, index)
    renamed = {}
    for list_name in RENAMABLE_LISTS:
        for item in data.get(list_name) or []:
            name = (names.get(item["_token"]) or "").strip()
            if name and name != item.get("name"):
                if list_name == "groups":
                    renamed[item.get("name")] = name
                item["name"] = name
    if renamed:
        for item in data.get("groups") or []:
            if item.get("parent") in renamed:
                item["parent"] = renamed[item["parent"]]
        for list_name in ("release_sources", "rules", "file_access_rules"):
            for item in data.get(list_name) or []:
                item["groups"] = [renamed.get(name, name) for name in item.get("groups") or []]
    if disabled is not None:
        for list_name in SWITCHABLE_LISTS:
            for item in data.get(list_name) or []:
                item["is_enabled"] = item["_token"] not in disabled
    skipped_groups = set()
    for list_name in PREVIEW_LISTS:
        if list_name not in data:
            continue
        kept = []
        for item in data[list_name]:
            if item["_token"] not in skip:
                kept.append(item)
            elif list_name == "groups":
                skipped_groups.add(item.get("name"))
            else:
                keep.add(deletion_key(list_name, item))
        data[list_name] = kept
    # a skipped group that the target doesn't have: the items of the file can't use it
    missing = {name for name in skipped_groups if not Group.objects.filter(name=name).exists()}
    if missing:
        for item in data.get("groups") or []:
            if item.get("parent") in missing:
                warnings.append(f"Group {item.get('name')}: parent group {item['parent']!r} skipped, none")
                item["parent"] = None
        for list_name in ("release_sources", "rules", "file_access_rules"):
            for item in data.get(list_name) or []:
                dropped = [name for name in item.get("groups") or [] if name in missing]
                if dropped:
                    item["groups"] = [name for name in item["groups"] if name not in missing]
                    label = item.get("name") or f"{item.get('rule_type')} {item.get('identifier')}"
                    warnings.append(f"{label}: skipped group {', '.join(dropped)} left out")
    return data, keep, warnings


class ConfigImportError(Exception):
    def __init__(self, errors):
        super().__init__("\n".join(errors))
        self.errors = errors


def export_config():
    groups = [{"name": g.name, "parent": g.parent.name if g.parent else None,
               **{f: getattr(g, f) for f in GROUP_FIELDS}}
              for g in Group.objects.select_related("parent").order_by("name")]
    tags = [{"name": t.name, "description": t.description} for t in Tag.objects.order_by("name")]
    release_sources = [
        {"name": s.name, **{f: getattr(s, f) for f in RELEASE_SOURCE_FIELDS},
         "groups": sorted(g.name for g in s.groups.all()),
         "tags": sorted(t.name for t in s.tags.all())}
        for s in ReleaseSource.objects.prefetch_related("groups", "tags").order_by("name")
    ]
    rules = [
        {**{f: getattr(r, f) for f in RULE_KEY_FIELDS + RULE_FIELDS},
         "groups": sorted(g.name for g in r.groups.all()),
         "machines": sorted(m.serial_number for m in r.machines.all()),
         "tags": sorted(t.name for t in r.tags.all())}
        for r in (Rule.objects.filter(release_source__isnull=True)
                              .prefetch_related("groups", "machines", "tags")
                              .order_by("rule_type", "identifier", "policy", "pk"))
    ]
    file_access_rules = [
        {"name": r.name, **{f: getattr(r, f) for f in FILE_ACCESS_RULE_FIELDS},
         "groups": sorted(g.name for g in r.groups.all()),
         "processes": [{f: getattr(p, f) for f in FILE_ACCESS_PROCESS_FIELDS} for p in r.processes.all()]}
        for r in FileAccessRule.objects.prefetch_related("groups", "processes").order_by("name")
    ]
    return {"format": FORMAT, "version": VERSION, "exported_at": timezone.now().isoformat(),
            "groups": groups, "tags": tags, "release_sources": release_sources, "rules": rules,
            "file_access_rules": file_access_rules}


class _Importer:
    def __init__(self, data, delete_missing, keep=()):
        self.data = data
        self.delete_missing = delete_missing
        # the keys of items not to delete (deletion_key): skipped in the preview, or kept there
        self.keep = set(keep)
        self.errors = []
        self.warnings = []
        self.changes = []  # (action, model verbose name, str(object))
        self.items = {}  # token of the file item: "created", "updated" or "unchanged"
        self.deletions = []  # (key, model verbose name, str(object))
        self.groups = {}
        self.tags = {}

    def record(self, action, obj):
        self.changes.append((action, obj._meta.verbose_name, str(obj)))

    def status(self, list_name, index, item, created, changed):
        token = item.get("_token") or item_token(list_name, index)
        self.items[token] = "created" if created else "updated" if changed else "unchanged"

    def delete(self, key, obj):
        """Delete obj (delete_missing), unless it is kept. Returns whether it is deleted."""
        if key in self.keep:
            return False
        self.record("deleted", obj)
        self.deletions.append((key, obj._meta.verbose_name, str(obj)))
        return True

    def save(self, obj, label):
        try:
            obj.full_clean(exclude=["sync_token"])
        except ValidationError as e:
            for field, messages in e.message_dict.items():
                self.errors.append(f"{label}: {field}: {' '.join(messages)}")
            return False
        obj.save()
        return True

    @staticmethod
    def _apply(obj, item, fields):
        changed = False
        for field in fields:
            if field in item and getattr(obj, field) != item[field]:
                setattr(obj, field, item[field])
                changed = True
        return changed

    def resolve_groups(self, names, label):
        groups = []
        for name in names or []:
            group = self.groups.get(name) or Group.objects.filter(name=name).first()
            if group is None:
                self.errors.append(f"{label}: unknown group {name!r}")
            else:
                groups.append(group)
        return groups

    def resolve_tags(self, names):
        # tags have no effect, an unknown one is simply created
        tags = []
        for name in names or []:
            tag = self.tags.get(name) or Tag.objects.filter(name=name).first()
            if tag is None:
                tag = Tag.objects.create(name=name)
                self.record("created", tag)
            self.tags[name] = tag
            tags.append(tag)
        return tags

    def import_tags(self):
        for item in self.data.get("tags", []):
            name = item.get("name")
            if not name:
                self.errors.append("Tag without name")
                continue
            tag = Tag.objects.filter(name=name).first()
            created = tag is None
            if created:
                tag = Tag(name=name)
            if self._apply(tag, item, ("description",)) or created:
                if not self.save(tag, f"Tag {name}"):
                    continue
                self.record("created" if created else "updated", tag)
            self.tags[name] = tag

    @staticmethod
    def set_m2m(manager, objects):
        wanted = {o.pk for o in objects}
        if set(manager.values_list("pk", flat=True)) == wanted:
            return False
        manager.set(objects)
        return True

    def import_groups(self):
        for index, item in enumerate(self.data.get("groups", [])):
            name = item.get("name")
            if not name:
                self.errors.append("Group without name")
                continue
            group = Group.objects.filter(name=name).first()
            created = group is None
            if created:
                group = Group(name=name)
            changed = self._apply(group, legacy_usb(item), GROUP_FIELDS)
            if changed or created:
                if not self.save(group, f"Group {name}"):
                    continue
                self.record("created" if created else "updated", group)
            self.status("groups", index, item, created, changed)
            self.groups[name] = group
        self.import_group_parents()

    def import_group_parents(self):
        """After all groups exist: the parents by name. A file without them (older versions) keeps the parents."""
        items = [item for item in self.data.get("groups", []) if item.get("name") in self.groups and "parent" in item]
        # the parents themselves first: they may lose their own parent (one level only)
        for item in sorted(items, key=lambda item: item["parent"] is not None):
            group = self.groups[item["name"]]
            parent = None
            if item["parent"] is not None:
                parent = self.groups.get(item["parent"]) or Group.objects.filter(name=item["parent"]).first()
                if parent is None:
                    self.errors.append(f"Group {group.name}: unknown parent group {item['parent']!r}")
                    continue
            if group.parent_id == (parent.pk if parent else None):
                continue
            group.parent = parent
            if self.save(group, f"Group {group.name}"):
                self.record("updated", group)
    def import_release_sources(self):
        names = set()
        for index, item in enumerate(self.data.get("release_sources", [])):
            name = item.get("name")
            if not name:
                self.errors.append("Release source without name")
                continue
            names.add(name)
            source = ReleaseSource.objects.filter(name=name).first()
            created = source is None
            if created:
                source = ReleaseSource(name=name)
            changed = self._apply(source, item, RELEASE_SOURCE_FIELDS) or created
            if changed and not self.save(source, f"Release source {name}"):
                continue
            groups = self.resolve_groups(item.get("groups"), f"Release source {name}")
            changed = self.set_m2m(source.groups, groups) or changed
            if "tags" in item:
                changed = self.set_m2m(source.tags, self.resolve_tags(item["tags"])) or changed
            if changed:
                self.record("created" if created else "updated", source)
            self.status("release_sources", index, item, created, changed)
        if self.delete_missing:
            for source in ReleaseSource.objects.all():
                if source.name not in names and self.delete(f"source:{source.name}", source):
                    source.delete()

    def import_rules(self):
        kept = set()
        for index, item in enumerate(self.data.get("rules", [])):
            label = f"Rule {item.get('rule_type')} {item.get('identifier')}"
            if any(not item.get(f) for f in RULE_KEY_FIELDS):
                self.errors.append(f"{label}: rule_type, identifier and policy are required")
                continue
            candidate = Rule(cel_expr=item.get("cel_expr", ""), **{f: item[f] for f in RULE_KEY_FIELDS})
            try:
                candidate.clean_fields(exclude=RULE_FIELDS)
                candidate.clean()
            except ValidationError as e:
                self.errors.append(f"{label}: {' '.join(e.messages)}")
                continue
            key = {f: getattr(candidate, f) for f in RULE_KEY_FIELDS}
            existing = list(Rule.objects.filter(release_source__isnull=True, **key).order_by("pk"))
            if len(existing) > 1:
                self.warnings.append(f"{label}: {len(existing)} rules on the target, only the first one is updated")
            rule = existing[0] if existing else candidate
            created = not existing
            changed = self._apply(rule, item, RULE_FIELDS) or created
            if changed and not self.save(rule, label):
                continue
            kept.add(rule.pk)
            groups = self.resolve_groups(item.get("groups"), label)
            machines = []
            for serial_number in item.get("machines") or []:
                machine = Machine.objects.filter(serial_number=serial_number).first()
                if machine is None:
                    self.warnings.append(f"{label}: machine {serial_number} unknown on the target, skipped")
                else:
                    machines.append(machine)
            changed = self.set_m2m(rule.groups, groups) or changed
            changed = self.set_m2m(rule.machines, machines) or changed
            if "tags" in item:
                changed = self.set_m2m(rule.tags, self.resolve_tags(item["tags"])) or changed
            if not rule.is_global and not groups and not machines:
                self.warnings.append(f"{label}: no scope on the target, it applies to no Mac")
            if changed:
                self.record("created" if created else "updated", rule)
            self.status("rules", index, item, created, changed)
        if self.delete_missing:
            # compared in Python: a NOT IN with every kept pk could exceed the 2100 parameters of SQL Server
            to_delete = [rule for rule in Rule.objects.filter(release_source__isnull=True) if rule.pk not in kept
                         and self.delete("rule:" + "|".join(str(getattr(rule, f)) for f in RULE_KEY_FIELDS), rule)]
            for start in range(0, len(to_delete), 1000):
                Rule.objects.filter(pk__in=[rule.pk for rule in to_delete[start:start + 1000]]).delete()

    def import_file_access_rules(self):
        names = set()
        for index, item in enumerate(self.data.get("file_access_rules", [])):
            name = item.get("name")
            if not name:
                self.errors.append("File access rule without name")
                continue
            names.add(name)
            label = f"File access rule {name}"
            rule = FileAccessRule.objects.filter(name=name).first()
            created = rule is None
            if created:
                rule = FileAccessRule(name=name)
            changed = self._apply(rule, item, FILE_ACCESS_RULE_FIELDS) or created
            processes = [FileAccessProcess(**{f: process.get(f, False if f == "platform_binary" else "")
                                              for f in FILE_ACCESS_PROCESS_FIELDS})
                         for process in item.get("processes") or []]
            invalid = False
            for process in processes:
                try:
                    # the field lengths too: an overlong value would fail in the database, not here
                    process.full_clean(exclude=["rule"])
                except ValidationError as e:
                    self.errors.append(f"{label}: {' '.join(e.messages)}")
                    invalid = True
            if invalid or (changed and not self.save(rule, label)):
                continue
            old = [{f: getattr(p, f) for f in FILE_ACCESS_PROCESS_FIELDS} for p in rule.processes.all()]
            if old != [{f: getattr(p, f) for f in FILE_ACCESS_PROCESS_FIELDS} for p in processes]:
                rule.processes.all().delete()
                for process in processes:
                    process.rule = rule
                    process.save()
                changed = True
            changed = self.set_m2m(rule.groups, self.resolve_groups(item.get("groups"), label)) or changed
            if changed:
                self.record("created" if created else "updated", rule)
            self.status("file_access_rules", index, item, created, changed)
        # files of older versions have no file access rules: nothing to compare with
        if self.delete_missing and "file_access_rules" in self.data:
            for rule in FileAccessRule.objects.all():
                if rule.name not in names and self.delete(f"file_access:{rule.name}", rule):
                    rule.delete()

    def run(self):
        if self.data.get("format") != FORMAT:
            raise ConfigImportError(["Not a santa-server configuration file"])
        if self.data.get("version") != VERSION:
            raise ConfigImportError([f"Unsupported version {self.data.get('version')}, expected {VERSION}"])
        self.import_groups()
        self.import_tags()
        self.import_release_sources()
        self.import_rules()
        self.import_file_access_rules()
        if self.errors:
            raise ConfigImportError(self.errors)


class _DryRun(Exception):
    pass


def import_config(data, delete_missing=False, dry_run=False, keep=()):
    """Import the exported data in one transaction. Nothing is written if there is an error, or on a dry run.

    keep: the keys (deletion_key) of the items delete_missing must leave.
    Returns {"changes": [(action, model, object)], "warnings": [...], "items": {token: status},
    "deletions": [(key, model, object)]}, raises ConfigImportError.
    """
    importer = _Importer(data, delete_missing, keep)
    try:
        with transaction.atomic():
            importer.run()
            if dry_run:
                raise _DryRun
    except _DryRun:
        pass
    return {"changes": importer.changes, "warnings": importer.warnings, "items": importer.items,
            "deletions": importer.deletions}
