from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.translation import gettext
from django.views.decorators.http import require_POST

from .. import catalog
from ..catalog import update_identifier_icons
from ..models import AccessRequest, AccessRequestPackage, Event, ReleaseSource, RuleType
from ..releases import ReleaseError, find_binaries, sync_release_source
from ..services import allow_identifier, binary_identifiers, existing_rules, machines_for_user, request_kinds_for
from .forms import (
    REQUESTABLE_PACKAGE_KINDS,
    SCOPE_GLOBAL,
    SCOPE_GROUPS,
    SCOPE_MACHINES,
    ApproveEventForm,
    ApproveOtherForm,
    ApprovePackagesForm,
    DenyForm,
    RequestEventForm,
    RequestOtherForm,
    RequestPackageForm,
)
from .utils import (
    drawer_done,
    log_addition,
    log_change,
    paginate,
    render_drawer,
    require_perms,
    sort_by,
    staff_required,
)
from .views_events import available_rule_types, default_rule_type, show_rule_preview
from .views_sources import catalog_results

MAX_PENDING_PER_USER = 20
REQUEST_EVENT_DAYS = 30
FORMS = {AccessRequest.Kind.EVENT: RequestEventForm, AccessRequest.Kind.PACKAGE: RequestPackageForm,
         AccessRequest.Kind.OTHER: RequestOtherForm}


def blocked_events_of(user):
    """The latest open block of every binary on the Macs of the user, last 30 days"""
    events = (Event.objects.select_related("machine")
                           .filter(machine__in=machines_for_user(user), decision__startswith="BLOCK_",
                                   resolved_at__isnull=True,
                                   execution_time__gte=timezone.now() - timedelta(days=REQUEST_EVENT_DAYS))
                           .order_by("-execution_time")[:500])
    latest = {}
    for event in events:
        latest.setdefault(event.file_sha256, event.pk)
    return Event.objects.select_related("machine").filter(pk__in=list(latest.values())[:100]) \
                        .order_by("-execution_time")


# The users


@login_required
def my_requests(request):
    page = paginate(request, AccessRequest.objects.filter(requester=request.user)
                                                  .select_related("event", "machine").prefetch_related("packages"))
    return render(request, "request/list.html", {"page": page, "can_request": bool(request_kinds_for(request.user))})


@login_required
def new_request(request):
    allowed = request_kinds_for(request.user)
    if not allowed:
        return render(request, "request/new.html", {"kinds": []}, status=403)
    kind = request.GET.get("kind") or request.POST.get("kind") or allowed[0]
    if kind not in allowed:
        if request.method == "POST":
            raise PermissionDenied
        kind = allowed[0]
    events = blocked_events_of(request.user)
    kwargs = {"events": events} if kind == AccessRequest.Kind.EVENT else {}
    initial = {}
    sha256 = request.GET.get("sha256", "").lower()
    if kind == AccessRequest.Kind.EVENT and sha256:
        # link of the block dialog (EventDetailURL with %file_sha%)
        initial["event"] = events.filter(file_sha256=sha256).first()
    form = FORMS[kind](request.POST if request.method == "POST" else None, initial=initial, **kwargs)
    if request.method == "POST" and form.is_valid():
        access_request = _build_request(request.user, kind, form.cleaned_data)
        error = _check_request(request.user, access_request, form.cleaned_data.get("packages", []))
        if error:
            form.add_error(None, error)
        else:
            with transaction.atomic():
                access_request.save()
                AccessRequestPackage.objects.bulk_create(
                    AccessRequestPackage(access_request=access_request, **package)
                    for package in form.cleaned_data.get("packages", []))
            messages.success(request, gettext("Your request was sent to your IT team. You'll see the answer here."))
            return redirect("requests:list")
    return render(request, "request/new.html", {
        "form": form, "kind": kind, "kinds": [(value, label) for value, label in AccessRequest.Kind.choices
                                              if value in allowed],
        "has_machines": machines_for_user(request.user).exists(),
        "package_kinds": [(k, ReleaseSource.Kind(k).label) for k in REQUESTABLE_PACKAGE_KINDS],
        "searchable_kinds": list(catalog.SEARCHABLE_KINDS),
    })


