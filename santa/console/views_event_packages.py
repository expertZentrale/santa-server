"""Blocked events → package rules: the package of each binary is suggested (package_detection.py), the administrator
can change every suggestion, then the packages go into new or existing package rules, which are checked right away.
At the end each binary shows whether a rule allows it now."""
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import Http404
from django.shortcuts import get_object_or_404, render
from django.utils.translation import gettext
from django.views.decorators.http import require_POST

from .. import catalog
from ..models import Event, ReleaseSource
from ..package_detection import (
    SOURCE_APP_NAME,
    SOURCE_BUNDLE_ID,
    SOURCE_PATH,
    SOURCE_RELEASE,
    Suggestion,
    full_path,
    suggest_packages,
)
from ..progress import AlreadyRunning, progress_for, run_source_check
from ..releases import ReleaseError, sync_release_source
from ..services import (
    add_packages_to_sources,
    chunked,
    decisions_on,
    existing_rules,
    packages_to_check,
    resolve_allowed_events,
)
from .forms import EventPackagesForm
from .utils import log_addition, log_change, render_drawer, require_perms, staff_required
from .views_events import events_by_ids, selected_events
from .views_sources import check_summary


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


def _verify(events, user):
    """After the check of the package rules: [(event, allowed, matches, partly)] per binary. As Santa decides on each
    Mac (decisions_on: scope, policy, rule type), not just "an allow rule exists": a block of a group still wins over
    a global allow. The events whose own Mac allows the binary now are resolved (resolve_allowed_events)."""
    loaded = [event for chunk in chunked([event.pk for event in events])
              for event in Event.objects.select_related("machine").filter(pk__in=chunk)]
    resolve_allowed_events(loaded, user)
    decisions = decisions_on(loaded)
    by_sha = {}
    for event in loaded:
        by_sha.setdefault(event.file_sha256, []).append(event)
    matches = existing_rules(loaded)
    rows = []
    for sha, binary_events in by_sha.items():
        allowed = [decisions[event.pk] for event in binary_events]
        rows.append((binary_events[0], all(allowed), matches.get(sha, []), any(allowed) and not all(allowed)))
    return rows


def _steps(new_sources, appended, sources_of):
    """[(source, identifier)]: each package checked by a request of its own (events_packages_check), one after the
    other: a long check, a worker restarted or a proxy that gives up stops only its own package"""
    return [(source, identifier) for source, only in packages_to_check(new_sources, appended, sources_of).values()
            for identifier in (source.identifiers if only is None else only)]


@staff_required
def events_packages(request):
    require_perms(request, "view_event", "add_releasesource", "change_releasesource", "add_rule")
    # from the start: the catalogs are asked before the first step, the page must see the request alive
    with progress_for(request):
        return _events_packages(request)


def _events_packages(request):
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
            # only the binaries of the packages chosen: the others were left out on purpose
            chosen = {event.pk for package, _target in approved
                      for event in (package.suggestion.events if package.suggestion else [])}
            context.update({"steps": _steps(new_sources, appended, sources_of), "chosen": sorted(chosen),
                            "saved_sources": list({s.pk: s for s in sources_of.values()}.values())})
            return render_drawer(request, "console/events/packages.html", "console/events/packages_drawer.html",
                                 context)
    context["rows"] = [(*row, _source_label(row[0].suggestion)) for row in form.rows()]
    return render_drawer(request, "console/events/packages.html", "console/events/packages_drawer.html", context)


@staff_required
@require_POST
def events_packages_check(request):
    """One step of events_packages: the check of one package of a package rule, its result as a line of the list"""
    require_perms(request, "view_event", "add_releasesource", "change_releasesource", "add_rule")
    source = get_object_or_404(ReleaseSource, pk=request.POST.get("source"))
    identifier = request.POST.get("identifier", "")
    if identifier not in source.identifiers:
        raise Http404
    status, text = "ok", ""
    with progress_for(request) as progress:
        try:
            with run_source_check(source, request.user, progress) as running:
                new_versions = sync_release_source(source, progress=running, only=[identifier])
        except AlreadyRunning:
            status = "wait"
            text = gettext("The package rule is being checked right now: its check takes this package too.")
        except ReleaseError as e:
            status, text = "bad", str(e)
        else:
            text = check_summary(source, new_versions) if new_versions else gettext("Up to date.")
    return render(request, "console/events/_package_step.html",
                  {"source": source, "identifier": identifier, "status": status, "text": text})


@staff_required
@require_POST
def events_packages_verify(request):
    """The last step of events_packages: each binary of the events, allowed now or not; the ones allowed resolved"""
    require_perms(request, "view_event", "add_releasesource", "change_releasesource", "add_rule")
    events = events_by_ids(request.POST.getlist("ids"))
    results = _verify(events, request.user)
    # also the ones the check resolved itself (its rules enabled right away)
    resolved = sum(Event.objects.filter(pk__in=chunk, resolved_at__isnull=False).count()
                   for chunk in chunked([event.pk for event in events]))
    sources = ReleaseSource.objects.filter(pk__in=[pk for pk in request.POST.getlist("sources") if pk.isdigit()])
    allowed_count = sum(1 for _event, allowed, _matches, _partly in results if allowed)
    return render(request, "console/events/_package_results.html", {
        "results": results, "events": events, "resolved_count": resolved, "event_count": len(events),
        "allowed_count": allowed_count, "saved_sources": list(sources),
        "in_drawer": request.POST.get("in_drawer") == "1"})
