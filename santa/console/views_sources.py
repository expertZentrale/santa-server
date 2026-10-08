from django.contrib import messages
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext, ngettext
from django.views.decorators.http import require_POST

from .. import catalog
from ..models import ReleaseSource, ReleaseVersion
from ..releases import ReleaseError, cleanup_preview, cleanup_release_source, stale_versions, sync_release_source
from ..services import set_rules_enabled
from .filters import Facet, any_of, chosen, filter_bar, remember_filters
from .forms import ReleaseSourceForm
from .utils import (
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
    for source in page or []:
        source.latest = latest_versions(source)
    return render_list(request, "console/sources/list.html", "console/sources/_rows.html", {
        "page": page, "params": request.GET, "sort": sort,
        **filter_bar(request, "sources", [
            Facet("kind", gettext("Catalog"), choices=ReleaseSource.Kind.choices),
            Facet("status", gettext("Status"), choices=[("error", gettext("With errors")),
                                                        ("disabled", gettext("Disabled"))]),
        ], hidden=("sort",), placeholder=gettext("Name or package…")),
    })


@staff_required
def source_detail(request, pk):
    require_perms(request, "view_releasesource")
    source = get_object_or_404(ReleaseSource.objects.prefetch_related("groups", "tags"), pk=pk)
    versions = {}
    for version in source.versions.prefetch_related("rules").order_by("-created_at"):
        # the dependencies with executables (rules); the others are scripts, nothing to allow
        with_rules = {rule.release_dependency for rule in version.rules.all()}
        version.dependency_rows = [(label, label in with_rules) for label in version.dependencies]
        versions.setdefault(version.identifier, []).append(version)
    per_identifier = [(identifier, source.identifier_icons.get(identifier) or {}, versions.pop(identifier, []))
                      for identifier in source.identifiers]
    # versions of identifiers that were removed, deleted at the next check
    per_identifier += [(identifier, {}, items) for identifier, items in versions.items()]
    stale = {version.pk for version in stale_versions(source, check_filters=True)}
    return render_drawer(request, "console/sources/detail.html", "console/sources/drawer_detail.html",
                         {"source": source, "per_identifier": per_identifier, "stale": stale})


@staff_required
def source_form(request, pk=None):
    source = get_object_or_404(ReleaseSource, pk=pk) if pk else None
    require_perms(request, "view_releasesource", "change_releasesource" if source else "add_releasesource")
    initial = {}
    if source is None and request.GET.get("kind") in ReleaseSource.Kind.values:
        initial = {"kind": request.GET["kind"], "identifier": request.GET.get("identifier", ""),
                   "name": request.GET.get("name", "")}
    form = ReleaseSourceForm(request.POST or None, instance=source, initial=initial)
    if request.method == "POST" and form.is_valid():
        created = source is None
        source = form.save()
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
        messages.success(request, gettext("Package rule %(source)s saved. Use “Check now” to get the current "
                                          "releases.") % {"source": source})
        return drawer_done(request, reverse("console:source", args=[source.pk]))
    return render_drawer(request, "console/sources/form.html", "console/sources/drawer_form.html", {
        "form": form, "source": source, "searchable_kinds": list(catalog.SEARCHABLE_KINDS),
    })


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
    rows = []
    for version in versions:
        if version.identifier not in identifiers:
            reason = gettext("package removed")
        elif version.pk in kept:
            reason = gettext("older than the versions to keep")
        else:
            reason = gettext("outside the version pattern")
        rows.append((version, reason))
    return render_drawer(request, "console/sources/cleanup.html", "console/sources/drawer_cleanup.html", {
        "source": source, "rows": rows, "rule_count": rule_count,
        "dependency_rule_count": dependency_rule_count,
    })


@staff_required
@require_POST
def source_check(request, pk):
    require_perms(request, "change_releasesource", "add_rule")
    source = get_object_or_404(ReleaseSource, pk=pk)
    failed = False
    try:
        new_versions = sync_release_source(source)
    except ReleaseError as e:
        messages.error(request, f"{source}: {e}")
        new_versions, failed = e.new_versions, True
    for version in new_versions:
        message = ngettext("%(version)s: new version, %(count)s binary hash", "%(version)s: new version, %(count)s "
                           "binary hashes", version.binary_count) % {"version": version, "count": version.binary_count}
        if not version.binary_count:
            message += " " + gettext("(no executable, nothing for Santa to allow)")
        if version.auto_enable_pending:
            message += ", " + gettext("enabled automatically on %(date)s") % {
                "date": f"{timezone.localtime(version.auto_enable_at):%d.%m.%Y %H:%M}"}
        elif not source.auto_approve:
            message += ", " + gettext("waiting for approval")
        messages.success(request, message + ".")
    if not new_versions and not failed:
        messages.info(request, gettext("%(source)s: up to date.") % {"source": source})
    if is_htmx(request):
        # the drawer of the package rule again, with the messages
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
        return source_detail(request, version.source_id)
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