def _build_request(user, kind, data):
    access_request = AccessRequest(requester=user, kind=kind, justification=data["justification"])
    if kind == AccessRequest.Kind.EVENT:
        event = data["event"]
        access_request.event = event
        access_request.machine = event.machine
        access_request.file_sha256 = event.file_sha256
        access_request.title = (event.bundle_name or event.file_name or event.file_path)[:200]
    elif kind == AccessRequest.Kind.PACKAGE:
        packages = data["packages"]
        if len(packages) == 1:
            package = packages[0]
            title = f"{ReleaseSource.Kind(package['kind']).label}: {package['name'] or package['identifier']}"
        else:
            title = gettext("%(count)s packages: %(names)s") % {
                "count": len(packages), "names": ", ".join(p["name"] or p["identifier"] for p in packages)}
        access_request.title = title if len(title) <= 200 else title[:199] + "…"
    else:
        access_request.title = data["title"]
        access_request.link = data["link"]
    return access_request


def _check_request(user, access_request, packages):
    pending = AccessRequest.objects.filter(requester=user, status=AccessRequest.Status.PENDING)
    if pending.count() >= MAX_PENDING_PER_USER:
        return gettext("You have %(count)s open requests already. Wait for an answer first.") % {
            "count": MAX_PENDING_PER_USER}
    if access_request.kind == AccessRequest.Kind.PACKAGE:
        requested = set()
        for chunk in _chunked([p["identifier"] for p in packages]):
            requested.update(AccessRequestPackage.objects.filter(access_request__in=pending, identifier__in=chunk)
                                                         .values_list("kind", "identifier"))
        duplicates = [p["identifier"] for p in packages if (p["kind"], p["identifier"]) in requested]
        if duplicates:
            return gettext("You have already asked for %(packages)s, the request is open. Remove it here.") % {
                "packages": ", ".join(duplicates)}
        return None
    duplicate = {
        AccessRequest.Kind.EVENT: {"file_sha256": access_request.file_sha256},
        AccessRequest.Kind.OTHER: {"title": access_request.title},
    }[access_request.kind]
    if pending.filter(kind=access_request.kind, **duplicate).exists():
        return gettext("You have already asked for this, the request is open.")
    return None


def _chunked(values, size=1000):
    for start in range(0, len(values), size):
        yield values[start:start + size]


@login_required
@require_POST
def cancel_request(request, pk):
    access_request = get_object_or_404(AccessRequest, pk=pk, requester=request.user,
                                       status=AccessRequest.Status.PENDING)
    access_request.status = AccessRequest.Status.CANCELLED
    access_request.decided_at = timezone.now()
    access_request.save(update_fields=["status", "decided_at"])
    messages.success(request, gettext("Request cancelled."))
    return redirect("requests:list")


@login_required
def request_catalog_search(request):
    if AccessRequest.Kind.PACKAGE not in request_kinds_for(request.user):
        raise PermissionDenied
    return catalog_results(request)


# The administrators


@staff_required
def admin_requests(request):
    require_perms(request, "view_accessrequest")
    status = request.GET.get("status", AccessRequest.Status.PENDING)
    queryset = AccessRequest.objects.select_related("requester", "machine", "decided_by").prefetch_related("packages")
    if status in AccessRequest.Status.values:
        queryset = queryset.filter(status=status)
    columns = {"request": "title", "kind": "kind", "requester": "requester__username", "created": "created_at",
               "status": "status"}
    queryset, sort = sort_by(request, queryset, columns, "-created")
    return render(request, "console/requests/list.html", {
        "page": paginate(request, queryset), "status": status, "statuses": AccessRequest.Status.choices, "sort": sort,
    })


def _event_of(access_request):
    """The event of the request, or the latest one of the same binary if cleanup_events deleted it"""
    if access_request.event:
        return access_request.event
    if access_request.file_sha256:
        return (Event.objects.select_related("machine", "group").filter(file_sha256=access_request.file_sha256)
                             .order_by("-execution_time").first())
    return None


def _approve_forms(access_request, data=None, files=None):
    if access_request.kind == AccessRequest.Kind.EVENT:
        event = _event_of(access_request)
        available = available_rule_types(event) if event else [RuleType.BINARY]
        initial = {"rule_type": default_rule_type(event) if event else RuleType.BINARY,
                   "groups": [event.group_id] if event else []}
        form = ApproveEventForm(data, initial=initial, available_types=available)
        if event:
            show_rule_preview(form.fields["rule_type"], event)
        return form
    if access_request.kind == AccessRequest.Kind.PACKAGE:
        groups = {machine.group_id for machine in machines_for_user(access_request.requester)}
        requester = access_request.requester.get_full_name() or access_request.requester.get_username()
        return ApprovePackagesForm(data, packages=access_request.packages.all(),
                                   initial={"new_name": f"Requested by {requester}"[:180], "groups": list(groups)})
    groups = list({machine.group_id for machine in machines_for_user(access_request.requester)})
    return ApproveOtherForm(data, files, initial={"groups": groups, "package_groups": groups,
                                                  "package_name": access_request.title[:180]})


