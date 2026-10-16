import time
from datetime import UTC, datetime

from django.contrib import messages
from django.core.cache import cache
from django.db.models import Q
from django.http import HttpResponse, JsonResponse, QueryDict
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.cache import patch_vary_headers
from django.utils.translation import gettext, ngettext
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from .. import catalog
from ..models import ReleaseSource, ReleaseVersion
from ..progress import (
    SHUTDOWN,
    AlreadyRunning,
    cancel_source_check,
    progress_for,
    read_progress,
    read_source_check,
    read_source_checks,
    run_source_check,
)
from ..releases import (
    LINUX_ONLY_ERROR,
    RULE_OUTSIDE_ASSET_PATTERN,
    ReleaseError,
    cleanup_preview,
    cleanup_release_source,
    pattern_stale_versions,
    preview_release_source,
    stale_rules,
    stale_versions,
    sync_release_source,
    unchecked_identifiers,
)
from ..services import set_rules_enabled
from .filters import Facet, any_of, chosen, filter_bar, remember_filters
from .forms import ReleaseSourceForm
from .utils import (
    ROWS_HEADER,
    changed_message,
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
    rows_only,
    safe_next,
    sort_by,
    staff_required,
)


def latest_versions(source):
    """The newest version of every identifier, in the order of the identifiers"""
    latest = {}
    for version in source.versions.order_by("-created_at"):
        latest.setdefault(version.identifier, version)
    return [(identifier, latest.get(identifier)) for identifier in source.identifiers]


@staff_required
def sources(request):
    require_perms(request, "view_releasesource")
    if remembered := remember_filters(request, "sources"):
        return remembered
    queryset = ReleaseSource.objects.prefetch_related("groups", "versions")
    if kinds := chosen(request.GET, "kind", ReleaseSource.Kind.values):
        queryset = queryset.filter(kind__in=kinds)
    statuses = {"error": ~Q(last_error=""), "disabled": Q(is_enabled=False)}
    status = any_of([statuses[value] for value in chosen(request.GET, "status", statuses)])
    if status is not None:
        queryset = queryset.filter(status)
    q = request.GET.get("q", "").strip()
    if q:
        queryset = queryset.filter(name__icontains=q) | queryset.filter(identifier__icontains=q)
    queryset, sort = sort_by(request, queryset, {"name": "name", "kind": "kind", "checked": "last_checked_at"},
                             "name")
    page = None if defer_rows(request) else paginate(request, queryset)
    checks = read_source_checks(source.pk for source in page or [])
    for source in page or []:
        source.latest = latest_versions(source)
        source.check = checks.get(source.pk)
    return render_list(request, "console/sources/list.html", "console/sources/_rows.html", {
        "page": page, "params": request.GET, "sort": sort,
        **filter_bar(request, "sources", [
            Facet("kind", gettext("Catalog"), choices=ReleaseSource.Kind.choices),
            Facet("status", gettext("Status"), choices=[("error", gettext("With errors")),
                                                        ("disabled", gettext("Disabled"))]),
        ], hidden=("sort",), placeholder=gettext("Name or package…")),
    })


@staff_required
def source_detail(request, pk, defer=None):
    """The package rule. In the drawer its versions come with a second request right after it (X-Santa-Rows, as the
    rows of a list): a rule with many packages opens at once. defer False: everything at once (an approval inside
    the tables, which stay where they are)."""
    require_perms(request, "view_releasesource")
    source = get_object_or_404(ReleaseSource.objects.prefetch_related("groups", "tags"), pk=pk)
    versions_only = rows_only(request)
    if defer is None:
        defer = is_htmx(request) and not versions_only
    context = {"source": source, "defer_versions": defer}
    if not versions_only:
        unchecked = unchecked_identifiers(source)
        # all of them new: "Check now" is the same
        context["unchecked_count"] = len(unchecked) if len(unchecked) < len(source.identifiers) else 0
    if not defer:
        context.update(_versions_context(source))
    if versions_only:
        response = render(request, "console/sources/_versions.html", {**context, "oob": True, "in_drawer": True})
    else:
        context.update({"check": check_texts(read_source_check(source.pk)), "error_groups": error_groups(source),
                        "removable": removable_packages(source)})
        response = render_drawer(request, "console/sources/detail.html", "console/sources/drawer_detail.html",
                                 context)
    patch_vary_headers(response, [ROWS_HEADER, "HX-Request"])
    return response


