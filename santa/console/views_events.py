import json

from django.contrib import messages
from django.db.models import Count, Max, Q
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext, ngettext
from django.views.decorators.http import require_POST

from ..cel import signing_prefix, suggested_prefix
from ..ignores import active_ignores, ignored_q, ignores_of, matching
from ..models import NEEDS_RULE, AccessRequest, Event, Group, IgnoredApp, Machine, Rule, RuleType
from ..package_detection import package_hints
from ..progress import Progress, progress_for
from ..services import SigningIdError, add_signing_prefix, allow_identifier, existing_rules, is_allowed, widen_rule
from .filters import (
    TIME_DAYS,
    TIME_PRESETS,
    Facet,
    chosen,
    date_range,
    filter_bar,
    in_range,
    lookup_values,
    remember_filters,
)
from .forms import SCOPE_GLOBAL, SCOPE_GROUPS, SCOPE_MACHINES, EventRuleForm, IgnoreForm
from .utils import (
    defer_rows,
    drawer_done,
    is_htmx,
    log_addition,
    log_change,
    log_deletion,
    paginate,
    render_drawer,
    render_list,
    require_perms,
    safe_next,
    sort_by,
    staff_required,
)
from .views_filters import MAX_LOOKUP_VALUES, machine_choices

UPDATE_ROWS_LIMIT = 100
EVENT_COLUMNS = {"time": "execution_time", "decision": "decision", "file": "file_name",
                 "mac": "machine__hostname", "user": "executing_user", "signing": "signing_id"}
APP_COLUMNS = {"program": "file_name", "blocks": "event_count", "macs": "machine_count", "users": "user_count",
               "last": "last_seen"}


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


def selected_events(request, data=None):
    """The events chosen in a list: by id ("All events"), or all open blocks of a binary ("Blocked apps", with the
    unknown ones of monitor mode). data: the POST of the list, or the GET of a link (the menu of a row)."""
    data = request.POST if data is None else data
    events = {event.pk: event for event in events_by_ids(data.getlist("ids"))}
    shas = [sha.lower() for sha in data.getlist("shas") if len(sha) == 64]
    # the same events as the list: its status filter
    blocks = with_status(Event.objects.select_related("machine", "group").filter(NEEDS_RULE),
                         data.get("resolved", "open"))
    for chunk in chunked(shas):
        for event in blocks.filter(file_sha256__in=chunk):
            events[event.pk] = event
    return sorted(events.values(), key=lambda e: e.execution_time, reverse=True)


def available_rule_types(event):
    return [rule_type for rule_type in RuleType.values if event.identifier_for(rule_type)]


def show_rule_preview(field, event):
    """The identifier of the binary for every rule type, forms.js shows the one of the chosen type"""
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
    if groups := [value for value in chosen(params, "group") if value.isdigit()]:
        events = events.filter(group_id__in=groups)
    if decisions := chosen(params, "decision"):
        events = events.filter(decision__in=decisions)
    machines, exclude = lookup_values(params, "machine")
    if machines := [value for value in machines[:MAX_LOOKUP_VALUES] if value.isdigit()]:
        events = events.exclude(machine_id__in=machines) if exclude else events.filter(machine_id__in=machines)
    users, exclude = lookup_values(params, "user")
    if users := users[:MAX_LOOKUP_VALUES]:
        events = events.exclude(executing_user__in=users) if exclude else events.filter(executing_user__in=users)
    if params.get("sha256"):
        events = events.filter(file_sha256=params["sha256"].lower())
    start, end = date_range(params, "days", TIME_DAYS, "7")
    events = in_range(events, "execution_time", start, end)
    return with_status(events, resolved_status(params))


RESOLVED_VALUES = ("open", "resolved", "ignored")


