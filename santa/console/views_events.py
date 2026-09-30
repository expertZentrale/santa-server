import json
from datetime import timedelta

from django.contrib import messages
from django.db.models import Count, Max, Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext, ngettext
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_POST

from ..models import AccessRequest, Event, Group, RuleType
from ..services import allow_identifier, existing_rules, is_allowed
from .forms import SCOPE_GLOBAL, SCOPE_GROUPS, SCOPE_MACHINES, EventRuleForm
from .utils import (
    drawer_done,
    is_htmx,
    log_addition,
    log_change,
    paginate,
    render_drawer,
    require_perms,
    safe_next,
    sort_by,
    staff_required,
)

UPDATE_ROWS_LIMIT = 100
EVENT_COLUMNS = {"time": "execution_time", "decision": "decision", "file": "file_name",
                 "mac": "machine__hostname", "user": "executing_user", "signing": "signing_id"}
APP_COLUMNS = {"program": "file_name", "blocks": "event_count", "macs": "machine_count", "users": "user_count",
               "last": "last_seen"}
DAYS_CHOICES = [("1", _("24 hours")), ("7", _("7 days")), ("30", _("30 days")), ("", _("All time"))]


def chunked(values, size=1000):
    values = list(values)
    for start in range(0, len(values), size):
        yield values[start:start + size]


def events_by_ids(ids):
    """The events of a list of ids, in chunks: SQL Server takes at most 2100 parameters per query"""
    pks = [int(pk) for pk in ids if str(pk).isdigit()]
    events = []
    for chunk in chunked(pks):
        events += Event.objects.select_related("machine", "group").filter(pk__in=chunk)
    return sorted(events, key=lambda e: e.execution_time, reverse=True)


def selected_events(request):
    """The events chosen in a list: by id ("All events"), or all open blocks of a binary ("Blocked apps")"""
    events = {event.pk: event for event in events_by_ids(request.POST.getlist("ids"))}
    shas = [sha.lower() for sha in request.POST.getlist("shas") if len(sha) == 64]
    blocks = Event.objects.select_related("machine", "group").filter(decision__startswith="BLOCK_")
    # the same events as the list: its "resolved" filter
    resolved = request.POST.get("resolved", "open")
    if resolved == "open":
        blocks = blocks.filter(resolved_at__isnull=True)
    elif resolved == "resolved":
        blocks = blocks.filter(resolved_at__isnull=False)
    for chunk in chunked(shas):
        for event in blocks.filter(file_sha256__in=chunk):
            events[event.pk] = event
    return sorted(events.values(), key=lambda e: e.execution_time, reverse=True)


def available_rule_types(event):
    return [rule_type for rule_type in RuleType.values if event.identifier_for(rule_type)]


def show_rule_preview(field, event):
    """The identifier of the binary for every rule type, console.js shows the one of the chosen type"""
    field.widget.attrs.update({
        "data-identifiers": json.dumps({rule_type: event.identifier_for(rule_type)
                                        for rule_type in available_rule_types(event)}),
        "data-preview-label": gettext("The rule uses"),
    })


def default_rule_type(event):
    return RuleType.SIGNINGID if event.signing_id else RuleType.BINARY


def filter_events(request, events):
    params = request.GET
    q = params.get("q", "").strip()
    if q:
        events = events.filter(
            Q(file_name__icontains=q) | Q(file_path__icontains=q) | Q(signing_id__icontains=q)
            | Q(team_id__iexact=q) | Q(file_sha256__iexact=q.lower()) | Q(bundle_id__icontains=q)
            | Q(machine__hostname__icontains=q) | Q(machine__serial_number__iexact=q)
            | Q(executing_user__iexact=q)
        )
    if params.get("group", "").isdigit():
        events = events.filter(group_id=params["group"])
    if params.get("decision"):
        events = events.filter(decision=params["decision"])
    if params.get("machine", "").isdigit():
        events = events.filter(machine_id=params["machine"])
    if params.get("user"):
        events = events.filter(executing_user=params["user"])
    if params.get("sha256"):
        events = events.filter(file_sha256=params["sha256"].lower())
    days = params.get("days", "7")
    if days.isdigit():
        events = events.filter(execution_time__gte=timezone.now() - timedelta(days=int(days)))
    resolved = params.get("resolved", "open")
    if resolved == "open":
        events = events.filter(resolved_at__isnull=True)
    elif resolved == "resolved":
        events = events.filter(resolved_at__isnull=False)
    return events