def _versions_context(source):
    """The versions per package, and what "Clean up" would delete"""
    versions = {}
    # what the table shows of the rules (tens of thousands with dependencies), not the rules: whether a version's
    # rules are enabled, and its dependencies with executables (rules); the others are scripts, nothing to allow
    with_rules = {}
    for version_id, label in (source.rules.exclude(release_dependency="").order_by()
                              .values_list("release_version_id", "release_dependency").distinct()):
        with_rules.setdefault(version_id, set()).add(label)
    # one query for all versions (an EXISTS per version is slow on SQL Server)
    enabled = set(source.rules.filter(is_enabled=True).order_by().values_list("release_version_id", flat=True)
                  .distinct())
    # the notes (the files found, up to 10,000 characters each) are not in the table
    for version in source.versions.defer("notes").order_by("-created_at"):
        version.rules_enabled = version.pk in enabled
        version.dependency_rows = [(label, label in with_rules.get(version.pk, ())) for label in version.dependencies]
        versions.setdefault(version.identifier, []).append(version)
    per_identifier = [(identifier, source.identifier_icons.get(identifier) or {}, versions.pop(identifier, []))
                      for identifier in source.identifiers]
    # versions of identifiers that were removed, deleted at the next check
    per_identifier += [(identifier, {}, items) for identifier, items in versions.items()]
    stale = {version.pk for version in stale_versions(source, check_filters=True)}
    return {"per_identifier": per_identifier, "stale": stale, "stale_count": len(stale) + len(stale_rules(source))}


def removable_packages(source):
    """The packages of the last check that can't work in the package rule (only for Linux): a button removes them.
    Never all of them, a package rule needs one."""
    identifiers = set(source.identifiers)
    removable = [identifier for identifier, messages in error_groups(source)
                 if identifier in identifiers and any(message.startswith(LINUX_ONLY_ERROR) for message, _ in messages)]
    return removable if len(removable) < len(identifiers) else []


@staff_required
@require_POST
def source_remove_identifiers(request, pk):
    """Remove packages from the package rule (the button of the errors box, sources/_errors.html)"""
    require_perms(request, "change_releasesource")
    source = get_object_or_404(ReleaseSource, pk=pk)
    chosen = set(request.POST.getlist("identifier"))
    removed = [identifier for identifier in source.identifiers if identifier in chosen]
    kept = [identifier for identifier in source.identifiers if identifier not in chosen]
    if removed and not kept:
        messages.error(request, gettext("%(source)s: that would leave no package, delete the package rule instead.")
                       % {"source": source})
    elif removed:
        source.identifier = "\n".join(kept)
        # their errors go with them: the box shows what is left
        source.last_error = "\n".join(line for line in source.last_error.splitlines()
                                      if not any(line.startswith(f"{identifier}: ") for identifier in removed))
        source.save(update_fields=["identifier", "last_error"])
        log_change(request.user, source, f"Removed from the packages: {', '.join(removed)}")
        messages.success(request, ngettext("%(count)s package removed from %(source)s: %(names)s.",
                                           "%(count)s packages removed from %(source)s: %(names)s.", len(removed))
                         % {"count": len(removed), "source": source, "names": ", ".join(removed)})
    if is_htmx(request):
        return source_detail(request, source.pk)
    return redirect("console:source", pk=source.pk)


def error_groups(source):
    """The errors of the last check per package: [(identifier, [(message, [versions])])]. The same message for
    several versions of a package (a dependency too big for each of them) is one, with the versions."""
    identifiers = sorted(source.identifiers, key=len, reverse=True) if source.has_several_identifiers else []
    versions = {}
    for identifier, version in source.versions.values_list("identifier", "version"):
        versions.setdefault(identifier, set()).add(version)
    groups = {}
    for line in filter(None, (line.strip() for line in source.last_error.splitlines())):
        identifier = next((i for i in identifiers if line.startswith(f"{i}: ")), "")
        message = line[len(identifier) + 2:] if identifier else line
        known = versions.get(identifier or (source.identifiers[0] if source.identifiers else ""), set())
        head, _, rest = message.partition(": ")
        version = head if rest and head in known else ""
        messages = groups.setdefault(identifier, {})
        found = messages.setdefault(rest if version else message, [])
        if version:
            found.append(version)
    return [(identifier, list(messages.items())) for identifier, messages in groups.items()]


