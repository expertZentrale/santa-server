"""Export and import the configuration (groups, release sources, rules) as JSON, e.g. from dev to live.

Everything is matched by natural keys, never by primary key:
- groups, tags and release sources by name,
- manual rules by (rule type, identifier, policy),
- machine scopes by serial number (machines are never created).

The sync tokens are never exported: a group keeps its token (and so its configuration profile) on the target,
a new group gets a new one. The rules of the release sources are not exported either, the target builds them
by checking the sources itself.
"""
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .models import Group, Machine, ReleaseSource, Rule, Tag

FORMAT = "santa-server-config"
VERSION = 1

GROUP_FIELDS = (
    "description", "client_mode", "batch_size", "full_sync_interval", "allowed_path_regex", "blocked_path_regex",
    "enable_bundles", "enable_transitive_rules", "enable_all_event_upload", "block_usb_mount", "remount_usb_mode",
    "event_detail_url", "event_detail_text", "unknown_block_message", "banned_block_message",
    "enable_bad_signature_protection",
)
RELEASE_SOURCE_FIELDS = (
    "kind", "identifier", "version_pattern", "asset_pattern", "binary_pattern", "include_prereleases", "rule_type",
    "policy", "custom_msg", "custom_url", "cel_expr", "is_global", "auto_approve", "auto_approve_delay_days",
    "keep_versions", "is_enabled",
)
RULE_KEY_FIELDS = ("rule_type", "identifier", "policy")
RULE_FIELDS = ("custom_msg", "custom_url", "cel_expr", "description", "is_global", "is_enabled")


class ConfigImportError(Exception):
    def __init__(self, errors):
        super().__init__("\n".join(errors))
        self.errors = errors


def export_config():
    groups = [{"name": g.name, **{f: getattr(g, f) for f in GROUP_FIELDS}} for g in Group.objects.order_by("name")]
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
    return {"format": FORMAT, "version": VERSION, "exported_at": timezone.now().isoformat(),
            "groups": groups, "tags": tags, "release_sources": release_sources, "rules": rules}


class _Importer:
    def __init__(self, data, delete_missing):
        self.data = data
        self.delete_missing = delete_missing
        self.errors = []
        self.warnings = []
        self.changes = []  # (action, model verbose name, str(object))
        self.groups = {}
        self.tags = {}

    def record(self, action, obj):
        self.changes.append((action, obj._meta.verbose_name, str(obj)))

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
        for item in self.data.get("groups", []):
            name = item.get("name")
            if not name:
                self.errors.append("Group without name")
                continue
            group = Group.objects.filter(name=name).first()
            created = group is None
            if created:
                group = Group(name=name)
            if self._apply(group, item, GROUP_FIELDS) or created:
                if not self.save(group, f"Group {name}"):
                    continue
                self.record("created" if created else "updated", group)
            self.groups[name] = group

    def import_release_sources(self):
        names = set()
        for item in self.data.get("release_sources", []):
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
        if self.delete_missing:
            for source in ReleaseSource.objects.all():
                if source.name not in names:
                    self.record("deleted", source)
                    source.delete()

    def import_rules(self):
        kept = set()
        for item in self.data.get("rules", []):
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
        if self.delete_missing:
            # compared in Python: a NOT IN with every kept pk could exceed the 2100 parameters of SQL Server
            to_delete = [rule for rule in Rule.objects.filter(release_source__isnull=True) if rule.pk not in kept]
            for rule in to_delete:
                self.record("deleted", rule)
            for start in range(0, len(to_delete), 1000):
                Rule.objects.filter(pk__in=[rule.pk for rule in to_delete[start:start + 1000]]).delete()

    def run(self):
        if self.data.get("format") != FORMAT:
            raise ConfigImportError(["Not a santa-server configuration file"])
        if self.data.get("version") != VERSION:
            raise ConfigImportError([f"Unsupported version {self.data.get('version')}, expected {VERSION}"])
        self.import_groups()
        self.import_tags()
        self.import_release_sources()
        self.import_rules()
        if self.errors:
            raise ConfigImportError(self.errors)


class _DryRun(Exception):
    pass


def import_config(data, delete_missing=False, dry_run=False):
    """Import the exported data in one transaction. Nothing is written if there is an error, or on a dry run.

    Returns {"changes": [(action, model, object)], "warnings": [...]}, raises ConfigImportError.
    """
    importer = _Importer(data, delete_missing)
    try:
        with transaction.atomic():
            importer.run()
            if dry_run:
                raise _DryRun
    except _DryRun:
        pass
    return {"changes": importer.changes, "warnings": importer.warnings}
