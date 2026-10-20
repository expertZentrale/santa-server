from dataclasses import dataclass, field

from django.db import transaction
from django.db.models import Count, IntegerField, OuterRef, Prefetch, Q, Subquery
from django.db.models.functions import Coalesce
from django.utils import timezone
from django.utils.translation import gettext

from .cel import (
    SIGNING_PREFIX_RE,
    merged_prefixes,
    prefix_decision,
    prefix_expression,
    rule_prefixes,
    signing_prefix,
    suggested_prefix,
)
from .models import (
    POLICY_PRECEDENCE,
    AccessRequest,
    AccessRequestPackage,
    Event,
    Group,
    Machine,
    Policy,
    ReleaseSource,
    ReleaseVersion,
    Rule,
    RuleType,
    with_parent_groups,
)
from .progress import Progress
from .rules import GLOBAL, GROUP, MACHINE, PARENT

EVENT_IDENTIFIER_FIELDS = {
    RuleType.BINARY: "file_sha256",
    RuleType.CERTIFICATE: "cert_sha256",
    RuleType.TEAMID: "team_id",
    RuleType.SIGNINGID: "signing_id",
    RuleType.CDHASH: "cdhash",
}


def related_aggregate(queryset, outer_field, aggregate):
    """An aggregate of related rows per object, as a subquery of its own. Several Count()s over joins multiply their
    rows first (a group with 20,000 rules and its Macs: seconds); a subquery each counts only its own.
    queryset: the related rows, outer_field: their field pointing at the object (e.g. "group")."""
    return Subquery(queryset.filter(**{outer_field: OuterRef("pk")}).order_by().values(outer_field)
                    .annotate(value=aggregate).values("value")[:1])


def related_count(queryset, outer_field):
    return Coalesce(related_aggregate(queryset, outer_field, Count("pk")), 0, output_field=IntegerField())


# The groups and Macs of list rows, with only what the lists show of them. A prefetch reads a group or a Mac once
# per row it belongs to, with every column: the branding logos of a group (hundreds of KB) or the rule ledger of
# a Mac, 25 times on a page of rules.
def groups_prefetch(lookup="groups"):
    return Prefetch(lookup, queryset=Group.objects.only("id", "name"))


def machines_prefetch(lookup="machines"):
    return Prefetch(lookup, queryset=Machine.objects.only("id", "serial_number", "hostname"))


def allow_identifier(rule_type, identifier, policy, is_global, groups, user, description="", tags=(), machines=(),
                     cel_expr="", expires_at=None):
    """Create the rule (of any policy, allow or block), or widen the scope of the manual rule that already exists
    for this identifier and policy (and, for CEL, the same expression). expires_at: see widen_rule.

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
    widen_rule(rule, is_global, groups, machines, user, tags, expires_at=expires_at)
    return rule, created


def widen_rule(rule, is_global, groups, machines, user, tags=(), expires_at=None):
    """Enable the rule and add the scope (all Macs, or the groups and Macs); the open events it covers now are
    resolved.

    expires_at: the end of this approval, None for good. A new rule gets it. A rule that already exists keeps the
    later end: one without an end stays so (it applies to others already), an approval for good takes its end away.
    """
    if not rule.pk:
        rule.expires_at = expires_at
    elif rule.expires_at is not None:
        rule.expires_at = None if expires_at is None else max(rule.expires_at, expires_at)
    rule.is_global = rule.is_global or is_global
    rule.is_enabled = True
    # only what it changes: a whole save would write back an expression another request changed meanwhile (a new
    # rule is saved as a whole)
    rule.save(update_fields=["is_global", "is_enabled", "expires_at", "updated_at"] if rule.pk else None)
    if not is_global:
        rule.groups.add(*groups)
        rule.machines.add(*machines)
    rule.tags.add(*tags)
    resolve_events(rule, user)


def resolve_events(rule, user):
    identifier_field = EVENT_IDENTIFIER_FIELDS[rule.rule_type]
    events = Event.objects.filter(resolved_at__isnull=True, **{identifier_field: rule.identifier})
    # a Team ID rule of signing ID prefixes allows only those binaries of the team, the others stay blocked
    if (prefixes := rule_prefixes(rule)) is not None:
        events = events.filter(any_of_q([Q(signing_id__startswith=f"{rule.identifier}:{prefix}")
                                         for prefix in prefixes]))
    if not rule.is_global:
        events = events.filter(Q(group__in=rule.groups.all()) | Q(machine__in=rule.machines.all()))
    return events.update(resolved_at=timezone.now(), resolved_by=user, resolution_rule=rule)


def any_of_q(conditions):
    combined = Q(pk__in=[])
    for condition in conditions:
        combined |= condition
    return combined


class SigningIdError(ValueError):
    """The signing ID can't be added to the rule (the message says why)"""