def check_texts(check):
    """The running check of a package rule as the console shows it (_check_status.html, checks.js), or None"""
    if check is None:
        return None
    started = timezone.localtime(datetime.fromtimestamp(check["started"], tz=UTC)) if check.get("started") else None
    at = f"{started:%H:%M}" if started else ""
    if check.get("job"):
        by = gettext("started by the scheduled job at %(time)s") % {"time": at}
    else:
        by = gettext("started by %(user)s at %(time)s") % {"user": check.get("user") or "?", "time": at}
    steps = []
    if check.get("current") and check.get("total"):
        steps.append(gettext("%(current)s / %(total)s") % {"current": check["current"], "total": check["total"]})
    if check.get("label"):
        steps.append(check["label"])
    percent = round(max(0, check["current"] - 1) / check["total"] * 100) if check.get("total") else 0
    cancelling = (gettext("Stopping after the current package (%(user)s)…") % {"user": check["cancelled_by"]}
                  if check.get("cancelled_by") else "")
    return {"running": True, "by": by, "step": " · ".join(steps), "percent": percent, "cancelling": cancelling}


@staff_required
def source_form(request, pk=None):
    source = get_object_or_404(ReleaseSource, pk=pk) if pk else None
    require_perms(request, "view_releasesource", "change_releasesource" if source else "add_releasesource")
    initial = {}
    if source is None and request.GET.get("kind") in ReleaseSource.Kind.values:
        initial = {"kind": request.GET["kind"], "identifier": request.GET.get("identifier", ""),
                   "name": request.GET.get("name", "")}
    # validating the form changes the instance: what it had before, for the added packages
    before = set(source.identifiers) if source else set()
    draft = None
    if request.method == "GET" and not initial:
        draft = cache.get(_draft_key(request, pk))
    if draft:
        # the form as it was previewed, after the drawer was closed or another view opened meanwhile
        form = ReleaseSourceForm(_draft_data(draft), instance=source)
    else:
        form = ReleaseSourceForm(request.POST or None, instance=source, initial=initial)
    if request.method == "POST" and form.is_valid():
        created = source is None
        source = form.save()
        cache.delete(_draft_key(request, pk))
        if created:
            log_addition(request.user, source)
        else:
            log_change(request.user, source, changed_message(form))
        if not source.is_global and not source.groups.exists():
            messages.warning(request, gettext("%(source)s: no group and not global, its rules apply to no Mac.")
                             % {"source": source})
        if not created and (stale := len(stale_versions(source, check_filters=True))):
            # never deleted silently: the console shows them, "Clean up" deletes them
            messages.info(request, ngettext(
                "%(count)s version is no longer covered by the package rule: “Clean up” deletes it with its rules.",
                "%(count)s versions are no longer covered by the package rule: “Clean up” deletes them with their "
                "rules.", stale) % {"count": stale})
        if not created and (outside := len(stale_rules(source))):
            messages.info(request, ngettext(
                "%(count)s rule is outside the asset or binary pattern now: “Clean up” deletes it.",
                "%(count)s rules are outside the asset or binary pattern now: “Clean up” deletes them.",
                outside) % {"count": outside})
        added = [identifier for identifier in source.identifiers if identifier not in before]
        if not created and added and len(added) < len(source.identifiers):
            messages.success(request, ngettext(
                "Package rule %(source)s saved, %(count)s package added: “Check the new packages” gets its "
                "releases.",
                "Package rule %(source)s saved, %(count)s packages added: “Check the new packages” gets their "
                "releases.", len(added)) % {"source": source, "count": len(added)})
        else:
            messages.success(request, gettext("Package rule %(source)s saved. Use “Check now” to get the current "
                                              "releases.") % {"source": source})
        return drawer_done(request, reverse("console:source", args=[source.pk]))
    return render_drawer(request, "console/sources/form.html", "console/sources/drawer_form.html", {
        "form": form, "source": source, "searchable_kinds": list(catalog.SEARCHABLE_KINDS),
        "draft": _draft_context(request, draft),
    })


# the form of a preview, kept for its user: closing the drawer while it runs doesn't lose it
DRAFT_TIMEOUT = 3600
DRAFT_SKIPPED_FIELDS = ("csrfmiddlewaretoken", "progress_id")


def _draft_key(request, pk):
    return f"santa:source-preview:{request.user.pk}:{pk or 'new'}"


