from django.db.models import Q
from django.utils import timezone

from .models import Event, Machine, ReleaseVersion, Rule, RuleType

EVENT_IDENTIFIER_FIELDS = {
    RuleType.BINARY: "file_sha256",
    RuleType.CERTIFICATE: "cert_sha256",
    RuleType.TEAMID: "team_id",
    RuleType.SIGNINGID: "signing_id",
    RuleType.CDHASH: "cdhash",
}


def allow_identifier(rule_type, identifier, policy, is_global, groups, user, description="", tags=(), machines=()):
    """Create the rule, or widen the scope of the manual rule that already exists for this identifier.

    Returns (rule, created).
    """
    rule = (Rule.objects.filter(rule_type=rule_type, identifier=identifier, policy=policy,
                                release_source__isnull=True)
                        .order_by("pk").first())
    created = rule is None
    if created:
        rule = Rule(rule_type=rule_type, identifier=identifier, policy=policy, created_by=user,
                    description=description[:500])
        rule.full_clean(exclude=["groups", "machines"])
    rule.is_global = rule.is_global or is_global
    rule.is_enabled = True
    rule.save()
    if not is_global:
        rule.groups.add(*groups)
        rule.machines.add(*machines)
    rule.tags.add(*tags)
    resolve_events(rule, user)
    return rule, created


def resolve_events(rule, user):
    identifier_field = EVENT_IDENTIFIER_FIELDS[rule.rule_type]
    events = Event.objects.filter(resolved_at__isnull=True, **{identifier_field: rule.identifier})
    if not rule.is_global:
        events = events.filter(Q(group__in=rule.groups.all()) | Q(machine__in=rule.machines.all()))
    return events.update(resolved_at=timezone.now(), resolved_by=user, resolution_rule=rule)


def set_rules_enabled(rules, enabled):
    """Enable or disable the rules. Returns (count, the release versions whose automatic approval was cancelled)."""
    cancelled = []
    if not enabled:
        # a release waiting for its auto approve delay must not enable them again
        cancelled = list(ReleaseVersion.objects.filter(rules__in=rules, auto_enable_pending=True).distinct())
        for release_version in cancelled:
            release_version.auto_enable_pending = False
            release_version.save(update_fields=["auto_enable_pending"])
    return rules.update(is_enabled=enabled), cancelled


def binary_identifiers(rule_type, info):
    """The identifiers of an inspected Mach-O file for a rule type (CDHash: one per architecture)"""
    return [identifier for identifier in {
        RuleType.BINARY: [info.sha256],
        RuleType.CERTIFICATE: [info.cert_sha256],
        RuleType.TEAMID: [info.team_id],
        RuleType.SIGNINGID: [info.signing_id],
        RuleType.CDHASH: info.cdhashes,
    }[rule_type] if identifier]


def can_see_sync_token(user):
    """The sync token (in the SyncBaseURL and the group profile) is the credential of the sync API: only for the
    users who may change the groups, never for read-only viewers."""
    return user.has_perm("santa.change_group")


def machines_for_user(user):
    """The Macs of a signed-in user.

    Santa reports MachineOwner as primary_user: the username (e.g. the UPN) with SANTA_PROFILE_MACHINE_OWNER,
    else the local account name.
    """
    username = user.get_username().strip()
    if not username:
        return Machine.objects.none()
    local_part = username.split("@", 1)[0]
    return Machine.objects.filter(Q(primary_user__iexact=username) | Q(primary_user__iexact=local_part))
