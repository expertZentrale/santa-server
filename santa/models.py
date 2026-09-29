import hashlib
import json
import re
import secrets
from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils.text import format_lazy
from django.utils.translation import gettext
from django.utils.translation import gettext_lazy as _

from .validators import combine_path_regexes, validate_identifier, validate_path_regexes


def generate_sync_token():
    return secrets.token_urlsafe(32)


class ClientMode(models.TextChoices):
    MONITOR = "MONITOR", _("Monitor")
    LOCKDOWN = "LOCKDOWN", _("Lockdown")


# mount flags Santa accepts for the USB remount
USB_REMOUNT_FLAGS = {"rdonly", "noexec", "nosuid", "nobrowse", "noowners", "nodev", "async", "-j"}


class Group(models.Model):
    """A group of Macs with its own Santa configuration and sync URL (one configuration profile per group)."""

    name = models.CharField(max_length=200, unique=True)
    description = models.TextField(blank=True)
    sync_token = models.CharField(
        max_length=64, unique=True, default=generate_sync_token, editable=False,
        help_text=_("Secret part of the sync URL. Regenerate it with the admin action if it leaks."),
    )

    client_mode = models.CharField(
        max_length=16, choices=ClientMode.choices, default=ClientMode.MONITOR,
        help_text=_("Monitor: everything runs except the blocked binaries, unknown binaries are logged as "
                  "ALLOW_UNKNOWN events (use it to prepare the rules). "
                  "Lockdown: only allowed binaries run, unknown ones are blocked (BLOCK_UNKNOWN)."),
    )
    batch_size = models.PositiveIntegerField(
        default=100,
        help_text=_("How many events the Mac uploads, and how many rules it downloads, per request (5 – 500)."),
    )
    full_sync_interval = models.PositiveIntegerField(
        default=600, help_text=_("Seconds between two syncs of a Mac (min. 60). New rules arrive at the next sync.")
    )
    allowed_path_regex = models.TextField(
        _("allowed path regexes"), blank=True,
        help_text=_("One regular expression per line, matched against the full path of the binary. Matching binaries "
                  "are allowed, even in lockdown, without any rule. Anchor them (^/Applications/Foo\\.app/) "
                  "and never use user-writable paths: anyone who can write there can run anything."),
    )
    blocked_path_regex = models.TextField(
        _("blocked path regexes"), blank=True,
        help_text=_("One regular expression per line. Matching binaries are blocked, also in monitor mode, "
                  "e.g. ^/Users/[^/]+/Downloads/"),
    )
    enable_bundles = models.BooleanField(
        default=False,
        help_text=_("When a binary from an app bundle is blocked, Santa computes a hash of the whole bundle and adds "
                  "it to the event. This server does not create bundle rules: allow apps with a Signing ID or Team ID "
                  "rule instead. Leave it off unless you want the bundle hashes in the events."),
    )
    enable_transitive_rules = models.BooleanField(
        default=False,
        help_text=_("Files written by a binary with an “Allow compiler” rule (e.g. clang, swiftc, go) are allowed "
                  "automatically on that Mac (transitive rules, local to the Mac). Needed by developers in lockdown "
                  "to run what they build. Without “Allow compiler” rules this option does nothing."),
    )
    enable_all_event_upload = models.BooleanField(
        default=False,
        help_text=_("Upload an event for every execution, including the allowed ones, not only for the blocked / "
                  "unknown ones. Useful to see what runs before switching a group to lockdown, but very noisy."),
    )
    block_usb_mount = models.BooleanField(
        _("block USB mass storage"), default=False,
        help_text=_("Block the mounting of USB mass storage devices (sticks, external disks)."),
    )
    remount_usb_mode = models.CharField(
        _("remount USB with flags"), max_length=200, blank=True,
        help_text=format_lazy(
            _("Only with “Block USB mass storage”: instead of blocking, remount the device with these flags, "
              "comma separated, e.g. rdonly,noexec (read only, nothing executable). Allowed: {flags}."),
            flags=", ".join(sorted(USB_REMOUNT_FLAGS)),
        ),
    )
    event_detail_url = models.URLField(
        _("block dialog URL"), max_length=800, blank=True,
        help_text=_("Button in the block dialog, e.g. a ticket form of your IT team. Santa replaces placeholders "
                  "such as %file_sha%, %machine_id% and %username% (see the Santa docs). Sent at every sync."),
    )
    event_detail_text = models.CharField(
        _("block dialog button text"), max_length=48, blank=True,
        help_text=_("e.g. “Request access”. Sent at every sync."),
    )

    # Only in the configuration profile (.mobileconfig), the sync protocol cannot set them
    unknown_block_message = models.TextField(
        blank=True,
        help_text=_("Profile only. Shown when an unknown binary is blocked in lockdown, e.g. how to ask for access. "
                  "Empty = Santa's default text."),
    )
    banned_block_message = models.TextField(
        blank=True,
        help_text=_("Profile only. Shown when a binary is blocked by a block rule. Empty = Santa's default text."),
    )
    enable_bad_signature_protection = models.BooleanField(
        default=False,
        help_text=_("Profile only. Also block, in monitor mode, the binaries with a broken code signature "
                  "(tampered files), unless a rule allows them."),
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    def clean(self):
        errors = {}
        if not 5 <= self.batch_size <= 500:
            errors["batch_size"] = gettext("Must be between 5 and 500.")
        if self.full_sync_interval < 60:
            errors["full_sync_interval"] = gettext("Must be at least 60 seconds.")
        for field in ("allowed_path_regex", "blocked_path_regex"):
            regex_errors = validate_path_regexes(getattr(self, field))
            if regex_errors:
                errors[field] = regex_errors
        unknown_flags = set(self.remount_usb_mode_list()) - USB_REMOUNT_FLAGS
        if unknown_flags:
            errors["remount_usb_mode"] = gettext("Unknown flag(s): %(flags)s.") % {
                "flags": ", ".join(sorted(unknown_flags))}
        elif self.remount_usb_mode_list() and not self.block_usb_mount:
            errors["remount_usb_mode"] = gettext("Only used with “Block USB mass storage”.")
        if errors:
            raise ValidationError(errors)

    @property
    def sync_base_url(self):
        # Santa resolves "preflight/<machine id>" relative to this URL, the trailing slash is required
        return f"{settings.SANTA_PUBLIC_BASE_URL}/sync/{self.sync_token}/"

    def allowed_path_regex_for_santa(self):
        return combine_path_regexes(self.allowed_path_regex)

    def blocked_path_regex_for_santa(self):
        return combine_path_regexes(self.blocked_path_regex)

    def remount_usb_mode_list(self):
        return [flag.strip() for flag in self.remount_usb_mode.split(",") if flag.strip()]


class Machine(models.Model):
    machine_id = models.CharField(max_length=64, unique=True, help_text=_("Santa machine ID, the hardware UUID"))
    serial_number = models.CharField(max_length=64, db_index=True)
    hostname = models.CharField(max_length=255, blank=True)
    model_identifier = models.CharField(max_length=64, blank=True)
    os_version = models.CharField(max_length=32, blank=True)
    os_build = models.CharField(max_length=32, blank=True)
    santa_version = models.CharField(max_length=64, blank=True)
    primary_user = models.CharField(max_length=255, blank=True)
    client_mode = models.CharField(max_length=16, choices=ClientMode.choices, blank=True,
                                   help_text=_("Mode reported by the client"))
    group = models.ForeignKey(Group, on_delete=models.PROTECT, related_name="machines")

    binary_rule_count = models.PositiveIntegerField(default=0)
    certificate_rule_count = models.PositiveIntegerField(default=0)
    teamid_rule_count = models.PositiveIntegerField(default=0)
    signingid_rule_count = models.PositiveIntegerField(default=0)
    cdhash_rule_count = models.PositiveIntegerField(default=0)
    compiler_rule_count = models.PositiveIntegerField(default=0)
    transitive_rule_count = models.PositiveIntegerField(default=0)

    # Rules the client confirmed during its last postflight: {"<rule_type>:<identifier>": "<payload hash>"}
    synced_rules = models.JSONField(default=dict, blank=True, editable=False)
    # Rules of the running sync session, committed to synced_rules on postflight
    pending_sync = models.JSONField(null=True, blank=True, editable=False)
    clean_sync_requested = models.BooleanField(
        default=False, help_text=_("Send a clean sync (client drops all its rules) at the next preflight.")
    )
    sync_in_clean_mode = models.BooleanField(default=False, editable=False)

    last_ip = models.GenericIPAddressField(null=True, blank=True)
    last_preflight_at = models.DateTimeField(null=True, blank=True)
    last_postflight_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["serial_number"]

    def __str__(self):
        return f"{self.serial_number} ({self.hostname})" if self.hostname else self.serial_number

    def reported_rule_count(self):
        return sum(getattr(self, f"{prefix}_rule_count") for prefix in RULE_COUNT_PREFIXES)


RULE_COUNT_PREFIXES = ("binary", "certificate", "teamid", "signingid", "cdhash", "compiler", "transitive")


class RuleType(models.TextChoices):
    SIGNINGID = "SIGNINGID", _("Signing ID")
    BINARY = "BINARY", _("Binary (SHA-256)")
    TEAMID = "TEAMID", _("Team ID")
    CERTIFICATE = "CERTIFICATE", _("Certificate (SHA-256)")
    CDHASH = "CDHASH", _("CDHash")


class Policy(models.TextChoices):
    ALLOWLIST = "ALLOWLIST", _("Allow")
    ALLOWLIST_COMPILER = "ALLOWLIST_COMPILER", _("Allow compiler")
    BLOCKLIST = "BLOCKLIST", _("Block")
    SILENT_BLOCKLIST = "SILENT_BLOCKLIST", _("Block silently")
    CEL = "CEL", _("CEL expression")


# When several rules target the same identifier for a machine, the most specific scope wins
# (machine > group > global), and on the same scope a block wins over an allow.
POLICY_PRECEDENCE = {
    Policy.ALLOWLIST: 0,
    Policy.ALLOWLIST_COMPILER: 1,
    Policy.CEL: 2,
    Policy.BLOCKLIST: 3,
    Policy.SILENT_BLOCKLIST: 4,
}


class Tag(models.Model):
    """A label to sort and find the rules. It has no effect on the Macs."""

    name = models.CharField(max_length=100, unique=True)
    description = models.CharField(max_length=500, blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


# ReleaseVersion.identifier is part of a unique index
IDENTIFIER_MAX_LENGTH = 450


class ReleaseSource(models.Model):
    """Watches the releases of an unsigned tool and allowlists the hashes of every new version."""

    class Kind(models.TextChoices):
        GITHUB_RELEASE = "GITHUB_RELEASE", _("GitHub release")
        HOMEBREW_FORMULA = "HOMEBREW_FORMULA", _("Homebrew formula (bottle)")
        HOMEBREW_CASK = "HOMEBREW_CASK", _("Homebrew cask")
        URL = "URL", _("Direct download URL")
        NPM_PACKAGE = "NPM_PACKAGE", _("npm package")
        VSCODE_EXTENSION = "VSCODE_EXTENSION", _("VS Code extension")
        JETBRAINS_PLUGIN = "JETBRAINS_PLUGIN", _("JetBrains plugin (PhpStorm, …)")

    name = models.CharField(max_length=200, unique=True)
    kind = models.CharField(max_length=32, choices=Kind.choices)
    identifier = models.TextField(
        _("identifiers"),
        help_text=_("One per line, each one is checked on its own and keeps its own versions. "
                  "GitHub: owner/repo (e.g. abiosoft/colima) · Homebrew: formula or cask name · URL: the full URL · "
                  "npm: package name (e.g. @esbuild/darwin-arm64) · VS Code: publisher.name "
                  "(e.g. rust-lang.rust-analyzer) · JetBrains: plugin ID (e.g. com.intellij.plugins.watcher)"),
    )
    asset_pattern = models.CharField(
        max_length=200, blank=True,
        help_text=_("Regex. GitHub: matched against the asset names (e.g. Darwin). "
                  "Homebrew formula: matched against the bottle tags (e.g. ^arm64_). "
                  "Homebrew cask: matched against the download URLs. "
                  "VS Code: matched against the target platforms (darwin-arm64, darwin-x64, universal). "
                  "JetBrains: matched against the compatible products (e.g. ^PHPSTORM$). Empty = everything."),
    )
    version_pattern = models.CharField(
        max_length=200, blank=True,
        help_text=_("Regex. Only releases whose version matches are taken (e.g. ^1\\. to stay on 1.x). "
                  "Empty = every version."),
    )
    binary_pattern = models.CharField(
        max_length=200, blank=True,
        help_text=_("Glob matched against the paths inside an archive (e.g. */bin/colima). "
                  "Empty = every Mach-O executable found."),
    )
    include_prereleases = models.BooleanField(default=False, help_text=_("GitHub, VS Code, JetBrains (EAP)"))

    rule_type = models.CharField(
        _("preferred rule type"), max_length=16, choices=RuleType.choices, default=RuleType.BINARY,
        help_text=_("The rules created for the executables found. Binary / CDHash: one rule per file and version. "
                  "Signing ID / Certificate / Team ID: one rule for all versions, for signed tools. "
                  "An executable without that information (unsigned) gets a Binary rule instead."),
    )
    policy = models.CharField(max_length=32, choices=Policy.choices, default=Policy.ALLOWLIST)
    custom_msg = models.CharField(_("custom message"), max_length=500, blank=True,
                                  help_text=_("Block and CEL: shown in the block dialog"))
    custom_url = models.URLField(max_length=800, blank=True,
                                 help_text=_("Block and CEL: opened from the block dialog"))
    cel_expr = models.TextField(_("CEL expression"), blank=True, help_text=_("Only with the CEL policy"))

    is_global = models.BooleanField(default=False, help_text=_("Allow on every Mac"))
    groups = models.ManyToManyField(Group, blank=True, related_name="release_sources")
    auto_approve = models.BooleanField(
        default=True, help_text=_("Enable the rules of new releases automatically. Otherwise they wait for approval.")
    )
    auto_approve_delay_days = models.PositiveIntegerField(
        _("auto approve delay (days)"), default=0,
        help_text=_("With auto approve: enable the rules of a new release only this many days after it was published "
                  "(GitHub), or first seen by this server (Homebrew, URL). Gives time to notice a compromised "
                  "release. 0 = right away. “Enable / approve” on the rules skips the wait, "
                  "disabling them cancels it."),
    )
    tags = models.ManyToManyField(Tag, blank=True, related_name="release_sources",
                                  help_text=_("Added to the rules of every new release"))
    keep_versions = models.PositiveIntegerField(
        default=3, help_text=_("Rules of older versions are deleted. 0 = keep all.")
    )
    is_enabled = models.BooleanField(default=True)

    # {identifier: {"name": …, "icon_url": …}} from the catalog, only for display
    identifier_icons = models.JSONField(default=dict, blank=True, editable=False)

    last_checked_at = models.DateTimeField(null=True, blank=True, editable=False)
    last_error = models.TextField(blank=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    @property
    def identifiers(self):
        return list(dict.fromkeys(line.strip() for line in self.identifier.splitlines() if line.strip()))

    @property
    def has_several_identifiers(self):
        return len(self.identifiers) > 1

    def clean(self):
        errors = {}
        identifiers = self.identifiers
        identifier_errors = [] if identifiers else [gettext("Enter at least one identifier.")]
        for identifier in identifiers:
            if len(identifier) > IDENTIFIER_MAX_LENGTH:
                identifier_errors.append(gettext("%(identifier)s…: at most %(max)s characters.") % {
                    "identifier": identifier[:50], "max": IDENTIFIER_MAX_LENGTH})
            elif self.kind == self.Kind.GITHUB_RELEASE and identifier.count("/") != 1:
                identifier_errors.append(gettext("%(identifier)s: use the owner/repo format.") % {
                    "identifier": identifier})
        if identifier_errors:
            errors["identifier"] = identifier_errors
        else:
            self.identifier = "\n".join(identifiers)
        for name in ("asset_pattern", "version_pattern"):
            try:
                re.compile(getattr(self, name))
            except re.error as e:
                errors[name] = gettext("Invalid regex: %(error)s") % {"error": e}
        if self.policy == Policy.CEL and not self.cel_expr:
            errors["cel_expr"] = gettext("Required for the CEL policy.")
        if self.policy != Policy.CEL and self.cel_expr:
            errors["cel_expr"] = gettext("Only used with the CEL policy.")
        if errors:
            raise ValidationError(errors)


class ReleaseVersion(models.Model):
    source = models.ForeignKey(ReleaseSource, on_delete=models.CASCADE, related_name="versions")
    # one line of the identifiers of the source
    identifier = models.CharField(max_length=IDENTIFIER_MAX_LENGTH, blank=True)
    version = models.CharField(max_length=200)
    published_at = models.DateTimeField(null=True, blank=True)
    binary_count = models.PositiveIntegerField(default=0)
    notes = models.TextField(blank=True)
    # the rules are disabled until the auto approve delay of the source has passed
    auto_enable_pending = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        unique_together = (("source", "identifier", "version"),)

    def __str__(self):
        if self.source.has_several_identifiers:
            return f"{self.source} {self.identifier} {self.version}"
        return f"{self.source} {self.version}"

    @property
    def auto_enable_at(self):
        return (self.published_at or self.created_at) + timedelta(days=self.source.auto_approve_delay_days)


class Rule(models.Model):
    rule_type = models.CharField(
        max_length=16, choices=RuleType.choices, default=RuleType.SIGNINGID,
        help_text=_("Binary: exactly this file, every new version needs a new rule (unsigned tools). "
                  "CDHash: this code signature of the file. "
                  "Signing ID: this program from this developer, all versions (best for signed apps). "
                  "Certificate: everything signed with this certificate. "
                  "Team ID: everything from this developer. "
                  "The most specific rule type wins on the Mac: "
                  "CDHash > Binary > Signing ID > Certificate > Team ID."),
    )
    identifier = models.CharField(
        max_length=256,
        help_text=_("SHA-256 for binaries / certificates, 10 character Team ID, "
                  "TEAMID:bundle.id (or platform:bundle.id) for signing IDs, 40 character CDHash."),
    )
    policy = models.CharField(
        max_length=32, choices=Policy.choices, default=Policy.ALLOWLIST,
        help_text=_("Allow compiler: allowed, and the files it writes are allowed too on Macs with transitive rules "
                  "enabled. Block silently: blocked without showing the dialog. "
                  "CEL: allow or block depending on a CEL expression (e.g. on the arguments)."),
    )
    custom_msg = models.CharField(_("custom message"), max_length=500, blank=True,
                                  help_text=_("Shown in the block dialog"))
    custom_url = models.URLField(max_length=800, blank=True, help_text=_("Opened from the block dialog"))
    cel_expr = models.TextField(_("CEL expression"), blank=True)
    description = models.CharField(max_length=500, blank=True)
    tags = models.ManyToManyField(Tag, blank=True, related_name="rules",
                                  help_text=_("Only to sort and find the rules, no effect on the Macs"))

    is_global = models.BooleanField(default=False, help_text=_("Apply to every Mac"))
    groups = models.ManyToManyField(Group, blank=True, related_name="rules")
    machines = models.ManyToManyField(Machine, blank=True, related_name="rules",
                                      help_text=_("Apply to these individual Macs"))
    is_enabled = models.BooleanField(default=True, help_text=_("Disabled rules are removed from the Macs"))

    release_source = models.ForeignKey(ReleaseSource, on_delete=models.CASCADE, null=True, blank=True,
                                       related_name="rules", editable=False)
    release_version = models.ForeignKey(ReleaseVersion, on_delete=models.CASCADE, null=True, blank=True,
                                        related_name="rules", editable=False)

    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name="+", editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["rule_type", "identifier"])]

    def __str__(self):
        return f"{self.get_policy_display()} {self.rule_type} {self.identifier}"

    def clean(self):
        self.identifier = self.identifier.strip()
        try:
            self.identifier = validate_identifier(self.rule_type, self.identifier)
        except ValidationError as e:
            raise ValidationError({"identifier": e.messages})
        if self.policy == Policy.CEL and not self.cel_expr:
            raise ValidationError({"cel_expr": gettext("Required for the CEL policy.")})
        if self.policy != Policy.CEL and self.cel_expr:
            raise ValidationError({"cel_expr": gettext("Only used with the CEL policy.")})

    @property
    def santa_key(self):
        return f"{self.rule_type}:{self.identifier}"

    def to_santa(self):
        rule = {"identifier": self.identifier, "rule_type": self.rule_type, "policy": self.policy}
        if self.policy in (Policy.BLOCKLIST, Policy.CEL):
            if self.custom_msg:
                rule["custom_msg"] = self.custom_msg
            if self.custom_url:
                rule["custom_url"] = self.custom_url
        if self.policy == Policy.CEL:
            rule["cel_expr"] = self.cel_expr
        return rule


def payload_hash(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:16]


class Event(models.Model):
    machine = models.ForeignKey(Machine, on_delete=models.CASCADE, related_name="events")
    group = models.ForeignKey(Group, on_delete=models.CASCADE, related_name="events")
    decision = models.CharField(max_length=64, db_index=True)
    execution_time = models.DateTimeField(db_index=True)
    file_sha256 = models.CharField(max_length=64, db_index=True)
    file_path = models.CharField(max_length=1024, blank=True)
    file_name = models.CharField(max_length=255, blank=True)
    executing_user = models.CharField(max_length=255, blank=True)
    parent_name = models.CharField(max_length=255, blank=True)
    bundle_id = models.CharField(max_length=255, blank=True)
    bundle_name = models.CharField(max_length=255, blank=True)
    bundle_version = models.CharField(max_length=128, blank=True)
    signing_id = models.CharField(max_length=255, blank=True)
    team_id = models.CharField(max_length=32, blank=True)
    cdhash = models.CharField(max_length=64, blank=True)
    cert_sha256 = models.CharField(max_length=64, blank=True)
    cert_cn = models.CharField(max_length=255, blank=True)
    raw = models.JSONField(default=dict)

    resolved_at = models.DateTimeField(null=True, blank=True)
    resolved_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name="+")
    resolution_rule = models.ForeignKey(Rule, on_delete=models.SET_NULL, null=True, blank=True,
                                        related_name="resolved_events")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-execution_time"]

    def __str__(self):
        return f"{self.decision} {self.file_name or self.file_sha256}"

    @property
    def is_blocked(self):
        return self.decision.startswith("BLOCK_")

    def identifier_for(self, rule_type):
        return {
            RuleType.BINARY: self.file_sha256,
            RuleType.CERTIFICATE: self.cert_sha256,
            RuleType.TEAMID: self.team_id,
            RuleType.SIGNINGID: self.signing_id,
            RuleType.CDHASH: self.cdhash,
        }[rule_type]