@staff_required
def admin_request_detail(request, pk, approve_form=None, deny_form=None):
    require_perms(request, "view_accessrequest")
    access_request = get_object_or_404(AccessRequest.objects.select_related("requester", "machine", "event"), pk=pk)
    event = _event_of(access_request) if access_request.kind == AccessRequest.Kind.EVENT else None
    others = AccessRequest.objects.none()
    if access_request.file_sha256:
        others = AccessRequest.objects.filter(file_sha256=access_request.file_sha256).exclude(pk=pk)
    elif access_request.kind == AccessRequest.Kind.PACKAGE:
        identifiers = list(access_request.packages.values_list("identifier", flat=True))
        others = AccessRequest.objects.filter(packages__identifier__in=identifiers).exclude(pk=pk).distinct()
    return render_drawer(request, "console/requests/detail.html", "console/requests/drawer_detail.html", {
        "access_request": access_request, "event": event, "others": others.select_related("requester")[:20],
        "matches": existing_rules([event]).get(event.file_sha256, []) if event else [],
        "approve_form": approve_form or _approve_forms(access_request),
        "deny_form": deny_form or DenyForm(),
        "requester_machines": machines_for_user(access_request.requester).select_related("group"),
    })


def _decide(access_request, user, status, note):
    access_request.status = status
    access_request.decided_by = user
    access_request.decided_at = timezone.now()
    access_request.decision_note = note
    access_request.save()
    log_change(user, access_request, f"{access_request.get_status_display()}: {note}"[:500])


@staff_required
@require_POST
def admin_request_approve(request, pk):
    require_perms(request, "change_accessrequest")
    access_request = get_object_or_404(AccessRequest.objects.select_related("requester", "machine", "event"),
                                       pk=pk, status=AccessRequest.Status.PENDING)
    form = _approve_forms(access_request, request.POST, request.FILES)
    if not form.is_valid():
        return admin_request_detail(request, pk, approve_form=form)
    data = form.cleaned_data
    try:
        with transaction.atomic():
            if access_request.kind == AccessRequest.Kind.EVENT:
                require_perms(request, "add_rule", "change_rule")
                _approve_event(request, access_request, form)
            elif access_request.kind == AccessRequest.Kind.PACKAGE:
                require_perms(request, "add_releasesource", "change_releasesource")
                _approve_packages(request, access_request, form)
            else:
                _approve_other(request, access_request, form)
            _decide(access_request, request.user, AccessRequest.Status.APPROVED, data.get("note", ""))
    except ValidationError as e:
        form.add_error(None, e)
        return admin_request_detail(request, pk, approve_form=form)
    source_ids = {p.result_source_id for p in access_request.packages.all() if p.result_source_id}
    if access_request.result_source_id:
        source_ids.add(access_request.result_source_id)
    for source in ReleaseSource.objects.filter(pk__in=source_ids):
        try:
            sync_release_source(source)
        except ReleaseError as e:
            messages.warning(request, f"{source}: {e}")
    messages.success(request, gettext("Request “%(title)s” approved.") % {"title": access_request.title})
    return drawer_done(request, "console:requests")


def _allow_for_request(request, access_request, form, identifiers):
    """Allow the identifiers in the scope of the approve form: the Macs of the requester, groups or all Macs.

    Returns the first rule.
    """
    data = form.cleaned_data
    machines = [access_request.machine] if access_request.machine else list(
        machines_for_user(access_request.requester))
    if data["scope"] == SCOPE_MACHINES and not machines:
        raise ValidationError(gettext("The Mac of the requester is not known, choose groups or all Macs."))
    rules = []
    for identifier in identifiers:
        rule, created = allow_identifier(
            data["rule_type"], identifier, data["policy"], data["scope"] == SCOPE_GLOBAL,
            data["groups"] if data["scope"] == SCOPE_GROUPS else [], request.user,
            f"{access_request.title} (request of {access_request.requester})", form.all_tags(),
            machines=machines if data["scope"] == SCOPE_MACHINES else [],
        )
        (log_addition if created else log_change)(request.user, rule, f"Access request {access_request.pk}")
        rules.append(rule)
    return rules[0]


def _approve_event(request, access_request, form):
    data = form.cleaned_data
    event = _event_of(access_request)
    identifier = event.identifier_for(data["rule_type"]) if event else access_request.file_sha256
    if not identifier:
        raise ValidationError(gettext("The binary has no %(rule_type)s.") % {"rule_type": data["rule_type"]})
    access_request.result_rule = _allow_for_request(request, access_request, form, [identifier])


