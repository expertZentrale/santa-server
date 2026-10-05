import csv

from django.contrib import messages
from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.translation import gettext, ngettext
from django.views.decorators.http import require_POST

from ..models import Group, Machine, Policy, Rule, RuleType, Tag
from ..releases import ReleaseError, find_binaries
from ..services import allow_identifier, binary_identifiers, set_rules_enabled, set_rules_policy
from .filters import (
    TIME_DAYS,
    TIME_PRESETS,
    Facet,
    any_of,
    chosen,
    date_range,
    filter_bar,
    in_range,
    remember_filters,
)
from .forms import RuleBulkForm, RuleForm, UploadBinaryForm
from .utils import (
    changed_message,
    drawer_done,
    is_htmx,
    log_addition,
    log_change,
    log_deletion,
    paginate,
    render_drawer,
    require_perms,
    safe_next,
    sort_by,
    staff_required,
)

RULE_COLUMNS = {"identifier": "identifier", "type": "rule_type", "policy": "policy", "created": "created_at",
                "enabled": "is_enabled"}


# the scopes of the filter besides the groups (their pks)
SCOPE_CONDITIONS = {"global": Q(is_global=True), "machines": Q(is_global=False, machines__isnull=False)}


def filter_rules(request, rules):
    """The rules of the list filters; several values of one filter are alternatives (OR)"""
    params = request.GET
    if rule_types := chosen(params, "type", RuleType.values):
        rules = rules.filter(rule_type__in=rule_types)
    if policies := chosen(params, "policy", Policy.values):
        rules = rules.filter(policy__in=policies)
    scope = any_of([SCOPE_CONDITIONS[value] if value in SCOPE_CONDITIONS else Q(groups__id=value)
                    for value in chosen(params, "scope") if value in SCOPE_CONDITIONS or value.isdigit()])
    if scope is not None:
        rules = rules.filter(scope)
    if tags := [value for value in chosen(params, "tag") if value.isdigit()]:
        rules = rules.filter(tags__id__in=tags)
    # both ticked is the same as none
    enabled = chosen(params, "enabled", ["yes", "no"])
    if len(enabled) == 1:
        rules = rules.filter(is_enabled=enabled[0] == "yes")
    origin = chosen(params, "origin", ["manual", "package"])
    if len(origin) == 1:
        rules = rules.filter(release_source__isnull=origin[0] == "manual")
    start, end = date_range(params, "created", TIME_DAYS, "", "created_from", "created_to")
    rules = in_range(rules, "created_at", start, end)
    if params.get("version", "").isdigit():
        rules = rules.filter(release_version_id=params["version"])
    q = params.get("q", "").strip()
    if q:
        rules = rules.filter(Q(identifier__icontains=q) | Q(description__icontains=q) | Q(tags__name__iexact=q))
    return rules.distinct()


@staff_required
def rules(request):
    require_perms(request, "view_rule")
    if remembered := remember_filters(request, "rules"):
        return remembered
    queryset, sort = sort_by(request, filter_rules(request, Rule.objects.all()), RULE_COLUMNS, "-created")
    page = paginate(request, queryset.select_related("release_source", "created_by")
                                     .prefetch_related("groups", "tags", "machines"))
    groups = Group.objects.order_by("name")
    facets = [
        Facet("type", gettext("Type"), choices=RuleType.choices),
        Facet("policy", gettext("Policy"), choices=Policy.choices),
        Facet("scope", gettext("Scope"), choices=[
            ("global", gettext("Global")), ("machines", gettext("Individual Macs")),
            *((group.pk, group.name) for group in groups)]),
        Facet("tag", gettext("Tag"), choices=Tag.objects.order_by("name").values_list("pk", "name")),
        Facet("origin", gettext("Origin"), choices=[("manual", gettext("Manual")),
                                                    ("package", gettext("From package rules"))]),
        Facet("enabled", gettext("Status"), choices=[("yes", gettext("Enabled")),
                                                      ("no", gettext("Disabled / waiting"))]),
        Facet("created", gettext("Created"), kind="time", choices=TIME_PRESETS, from_name="created_from",
              to_name="created_to"),
    ]
    return render(request, "console/rules/list.html", {
        "page": page, "sort": sort, "params": request.GET, "policies": Policy.choices,
        "groups": groups,
        **filter_bar(request, "rules", facets, hidden=("sort",),
                     placeholder=gettext("Identifier, description, tag…")),
    })


# a spreadsheet would run a cell starting with one of these as a formula
CSV_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def csv_cell(value):
    value = "" if value is None else str(value)
    return f"'{value}" if value.startswith(CSV_FORMULA_START) else value