class AccessRequest(models.Model):
    """A user asks for software: a blocked binary of their Mac, a package, or anything else."""

    class Kind(models.TextChoices):
        EVENT = "EVENT", _("Blocked on my Mac")
        PACKAGE = "PACKAGE", _("Package")
        OTHER = "OTHER", _("Other")

    class Status(models.TextChoices):
        PENDING = "PENDING", _("Pending")
        APPROVED = "APPROVED", _("Approved")
        DENIED = "DENIED", _("Denied")
        CANCELLED = "CANCELLED", _("Cancelled")

    requester = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="access_requests")
    kind = models.CharField(max_length=16, choices=Kind.choices)
    event = models.ForeignKey(Event, on_delete=models.SET_NULL, null=True, blank=True, related_name="access_requests")
    machine = models.ForeignKey(Machine, on_delete=models.SET_NULL, null=True, blank=True,
                                related_name="access_requests")
    # SHA-256 of the binary, kept when the event is deleted by cleanup_events
    file_sha256 = models.CharField(max_length=64, blank=True, db_index=True)
    title = models.CharField(max_length=200)
    justification = models.TextField(max_length=1000)
    link = models.URLField(max_length=800, blank=True)

    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING, db_index=True)
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name="+")
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.TextField(blank=True)
    result_rule = models.ForeignKey(Rule, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    result_source = models.ForeignKey(ReleaseSource, on_delete=models.SET_NULL, null=True, blank=True,
                                      related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        permissions = [
            ("request_event", "Can request the apps blocked on their Macs"),
            ("request_package", "Can request packages"),
            ("request_other", "Can request other software"),
        ]

    def __str__(self):
        return f"{self.title} ({self.requester})"

    @property
    def is_pending(self):
        return self.status == self.Status.PENDING


class AccessRequestPackage(models.Model):
    """One of the packages of a request: the approver decides for each one"""

    class Status(models.TextChoices):
        PENDING = "PENDING", _("Pending")
        APPROVED = "APPROVED", _("Approved")
        DENIED = "DENIED", _("Not approved")

    access_request = models.ForeignKey(AccessRequest, on_delete=models.CASCADE, related_name="packages")
    kind = models.CharField(max_length=32, choices=ReleaseSource.Kind.choices)
    identifier = models.CharField(max_length=IDENTIFIER_MAX_LENGTH)
    # from the catalog suggestion the user picked, only for display
    name = models.CharField(max_length=200, blank=True)
    icon_url = models.URLField(max_length=800, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    result_source = models.ForeignKey(ReleaseSource, on_delete=models.SET_NULL, null=True, blank=True,
                                      related_name="+")

    class Meta:
        ordering = ["pk"]
        indexes = [models.Index(fields=["kind", "identifier"])]

    def __str__(self):
        return f"{self.get_kind_display()}: {self.identifier}"


class UserProfile(models.Model):
    """Preferences of a user of the console and the request form"""

    class Theme(models.TextChoices):
        AUTO = "auto", _("System")
        LIGHT = "light", _("Light")
        DARK = "dark", _("Dark")

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="santa_profile")
    theme = models.CharField(max_length=8, choices=Theme.choices, default=Theme.AUTO,
                             help_text=_("System: follows the light or dark mode of your computer."))
    # empty: the language of the browser, else the default of the server (LANGUAGE_CODE)
    language = models.CharField(max_length=8, blank=True, choices=settings.LANGUAGES)
    # OIDC_ADMIN_ROLE was in the ID token of the last sign-in: keeps "Santa admins" when the roles are recomputed
    has_admin_role = models.BooleanField(default=False, editable=False)

    def __str__(self):
        return f"Profile of {self.user}"


class SignInGroup(models.Model):
    """A group of the OpenID Connect provider (in OIDC_GROUPS_CLAIM) whose members get roles at sign-in.

    Only the groups added here count: the others in the claim are ignored and never stored.
    """

    EVERYONE = "*"

    claim_value = models.CharField(
        max_length=255, unique=True,
        help_text=_("The value in the groups claim of the ID token: the object ID or the name of the group, "
                    "depending on the provider. * is everyone who signs in."))
    name = models.CharField(max_length=200, help_text=_("A name to recognise the group, only for display"))
    console_access = models.BooleanField(
        default=False, help_text=_("The members can open the console (with the permissions of their roles)"))
    roles = models.ManyToManyField("auth.Group", blank=True, related_name="sign_in_groups",
                                   help_text=_("The members get these roles"))
    # state, not configuration: who had the group at their last sign-in
    members = models.ManyToManyField(settings.AUTH_USER_MODEL, blank=True, related_name="sign_in_groups",
                                     editable=False)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name