def resolved_status(params):
    """The status filter as one string, for the hidden field of the selection too: the values ticked ("open",
    "resolved ignored"), or "all" (all or none ticked; also an old ?resolved=all).

    Open before the filter form was sent, like the other default filters.
    """
    if "resolved" not in params and "q" not in params:
        return "open"
    values = chosen(params, "resolved", RESOLVED_VALUES)
    return " ".join(values) if 0 < len(values) < len(RESOLVED_VALUES) else "all"


def with_status(events, status):
    """The events of a status of resolved_status(): open and resolved leave the ignored apps out, ignored are only
    those (santa/ignores.py)"""
    values = set(status.split()) & set(RESOLVED_VALUES)
    if not values or values == set(RESOLVED_VALUES):
        return events
    ignored = ignored_q()
    shown = Q() if ignored is None else ~ignored
    conditions = []
    if "open" in values:
        conditions.append(Q(resolved_at__isnull=True) & shown)
    if "resolved" in values:
        conditions.append(Q(resolved_at__isnull=False) & shown)
    if "ignored" in values and ignored is not None:
        conditions.append(ignored)
    if not conditions:
        return events.none()
    combined = conditions[0]
    for condition in conditions[1:]:
        combined |= condition
    return events.filter(combined)


def event_facets(request, view):
    """The filters of the bar; sha256 comes from links (e.g. "events" of a blocked app), machine and user from links
    or the search of their chip"""
    params = request.GET
    facets = [Facet("group", gettext("Group"), choices=Group.objects.order_by("name").values_list("pk", "name"))]
    if view == "all":
        decisions = Event.objects.order_by("decision").values_list("decision", flat=True).distinct()
        facets.append(Facet("decision", gettext("Decision"), choices=[(d, d) for d in decisions]))
    facets += [
        Facet("days", gettext("Time range"), kind="time", choices=TIME_PRESETS, default="7"),
        Facet("resolved", gettext("Status"), choices=[("open", gettext("Open")), ("resolved", gettext("Resolved")),
                                                      ("ignored", gettext("Ignored"))], default=["open"]),
    ]
    if sha256 := params.get("sha256", ""):
        facets.append(Facet("sha256", gettext("Binary"), kind="fixed", text=f"{sha256[:12]}…"))
    facets += [
        Facet("machine", gettext("Mac"), kind="lookup", choices=machine_choices(params),
              lookup_url=reverse("console:filter_macs"), max_values=MAX_LOOKUP_VALUES),
        Facet("user", gettext("User"), kind="lookup",
              choices=[(user, user) for user in chosen(params, "user")[:MAX_LOOKUP_VALUES]],
              lookup_url=f"{reverse('console:filter_users')}?source=events", max_values=MAX_LOOKUP_VALUES),
    ]
    return facets




def filter_context(request):
    return {
        # the hidden field of the selection: the status of the filter (resolved_status)
        "resolved": resolved_status(request.GET),
        "params": request.GET,
        "app_filters": app_filters(request),
    }


def app_filters(request):
    """The filters of the list for the drawers of a blocked app (its counts): not the page, order or live update"""
    params = request.GET.copy()
    for key in ("page", "sort", "after", "rows"):
        params.pop(key, None)
    return params.urlencode()


@staff_required
def events(request):
    require_perms(request, "view_event")
    view = request.GET.get("view", "blocked")
    page_key = f"events-{'all' if view == 'all' else 'blocked'}"
    if remembered := remember_filters(request, page_key, keep=("view",)):
        return remembered
    events = filter_events(request, Event.objects.all())
    context = {**filter_context(request), "view": view, "max_pk": Event.objects.aggregate(m=Max("pk"))["m"] or 0,
               **filter_bar(request, page_key, event_facets(request, view), hidden=("view", "sort"), keep=("view",),
                            placeholder=gettext("File, signing ID, SHA-256, Mac, user…"))}
    deferred = defer_rows(request)
    if view == "all":
        queryset, context["sort"] = sort_by(request, events.select_related("machine", "group"), EVENT_COLUMNS,
                                            "-time")
        page = None if deferred else paginate(request, queryset)
    else:
        binaries, context["sort"] = sort_by(request, blocked_apps(events), APP_COLUMNS, "-last",
                                            tiebreak="file_sha256")
        page = None if deferred else paginate(request, binaries)
        if page is not None:
            annotate_app_rows(page)
    context["page"] = page
    # new rows are added on top by live.js, in the default order on the first page
    context["live_insert"] = (page is not None and page.number == 1
                              and context["sort"] == ("-time" if view == "all" else "-last"))
    return render_list(request, "console/events/list.html", "console/events/_rows.html", context)