def filter_context(request):
    return {
        "groups": Group.objects.order_by("name"),
        "decisions": (Event.objects.order_by("decision").values_list("decision", flat=True).distinct()),
        "days_choices": DAYS_CHOICES,
        "params": request.GET,
    }


@staff_required
def events(request):
    require_perms(request, "view_event")
    view = request.GET.get("view", "blocked")
    events = filter_events(request, Event.objects.all())
    context = {**filter_context(request), "view": view, "max_pk": Event.objects.aggregate(m=Max("pk"))["m"] or 0}
    if view == "all":
        queryset, context["sort"] = sort_by(request, events.select_related("machine", "group"), EVENT_COLUMNS,
                                            "-time")
        page = context["page"] = paginate(request, queryset)
    else:
        binaries, context["sort"] = sort_by(request, blocked_apps(events), APP_COLUMNS, "-last",
                                            tiebreak="file_sha256")
        page = paginate(request, binaries)
        annotate_app_rows(page)
        context["page"] = page
    # new rows are added on top by console.js, in the default order on the first page
    context["live_insert"] = page.number == 1 and context["sort"] == ("-time" if view == "all" else "-last")
    return render(request, "console/events/list.html", context)


def blocked_apps(events):
    """One row per blocked binary: a GROUP BY, fine on SQL Server"""
    return (events.filter(decision__startswith="BLOCK_")
                  .values("file_sha256")
                  .annotate(file_name=Max("file_name"), file_path=Max("file_path"),
                            signing_id=Max("signing_id"), team_id=Max("team_id"),
                            event_count=Count("id"), machine_count=Count("machine", distinct=True),
                            user_count=Count("executing_user", distinct=True),
                            last_seen=Max("execution_time"), last_pk=Max("id")))


def annotate_app_rows(rows):
    """The pending requests and the rules that already match (from the newest event of each binary)"""
    shas = [row["file_sha256"] for row in rows]
    pending = set()
    for chunk in chunked(shas):
        pending.update(AccessRequest.objects.filter(status=AccessRequest.Status.PENDING, file_sha256__in=chunk)
                                            .values_list("file_sha256", flat=True))
    matches = existing_rules(events_by_ids([row["last_pk"] for row in rows]))
    for row in rows:
        row["has_request"] = row["file_sha256"] in pending
        row["rules"] = matches.get(row["file_sha256"], [])


@staff_required
def event_updates(request):
    """The rows of the list that changed since ?after=<pk>: new events, or the blocked apps with new blocks.

    console.js adds them to the list without a reload, so the selection and an open drawer stay.
    """
    require_perms(request, "view_event")
    after = request.GET.get("after", "0")
    after = int(after) if after.isdigit() else 0
    max_pk = Event.objects.aggregate(m=Max("pk"))["m"] or 0
    if max_pk <= after:
        return HttpResponse(status=204)
    new = filter_events(request, Event.objects.filter(pk__gt=after, pk__lte=max_pk))
    context = {"max_pk": max_pk, "view": request.GET.get("view", "blocked"), "params": request.GET}
    if context["view"] == "all":
        context["events"] = list(new.select_related("machine", "group").order_by("-pk")[:UPDATE_ROWS_LIMIT])
    else:
        shas = list(new.filter(decision__startswith="BLOCK_").values_list("file_sha256", flat=True).distinct())
        rows = []
        for chunk in chunked(shas):
            rows += blocked_apps(filter_events(request, Event.objects.filter(file_sha256__in=chunk)))
        rows.sort(key=lambda row: row["last_seen"], reverse=True)
        annotate_app_rows(rows)
        context["rows"] = rows
    return render(request, "console/events/_updates.html", context)