@staff_required
def rules_export(request):
    """The rules of the list with its filters, all pages, as CSV"""
    require_perms(request, "view_rule")
    queryset, _sort = sort_by(request, filter_rules(request, Rule.objects.all()), RULE_COLUMNS, "-created")
    queryset = queryset.select_related("release_source", "created_by").prefetch_related("groups", "tags", "machines")
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="santa-rules-{timezone.localdate():%Y-%m-%d}.csv"'
    # the BOM makes Excel read UTF-8
    response.write("\ufeff")
    writer = csv.writer(response)
    writer.writerow([gettext("Type"), gettext("Identifier"), gettext("Policy"), gettext("Scope"), gettext("Tags"),
                     gettext("Enabled"), gettext("Package rule"), gettext("Comment"), gettext("Created"),
                     gettext("Created by")])
    for rule in queryset.iterator(chunk_size=500):
        if rule.is_global:
            scope = gettext("All Macs")
        else:
            scope = ", ".join([group.name for group in rule.groups.all()]
                              + [machine.hostname or machine.serial_number for machine in rule.machines.all()])
        writer.writerow([csv_cell(value) for value in (
            rule.get_rule_type_display(), rule.identifier, rule.get_policy_display(), scope,
            ", ".join(tag.name for tag in rule.tags.all()), gettext("yes") if rule.is_enabled else gettext("no"),
            rule.release_source.name if rule.release_source else "", rule.description,
            timezone.localtime(rule.created_at).strftime("%Y-%m-%d %H:%M"),
            rule.created_by.get_username() if rule.created_by else "",
        )])
    return response


@staff_required
@require_POST
def rules_bulk(request):
    require_perms(request, "view_rule", "change_rule")
    form = RuleBulkForm(request.POST)
    if not form.is_valid():
        for errors in form.errors.values():
            messages.error(request, " ".join(errors))
        return redirect(safe_next(request, "console:rules"))
    action = form.cleaned_data["action"]
    rule_ids = [rule.pk for rule in form.cleaned_data["rules"]]
    queryset = Rule.objects.filter(pk__in=rule_ids)
    rules_list = list(queryset)
    if action in ("enable", "disable"):
        count, cancelled = set_rules_enabled(queryset, action == "enable")
        log_change(request.user, rules_list, "Enabled" if action == "enable" else "Disabled")
        for release_version in cancelled:
            messages.warning(request, gettext("%(version)s: automatic approval cancelled.") % {
                "version": release_version})
        if action == "enable":
            message = ngettext("%(count)s rule enabled.", "%(count)s rules enabled.", count)
        else:
            message = ngettext("%(count)s rule disabled.", "%(count)s rules disabled.", count)
        messages.success(request, message % {"count": count})
    elif action in ("add_tag", "remove_tag"):
        name = form.cleaned_data["tag"].strip()
        if action == "add_tag":
            tag, _ = Tag.objects.get_or_create(name=name[:100])
            tag.rules.add(*rules_list)
            messages.success(request, ngettext("Tag “%(tag)s” added to %(count)s rule.",
                                               "Tag “%(tag)s” added to %(count)s rules.", len(rules_list))
                             % {"tag": tag, "count": len(rules_list)})
        else:
            tag = Tag.objects.filter(name=name).first()
            if tag:
                tag.rules.remove(*rules_list)
            messages.success(request, ngettext("Tag “%(tag)s” removed from %(count)s rule.",
                                               "Tag “%(tag)s” removed from %(count)s rules.", len(rules_list))
                             % {"tag": name, "count": len(rules_list)})
        log_change(request.user, rules_list, f"Tag {name} {'added' if action == 'add_tag' else 'removed'}")
    elif action == "set_policy":
        policy = Policy(form.cleaned_data["policy"])
        changed, skipped = set_rules_policy(rules_list, policy)
        log_change(request.user, changed, f"Policy set to {policy.value}")
        message = ngettext("%(count)s rule set to “%(policy)s”.", "%(count)s rules set to “%(policy)s”.", len(changed))
        messages.success(request, message % {"count": len(changed), "policy": policy.label})
        if skipped:
            messages.warning(request, gettext("The rules of package rules are managed by their package rule, "
                                              "change the policy there."))
    elif action == "add_groups":
        groups = list(form.cleaned_data["groups"])
        changed = [rule for rule in rules_list if not rule.is_global]
        for rule in changed:
            rule.groups.add(*groups)
        log_change(request.user, changed, f"Groups added: {', '.join(g.name for g in groups)}")
        message = ngettext("%(groups)s added to %(count)s rule.", "%(groups)s added to %(count)s rules.", len(changed))
        messages.success(request, message % {"groups": ", ".join(g.name for g in groups), "count": len(changed)})
    elif action == "delete":
        require_perms(request, "delete_rule")
        manual = [rule for rule in rules_list if rule.release_source_id is None]
        for rule in manual:
            log_deletion(request.user, rule)
            rule.delete()
        messages.success(request, ngettext("%(count)s rule deleted. The Macs remove it at their next sync.",
                                           "%(count)s rules deleted. The Macs remove them at their next sync.",
                                           len(manual)) % {"count": len(manual)})
        if len(manual) < len(rules_list):
            messages.warning(request, gettext("The rules of package rules are managed by their package rule, "
                                              "disable them instead."))
    return redirect(safe_next(request, "console:rules"))