def _draft_data(draft):
    data = QueryDict(mutable=True)
    for name, values in draft["data"].items():
        data.setlist(name, values)
    return data


def _draft_context(request, draft):
    if not draft:
        return None
    context = {"started": timezone.localtime(datetime.fromtimestamp(draft["started"], tz=UTC)),
               "running": draft["state"] == "running"}
    if context["running"]:
        progress = read_progress(request.user, draft["progress_id"])
        # without a heartbeat, or without an entry at all a while after it started (it expired with its request)
        dead = progress["beat_age"] is not None and progress["beat_age"] > 35 if progress is not None \
            else time.time() - draft["started"] > 30
        if dead:
            # the request died: its form stays, without a preview
            context["running"] = False
            context["died"] = True
        else:
            context["progress_url"] = reverse("console:progress", args=[draft["progress_id"]])
            context["progress_id"] = draft["progress_id"]
    else:
        context.update(draft["preview"])
    return context


@staff_required
@require_POST
def source_preview(request, pk=None):
    """What the patterns of the form take (htmx partial under the filters): the latest releases of every package
    with their assets, the recorded files against the binary pattern, and what the clean up would delete"""
    source = get_object_or_404(ReleaseSource, pk=pk) if pk else None
    require_perms(request, "view_releasesource", "change_releasesource" if source else "add_releasesource")
    form = ReleaseSourceForm(request.POST, instance=source)
    if not form.is_valid():
        return render(request, "console/sources/_preview.html", {"form": form, "invalid": True})
    # validating filled the instance; never saved here (save() would also look up the icons in the catalogs)
    source = form.instance
    key = _draft_key(request, pk)
    draft = {"data": {name: request.POST.getlist(name) for name in request.POST if name not in DRAFT_SKIPPED_FIELDS},
             "started": timezone.now().timestamp(), "state": "running",
             "progress_id": request.POST.get("progress_id", "")}
    cache.set(key, draft, DRAFT_TIMEOUT)
    with progress_for(request) as progress:
        preview = {"results": preview_release_source(source, progress=progress),
                   "cancelled_by": progress.cancelled_by, "restart": progress.cancelled_by == SHUTDOWN}
        if source.pk:
            preview["stale_versions"] = len(stale_versions(source, check_filters=True))
            preview["stale_rules"] = len(stale_rules(source))
        # before the progress is done: the restored form (checks.js) asks for the result then
        cache.set(key, {**draft, "state": "done", "preview": preview}, DRAFT_TIMEOUT)
    return render(request, "console/sources/_preview.html", {"form": form, "source": source, **preview})


@staff_required
@require_GET
@never_cache
def source_preview_result(request, pk=None):
    """The preview of a restored form, once it is done (checks.js)"""
    source = get_object_or_404(ReleaseSource, pk=pk) if pk else None
    require_perms(request, "view_releasesource", "change_releasesource" if source else "add_releasesource")
    draft = cache.get(_draft_key(request, pk))
    if not draft or draft["state"] != "done":
        return render(request, "console/sources/_preview.html", {"gone": True})
    return render(request, "console/sources/_preview.html", {"source": source, **draft["preview"]})


@staff_required
@require_POST
def source_preview_discard(request, pk=None):
    """The form as saved again, without the restored changes"""
    source = get_object_or_404(ReleaseSource, pk=pk) if pk else None
    require_perms(request, "view_releasesource", "change_releasesource" if source else "add_releasesource")
    cache.delete(_draft_key(request, pk))
    if is_htmx(request):
        # the view of the drawer again from its URL (drawer.js)
        return HttpResponse(status=204, headers={"HX-Trigger": "drawerReload"})
    return redirect(reverse("console:source_edit", args=[pk]) if pk else reverse("console:source_add"))


@staff_required
@require_POST
def source_delete(request, pk):
    require_perms(request, "delete_releasesource")
    source = get_object_or_404(ReleaseSource, pk=pk)
    log_deletion(request.user, source)
    source.delete()
    messages.success(request, gettext("Package rule %(source)s deleted with its rules.") % {"source": source})
    return redirect("console:sources")