def blocked_apps(events):
    """One row per blocked binary: a GROUP BY, fine on SQL Server.

    Also the binaries that ran in monitor mode only because they are unknown: lockdown would block them.
    """
    return (events.filter(NEEDS_RULE)
                  .values("file_sha256")
                  .annotate(file_name=Max("file_name"), file_path=Max("file_path"),
                            block_count=Count("id", filter=Q(decision__startswith="BLOCK_")),
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
    events = events_by_ids([row["last_pk"] for row in rows])
    matches = existing_rules(events)
    packages = package_hints(events)
    last = {event.pk: event for event in events}
    ignores = list(active_ignores())
    for row in rows:
        # who ran it last: the user and the Mac of the newest event
        row["last_event"] = last.get(row["last_pk"])
        row["ignores"] = matching(row["last_event"], ignores) if row["last_event"] else []
        row["has_request"] = row["file_sha256"] in pending
        row["rules"] = matches.get(row["file_sha256"], [])
        # only Team ID rules of signing ID prefixes that leave this one out: they block it, no "rule exists"
        row["signing_id_missing"] = bool(row["rules"]) and all(match["cel"] is False for match in row["rules"])
        row["package"] = packages.get(row["file_sha256"])


# the rows a drawer of a blocked app shows: the newest ones, the list behind it has them all
APP_PART_ROWS = 100


@staff_required
def blocked_app(request, sha256, part):
    """The events, Macs or users of a blocked app, with the filters of the list (the counts of its row)"""
    require_perms(request, "view_event")
    if part not in ("events", "macs", "users"):
        raise Http404
    events = filter_events(request, Event.objects.filter(NEEDS_RULE, file_sha256=sha256.lower()))
    newest = events.order_by("-execution_time").only("file_name", "file_path").first()
    if newest is None:
        raise Http404
    context = {"sha256": sha256.lower(), "part": part, "newest": newest, "limit": APP_PART_ROWS,
               "app_parts": [("events", gettext("Blocks")), ("macs", gettext("Macs")), ("users", gettext("Users"))],
               # the filters of the list, for the tabs of the drawer and the link to the whole list
               "query": request.GET.urlencode()}
    if part == "events":
        context["total"] = events.count()
        context["rows"] = events.select_related("machine", "group").order_by("-execution_time")[:APP_PART_ROWS]
    elif part == "macs":
        rows = list(events.values("machine").annotate(count=Count("id"), last_seen=Max("execution_time"))
                    .order_by("-last_seen")[:APP_PART_ROWS])
        machines = Machine.objects.select_related("group").in_bulk([row["machine"] for row in rows])
        for row in rows:
            row["machine"] = machines.get(row["machine"])
        context["total"] = events.values("machine").distinct().count()
        context["rows"] = rows
    else:
        context["total"] = events.values("executing_user").distinct().count()
        context["rows"] = (events.values("executing_user")
                                 .annotate(count=Count("id"), machine_count=Count("machine", distinct=True),
                                           last_seen=Max("execution_time"))
                                 .order_by("-last_seen")[:APP_PART_ROWS])
    return render_drawer(request, "console/events/app_part.html", "console/events/drawer_app_part.html", context)


@staff_required
def event_updates(request):
    """The rows of the list that changed since ?after=<pk>: new events, or the blocked apps with new blocks.

    live.js adds them to the list without a reload, so the selection and an open drawer stay.
    """
    require_perms(request, "view_event")
    after = request.GET.get("after", "0")
    after = int(after) if after.isdigit() else 0
    max_pk = Event.objects.aggregate(m=Max("pk"))["m"] or 0
    if max_pk <= after:
        return HttpResponse(status=204)
    new = filter_events(request, Event.objects.filter(pk__gt=after, pk__lte=max_pk))
    context = {"max_pk": max_pk, "view": request.GET.get("view", "blocked"), "params": request.GET,
               "app_filters": app_filters(request)}
    if context["view"] == "all":
        context["events"] = list(new.select_related("machine", "group").order_by("-pk")[:UPDATE_ROWS_LIMIT])
    else:
        shas = list(new.filter(NEEDS_RULE).values_list("file_sha256", flat=True).distinct())
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
                                      "ignores": ignores_of(event),
                                      "requests": event.access_requests.select_related("requester"),
                                      "matches": matches,
                                      # without a signing ID there is nothing to add
                                      "add_signing_url": reverse("console:event_add_signing_id", args=(event.pk,))
                                      if event.signing_id else "",
                                      "package": package_hints([event]).get(event.file_sha256)})