def add_signing_prefix(rule, signing_id, user, prefix=None):
    """Add the signing ID to a Team ID rule of signing ID prefixes ("only signing IDs starting with"); the open
    events it allows now are resolved. prefix: a shorter start of it to add instead (cel.suggested_prefix), the
    prefixes it covers go. Returns the prefix added (None: it was in the rule already)."""
    with transaction.atomic():
        # read, changed and saved under a lock: two additions at the same time (several binaries of a selection,
        # two administrators) don't overwrite each other
        locked = Rule.objects.select_for_update().get(pk=rule.pk)
        for name in ("policy", "rule_type", "identifier", "cel_expr", "release_source_id", "is_enabled"):
            setattr(rule, name, getattr(locked, name))
        prefixes = rule_prefixes(rule)
        if prefixes is None:
            raise SigningIdError(gettext("Only a Team ID rule with “Only signing IDs starting with” takes signing "
                                         "IDs."))
        if rule.release_source_id:
            raise SigningIdError(gettext("This rule comes from the package rule %(source)s: change it there.")
                                 % {"source": rule.release_source})
        own = signing_prefix(rule, signing_id)
        if own is None:
            raise SigningIdError(gettext("The signing ID %(signing_id)s doesn't belong to the team %(team_id)s.")
                                 % {"signing_id": signing_id or "–", "team_id": rule.identifier})
        prefix = (prefix or "").strip() or own
        if not own.startswith(prefix) or not SIGNING_PREFIX_RE.match(prefix):
            raise SigningIdError(gettext("The prefix %(prefix)s is not the start of the signing ID %(signing_id)s.")
                                 % {"prefix": prefix, "signing_id": signing_id})
        if prefix_decision(rule, signing_id) and prefix == own:
            return None
        rule.cel_expr = prefix_expression(rule.identifier, merged_prefixes(prefixes, prefix))
        rule.save(update_fields=["cel_expr", "updated_at"])
    # a disabled rule doesn't reach the Macs: its events stay open (enabling it, widen_rule resolves them)
    if rule.is_enabled:
        resolve_events(rule, user)
    return prefix


def resolve_binary_events(file_sha256, machines, user, rule=None):
    """Resolve the open events of a binary on these Macs (an approval with an existing rule, or without one)"""
    now = timezone.now()
    # all the Macs of a requester: SQL Server takes at most 2100 parameters
    return sum(Event.objects.filter(resolved_at__isnull=True, file_sha256=file_sha256, machine__in=chunk)
                            .update(resolved_at=now, resolved_by=user, resolution_rule=rule)
               for chunk in chunked([getattr(machine, "pk", machine) for machine in machines]))


# the most specific rule type wins on the Mac
RULE_TYPE_PRECEDENCE = [RuleType.CDHASH, RuleType.BINARY, RuleType.SIGNINGID, RuleType.CERTIFICATE, RuleType.TEAMID]
ALLOW_POLICIES = (Policy.ALLOWLIST, Policy.ALLOWLIST_COMPILER)


