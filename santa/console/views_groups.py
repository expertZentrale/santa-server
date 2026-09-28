from datetime import timedelta

from django.contrib import messages
from django.db.models import Count, Max, Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.text import slugify
from django.utils.translation import gettext
from django.views.decorators.http import require_POST

from ..models import AccessRequest, ClientMode, Event, Group, Machine, Rule, generate_sync_token
from ..profiles import base_profile, group_profile
from ..rules import effective_rules
from ..services import can_see_sync_token
from .forms import GroupForm
from .utils import log_addition, log_change, log_deletion, paginate, require_perms, sort_by, staff_required

# a Mac that has not synced for this long is shown as stale
STALE_AFTER = timedelta(days=2)


def mobileconfig_response(content, filename):
    response = HttpResponse(content, content_type="application/x-apple-aspen-config")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


# Groups


@staff_required
def groups(request):
    require_perms(request, "view_group")
    queryset = Group.objects.annotate(machine_count=Count("machines", distinct=True),
                                      rule_count=Count("rules", distinct=True),
                                      last_sync=Max("machines__last_postflight_at"))
    queryset, sort = sort_by(request, queryset, {"name": "name", "macs": "machine_count", "rules": "rule_count",
                                                 "sync": "last_sync"}, "name")
    return render(request, "console/groups/list.html", {"groups": queryset, "sort": sort})


@staff_required
def group_form(request, pk=None):
    group = get_object_or_404(Group, pk=pk) if pk else None
    require_perms(request, "view_group")
    can_change = request.user.has_perm("santa.change_group" if group else "santa.add_group")
    form = GroupForm(request.POST or None, instance=group)
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
                log_change(request.user, group, f"Changed in the console: {', '.join(form.changed_data)}")
                message = gettext("Group %(group)s saved.") % {"group": group} + " "
                if any(name in form.changed_data for name in GroupForm.SECTIONS[-1][2]):
                    message += gettext("Profile settings changed: download the profile again and replace it in "
                                       "your MDM.")
                else:
                    message += gettext("The Macs get the changes at their next sync.")
                messages.success(request, message)
            return redirect("console:group", pk=group.pk)
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
            "open_blocks": Event.objects.filter(group=group, resolved_at__isnull=True,
                                                decision__startswith="BLOCK_").count(),
        })
    return render(request, "console/groups/form.html", context)


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
    if params.get("group", "").isdigit():
        machines = machines.filter(group_id=params["group"])
    if params.get("mode") in ClientMode.values:
        machines = machines.filter(client_mode=params["mode"])
    if params.get("status") == "stale":
        machines = machines.filter(Q(last_postflight_at__lt=timezone.now() - STALE_AFTER)
                                   | Q(last_postflight_at__isnull=True))
    elif params.get("status") == "clean":
        machines = machines.filter(clean_sync_requested=True)
    return machines


@staff_required
def machines(request):
    require_perms(request, "view_machine")
    queryset, sort = sort_by(request, filter_machines(request, Machine.objects.select_related("group")), {
        "mac": ["hostname", "serial_number"], "user": "primary_user", "group": "group__name", "mode": "client_mode",
        "santa": "santa_version", "sync": "last_postflight_at",
    }, "mac")
    page = paginate(request, queryset)
    stale_before = timezone.now() - STALE_AFTER
    for machine in page:
        machine.is_stale = not machine.last_postflight_at or machine.last_postflight_at < stale_before
    return render(request, "console/machines/list.html", {
        "page": page, "sort": sort, "params": request.GET, "groups": Group.objects.order_by("name"),
        "modes": ClientMode.choices,
    })


@staff_required
def machine_detail(request, pk):
    require_perms(request, "view_machine")
    machine = get_object_or_404(Machine.objects.select_related("group"), pk=pk)
    expected = effective_rules(machine)
    synced = machine.synced_rules or {}
    context = {
        "machine": machine,
        "expected_count": len(expected),
        "synced_count": len(synced),
        "missing_count": len(set(expected) - set(synced)),
        "extra_count": len(set(synced) - set(expected)),
        "is_stale": not machine.last_postflight_at or machine.last_postflight_at < timezone.now() - STALE_AFTER,
        "rules": Rule.objects.filter(machines=machine).prefetch_related("tags").order_by("-created_at"),
        "events": (Event.objects.filter(machine=machine).order_by("-execution_time")[:15]),
        "open_blocks": Event.objects.filter(machine=machine, resolved_at__isnull=True,
                                            decision__startswith="BLOCK_").count(),
        "requests": AccessRequest.objects.filter(machine=machine).select_related("requester")[:10],
    }
    return render(request, "console/machines/detail.html", context)


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