@staff_required
@require_POST
def event_add_signing_id(request, pk):
    """The signing ID of the event added to a Team ID rule of signing ID prefixes ("only signing IDs starting
    with"), which blocks it so far"""
    require_perms(request, "view_event", "change_rule")
    event = get_object_or_404(Event, pk=pk)
    rule = get_object_or_404(Rule, pk=request.POST.get("rule"), rule_type=RuleType.TEAMID, identifier=event.team_id)
    try:
        prefix = add_signing_prefix(rule, event.signing_id, request.user, request.POST.get("prefix"))
    except SigningIdError as e:
        messages.error(request, str(e))
    else:
        if prefix:
            log_change(request.user, rule, f"Signing ID prefix {prefix} added from event {event.pk}")
            messages.success(request, gettext("%(prefix)s added to the rule of the team %(team_id)s.")
                             % {"prefix": f"{rule.identifier}:{prefix}", "team_id": rule.identifier})
    return drawer_done(request, reverse("console:event", args=(event.pk,)), open_in_drawer=True)


def signing_rules_of(matches, rows):
    """{SHA-256: the Team ID rule of signing ID prefixes that leaves the binary out and can take its signing ID}
    (existing_rules() of the binaries, rows: their events); not a rule of a package rule, which writes its rules
    itself, nor for a binary without a signing ID"""
    rules = {}
    for row in rows:
        sha = row[0].file_sha256
        for match in matches.get(sha, []):
            if (match["cel"] is False and not match["rule"].release_source_id
                    and signing_prefix(match["rule"], row[0].signing_id)):
                rules[sha] = match["rule"]
                break
    return rules


def event_rule_form(rows, data=None, preview=True, initial=None, include=None, signing_rules=None, add_signing=None):
    """The rule form for the binaries of some events (rows: the events grouped by binary)

    preview: the identifier under the rule type (one event; the list of binaries shows its own)
    initial / include: the values and the checked binaries to show (the drawer after a change of the selection)
    signing_rules / add_signing: the binaries that can have their signing ID added to a Team ID rule, the ones
    checked (by default the ones still blocked, see the view)
    """
    events = [event for row in rows for event in row]
    types = [rule_type for rule_type in RuleType.values if any(rule_type in available_rule_types(row[0])
                                                                 for row in rows)]
    # groups: "this Mac" is for tests and exceptions
    signing_rules = signing_rules or {}
    add_signing = add_signing or []
    defaults = {"groups": sorted({event.group_id for event in events}), "scope": SCOPE_GROUPS,
                "include": [sha for sha in (include if include is not None else [row[0].file_sha256 for row in rows])
                            if sha not in add_signing],
                "add_signing": add_signing}
    if len(rows) == 1:
        defaults["rule_type"] = default_rule_type(rows[0][0])
    form = EventRuleForm(data, rows=rows, initial={**defaults, **(initial or {})}, available_types=types,
                         signing_rules=signing_rules)
    if len(rows) == 1 and preview:
        show_rule_preview(form.fields["rule_type"], rows[0][0])
    return form