def _matching_rules(identifiers_of):
    """{key: [rules]} of the rules with an identifier of {key: {rule_type: [identifier, …]}}"""
    found = {key: [] for key in identifiers_of}
    for rule_type in EVENT_IDENTIFIER_FIELDS:
        keys_of = {}
        for key, identifiers in identifiers_of.items():
            for identifier in identifiers.get(rule_type, ()):
                keys_of.setdefault(identifier, set()).add(key)
        # SQL Server: at most 2100 parameters per query
        for chunk in chunked(keys_of):
            for rule in (Rule.objects.filter(rule_type=rule_type, identifier__in=chunk)
                                     .prefetch_related(groups_prefetch(), machines_prefetch())):
                for key in keys_of[rule.identifier]:
                    found[key].append(rule)
    return {key: sorted(rules, key=lambda rule: RULE_TYPE_PRECEDENCE.index(rule.rule_type))
            for key, rules in found.items()}


def existing_rules(events, machines=None):
    """The rules that already match the binaries of the events, by SHA-256: [{"rule", "applies", "cel"}]

    applies: the rule is enabled and reaches at least one Mac of the events (global, its group, the parent group
    it is based on, or the Mac). machines: these Macs instead of the ones of the events (the Macs of a requester,
    when the event is of another Mac). cel: what the expression of a Team ID rule with "only signing IDs starting
    with" does with the signing ID (True allows, False blocks; None: no such rule).
    """
    events_of = {}
    for event in events:
        events_of.setdefault(event.file_sha256, []).append(event)
    found = _matching_rules({sha: {rule_type: {getattr(event, field) for event in binary_events
                                               if getattr(event, field)}
                                   for rule_type, field in EVENT_IDENTIFIER_FIELDS.items()}
                             for sha, binary_events in events_of.items()})
    result = {}
    rule_groups = {}
    for sha, rules in found.items():
        if machines is None:
            machine_ids = {event.machine_id for event in events_of[sha]}
            group_ids = frozenset(event.machine.group_id for event in events_of[sha])
        else:
            machine_ids = {machine.pk for machine in machines}
            group_ids = frozenset(machine.group_id for machine in machines)
        if group_ids not in rule_groups:
            rule_groups[group_ids] = with_parent_groups(group_ids, "inherit_rules")
        groups = rule_groups[group_ids]
        signing_id = next((event.signing_id for event in events_of[sha] if event.signing_id), "")
        result[sha] = [{"rule": rule, "applies": rule.is_active and (
            rule.is_global or any(group.pk in groups for group in rule.groups.all())
            or any(machine.pk in machine_ids for machine in rule.machines.all())),
            "cel": prefix_decision(rule, signing_id),
            # the prefix "Add signing ID" proposes (cel.suggested_prefix), when the rule leaves it out
            "prefix": suggested_prefix(rule, signing_id) if prefix_decision(rule, signing_id) is False else None}
            for rule in rules]
    return result


def existing_rules_for_binaries(binaries):
    """The rules that already match uploaded binaries, by SHA-256: [{"rule", "applies"}]

    No Mac ran them yet: applies is the rule being enabled, its scope is shown with it.
    """
    found = _matching_rules({binary.sha256: {rule_type: binary_identifiers(rule_type, binary)
                                             for rule_type in EVENT_IDENTIFIER_FIELDS}
                             for binary in binaries})
    signing_ids = {binary.sha256: binary.signing_id for binary in binaries}
    return {sha: [{"rule": rule, "applies": rule.is_active, "cel": prefix_decision(rule, signing_ids[sha])}
                  for rule in rules] for sha, rules in found.items()}


def _event_identifiers(event):
    return {rule_type: {value} for rule_type, field in EVENT_IDENTIFIER_FIELDS.items()
            if (value := getattr(event, field))}


def effective_policy(rule, signing_id):
    """The policy of the rule for a binary: the one a Team ID rule with "only signing IDs starting with" gives its
    signing ID, as Santa evaluates it; another CEL expression stays CEL (not known to allow)"""
    decision = prefix_decision(rule, signing_id)
    if decision is None:
        return rule.policy
    return Policy.ALLOWLIST if decision else Policy.BLOCKLIST