@staff_required
@require_POST
def rule_toggle(request, pk):
    require_perms(request, "view_rule", "change_rule")
    rule = get_object_or_404(Rule, pk=pk)
    set_rules_enabled(Rule.objects.filter(pk=pk), not rule.is_enabled)
    rule.refresh_from_db()
    log_change(request.user, rule, "Enabled" if rule.is_enabled else "Disabled")
    if is_htmx(request):
        return render(request, "console/rules/_row.html", {"rule": rule})
    return redirect(safe_next(request, "console:rules"))


@staff_required
@require_POST
def rule_delete(request, pk):
    require_perms(request, "view_rule", "delete_rule")
    # the rules of a package rule are managed by it: disabled there, not deleted
    rule = get_object_or_404(Rule, pk=pk, release_source__isnull=True)
    log_deletion(request.user, rule)
    rule.delete()
    messages.success(request, gettext("Rule deleted: %(rule)s. The Macs remove it at their next sync.")
                     % {"rule": rule})
    return drawer_done(request, "console:rules")


@staff_required
def rules_existing(request):
    """The rules with the identifier typed in "New rule" (htmx partial)"""
    require_perms(request, "view_rule")
    identifier = request.GET.get("identifier", "").strip()
    rules = []
    if identifier and request.GET.get("rule_type") in RuleType.values:
        rules = (Rule.objects.filter(rule_type=request.GET["rule_type"], identifier__iexact=identifier)
                             .prefetch_related("groups", "machines")[:10])
    # "applies" only means something for a binary of an event
    return render(request, "console/rules/_existing.html", {"matches": [{"rule": rule, "applies": True}
                                                                        for rule in rules], "same_identifier": True})


@staff_required
def rule_form(request, pk=None):
    rule = get_object_or_404(Rule.objects.select_related("release_source", "release_version"), pk=pk) if pk else None
    require_perms(request, "view_rule", "change_rule" if rule else "add_rule")
    if rule and rule.release_source_id:
        # the package rule owns them, only enable / disable here
        return render_drawer(request, "console/rules/package_rule.html", "console/rules/drawer_package_rule.html",
                             {"rule": rule})
    initial = {}
    if rule is None and request.GET.get("machine", "").isdigit():
        # "New rule" on the page of a Mac
        initial["machines"] = Machine.objects.filter(pk=request.GET["machine"])
    form = RuleForm(request.POST or None, instance=rule, initial=initial)
    if request.method == "POST" and form.is_valid():
        created = rule is None
        rule = form.save(commit=False)
        if created:
            rule.created_by = request.user
        rule.save()
        form.save_m2m()
        rule.tags.add(*form.all_tags())
        if created:
            log_addition(request.user, rule)
        else:
            log_change(request.user, rule, changed_message(form))
        messages.success(request, gettext("Rule saved: %(rule)s. The Macs get it at their next sync.")
                         % {"rule": rule})
        return drawer_done(request, "console:rules")
    return render_drawer(request, "console/rules/form.html", "console/rules/drawer_form.html",
                         {"form": form, "rule": rule})


@staff_required
def rule_upload(request):
    require_perms(request, "view_rule", "add_rule", "change_rule")
    form = UploadBinaryForm(request.POST or None, request.FILES or None)
    found = None
    if request.method == "POST" and form.is_valid():
        upload = form.cleaned_data["file"]
        try:
            found = list(find_binaries(upload, upload.name, form.cleaned_data["binary_pattern"]))
        except ReleaseError as e:
            form.add_error("file", str(e))
        else:
            if not found:
                form.add_error("file", gettext("No Mach-O executable found in this file."))
            elif _rules_from_upload(request, form, found):
                return redirect("console:rules")
    return render(request, "console/rules/upload.html", {"form": form, "found": found})


def _rules_from_upload(request, form, found):
    data = form.cleaned_data
    identifiers = {}
    for path_in_file, info in found:
        for identifier in binary_identifiers(data["rule_type"], info):
            identifiers.setdefault(identifier, path_in_file)
    if not identifiers:
        form.add_error("rule_type", gettext("The file has no %(rule_type)s (unsigned or ad-hoc signed?). "
                                            "Use a binary rule instead.") % {"rule_type": data["rule_type"]})
        return 0
    created_count = 0
    for identifier, path_in_file in identifiers.items():
        rule, created = allow_identifier(
            data["rule_type"], identifier, data["policy"], data["is_global"], data["groups"], request.user,
            data["description"] or f"Upload {data['file'].name}: {path_in_file}", form.all_tags(),
        )
        if created:
            created_count += 1
            log_addition(request.user, rule, "Created from an uploaded file")
        else:
            log_change(request.user, rule, "Scope extended from an uploaded file")
    messages.success(request, ngettext("%(count)s rule saved (%(new)s new).", "%(count)s rules saved (%(new)s new).",
                                       len(identifiers)) % {"count": len(identifiers), "new": created_count})
    return len(identifiers)
