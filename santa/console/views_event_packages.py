"""Blocked events → package rules: the package of each binary is suggested (package_detection.py), the administrator
can change every suggestion, then the packages go into new or existing package rules, which are checked right away.
At the end each binary shows whether a rule allows it now."""
from django.contrib import messages
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
from ..releases import ReleaseError, sync_release_source
from ..services import add_packages_to_sources, chunked, existing_rules, is_allowed
from .forms import EventPackagesForm
from .utils import log_addition, log_change, render_drawer, require_perms, staff_required
from .views_events import events_by_ids, selected_events


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
    """[(binary name, path, allowed, matches)] per binary of the events, after the check of the package rules"""
    by_sha = {}
    for event in events:
        by_sha.setdefault(event.file_sha256, event)
    matches = existing_rules(list(by_sha.values()))
    return [(event, is_allowed(matches.get(sha, [])), matches.get(sha, [])) for sha, event in by_sha.items()]


@staff_required
def events_packages(request):
    require_perms(request, "view_event", "add_releasesource", "change_releasesource", "add_rule")
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
        with transaction.atomic():
            new_sources, appended, sources_of = add_packages_to_sources(
                approved, form.new_rule_names(), form.cleaned_data, form.new_source_options)
            for source in new_sources.values():
                log_addition(request.user, source, "Created from blocked events")
            for target, identifier in appended:
                log_change(request.user, target, f"Identifier {identifier} added from blocked events")
        with progress_for(request) as progress:
            for source in {source.pk: source for source in sources_of.values()}.values():
                try:
                    sync_release_source(source, progress=progress)
                except ReleaseError as e:
                    messages.warning(request, f"{source}: {e}")
        # only the binaries of the packages chosen: the others were left out on purpose
        chosen = list({event.pk: event for package, _target in approved
                       for event in (package.suggestion.events if package.suggestion else [])}.values())
        results = _verify(chosen)
        allowed_shas = {event.file_sha256 for event, allowed, _matches in results if allowed}
        resolved = 0
        if form.cleaned_data.get("resolve") and allowed_shas:
            pks = [event.pk for event in chosen if event.file_sha256 in allowed_shas and event.resolved_at is None]
            for chunk in chunked(pks):
                resolved += Event.objects.filter(pk__in=chunk, resolved_at__isnull=True).update(
                    resolved_at=timezone.now(), resolved_by=request.user)
        allowed_count = len(allowed_shas)
        messages.success(request, ngettext(
            "%(count)s package in package rules: %(allowed)s of %(binaries)s binary allowed now.",
            "%(count)s packages in package rules: %(allowed)s of %(binaries)s binaries allowed now.",
            len(approved)) % {"count": len(approved), "allowed": allowed_count, "binaries": len(results)})
        context.update({"results": results, "saved_sources": list({s.pk: s for s in sources_of.values()}.values()),
                        "resolved_count": resolved})
    context["rows"] = [(*row, _source_label(row[0].suggestion)) for row in form.rows()]
    return render_drawer(request, "console/events/packages.html", "console/events/packages_drawer.html", context)
