"""Blocked events → package rules: the package of each binary is suggested (package_detection.py), the administrator
can change every suggestion, then the packages go into new or existing package rules, which are checked right away.
At the end each binary shows whether a rule allows it now."""
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext, ngettext

from .. import catalog
from ..models import Event
from ..package_detection import (
    SOURCE_APP_NAME,
    SOURCE_BUNDLE_ID,
    SOURCE_PATH,
    SOURCE_RELEASE,
    Suggestion,
    full_path,
    suggest_packages,
)
from ..progress import progress_for
from ..releases import sync_release_source
from ..services import add_packages_to_sources, chunked, decisions_on, existing_rules, packages_to_check
from .forms import EventPackagesForm
from .utils import log_addition, log_change, render_drawer, require_perms, staff_required
from .views_events import events_by_ids, selected_events
from .views_sources import check_added_packages


def _source_label(suggestion):
    """Where a suggestion comes from, the most reliable first"""
    labels = {
        SOURCE_RELEASE: gettext("same file as in a recorded release"),
        SOURCE_BUNDLE_ID: gettext("bundle ID in the Homebrew cask"),
        SOURCE_APP_NAME: gettext("app name in the Homebrew cask"),
        SOURCE_PATH: gettext("install path"),
    }
    return labels.get(suggestion.source, "")


def _selection(request):
    if request.method == "POST":
        return selected_events(request)
    return events_by_ids(request.GET.getlist("ids"))


def _unmatched_suggestions(events):
    """One empty row per binary nobody recognised, to fill in by hand"""
    rows = {}
    for event in events:
        row = rows.setdefault(event.file_sha256, Suggestion("", "", ""))
        row.events.append(event)
        row.shas.add(event.file_sha256)
        row.example_path = row.example_path or full_path(event)
    return list(rows.values())


def _verify(events):
    """After the check of the package rules: ([(event, allowed, matches, partly)] per binary, the pks of the events
    whose own Mac allows it now). As Santa decides on each Mac (decisions_on: scope, policy, rule type), not just
    "an allow rule exists": a block of a group still wins over a global allow."""
    loaded = [event for chunk in chunked([event.pk for event in events])
              for event in Event.objects.select_related("machine").filter(pk__in=chunk)]
    decisions = decisions_on(loaded)
    by_sha = {}
    for event in loaded:
        by_sha.setdefault(event.file_sha256, []).append(event)
    matches = existing_rules(loaded)
    rows = []
    for sha, binary_events in by_sha.items():
        allowed = [decisions[event.pk] for event in binary_events]
        rows.append((binary_events[0], all(allowed), matches.get(sha, []), any(allowed) and not all(allowed)))
    return rows, {pk for pk, allowed in decisions.items() if allowed}


def _check_and_verify(request, progress, approved, form, new_sources, appended, sources_of):
    # only what was added: a package appended to a big package rule doesn't check the whole list
    for source, only in packages_to_check(new_sources, appended, sources_of).values():
        check_added_packages(request, source, progress, only, sync_release_source)
    # only the binaries of the packages chosen: the others were left out on purpose
    chosen = list({event.pk: event for package, _target in approved
                   for event in (package.suggestion.events if package.suggestion else [])}.values())
    results, covered = _verify(chosen)
    resolved = 0
    if form.cleaned_data.get("resolve") and covered:
        # the events of the Macs that allow it now, not every event of the binary
        pks = [event.pk for event in chosen if event.pk in covered and event.resolved_at is None]
        for chunk in chunked(pks):
            resolved += Event.objects.filter(pk__in=chunk, resolved_at__isnull=True).update(
                resolved_at=timezone.now(), resolved_by=request.user)
    allowed_count = sum(1 for _event, allowed, _matches, _partly in results if allowed)
    messages.success(request, ngettext(
        "%(count)s package in package rules: %(allowed)s of %(binaries)s binary allowed now.",
        "%(count)s packages in package rules: %(allowed)s of %(binaries)s binaries allowed now.",
        len(approved)) % {"count": len(approved), "allowed": allowed_count, "binaries": len(results)})
    return {"results": results, "saved_sources": list({s.pk: s for s in sources_of.values()}.values()),
            "resolved_count": resolved}


@staff_required
def events_packages(request):
    require_perms(request, "view_event", "add_releasesource", "change_releasesource", "add_rule")
    # from the start: the catalogs are asked before the first step, the page must see the request alive
    with progress_for(request) as progress:
        return _events_packages(request, progress)


def _events_packages(request, progress):
    events = _selection(request)
    applying = request.method == "POST" and "apply" in request.POST
    suggestions, unmatched = suggest_packages(events)
    suggestions += _unmatched_suggestions(unmatched)
    form = EventPackagesForm(request.POST if applying else None, suggestions=suggestions)
    context = {"events": events, "form": form, "resolved": request.POST.get("resolved", "open"), "tab": "packages",
               "searchable_kinds": list(catalog.SEARCHABLE_KINDS),
               "package_count": len([s for s in suggestions if s.identifier])}
    if applying and form.is_valid():
        approved = form.approved()
        try:
            with transaction.atomic():
                new_sources, appended, sources_of = add_packages_to_sources(
                    approved, form.new_rule_names(), form.cleaned_data, form.new_source_options)
                for source in new_sources.values():
                    log_addition(request.user, source, "Created from blocked events")
                for target, identifier in appended:
                    log_change(request.user, target, f"Identifier {identifier} added from blocked events")
        except ValidationError as e:
            # an identifier the catalog doesn't take (GitHub without owner/repo): nothing saved, the form again
            form.add_error(None, e.messages)
        else:
            context.update(_check_and_verify(request, progress, approved, form, new_sources, appended, sources_of))
    context["rows"] = [(*row, _source_label(row[0].suggestion)) for row in form.rows()]
    return render_drawer(request, "console/events/packages.html", "console/events/packages_drawer.html", context)
