import json
from datetime import timedelta

from django.contrib import messages
from django.db.models import Count, Max, Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.translation import gettext, ngettext
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_POST

from ..models import AccessRequest, Event, Group, RuleType
from ..services import allow_identifier
from .forms import SCOPE_GLOBAL, SCOPE_MACHINES, AllowFormSet, AllowSharedForm, ApproveEventForm, rule_type_choices
from .utils import is_htmx, log_addition, log_change, paginate, require_perms, safe_next, sort_by, staff_required

LIVE_ROWS_LIMIT = 100
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
        page = paginate(request, queryset)
        context["page"] = page
        # new events are added on top: only in the default order
        context["live"] = page.number == 1 and context["sort"] == "-time"
    else:
        # one row per binary: a GROUP BY, fine on SQL Server
        binaries = (events.filter(decision__startswith="BLOCK_")
                          .values("file_sha256")
                          .annotate(file_name=Max("file_name"), file_path=Max("file_path"),
                                    signing_id=Max("signing_id"), team_id=Max("team_id"),
                                    event_count=Count("id"), machine_count=Count("machine", distinct=True),
                                    user_count=Count("executing_user", distinct=True),
                                    last_seen=Max("execution_time"), last_pk=Max("id")))
        binaries, context["sort"] = sort_by(request, binaries, APP_COLUMNS, "-last", tiebreak="file_sha256")
        page = paginate(request, binaries)
        shas = [row["file_sha256"] for row in page]
        pending = set()
        for chunk in chunked(shas):
            pending.update(AccessRequest.objects.filter(status=AccessRequest.Status.PENDING, file_sha256__in=chunk)
                                                .values_list("file_sha256", flat=True))
        for row in page:
            row["has_request"] = row["file_sha256"] in pending
        context["page"] = page
    return render(request, "console/events/list.html", context)


@staff_required
def event_rows(request):
    """The events newer than ?after=<pk>, for the live refresh of the list"""
    require_perms(request, "view_event")
    after = request.GET.get("after", "0")
    after = int(after) if after.isdigit() else 0
    rows = list(filter_events(request, Event.objects.filter(pk__gt=after))
                .select_related("machine", "group").order_by("-pk")[:LIVE_ROWS_LIMIT])
    if not rows:
        return HttpResponse(status=204)
    return render(request, "console/events/_live_rows.html",
                  {"events": rows, "max_pk": rows[0].pk, "params": request.GET})


@staff_required
def new_events_count(request):
    """Badge for the grouped view: how many blocks came in since the page was loaded"""
    require_perms(request, "view_event")
    after = request.GET.get("after", "0")
    count = filter_events(request, Event.objects.filter(pk__gt=int(after) if after.isdigit() else 0,
                                                        decision__startswith="BLOCK_")).count()
    return render(request, "console/events/_new_count.html", {"count": count, "params": request.GET})


@staff_required
def event_detail(request, pk):
    require_perms(request, "view_event")
    event = get_object_or_404(Event.objects.select_related("machine", "group", "resolution_rule", "resolved_by"),
                              pk=pk)
    same_binary = (Event.objects.select_related("machine").filter(file_sha256=event.file_sha256)
                                .exclude(pk=event.pk).order_by("-execution_time")[:20])
    form = ApproveEventForm(initial={"rule_type": default_rule_type(event), "groups": [event.group_id]},
                            available_types=available_rule_types(event))
    form.fields["scope"].choices = [(SCOPE_MACHINES, "This Mac"), ("groups", "Groups"), (SCOPE_GLOBAL, "All Macs")]
    form.fields.pop("note")
    show_rule_preview(form.fields["rule_type"], event)
    template = "console/events/_detail.html" if is_htmx(request) else "console/events/detail.html"
    return render(request, template, {"event": event, "same_binary": same_binary, "form": form,
                                      "requests": event.access_requests.select_related("requester")})