@staff_required
def source_cleanup(request, pk):
    """Delete the versions (with their rules) the package rule no longer covers: removed packages, versions the
    version pattern no longer matches, outside the range to keep, and the rules of dependencies no longer allowed"""
    require_perms(request, "change_releasesource", "delete_rule")
    source = get_object_or_404(ReleaseSource, pk=pk)
    if request.method == "POST":
        versions, rules = cleanup_release_source(source)
        if versions or rules:
            log_change(request.user, source, f"Cleaned up: {versions} version(s), {rules} rule(s) deleted")
            messages.success(request, gettext("%(source)s cleaned up: %(versions)s, %(rules)s deleted.") % {
                "source": source,
                "versions": ngettext("%(count)s version", "%(count)s versions", versions) % {"count": versions},
                "rules": ngettext("%(count)s rule", "%(count)s rules", rules) % {"count": rules}})
        else:
            messages.info(request, gettext("%(source)s: nothing to clean up.") % {"source": source})
        return drawer_done(request, reverse("console:source", args=[source.pk]))
    versions, rule_count, dependency_rule_count = cleanup_preview(source)
    identifiers = set(source.identifiers)
    kept = {version.pk for version in stale_versions(source)}
    outside_files = pattern_stale_versions(source)
    rows = []
    for version in versions:
        if version.identifier not in identifiers:
            reason = gettext("package removed")
        elif version.pk in kept:
            reason = gettext("older than the versions to keep")
        elif version.pk in outside_files:
            reason = gettext("no file inside the asset or binary pattern")
        else:
            reason = gettext("outside the version pattern")
        rows.append((version, reason))
    rule_rows = [(rule, gettext("outside the asset pattern") if reason == RULE_OUTSIDE_ASSET_PATTERN
                  else gettext("outside the binary pattern"))
                 for rule, reason in stale_rules(source)]
    return render_drawer(request, "console/sources/cleanup.html", "console/sources/drawer_cleanup.html", {
        "source": source, "rows": rows, "rule_count": rule_count,
        "dependency_rule_count": dependency_rule_count,
        "rule_rows": rule_rows,
    })


@staff_required
@require_POST
def source_check(request, pk):
    require_perms(request, "change_releasesource", "add_rule")
    source = get_object_or_404(ReleaseSource, pk=pk)
    failed = False
    try:
        with run_source_check(source, request.user, progress_for(request)) as progress:
            try:
                # "Check the new packages": the ones never checked, not the whole list
                only = unchecked_identifiers(source) if request.POST.get("only") == "new" else None
                new_versions = sync_release_source(source, progress=progress, only=only)
            except ReleaseError as e:
                messages.error(request, f"{source}: {e}")
                new_versions, failed = e.new_versions, True
    except AlreadyRunning as e:
        texts = check_texts(e.check)
        messages.info(request, gettext("%(source)s is being checked already (%(by)s).")
                      % {"source": source, "by": texts["by"] if texts else "?"})
        new_versions = None
    if new_versions is not None:
        # the next one raised before it counted
        counts = {"source": source, "current": progress.current, "total": progress.total}
        if progress.cancelled_by == SHUTDOWN:
            messages.warning(request, gettext("%(source)s: the server is restarting, the check stopped after "
                                              "%(current)s of %(total)s packages. Start it again.") % counts)
        elif progress.cancelled:
            messages.warning(request, gettext("%(source)s: check stopped by %(user)s after %(current)s of %(total)s "
                                              "packages.") % {**counts, "user": progress.cancelled_by})
        if new_versions:
            messages.success(request, check_summary(source, new_versions))
        elif not failed and not progress.cancelled:
            messages.info(request, gettext("%(source)s: up to date.") % {"source": source})
    if is_htmx(request):
        # the drawer of the package rule again, with the messages
        return source_detail(request, source.pk)
    return redirect(safe_next(request, "console:sources"))


def check_added_packages(request, source, progress, only, sync):
    """The check after packages were added to a package rule (an approved request, event packages): only them, under
    the guard of the package rule (run_source_check), in the progress of the request. One running already gets a
    message; the packages are saved, its next check takes them. sync: sync_release_source of the caller."""
    try:
        with run_source_check(source, request.user, progress) as running:
            sync(source, progress=running, only=only)
    except AlreadyRunning:
        messages.warning(request, gettext("%(source)s is being checked right now: check the new packages when it has "
                                          "ended.") % {"source": source})
    except ReleaseError as e:
        messages.warning(request, f"{source}: {e}")


SUMMARY_VERSIONS = 3