@staff_required
def event_detail(request, pk):
    require_perms(request, "view_event")
    event = get_object_or_404(Event.objects.select_related("machine", "group", "resolution_rule", "resolved_by"),
                              pk=pk)
    same_binary = (Event.objects.select_related("machine").filter(file_sha256=event.file_sha256)
                                .exclude(pk=event.pk).order_by("-execution_time")[:20])
    template = "console/events/_detail.html" if is_htmx(request) else "console/events/detail.html"
    matches = existing_rules([event]).get(event.file_sha256, [])
    return render(request, template, {"event": event, "same_binary": same_binary, "form": event_rule_form([[event]]),
                                      "requests": event.access_requests.select_related("requester"),
                                      "matches": matches})


def event_rule_form(rows, data=None, preview=True, initial=None, include=None):
    """The rule form for the binaries of some events (rows: the events grouped by binary)

    preview: the identifier under the rule type (one event; the list of binaries shows its own)
    initial / include: the values and the checked binaries to show (the drawer after a change of the selection)
    """
    events = [event for row in rows for event in row]
    types = [rule_type for rule_type in RuleType.values if any(rule_type in available_rule_types(row[0])
                                                                 for row in rows)]
    # groups: "this Mac" is for tests and exceptions
    defaults = {"groups": sorted({event.group_id for event in events}), "scope": SCOPE_GROUPS,
                "include": include if include is not None else [row[0].file_sha256 for row in rows]}
    if len(rows) == 1:
        defaults["rule_type"] = default_rule_type(rows[0][0])
    form = EventRuleForm(data, rows=rows, initial={**defaults, **(initial or {})}, available_types=types)
    if len(rows) == 1 and preview:
        show_rule_preview(form.fields["rule_type"], rows[0][0])
    return form


def rule_type_for(event, rule_type):
    """The chosen rule type, or the suggestion when the binary has no identifier of that type"""
    return rule_type if rule_type in available_rule_types(event) else default_rule_type(event)


def rule_from_event(user, event, rule_type, policy, scope, groups, tags, description, machines=None):
    """Create (or widen) the rule of any policy for the binary of an event.

    Returns (rule, created), or None without identifier.
    """
    identifier = event.identifier_for(rule_type)
    if not identifier:
        return None
    machines = machines if machines is not None else [event.machine]
    rule, created = allow_identifier(
        rule_type, identifier, policy, scope == SCOPE_GLOBAL, groups if scope == SCOPE_GROUPS else [], user,
        description or f"{event.file_name} ({event.file_path})", tags,
        machines=machines if scope == SCOPE_MACHINES else [],
    )
    if created:
        log_addition(user, rule, f"Created from event {event.pk}")
    else:
        log_change(user, rule, f"Scope extended from event {event.pk}")
    return rule, created


def save_event_rules(user, form):
    """The rules of the included binaries. Returns (saved, created)."""
    data = form.cleaned_data
    saved = created = 0
    for row in form.included_rows():
        result = rule_from_event(user, row[0], rule_type_for(row[0], data["rule_type"]), data["policy"],
                                 data["scope"], data["groups"], form.all_tags(), data["description"],
                                 machines=list({event.machine for event in row}))
        if result:
            saved += 1
            created += result[1]
    return saved, created


@staff_required
@require_POST
def event_create_rule(request, pk):
    require_perms(request, "view_event", "add_rule", "change_rule")
    event = get_object_or_404(Event.objects.select_related("machine", "group"), pk=pk)
    form = event_rule_form([[event]], request.POST)
    if not form.is_valid():
        messages.error(request, "; ".join(f"{k}: {' '.join(v)}" for k, v in form.errors.items()))
    else:
        data = form.cleaned_data
        rule, created = rule_from_event(request.user, event, data["rule_type"], data["policy"], data["scope"],
                                        data["groups"], form.all_tags(), data["description"])
        message = gettext("Rule created: %(rule)s.") if created else gettext("Rule updated: %(rule)s.")
        messages.success(request, (message % {"rule": rule}) + " " + gettext("The Macs get it at their next sync."))
    return redirect(safe_next(request, "console:events"))


def _rows_for(events):
    """The selected events grouped by binary, newest first"""
    rows = {}
    for event in events:
        rows.setdefault(event.file_sha256, []).append(event)
    return list(rows.values())