def _allowed_on(machine, group, rules, signing_id=""):
    """What Santa does with the binary on the Mac, by the rules matching its identifiers: as the sync sends them
    (rules.effective_rule_objects: the most specific scope, then the strictest policy, per identifier), then the
    most specific rule type decides. Allowed only by an allow rule, or a CEL expression of signing ID prefixes that
    includes it; another CEL expression is not known to allow."""
    chosen = {}
    for rule in rules:
        if not rule.is_active:
            continue
        rule_groups = {rule_group.pk for rule_group in rule.groups.all()}
        levels = [level for level, applies in (
            (GLOBAL, rule.is_global),
            (PARENT, bool(group and group.parent_id and group.inherit_rules and group.parent_id in rule_groups)),
            (GROUP, bool(group and group.pk in rule_groups)),
            (MACHINE, any(rule_machine.pk == machine.pk for rule_machine in rule.machines.all()))) if applies]
        if not levels:
            continue
        rank = (max(levels), POLICY_PRECEDENCE[rule.policy], rule.pk)
        if rule.santa_key not in chosen or rank > chosen[rule.santa_key][0]:
            chosen[rule.santa_key] = (rank, rule)
    for rule_type in RULE_TYPE_PRECEDENCE:
        decisive = [rule for _rank, rule in chosen.values() if rule.rule_type == rule_type]
        if decisive:
            # several identifiers of one type (the CDHashes of the slices): the strictest
            policies = [effective_policy(rule, signing_id) for rule in decisive]
            return max(policies, key=POLICY_PRECEDENCE.__getitem__) in ALLOW_POLICIES
    return False


def decisions_on(events):
    """{event pk: allowed} of the binaries of the events on their own Macs (allowed_on); events with machine"""
    events = list(events)
    found = _matching_rules({event.pk: _event_identifiers(event) for event in events})
    groups = Group.objects.in_bulk({event.machine.group_id for event in events})
    return {event.pk: _allowed_on(event.machine, groups.get(event.machine.group_id), found[event.pk],
                                  event.signing_id)
            for event in events}


def decisions_for_machines(event, machines):
    """{machine pk: allowed} of the binary of the event on these Macs (the Macs of a requester)"""
    machines = list(machines)
    rules = _matching_rules({0: _event_identifiers(event)})[0]
    groups = Group.objects.in_bulk({machine.group_id for machine in machines})
    return {machine.pk: _allowed_on(machine, groups.get(machine.group_id), rules, event.signing_id)
            for machine in machines}


def is_allowed(matches):
    """An allow rule of existing_rules() reaches the Macs of the binary (also a CEL rule of signing ID prefixes that
    includes its signing ID)"""
    return any(match["applies"] and (match["rule"].policy in ALLOW_POLICIES or match.get("cel") is True)
               for match in matches)


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


@dataclass
class UploadedBinary:
    """An executable of an uploaded file, as the console keeps it between the check and the rules (signed in the
    form: the file itself is never stored)"""
    path: str
    sha256: str
    signing_id: str = ""
    team_id: str = ""
    cert_sha256: str = ""
    cert_cn: str = ""
    cdhashes: list = field(default_factory=list)
    archs: list = field(default_factory=list)
    signature: str = "unsigned"

    @classmethod
    def from_info(cls, path, info):
        signed = [s for s in info.slices if s.signed]
        signature = "unsigned" if not signed else "adhoc" if all(s.adhoc for s in signed) else "signed"
        return cls(path=path, sha256=info.sha256, signing_id=info.signing_id, team_id=info.team_id,
                   cert_sha256=info.cert_sha256, cert_cn=info.cert_cn, cdhashes=info.cdhashes,
                   archs=[s.arch for s in info.slices], signature=signature)

    # the names of an event: the rule forms of the events take these binaries too
    @property
    def file_sha256(self):
        return self.sha256

    @property
    def file_name(self):
        return self.path.rsplit("/", 1)[-1]

    machine_id = None

    @property
    def rule_types(self):
        return [rule_type for rule_type in RuleType.values if binary_identifiers(rule_type, self)]

    @property
    def suggested_rule_type(self):
        return RuleType.SIGNINGID if self.signing_id else RuleType.BINARY

    def rule_type_for(self, rule_type):
        """The chosen rule type, or the suggestion when the binary has no identifier of that type"""
        return rule_type if rule_type in self.rule_types else self.suggested_rule_type