def allow_event(user, event, rule_type, policy, scope, groups, tags, description, machines=None):
    """Create (or widen) the rule for the binary of an event. Returns (rule, created), or None without identifier."""
    identifier = event.identifier_for(rule_type)
    if not identifier:
        return None
    machines = machines if machines is not None else [event.machine]
    rule, created = allow_identifier(
        rule_type, identifier, policy, scope == SCOPE_GLOBAL, groups if scope == "groups" else [], user,
        description or f"{event.file_name} ({event.file_path})", tags,
        machines=machines if scope == SCOPE_MACHINES else [],
    )
    if created:
        log_addition(user, rule, f"Created from event {event.pk}")
    else:
        log_change(user, rule, f"Scope extended from event {event.pk}")
    return rule, created


@staff_required
@require_POST
def event_allow_one(request, pk):
    require_perms(request, "view_event", "add_rule", "change_rule")
    event = get_object_or_404(Event.objects.select_related("machine", "group"), pk=pk)
    form = ApproveEventForm(request.POST, available_types=available_rule_types(event))
    if not form.is_valid():
        messages.error(request, "; ".join(f"{k}: {' '.join(v)}" for k, v in form.errors.items()))
    else:
        data = form.cleaned_data
        rule, created = allow_event(request.user, event, data["rule_type"], data["policy"], data["scope"],
                                    data["groups"], form.all_tags(), "")
        message = gettext("Rule created: %(rule)s.") if created else gettext("Rule updated: %(rule)s.")
        messages.success(request, (message % {"rule": rule}) + " " + gettext("The Macs get it at their next sync."))
    return redirect(safe_next(request, "console:events"))


def _rows_for(events):
    """The selected events grouped by binary, newest first"""
    rows = {}
    for event in events:
        rows.setdefault(event.file_sha256, []).append(event)
    return list(rows.values())


@staff_required
@require_POST
def events_allow(request):
    """Allow several binaries at once, each row with its own rule type, policy, scope and tags"""
    require_perms(request, "view_event", "add_rule", "change_rule")
    events = selected_events(request)
    if not events:
        messages.warning(request, gettext("Select at least one event."))
        return redirect("console:events")
    rows = _rows_for(events)
    # the row of a binary is found with any of its events
    row_of = {event.pk: row for row in rows for event in row}
    applying = "apply" in request.POST
    if applying:
        formset = AllowFormSet(request.POST, prefix="rows")
        shared = AllowSharedForm(request.POST)
    else:
        formset = AllowFormSet(prefix="rows", initial=[
            {"event": row[0].pk, "rule_type": default_rule_type(row[0]),
             "description": f"{row[0].file_name} ({row[0].file_path})"[:500]} for row in rows])
        shared = AllowSharedForm(initial={"groups": list({e.group_id for e in events})})
    for form in formset:
        event_pk = form["event"].value()
        row = row_of.get(int(event_pk)) if str(event_pk).isdigit() else None
        form.events = row or []
        form.fields["rule_type"].choices = rule_type_choices(available_rule_types(row[0]) if row else [])
        if row:
            show_rule_preview(form.fields["rule_type"], row[0])
    if applying and formset.is_valid() and shared.is_valid():
        saved = created_count = 0
        for form in formset:
            data = form.cleaned_data
            if not data.get("include") or not form.events:
                continue
            if data["scope"] == "groups" and not shared.cleaned_data["groups"]:
                form.add_error("scope", gettext("Select groups above, or choose another scope."))
                continue
            result = allow_event(request.user, form.events[0], data["rule_type"], data["policy"], data["scope"],
                                 shared.cleaned_data["groups"], form.all_tags(), data["description"],
                                 machines=list({e.machine for e in form.events}))
            if result:
                saved += 1
                created_count += result[1]
        if not any(form.errors for form in formset):
            messages.success(request, ngettext(
                "%(count)s rule saved (%(new)s new). The Macs get it at their next sync.",
                "%(count)s rules saved (%(new)s new). The Macs get them at their next sync.", saved,
            ) % {"count": saved, "new": created_count})
            return redirect("console:events")
    return render(request, "console/events/allow.html", {"formset": formset, "shared": shared, "events": events,
                                                         "rule_types": rule_type_choices()})


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