def _approve_other(request, access_request, form):
    data = form.cleaned_data
    if data["result"] == ApproveOtherForm.RULE:
        require_perms(request, "add_rule", "change_rule")
        identifiers = [data["identifier"]] if data["identifier"] else _identifiers_of_upload(data)
        access_request.result_rule = _allow_for_request(request, access_request, form, identifiers)
    elif data["result"] == ApproveOtherForm.PACKAGE:
        require_perms(request, "add_releasesource", "change_releasesource")
        access_request.result_source = _package_rule_for_request(request, access_request, form)
    elif data["result"] == ApproveOtherForm.EXISTING:
        access_request.result_rule = data["rule"]


def _identifiers_of_upload(data):
    upload = data["file"]
    identifiers = []
    try:
        for _path, info in find_binaries(upload, upload.name, data["binary_pattern"]):
            identifiers += binary_identifiers(data["rule_type"], info)
    except ReleaseError as e:
        raise ValidationError(str(e)) from e
    identifiers = list(dict.fromkeys(identifiers))
    if not identifiers:
        raise ValidationError(gettext("No Mach-O executable with a %(rule_type)s found in this file (unsigned or "
                                      "ad-hoc signed? Use a binary rule instead).") % {"rule_type": data["rule_type"]})
    return identifiers


def _package_rule_for_request(request, access_request, form):
    """A new package rule for the requested packages, or the packages added to an existing one"""
    data = form.cleaned_data
    source = data["package_source"]
    try:
        if source is None:
            source = ReleaseSource(name=data["package_name"].strip(), kind=data["package_kind"],
                                   identifier="\n".join(data["package_identifiers"]),
                                   rule_type=data["package_rule_type"], is_global=data["package_is_global"],
                                   auto_approve=data["package_auto_approve"])
            source.full_clean()
            source.save()
            source.groups.set([] if source.is_global else data["package_groups"])
            source.tags.add(*form.all_tags())
            log_addition(request.user, source, f"Created for access request {access_request.pk}")
        else:
            added = [identifier for identifier in data["package_identifiers"] if identifier not in source.identifiers]
            if added:
                source.identifier = "\n".join(source.identifiers + added)
                source.full_clean()
                source.save()
                log_change(request.user, source,
                           f"Identifiers {', '.join(added)} added for access request {access_request.pk}")
    except ValidationError as e:
        raise ValidationError(e.messages) from e
    return source


def _approve_packages(request, access_request, form):
    """Add the approved packages to their package rules; the others are not approved"""
    data = form.cleaned_data
    approved = form.approved()
    new_sources = {}
    for kind, name in form.new_rule_names().items():
        packages = [package for package, target in approved if target is None and package.kind == kind]
        source = ReleaseSource(name=name, kind=kind, identifier="\n".join(p.identifier for p in packages),
                               rule_type=data["rule_type"] or RuleType.BINARY, is_global=data["is_global"])
        source.full_clean()
        update_identifier_icons(source, {p.identifier: {"name": p.name, "icon_url": p.icon_url}
                                         for p in packages if p.name or p.icon_url})
        source.save()
        source.groups.set(data["groups"])
        log_addition(request.user, source, f"Created for access request {access_request.pk}")
        new_sources[kind] = source
    for package, target in approved:
        source = target or new_sources[package.kind]
        if target is not None and package.identifier not in target.identifiers:
            target.refresh_from_db()
            target.identifier = "\n".join(target.identifiers + [package.identifier])
            target.full_clean()
            update_identifier_icons(target, {package.identifier: {"name": package.name, "icon_url": package.icon_url}}
                                    if package.name or package.icon_url else None)
            target.save()
            log_change(request.user, target,
                       f"Identifier {package.identifier} added for access request {access_request.pk}")
        package.status = AccessRequestPackage.Status.APPROVED
        package.result_source = source
        package.save(update_fields=["status", "result_source"])
    approved_pks = {package.pk for package, _ in approved}
    access_request.packages.exclude(pk__in=approved_pks).update(status=AccessRequestPackage.Status.DENIED)
    access_request.result_source = next(iter(new_sources.values()), None) or approved[0][1]


@staff_required
@require_POST
def admin_request_deny(request, pk):
    require_perms(request, "change_accessrequest")
    access_request = get_object_or_404(AccessRequest, pk=pk, status=AccessRequest.Status.PENDING)
    form = DenyForm(request.POST)
    if not form.is_valid():
        return admin_request_detail(request, pk, deny_form=form)
    _decide(access_request, request.user, AccessRequest.Status.DENIED, form.cleaned_data["note"])
    access_request.packages.update(status=AccessRequestPackage.Status.DENIED)
    messages.success(request, gettext("Request “%(title)s” denied.") % {"title": access_request.title})
    return drawer_done(request, "console:requests")