def rule_type_for(event, rule_type):
    """The chosen rule type, or the suggestion when the binary has no identifier of that type"""
    return rule_type if rule_type in available_rule_types(event) else default_rule_type(event)


def rule_from_event(user, event, rule_type, policy, scope, groups, tags, description, machines=None, cel_expr="",
                    expires_at=None):
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
        machines=machines if scope == SCOPE_MACHINES else [], cel_expr=cel_expr, expires_at=expires_at,
    )
    if created:
        log_addition(user, rule, f"Created from event {event.pk}")
    else:
        log_change(user, rule, f"Scope extended from event {event.pk}")
    return rule, created


def save_event_rules(user, form, progress=None):
    """The rules of the included binaries, and the signing IDs added to their Team ID rules with the scope of the
    form. Returns (saved, created, failed): failed, [(row, message)] of the signing IDs that couldn't be added."""
    data = form.cleaned_data
    progress = progress or Progress()
    rows = form.included_rows()
    signing_rows = form.signing_rows()
    progress.add_total(len(rows) + len(signing_rows))
    saved = created = 0
    failed = []
    for row, rule in signing_rows:
        progress.start(row[0].bundle_name or row[0].file_name or row[0].file_path)
        machines = list({event.machine for event in row})
        try:
            prefix = add_signing_prefix(rule, row[0].signing_id, user,
                                        form.data.get(f"signing_prefix_{row[0].file_sha256}"))
        except SigningIdError as e:
            failed.append((row, str(e)))
            continue
        widen_rule(rule, data["scope"] == SCOPE_GLOBAL, data["groups"] if data["scope"] == SCOPE_GROUPS else [],
                   machines if data["scope"] == SCOPE_MACHINES else [], user, expires_at=data.get("expires_at"))
        log_change(user, rule, f"{f'Signing ID prefix {prefix} added, s' if prefix else 'S'}cope extended from "
                               f"event {row[0].pk}")
        saved += 1
    for row in rows:
        progress.start(row[0].bundle_name or row[0].file_name or row[0].file_path)
        result = rule_from_event(user, row[0], rule_type_for(row[0], data["rule_type"]), data["policy"],
                                 data["scope"], data["groups"], form.all_tags(), data["description"],
                                 machines=list({event.machine for event in row}), cel_expr=form.cel_expr_for(row[0]),
                                 expires_at=data.get("expires_at"))
        if result:
            saved += 1
            created += result[1]
    return saved, created, failed


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
                                        data["groups"], form.all_tags(), data["description"],
                                        cel_expr=form.cel_expr_for(event), expires_at=data.get("expires_at"))
        message = gettext("Rule created: %(rule)s.") if created else gettext("Rule updated: %(rule)s.")
        messages.success(request, (message % {"rule": rule}) + " " + gettext("The Macs get it at their next sync."))
    return redirect(safe_next(request, "console:events"))


def _rows_for(events):
    """The selected events grouped by binary, newest first"""
    rows = {}
    for event in events:
        rows.setdefault(event.file_sha256, []).append(event)
    return list(rows.values())


def _binaries(rows, matches, data=None):
    """The binaries of the form with what their rule would use for every rule type (forms.js shows the chosen one).
    data: the POST of the drawer, it keeps a prefix entered"""
    data = data or {}
    binaries = []
    for row in rows:
        binary = _binary(row, matches)
        if binary["signing_rule"]:
            binary["signing_prefix"] = (data.get(f"signing_prefix_{row[0].file_sha256}")
                                        or suggested_prefix(binary["signing_rule"], row[0].signing_id))
        binaries.append(binary)
    return binaries