def allow_binaries(choices, policy, is_global, groups, user, description, tags=(), cel_expr_for=None,
                   progress=None, expires_at=None):
    """The rules of uploaded binaries, choices: [(UploadedBinary, rule type)]. One rule per identifier (CDHash: per
    architecture); binaries with the same identifier (the helpers of an app with one Team ID) share it.

    description: a text, or a function of the binary. Returns [(rule, created)].
    """
    progress = progress or Progress()
    progress.add_total(len(choices))
    rules = {}
    for binary, rule_type in choices:
        progress.start(binary.path)
        for identifier in binary_identifiers(rule_type, binary):
            if (rule_type, identifier) not in rules:
                rules[(rule_type, identifier)] = allow_identifier(
                    rule_type, identifier, policy, is_global, groups, user,
                    description(binary) if callable(description) else description, tags,
                    cel_expr=cel_expr_for(binary) if cel_expr_for else "", expires_at=expires_at)
    return list(rules.values())


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

    Package rules are global or for groups, so the groups of the user's Macs (and the parents they take the rules
    of) decide.
    """
    wanted = {(package["kind"], package["identifier"]) for package in packages}
    if not wanted:
        return set()
    groups = with_parent_groups(machines_for_user(user).values_list("group_id", flat=True), "inherit_rules")
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


def packages_to_check(new_sources, appended, sources_of):
    """{source pk: (source, identifiers, or None for all)} to check after add_packages_to_sources(): the new package
    rules, the packages appended, and the ones a package rule had already but never checked"""
    from .releases import unchecked_identifiers

    to_check = {source.pk: (source, None) for source in new_sources.values()}
    for target, identifier in appended:
        to_check.setdefault(target.pk, (target, []))[1].append(identifier)
    for package, source in sources_of.items():
        if source.pk in to_check and to_check[source.pk][1] is None:
            continue
        if package.identifier in unchecked_identifiers(source):
            identifiers = to_check.setdefault(source.pk, (source, []))[1]
            if package.identifier not in identifiers:
                identifiers.append(package.identifier)
    return to_check


def add_packages_to_sources(approved, new_names, data, options_for):
    """Put packages into package rules: one new ReleaseSource per catalog (new_names {kind: name}) for the packages
    without a target, the others appended to their target unless it has them already.

    approved: [(package, target ReleaseSource or None)], a package has kind, identifier, name and icon_url.
    data: rule_type, is_global and groups of the new rules; options_for(kind): their other fields.
    Returns ({kind: new source}, [(target, identifier)] appended, {package: its source}). The caller logs them.
    """
    from .catalog import update_identifier_icons

    def picked(packages):
        return {p.identifier: {"name": p.name, "icon_url": p.icon_url} for p in packages if p.name or p.icon_url}

    new_sources = {}
    for kind, name in new_names.items():
        packages = [package for package, target in approved if target is None and package.kind == kind]
        source = ReleaseSource(name=name, kind=kind, identifier="\n".join(dict.fromkeys(p.identifier
                                                                                       for p in packages)),
                               rule_type=data.get("rule_type") or RuleType.BINARY, is_global=data["is_global"],
                               **options_for(kind))
        source.full_clean()
        update_identifier_icons(source, picked(packages))
        source.save()
        source.groups.set(data["groups"])
        new_sources[kind] = source
    appended = []
    sources_of = {}
    for package, target in approved:
        source = target or new_sources[package.kind]
        sources_of[package] = source
        if target is not None and package.identifier not in target.identifiers:
            target.refresh_from_db()
            target.identifier = "\n".join(target.identifiers + [package.identifier])
            target.full_clean()
            update_identifier_icons(target, picked([package]) or None)
            target.save()
            appended.append((target, package.identifier))
    return new_sources, appended, sources_of