def _binaries(rows, matches):
    """The binaries of the form with what their rule would use for every rule type (console.js shows the chosen one)"""
    return [{
        "matches": matches.get(row[0].file_sha256, []),
        "event": row[0], "events": len(row), "macs": len({event.machine_id for event in row}),
        "identifiers": json.dumps({rule_type: row[0].identifier_for(rule_type)
                                   for rule_type in available_rule_types(row[0])}),
        "suggested": default_rule_type(row[0]),
    } for row in rows]


# the fields of the rule form kept when the selection changes while the drawer is open
KEPT_FIELDS = ("rule_type", "policy", "scope", "new_tags", "description")
KEPT_LISTS = ("groups", "tags")


@staff_required
@require_POST
def events_create_rules(request):
    """One rule decision for the binaries of the selected events, in the drawer.

    The binaries left out stay in the drawer afterwards, for a second decision (e.g. block the rest). A change of
    the selection in the list while the drawer is open posts ?refresh: the drawer shows the new binaries and keeps
    what was entered, without saving.
    """
    require_perms(request, "view_event", "add_rule", "change_rule")
    events = selected_events(request)
    refresh = "refresh" in request.POST
    if not events and not refresh:
        messages.warning(request, gettext("Select at least one event."))
        return redirect("console:events")
    rows = _rows_for(events)
    matches = existing_rules(events)
    # a binary an allow rule already reaches starts unchecked
    wanted = [row[0].file_sha256 for row in rows if not is_allowed(matches.get(row[0].file_sha256, []))]
    saved_before = request.POST.get("saved") == "1"
    saved_shas = []
    if refresh:
        known = set(request.POST.getlist("known"))
        checked = set(request.POST.getlist("include"))
        include = [sha for sha in (row[0].file_sha256 for row in rows)
                   if sha in checked or (sha not in known and sha in wanted)]
        initial = {name: request.POST[name] for name in KEPT_FIELDS if name in request.POST}
        initial.update({name: request.POST.getlist(name) for name in KEPT_LISTS if name in request.POST})
        form = event_rule_form(rows, preview=False, initial=initial, include=include)
    else:
        form = event_rule_form(rows, request.POST if "apply" in request.POST else None, preview=False,
                               include=wanted)
    if form.is_bound and form.is_valid():
        saved, created = save_event_rules(request.user, form)
        messages.success(request, ngettext(
            "%(count)s rule saved (%(new)s new). The Macs get it at their next sync.",
            "%(count)s rules saved (%(new)s new). The Macs get them at their next sync.", saved,
        ) % {"count": saved, "new": created})
        saved_shas = [row[0].file_sha256 for row in form.included_rows()]
        rest = [row for row in rows if row[0].file_sha256 not in saved_shas]
        if not rest:
            return drawer_done(request, reverse("console:events"))
        rows, saved_before = rest, True
        events = [event for row in rest for event in row]
        matches = existing_rules(events)
        form = event_rule_form(rest, preview=False, include=[row[0].file_sha256 for row in rest
                                                             if not is_allowed(matches.get(row[0].file_sha256, []))])
        messages.info(request, ngettext("%(count)s binary left: choose its rule, or close.",
                                        "%(count)s binaries left: choose their rule, or close.", len(rest))
                      % {"count": len(rest)})
    return render_drawer(request, "console/events/create_rules.html", "console/events/create_rules_drawer.html", {
        "form": form, "binaries": _binaries(rows, matches), "events": events,
        "rule_types": json.dumps({value: str(label) for value, label in RuleType.choices}), "saved": saved_before,
        "saved_shas": " ".join(saved_shas), "resolved": request.POST.get("resolved", "open"),
    })


@staff_required
@require_POST
def events_resolve(request):
    require_perms(request, "view_event", "change_event")
    count = 0
    for chunk in chunked(event.pk for event in selected_events(request) if event.resolved_at is None):
        count += Event.objects.filter(pk__in=chunk, resolved_at__isnull=True).update(
            resolved_at=timezone.now(), resolved_by=request.user)
    messages.success(request, ngettext("%(count)s event marked as resolved.", "%(count)s events marked as resolved.",
                                       count) % {"count": count})
    return redirect(safe_next(request, "console:events"))
