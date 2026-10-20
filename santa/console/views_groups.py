from datetime import timedelta

from django.contrib import messages
from django.db.models import Max, Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify
from django.utils.translation import gettext, ngettext
from django.views.decorators.http import require_POST

from ..models import NEEDS_RULE, AccessRequest, ClientMode, Event, Group, Machine, Rule, generate_sync_token
from ..profiles import base_profile, file_access_rules, group_profile
from ..rules import GLOBAL, GROUP, LIST_FIELDS, MACHINE, PARENT, effective_rule_values
from ..services import (
    can_see_sync_token,
    groups_prefetch,
    related_aggregate,
    related_count,
    remove_machine_from_rules,
    rules_only_for,
)
from .filters import Facet, any_of, chosen, filter_bar, remember_filters
from .forms import GroupForm, MachineModeForm
from .utils import (
    changed_message,
    defer_rows,
    drawer_done,
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

# a Mac that has not synced for this long is shown as stale
STALE_AFTER = timedelta(days=2)
# the rules of one page at most, and far below the 2100 parameters of SQL Server
MAX_BULK = 500


def mobileconfig_response(content, filename):
    response = HttpResponse(content, content_type="application/x-apple-aspen-config")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


# Groups


@staff_required
def groups(request):
    require_perms(request, "view_group")
    if remembered := remember_filters(request, "groups"):
        return remembered
    queryset = Group.objects.select_related("parent").annotate(
        machine_count=related_count(Machine.objects.all(), "group"),
        rule_count=related_count(Rule.groups.through.objects.all(), "group"),
        last_sync=related_aggregate(Machine.objects.all(), "group", Max("last_postflight_at")))
    if modes := chosen(request.GET, "mode", ClientMode.values):
        # the mode the Macs get: a group based on another one can inherit it (overridden_settings is a document)
        queryset = queryset.filter(pk__in=[group.pk for group in Group.objects.select_related("parent")
                                           if group.effective().client_mode in modes])
    q = request.GET.get("q", "").strip()
    if q:
        queryset = queryset.filter(Q(name__icontains=q) | Q(description__icontains=q))
    queryset, sort = sort_by(request, queryset, {"name": "name", "macs": "machine_count", "rules": "rule_count",
                                                 "sync": "last_sync"}, "name")
    return render(request, "console/groups/list.html", {
        "page": paginate(request, queryset), "sort": sort, "params": request.GET,
        **filter_bar(request, "groups", [Facet("mode", gettext("Mode"), choices=ClientMode.choices)],
                     hidden=("sort",), placeholder=gettext("Name, description…")),
    })


def profile_changed_children(group, form):
    """The groups based on the group whose profile changed with it: they take a changed profile setting"""
    changed = {key for key in form.changed_settings() if key.startswith(form.PROFILE_FIELDS)}
    return [child for child in group.children.order_by("name") if changed & set(child.inherited_settings())]


@staff_required
def group_form(request, pk=None):
    group = get_object_or_404(Group, pk=pk) if pk else None
    require_perms(request, "view_group")
    can_change = request.user.has_perm("santa.change_group" if group else "santa.add_group")
    form = GroupForm(request.POST or None, request.FILES or None, instance=group)
    if request.method == "POST":
        require_perms(request, "change_group" if group else "add_group")
        if form.is_valid():
            created = group is None
            group = form.save()
            if created:
                log_addition(request.user, group)
                messages.success(request, gettext("Group %(group)s created. Download its configuration profile below.")
                                 % {"group": group})
            else:
                log_change(request.user, group, changed_message(form))
                message = gettext("Group %(group)s saved.") % {"group": group} + " "
                if form.profile_changed():
                    message += gettext("Profile settings changed: download the profile again and replace it in "
                                       "your MDM.")
                else:
                    message += gettext("The Macs get the changes at their next sync.")
                if children := profile_changed_children(group, form):
                    message += " " + gettext("The profile of the groups based on it changed too: %(groups)s.") % {
                        "groups": ", ".join(child.name for child in children)}
                messages.success(request, message)
            return drawer_done(request, reverse("console:group", args=[group.pk]), open_in_drawer=created)
    context = {"form": form, "group": group, "can_change": can_change,
               "can_see_sync_token": can_see_sync_token(request.user)}
    if group:
        machines = group.machines.all()
        context.update({
            "machine_count": machines.count(),
            "stale_count": machines.filter(Q(last_postflight_at__lt=timezone.now() - STALE_AFTER)
                                           | Q(last_postflight_at__isnull=True)).count(),
            "lockdown_count": machines.filter(client_mode=ClientMode.LOCKDOWN).count(),
            "rule_count": group.rules.count(),
            "children": group.children.order_by("name"),
            "file_access_count": file_access_rules(group).count(),
            "open_blocks": Event.objects.filter(NEEDS_RULE, group=group, resolved_at__isnull=True).count(),
        })
    return render_drawer(request, "console/groups/form.html", "console/groups/drawer_form.html", context)


@staff_required
@require_POST
def group_regenerate_token(request, pk):
    require_perms(request, "change_group")
    group = get_object_or_404(Group, pk=pk)
    group.sync_token = generate_sync_token()
    group.save(update_fields=["sync_token", "updated_at"])
    log_change(request.user, group, "Regenerated the sync token")
    messages.warning(request, gettext("New sync URL for %(group)s. Download the profile again and replace it in "
                                      "your MDM: the Macs with the old profile can no longer sync.")
                     % {"group": group})
    return redirect("console:group", pk=group.pk)


@staff_required
@require_POST
def group_delete(request, pk):
    require_perms(request, "delete_group")
    group = get_object_or_404(Group, pk=pk)
    if group.machines.exists():
        messages.error(request, gettext("%(group)s still has Macs. Move them to another group first "
                                        "(new configuration profile), then delete it.") % {"group": group})
        return redirect("console:group", pk=group.pk)
    if group.children.exists():
        messages.error(request, gettext("Other groups are based on %(group)s. Choose another group for them first, "
                                        "then delete it.") % {"group": group})
        return redirect("console:group", pk=group.pk)
    log_deletion(request.user, group)
    group.delete()
    messages.success(request, gettext("Group %(group)s deleted.") % {"group": group})
    return redirect("console:groups")


@staff_required
def group_profile_download(request, pk):
    # the profile contains the sync token
    require_perms(request, "view_group", "change_group")
    group = get_object_or_404(Group, pk=pk)
    return mobileconfig_response(group_profile(group), f"santa-{slugify(group.name) or group.pk}.mobileconfig")


@staff_required
def base_profile_download(request):
    require_perms(request, "view_group")
    return mobileconfig_response(base_profile(), "santa-base.mobileconfig")


# Macs


def filter_machines(request, machines):
    params = request.GET
    q = params.get("q", "").strip()
    if q:
        machines = machines.filter(Q(serial_number__icontains=q) | Q(hostname__icontains=q)
                                   | Q(primary_user__icontains=q) | Q(machine_id__iexact=q))
    if groups := [value for value in chosen(params, "group") if value.isdigit()]:
        machines = machines.filter(group_id__in=groups)
    if modes := chosen(params, "mode", ClientMode.values):
        machines = machines.filter(client_mode__in=modes)
    statuses = {"stale": Q(last_postflight_at__lt=timezone.now() - STALE_AFTER) | Q(last_postflight_at__isnull=True),
                "clean": Q(clean_sync_requested=True)}
    status = any_of([statuses[value] for value in chosen(params, "status", statuses)])
    if status is not None:
        machines = machines.filter(status)
    return machines


@staff_required
def machines(request):
    require_perms(request, "view_machine")
    if remembered := remember_filters(request, "machines"):
        return remembered
    queryset, sort = sort_by(request, filter_machines(request, Machine.objects.select_related("group")), {
        "mac": ["hostname", "serial_number"], "user": "primary_user", "group": "group__name", "mode": "client_mode",
        "santa": "santa_version", "sync": "last_postflight_at",
    }, "mac")
    page = None if defer_rows(request) else paginate(request, queryset)
    stale_before = timezone.now() - STALE_AFTER
    for machine in page or []:
        machine.is_stale = not machine.last_postflight_at or machine.last_postflight_at < stale_before
    return render_list(request, "console/machines/list.html", "console/machines/_rows.html", {
        "page": page, "sort": sort, "params": request.GET,
        **filter_bar(request, "machines", [
            Facet("group", gettext("Group"), choices=Group.objects.order_by("name").values_list("pk", "name")),
            Facet("mode", gettext("Mode"), choices=ClientMode.choices),
            Facet("status", gettext("Status"), choices=[("stale", gettext("Not synced for 2 days")),
                                                        ("clean", gettext("Clean sync pending"))]),
        ], hidden=("sort",), placeholder=gettext("Hostname, serial number, user…")),
    })


MACHINE_RULE_SCOPES = {"mac": MACHINE, "group": GROUP, "parent": PARENT, "global": GLOBAL}


def machine_rules(machine, effective, params):
    """The rules that reach the Mac (the winning one per identifier), plus the disabled or overridden rules
    that list the Mac itself: those can still be removed from it. As [(scope level, row, wins)] of LIST_FIELDS
    tuples, sorted: thousands of rules, models only for the page shown (rules_of_page)."""
    winners = {row[0] for _level, row in effective.values()}
    rows = {row[0]: (level, row) for level, row in effective.values()}
    for row in Rule.objects.filter(machines=machine).values_list(*LIST_FIELDS):
        rows.setdefault(row[0], (MACHINE, row))
    level = MACHINE_RULE_SCOPES.get(params.get("rules"))
    q = params.get("q", "").strip().lower()
    description, created_at = LIST_FIELDS.index("description"), LIST_FIELDS.index("created_at")
    result = [(rule_level, row, row[0] in winners) for rule_level, row in rows.values()
              if (level is None or rule_level == level)
              and (not q or q in row[2].lower() or q in row[description].lower())]
    result.sort(key=lambda item: (-item[0], -item[1][created_at].timestamp(), -item[1][0]))
    return result


def rules_of_page(items):
    """The rules of a page of machine_rules(), as models with their scope_level and wins"""
    rules = Rule.objects.prefetch_related("tags", groups_prefetch()).in_bulk([row[0] for _level, row, _wins in items])
    page = []
    for rule_level, row, wins in items:
        rule = rules[row[0]]
        rule.scope_level, rule.wins = rule_level, wins
        page.append(rule)
    return page


@staff_required
def machine_detail(request, pk):
    require_perms(request, "view_machine")
    machine = get_object_or_404(Machine.objects.select_related("group__parent"), pk=pk)
    effective = effective_rule_values(machine, LIST_FIELDS)
    synced = machine.synced_rules or {}
    page = paginate(request, machine_rules(machine, effective, request.GET))
    page.object_list = rules_of_page(page.object_list)
    on_mac = set(machine.rules.values_list("pk", flat=True))
    for rule in page:
        rule.on_mac = rule.pk in on_mac
        rule.in_group = any(group.pk == machine.group_id for group in rule.groups.all())
    context = {
        "machine": machine,
        # inherited from a parent group too
        "group_mode": machine.group.effective().get_client_mode_display(),
        "expected_count": len(effective),
        "synced_count": len(synced),
        "missing_count": len(set(effective) - set(synced)),
        "extra_count": len(set(synced) - set(effective)),
        "is_stale": not machine.last_postflight_at or machine.last_postflight_at < timezone.now() - STALE_AFTER,
        "page": page,
        "params": request.GET,
        "levels": {"machine": MACHINE, "group": GROUP, "parent": PARENT, "global": GLOBAL},
        "events": (Event.objects.filter(machine=machine).order_by("-execution_time")[:15]),
        "open_blocks": Event.objects.filter(NEEDS_RULE, machine=machine, resolved_at__isnull=True).count(),
        "requests": AccessRequest.objects.filter(machine=machine).select_related("requester")[:10],
    }
    return render_drawer(request, "console/machines/detail.html", "console/machines/drawer_detail.html", context)


@staff_required
@require_POST
def machine_rules_remove(request, pk):
    require_perms(request, "view_rule", "change_rule")
    machine = get_object_or_404(Machine, pk=pk)
    rule_ids = [value for value in request.POST.getlist("rules") if value.isdigit()][:MAX_BULK]
    rules = list(machine.rules.filter(pk__in=rule_ids))
    orphans = rules_only_for(machine, rules)
    if orphans and not request.user.has_perm("santa.delete_rule"):
        messages.warning(request, ngettext(
            "%(count)s rule only applies to this Mac: removing the Mac would delete it, which needs the permission "
            "to delete rules. It was kept.",
            "%(count)s rules only apply to this Mac: removing the Mac would delete them, which needs the permission "
            "to delete rules. They were kept.", len(orphans)) % {"count": len(orphans)})
        rules = [rule for rule in rules if rule not in orphans]
        orphans = []
    narrowed = [rule for rule in rules if rule not in orphans]
    remove_machine_from_rules(machine, narrowed)
    log_change(request.user, narrowed, f"Removed from the Mac {machine}")
    for rule in orphans:
        log_deletion(request.user, rule, f"Deleted in the console: removed from its only Mac {machine}")
        rule.delete()
    if narrowed:
        messages.success(request, ngettext(
            "%(mac)s removed from %(count)s rule. Its group and global scopes stay, the Mac gets the change at its "
            "next sync.",
            "%(mac)s removed from %(count)s rules. Their group and global scopes stay, the Mac gets the change at its "
            "next sync.",
            len(narrowed)) % {"mac": machine, "count": len(narrowed)})
    if orphans:
        messages.success(request, ngettext("%(count)s rule that only applied to this Mac deleted.",
                                           "%(count)s rules that only applied to this Mac deleted.",
                                           len(orphans)) % {"count": len(orphans)})
    return redirect(safe_next(request, reverse("console:machine", args=[machine.pk])))


@staff_required
def machine_mode(request, pk):
    """The mode of one Mac instead of its group's (Monitor / Lockdown), for good or until a date"""
    require_perms(request, "change_machine")
    machine = get_object_or_404(Machine.objects.select_related("group__parent"), pk=pk)
    group_mode = machine.group.effective().get_client_mode_display()
    form = MachineModeForm(request.POST or None, instance=machine, group_mode=group_mode)
    if request.method == "POST" and form.is_valid():
        machine = form.save()
        log_change(request.user, machine, changed_message(form))
        if machine.client_mode_override:
            text = gettext("%(machine)s: %(mode)s from its next sync on") % {
                "machine": machine, "mode": machine.get_client_mode_override_display()}
        else:
            text = gettext("%(machine)s: the mode of its group from its next sync on") % {"machine": machine}
        messages.success(request, text)
        return drawer_done(request, reverse("console:machine", args=[machine.pk]))
    return render_drawer(request, "console/machines/mode_form.html", "console/machines/drawer_mode_form.html",
                         {"machine": machine, "form": form})


@staff_required
@require_POST
def machine_clean_sync(request, pk):
    require_perms(request, "change_machine")
    machine = get_object_or_404(Machine, pk=pk)
    machine.clean_sync_requested = True
    machine.save(update_fields=["clean_sync_requested"])
    log_change(request.user, machine, "Clean sync requested")
    messages.success(request, gettext("%(machine)s: clean sync at the next sync (the Mac drops and downloads all its "
                                      "rules).") % {"machine": machine})
    return redirect("console:machine", pk=machine.pk)