def _binary(row, matches):
    return {
        "matches": matches.get(row[0].file_sha256, []),
        "signing_rule": signing_rules_of(matches, [row]).get(row[0].file_sha256),
        "event": row[0], "events": len(row), "macs": len({event.machine_id for event in row}),
        "identifiers": json.dumps({rule_type: row[0].identifier_for(rule_type)
                                   for rule_type in available_rule_types(row[0])}),
        "suggested": default_rule_type(row[0]),
    }


# the fields of the rule form kept when the selection changes while the drawer is open
KEPT_FIELDS = ("rule_type", "policy", "signing_prefixes", "cel_expr", "scope", "new_tags", "description")
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
    signing_rules = signing_rules_of(matches, rows)
    # a binary an allow rule already reaches starts unchecked; one a Team ID rule leaves out gets its signing ID
    # added, unless another rule allows it already (a prefix would widen the rule for nothing)
    wanted = [row[0].file_sha256 for row in rows if not is_allowed(matches.get(row[0].file_sha256, []))]
    saved_before = request.POST.get("saved") == "1"
    saved_shas = []
    if refresh:
        known = set(request.POST.getlist("known"))
        checked = set(request.POST.getlist("include"))
        include = [sha for sha in (row[0].file_sha256 for row in rows)
                   if sha in checked or (sha not in known and sha in wanted)]
        adding = set(request.POST.getlist("add_signing"))
        add_signing = [sha for sha in signing_rules if sha in adding or (sha not in known and sha in wanted)]
        initial = {name: request.POST[name] for name in KEPT_FIELDS if name in request.POST}
        initial.update({name: request.POST.getlist(name) for name in KEPT_LISTS if name in request.POST})
        form = event_rule_form(rows, preview=False, initial=initial, include=include, signing_rules=signing_rules,
                               add_signing=add_signing)
    else:
        form = event_rule_form(rows, request.POST if "apply" in request.POST else None, preview=False,
                               include=wanted, signing_rules=signing_rules,
                               add_signing=[sha for sha in signing_rules if sha in wanted])
    if form.is_bound and form.is_valid():
        with progress_for(request) as progress:
            saved, created, failed = save_event_rules(request.user, form, progress)
        messages.success(request, ngettext(
            "%(count)s rule saved (%(new)s new). The Macs get it at their next sync.",
            "%(count)s rules saved (%(new)s new). The Macs get them at their next sync.", saved,
        ) % {"count": saved, "new": created})
        for row, message in failed:
            messages.error(request, f"{row[0].file_name or row[0].file_sha256}: {message}")
        # the binaries done leave the drawer; one whose signing ID couldn't be added stays
        failed_shas = {row[0].file_sha256 for row, _message in failed}
        saved_shas = [row[0].file_sha256 for row in form.included_rows()]
        saved_shas += [row[0].file_sha256 for row, _rule in form.signing_rows()
                       if row[0].file_sha256 not in failed_shas]
        rest = [row for row in rows if row[0].file_sha256 not in saved_shas]
        if not rest:
            return drawer_done(request, reverse("console:events"))
        rows, saved_before = rest, True
        events = [event for row in rest for event in row]
        matches = existing_rules(events)
        signing_rules = signing_rules_of(matches, rest)
        wanted = [row[0].file_sha256 for row in rest if not is_allowed(matches.get(row[0].file_sha256, []))]
        form = event_rule_form(rest, preview=False, include=wanted, signing_rules=signing_rules,
                               add_signing=[sha for sha in signing_rules if sha in wanted])
        messages.info(request, ngettext("%(count)s binary left: choose its rule, or close.",
                                        "%(count)s binaries left: choose their rule, or close.", len(rest))
                      % {"count": len(rest)})
    return render_drawer(request, "console/events/create_rules.html", "console/events/create_rules_drawer.html", {
        "form": form, "binaries": _binaries(rows, matches, request.POST), "events": events,
        "rule_types": json.dumps({value: str(label) for value, label in RuleType.choices}), "saved": saved_before,
        "saved_shas": " ".join(saved_shas), "resolved": request.POST.get("resolved", "open"),
        "tab": "rules", "package_count": len({s.key for s in package_hints(events).values()}),
    })