def check_summary(source, new_versions):
    """One message for the new versions of a check, however many (the drawer lists them)"""
    count = len(new_versions)
    names = [f"{version.identifier} {version.version}" if source.has_several_identifiers else version.version
             for version in new_versions[:SUMMARY_VERSIONS]]
    if count > SUMMARY_VERSIONS:
        names.append(ngettext("%(count)s more", "%(count)s more", count - SUMMARY_VERSIONS)
                     % {"count": count - SUMMARY_VERSIONS})
    parts = [ngettext("%(source)s: %(count)s new version (%(versions)s).",
                      "%(source)s: %(count)s new versions (%(versions)s).", count)
             % {"source": source, "count": count, "versions": ", ".join(names)}]
    pending = [version for version in new_versions if version.binary_count and version.auto_enable_pending]
    waiting = [version for version in new_versions
               if version.binary_count and not version.auto_enable_pending and not source.auto_approve]
    empty = [version for version in new_versions if not version.binary_count]
    if pending:
        first = min(version.auto_enable_at for version in pending)
        parts.append(ngettext("%(count)s is enabled automatically on %(date)s.",
                              "%(count)s are enabled automatically from %(date)s.", len(pending))
                     % {"count": len(pending), "date": f"{timezone.localtime(first):%d.%m.%Y %H:%M}"})
    if waiting:
        parts.append(ngettext("%(count)s waits for approval.", "%(count)s wait for approval.", len(waiting))
                     % {"count": len(waiting)})
    if empty:
        parts.append(ngettext("%(count)s has no executable (nothing for Santa to allow).",
                              "%(count)s have no executable (nothing for Santa to allow).", len(empty))
                     % {"count": len(empty)})
    return " ".join(parts)


@staff_required
@never_cache
def source_check_status(request, pk):
    """The check of the package rule while it runs (checks.js): {"running": false} once it ended"""
    require_perms(request, "view_releasesource")
    return JsonResponse(check_texts(read_source_check(pk)) or {"running": False})


@staff_required
@require_POST
def source_check_cancel(request, pk):
    """Stop the check of a package rule, also one someone else (or the scheduled job) started"""
    require_perms(request, "change_releasesource")
    source = get_object_or_404(ReleaseSource, pk=pk)
    if cancel_source_check(source.pk, request.user):
        log_change(request.user, source, "Check stopped")
        messages.info(request, gettext("%(source)s: the check stops after the current package.") % {"source": source})
    else:
        messages.info(request, gettext("%(source)s: no check is running.") % {"source": source})
    if is_htmx(request):
        return source_detail(request, source.pk)
    return redirect(safe_next(request, "console:sources"))


@staff_required
@require_POST
def version_set_enabled(request, pk, enabled):
    require_perms(request, "change_rule")
    version = get_object_or_404(ReleaseVersion.objects.select_related("source"), pk=pk)
    rules = list(version.rules.all())
    set_rules_enabled(version.rules.all(), enabled)
    if enabled and version.auto_enable_pending:
        # approving skips the auto approve delay
        version.auto_enable_pending = False
        version.save(update_fields=["auto_enable_pending"])
    log_change(request.user, rules, f"{'Approved' if enabled else 'Disabled'} with version {version}")
    if enabled:
        message = ngettext("%(version)s: %(count)s rule enabled.", "%(version)s: %(count)s rules enabled.", len(rules))
    else:
        message = ngettext("%(version)s: %(count)s rule disabled.", "%(version)s: %(count)s rules disabled.",
                           len(rules))
    messages.success(request, message % {"version": version, "count": len(rules)})
    if is_htmx(request):
        # the tables at once: the approved version stays where it was
        return source_detail(request, version.source_id, defer=False)
    return redirect("console:source", pk=version.source_id)


@staff_required
def catalog_search(request):
    """Suggestions for the identifier field (htmx partial)"""
    require_perms(request, "view_releasesource")
    return catalog_results(request)


def catalog_context(request):
    kind = request.GET.get("kind", "")
    query = request.GET.get("q", "")
    error = None
    try:
        suggestions = catalog.search(kind, query)
    except catalog.CatalogError as e:
        suggestions, error = [], str(e)
    return {"suggestions": suggestions, "error": error, "query": query.strip(),
            "searchable": kind in catalog.SEARCHABLE_KINDS}


def catalog_results(request):
    return render(request, "console/widgets/_suggestions.html", catalog_context(request))
