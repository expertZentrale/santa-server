from django.db.models import Q
from django.utils import timezone

from .models import (
    AccessRequest,
    AccessRequestPackage,
    Event,
    Machine,
    Policy,
    ReleaseSource,
    ReleaseVersion,
    Rule,
    RuleType,
)

EVENT_IDENTIFIER_FIELDS = {
    RuleType.BINARY: "file_sha256",
    RuleType.CERTIFICATE: "cert_sha256",
    RuleType.TEAMID: "team_id",
    RuleType.SIGNINGID: "signing_id",
    RuleType.CDHASH: "cdhash",
}


def allow_identifier(rule_type, identifier, policy, is_global, groups, user, description="", tags=(), machines=(),
                     cel_expr=""):
    """Create the rule (of any policy, allow or block), or widen the scope of the manual rule that already exists
    for this identifier and policy (and, for CEL, the same expression).

    Returns (rule, created).
    """
    candidates = (Rule.objects.filter(rule_type=rule_type, identifier=identifier, policy=policy,
                                      release_source__isnull=True)
                              .order_by("pk"))
    # cel_expr is a TextField: compared here, not in the query
    rule = next((rule for rule in candidates if policy != Policy.CEL or rule.cel_expr == cel_expr), None)
    created = rule is None
    if created:
        rule = Rule(rule_type=rule_type, identifier=identifier, policy=policy, created_by=user,
                    description=description[:500], cel_expr=cel_expr if policy == Policy.CEL else "")
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


# the most specific rule type wins on the Mac
RULE_TYPE_PRECEDENCE = [RuleType.CDHASH, RuleType.BINARY, RuleType.SIGNINGID, RuleType.CERTIFICATE, RuleType.TEAMID]
ALLOW_POLICIES = (Policy.ALLOWLIST, Policy.ALLOWLIST_COMPILER)


def existing_rules(events):
    """The rules that already match the binaries of the events, by SHA-256: [{"rule", "applies"}]

    applies: the rule is enabled and reaches at least one Mac of the events (global, its group or the Mac).
    """
    events_of = {}
    for event in events:
        events_of.setdefault(event.file_sha256, []).append(event)
    found = {sha: [] for sha in events_of}
    for rule_type, field in EVENT_IDENTIFIER_FIELDS.items():
        binaries_of = {}
        for sha, binary_events in events_of.items():
            for event in binary_events:
                if getattr(event, field):
                    binaries_of.setdefault(getattr(event, field), set()).add(sha)
        identifiers = list(binaries_of)
        # SQL Server: at most 2100 parameters per query
        for start in range(0, len(identifiers), 1000):
            rules = (Rule.objects.filter(rule_type=rule_type, identifier__in=identifiers[start:start + 1000])
                                 .prefetch_related("groups", "machines"))
            for rule in rules:
                for sha in binaries_of[rule.identifier]:
                    found[sha].append(rule)
    result = {}
    for sha, rules in found.items():
        machines = {event.machine_id for event in events_of[sha]}
        groups = {event.machine.group_id for event in events_of[sha]}
        result[sha] = [{"rule": rule, "applies": rule.is_enabled and (
            rule.is_global or any(group.pk in groups for group in rule.groups.all())
            or any(machine.pk in machines for machine in rule.machines.all()))}
            for rule in sorted(rules, key=lambda rule: RULE_TYPE_PRECEDENCE.index(rule.rule_type))]
    return result


def is_allowed(matches):
    """An allow rule of existing_rules() reaches the Macs of the binary"""
    return any(match["applies"] and match["rule"].policy in ALLOW_POLICIES for match in matches)


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


def set_rules_policy(rules, policy):
    """Set the policy of the manual rules; the rules of package rules follow their package rule.

    Returns (the changed rules, the number of skipped package rules).
    """
    manual = [rule for rule in rules if rule.release_source_id is None]
    for rule in manual:
        rule.policy = policy
        # an expression only belongs to a CEL rule
        rule.cel_expr = ""
        rule.save(update_fields=["policy", "cel_expr", "updated_at"])
    return manual, len(rules) - len(manual)


def rules_only_for(machine, rules):
    """The manual rules that have no scope left without this Mac (no global, no group, no other Mac)"""
    return [rule for rule in rules
            if rule.release_source_id is None and not rule.is_global and not rule.groups.exists()
            and not rule.machines.exclude(pk=machine.pk).exists()]


def remove_machine_from_rules(machine, rules):
    """Take the Mac out of the scope of the rules; their global and group scopes stay as they are"""
    machine.rules.remove(*rules)


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


REQUEST_PERMISSIONS = {
    AccessRequest.Kind.EVENT: "santa.request_event",
    AccessRequest.Kind.PACKAGE: "santa.request_package",
    AccessRequest.Kind.OTHER: "santa.request_other",
}


def request_kinds_for(user):
    """The kinds of requests the user may make (roles, e.g. "Santa requesters" of the sign-in group for everyone)"""
    return [kind for kind in AccessRequest.Kind if user.has_perm(REQUEST_PERMISSIONS[kind])]


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


def chunked(values, size=1000):
    """SQL Server: at most 2100 parameters per query"""
    values = list(values)
    for start in range(0, len(values), size):
        yield values[start:start + size]


def allowed_packages(user, packages):
    """The (kind, identifier) of the packages that an enabled allow package rule already covers on the Macs of the user

    Package rules are global or for groups, so the groups of the user's Macs decide.
    """
    wanted = {(package["kind"], package["identifier"]) for package in packages}
    if not wanted:
        return set()
    groups = machines_for_user(user).values_list("group_id", flat=True)
    # identifier is a multi-line TextField: split it here instead of filtering on it
    sources = (ReleaseSource.objects.filter(kind__in={kind for kind, _ in wanted}, is_enabled=True,
                                            policy__in=ALLOW_POLICIES)
                                    .filter(Q(is_global=True) | Q(groups__in=groups))
                                    .values_list("kind", "identifier"))
    return {(kind, line.strip()) for kind, identifier in sources for line in identifier.splitlines()} & wanted


def latest_package_requests(packages, **filters):
    """The newest request of every (kind, identifier), of anyone, not cancelled: {(kind, identifier): package}"""
    wanted = {(package["kind"], package["identifier"]) for package in packages}
    kinds = {kind for kind, _ in wanted}
    latest = {}
    for chunk in chunked({identifier for _, identifier in wanted}):
        # kind and identifier: the index of AccessRequestPackage
        found = (AccessRequestPackage.objects.select_related("access_request")
                                             .filter(kind__in=kinds, identifier__in=chunk, **filters)
                                             .exclude(access_request__status=AccessRequest.Status.CANCELLED)
                                             .order_by("-access_request__created_at"))
        for package in found:
            if (package.kind, package.identifier) in wanted:
                latest.setdefault((package.kind, package.identifier), package)
    return latest


def latest_binary_requests(sha256s, exclude_user=None):
    """The newest event request of every binary, cancelled ones left out: {sha256: request}"""
    latest = {}
    for chunk in chunked(set(sha256s)):
        found = (AccessRequest.objects.filter(kind=AccessRequest.Kind.EVENT, file_sha256__in=chunk)
                                      .exclude(status=AccessRequest.Status.CANCELLED).order_by("-created_at"))
        if exclude_user is not None:
            found = found.exclude(requester=exclude_user)
        for access_request in found:
            latest.setdefault(access_request.file_sha256, access_request)
    return latest