def save_ignores(user, ignores, expires_at, note):
    """The ignores of the form (IgnoredApp, not saved): new ones, or the ones that exist already with the later end
    (one for good stays so). Returns how many."""
    for candidate in ignores:
        existing = IgnoredApp.objects.filter(kind=candidate.kind, value=candidate.value).first()
        if existing is None:
            candidate.expires_at, candidate.note, candidate.created_by = expires_at, note, user
            candidate.save()
            log_addition(user, candidate, "Ignored in the console")
            continue
        if existing.expires_at is not None:
            existing.expires_at = None if expires_at is None else max(existing.expires_at, expires_at)
        existing.note = note or existing.note
        existing.name = existing.name or candidate.name
        existing.save()
        log_change(user, existing, "Ignored again in the console")
    return len(ignores)


@staff_required
def events_ignore(request):
    """Ignore the blocked apps of the selected events, in the drawer: the lists no longer show them, also with new
    events (santa/ignores.py). From the selection of a list (POST) or the menu of a row (GET)."""
    require_perms(request, "view_event", "add_ignoredapp")
    data = request.POST if request.method == "POST" else request.GET
    events = selected_events(request, data)
    if not events:
        messages.warning(request, gettext("Select at least one event."))
        return redirect("console:events")
    rows = _rows_for(events)
    form = IgnoreForm(request.POST if "apply" in request.POST else None, rows=rows)
    if form.is_bound and form.is_valid():
        count = save_ignores(request.user, form.ignores, form.cleaned_data["expires_at"], form.cleaned_data["note"])
        messages.success(request, ngettext(
            "%(count)s app ignored: the lists no longer show it, also with new events.",
            "%(count)s apps ignored: the lists no longer show them, also with new events.", count)
            % {"count": count})
        return drawer_done(request, reverse("console:events"))
    return render_drawer(request, "console/events/ignore.html", "console/events/ignore_drawer.html", {
        "form": form, "events": events, "resolved": data.get("resolved", "open")})


@staff_required
@require_POST
def events_unignore(request):
    """No longer ignore the apps of the selected events: the ignores that take them are deleted"""
    require_perms(request, "view_event", "delete_ignoredapp")
    removed = {}
    for event in {event.file_sha256: event for event in selected_events(request)}.values():
        for ignore in ignores_of(event):
            removed[ignore.pk] = ignore
    for ignore in removed.values():
        log_deletion(request.user, ignore, "No longer ignored")
        ignore.delete()
    messages.success(request, ngettext("%(count)s app is listed again.", "%(count)s apps are listed again.",
                                       len(removed)) % {"count": len(removed)})
    return _list_done(request)


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
    return _list_done(request)


def _list_done(request):
    """After an action on the selection of the list: the list again. From the bar of the list (htmx, events_bulk):
    drawer.js reloads the page behind the drawer, with the message."""
    if is_htmx(request):
        return drawer_done(request, safe_next(request, "console:events"))
    return redirect(safe_next(request, "console:events"))


BULK_ACTIONS = {"create_rules": events_create_rules, "resolve": events_resolve, "ignore": events_ignore,
                "unignore": events_unignore}


@staff_required
@require_POST
def events_bulk(request):
    """The action chosen in the bar of the list, on its selection: the views check their permissions"""
    view = BULK_ACTIONS.get(request.POST.get("action", ""))
    if view is None:
        messages.error(request, gettext("Choose an action."))
        return _list_done(request)
    return view(request)
