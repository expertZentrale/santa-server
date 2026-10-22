"""Blocked apps the console no longer lists (IgnoredApp): by signing ID, Team ID, SHA-256 or a path below which they
are. Only the lists of the console: the rules, the sync and the request form of the users stay as they are."""
from functools import reduce
from operator import or_

from django.db.models import Exists, F, OuterRef, Q
from django.db.models.lookups import StartsWith
from django.utils import timezone

from .models import IgnoredApp, unexpired

FIELDS = {IgnoredApp.Kind.SHA256: "file_sha256", IgnoredApp.Kind.SIGNINGID: "signing_id",
          IgnoredApp.Kind.TEAMID: "team_id"}


def active_ignores(now=None):
    # created_by: the lists show who ignored it
    return IgnoredApp.objects.filter(unexpired(now or timezone.now())).select_related("created_by")


def ignored_q(now=None):
    """The events an ignored app takes, or None when nothing is ignored.

    Subqueries (EXISTS) against the table, not the values in the query: SQL Server takes at most 2100 parameters,
    however many apps are ignored. An empty value takes nothing (an unsigned binary has no Team ID: "no Team ID" is
    not a Team ID)."""
    active = active_ignores(now).exclude(value="")
    if not active.exists():
        return None
    conditions = [Exists(active.filter(kind=kind, value=OuterRef(field))) for kind, field in FIELDS.items()]
    # the path of the event starts with the value: LIKE value + '%', its wildcards escaped
    conditions.append(Exists(active.filter(StartsWith(OuterRef("file_path"), F("value")), kind=IgnoredApp.Kind.PATH)))
    return reduce(or_, (Q(condition) for condition in conditions))


def without_ignored(events, now=None):
    condition = ignored_q(now)
    return events if condition is None else events.exclude(condition)


def only_ignored(events, now=None):
    condition = ignored_q(now)
    return events.none() if condition is None else events.filter(condition)


def matching(event, ignores):
    """The ignores of a list (active_ignores) that take the event: for many events, one query"""
    values = {IgnoredApp.Kind.SHA256: event.file_sha256, IgnoredApp.Kind.SIGNINGID: event.signing_id,
              IgnoredApp.Kind.TEAMID: event.team_id}
    return [ignore for ignore in ignores if ignore.value and (
        ignore.value == values.get(ignore.kind)
        or (ignore.kind == IgnoredApp.Kind.PATH and event.file_path.startswith(ignore.value)))]


def ignores_of(event, now=None):
    """The active ignores that take the event (one is enough to hide it)"""
    return matching(event, list(active_ignores(now)))
