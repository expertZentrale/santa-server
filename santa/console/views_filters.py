"""The searches of the "lookup" filters (filters.Facet kind "lookup"): Macs and users, too many to list in a chip"""
from datetime import timedelta

from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.cache import never_cache

from ..models import Event, Machine
from .utils import require_perms, staff_required

LOOKUP_LIMIT = 20
USER_DAYS = 90
# the parameter the hits are ticked into: one of the lookup filters, never anything else of the form
LOOKUP_NAMES = ("machine", "user")


# a filter is a link: never more values than one query takes (SQL Server: 2100 parameters)
MAX_LOOKUP_VALUES = 500


def machine_label(machine):
    return machine.hostname or machine.serial_number or machine.machine_id


def machine_choices(params):
    """The labels of the Macs of a filter (chosen by their pk); one that is gone shows its pk"""
    pks = [value for value in params.getlist("machine") if value.isdigit()][:MAX_LOOKUP_VALUES]
    found = Machine.objects.in_bulk([int(pk) for pk in pks]) if pks else {}
    return [(pk, machine_label(found[int(pk)]) if int(pk) in found else pk) for pk in dict.fromkeys(pks)]


def _name(request):
    name = request.GET.get("name", "")
    return name if name in LOOKUP_NAMES else LOOKUP_NAMES[0]


def _chosen(request):
    """The values ticked in the chip (sent with the search): they stay on top, ticked, whatever is searched"""
    return list(dict.fromkeys(value for value in request.GET.getlist(_name(request)) if value))[:MAX_LOOKUP_VALUES]


def _render(request, chosen, hits):
    """chosen and hits: [(value, label, note)]; the hits without the chosen ones"""
    values = {str(value) for value, _label, _note in chosen}
    options = [(value, label, note, True) for value, label, note in chosen]
    options += [(value, label, note, False) for value, label, note in hits if str(value) not in values]
    return render(request, "console/widgets/_lookup_options.html", {"options": options, "name": _name(request)})


@staff_required
@never_cache
def filter_macs(request):
    require_perms(request, "view_machine")
    q = request.GET.get("q", "").strip()
    machines = Machine.objects.order_by("hostname", "serial_number")
    if q:
        machines = machines.filter(Q(hostname__icontains=q) | Q(serial_number__icontains=q)
                                   | Q(primary_user__icontains=q))
    pks = [int(value) for value in _chosen(request) if value.isdigit()]
    found = Machine.objects.in_bulk(pks) if pks else {}
    chosen = [(pk, machine_label(found[pk]), found[pk].primary_user) if pk in found else (pk, str(pk), "")
              for pk in pks]
    return _render(request, chosen, [(machine.pk, machine_label(machine), machine.primary_user)
                                     for machine in machines.exclude(pk__in=pks)[:LOOKUP_LIMIT]])


@staff_required
@never_cache
def filter_users(request):
    """events: the users who ran the binaries; machines: the owners of the Macs (MachineOwner)"""
    q = request.GET.get("q", "").strip()
    if request.GET.get("source") == "events":
        require_perms(request, "view_event")
        # the users of the recent events: a DISTINCT over every event ever would read the whole table
        values = Event.objects.exclude(executing_user="").filter(
            execution_time__gte=timezone.now() - timedelta(days=USER_DAYS))
        field = "executing_user"
    else:
        if not (request.user.has_perm("santa.view_machine") or request.user.has_perm("santa.view_rule")):
            raise PermissionDenied
        values = Machine.objects.exclude(primary_user="")
        field = "primary_user"
    if q:
        values = values.filter(**{f"{field}__icontains": q})
    chosen = _chosen(request)
    users = values.exclude(**{f"{field}__in": chosen}).order_by(field).values_list(field, flat=True).distinct()
    return _render(request, [(user, user, "") for user in chosen],
                   [(user, user, "") for user in users[:LOOKUP_LIMIT]])
