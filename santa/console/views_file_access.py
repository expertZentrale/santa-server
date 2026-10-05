"""File access rules (File Access Authorization): in the configuration profiles of the groups, not in the sync"""
from django.contrib import messages
from django.db import transaction
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext
from django.views.decorators.http import require_POST

from ..models import FileAccessRule, FileAccessRuleType, Group
from .filters import Facet, chosen, filter_bar, remember_filters
from .forms import FileAccessProcessFormSet, FileAccessRuleForm
from .utils import (
    changed_message,
    drawer_done,
    log_addition,
    log_change,
    log_deletion,
    paginate,
    render_drawer,
    require_perms,
    sort_by,
    staff_required,
)


def affected_groups(rule):
    return list(Group.objects.order_by("name") if rule.is_global else rule.groups.order_by("name"))


def profile_message(groups):
    """The groups whose profile changed: they have to download it again"""
    if not groups:
        return gettext("It applies to no group yet.")
    return gettext("Download the configuration profile of these groups again and replace it in your MDM: "
                   "%(groups)s.") % {"groups": ", ".join(group.name for group in groups)}


@staff_required
def file_access_rules(request):
    require_perms(request, "view_fileaccessrule")
    if remembered := remember_filters(request, "file-access"):
        return remembered
    queryset = FileAccessRule.objects.prefetch_related("groups").annotate(process_count=Count("processes"))
    if groups := [value for value in chosen(request.GET, "group") if value.isdigit()]:
        queryset = queryset.filter(Q(is_global=True) | Q(groups__id__in=groups)).distinct()
    if rule_types := chosen(request.GET, "type", FileAccessRuleType.values):
        queryset = queryset.filter(rule_type__in=rule_types)
    # both ticked is the same as none
    mode = chosen(request.GET, "mode", ["audit", "block"])
    if len(mode) == 1:
        queryset = queryset.filter(audit_only=mode[0] == "audit")
    q = request.GET.get("q", "").strip()
    if q:
        queryset = queryset.filter(Q(name__icontains=q) | Q(description__icontains=q) | Q(paths__icontains=q)
                                   | Q(path_prefixes__icontains=q))
    queryset, sort = sort_by(request, queryset, {"name": "name", "type": "rule_type", "changed": "updated_at"},
                             "name")
    return render(request, "console/file_access/list.html", {
        "page": paginate(request, queryset), "params": request.GET, "sort": sort,
        **filter_bar(request, "file-access", [
            Facet("group", gettext("Group"), choices=Group.objects.order_by("name").values_list("pk", "name")),
            Facet("type", gettext("Rule type"), choices=FileAccessRuleType.choices),
            Facet("mode", gettext("Mode"), choices=[("audit", gettext("Audit only")), ("block", gettext("Blocks"))]),
        ], hidden=("sort",), placeholder=gettext("Name, path…")),
    })


@staff_required
def file_access_rule_form(request, pk=None):
    rule = get_object_or_404(FileAccessRule, pk=pk) if pk else None
    require_perms(request, "view_fileaccessrule")
    can_change = request.user.has_perm("santa.change_fileaccessrule" if rule else "santa.add_fileaccessrule")
    form = FileAccessRuleForm(request.POST or None, instance=rule)
    formset = FileAccessProcessFormSet(request.POST or None, instance=form.instance, prefix="processes")
    if request.method == "POST":
        require_perms(request, "change_fileaccessrule" if rule else "add_fileaccessrule")
        # the formset checks the processes against the rule type of the form
        if form.is_valid() and formset.is_valid():
            created = rule is None
            before = affected_groups(rule) if rule else []
            with transaction.atomic():
                rule = form.save()
                formset.instance = rule
                formset.save()
            if created:
                log_addition(request.user, rule)
            else:
                message = changed_message(form)
                if formset.has_changed():
                    message += " Processes changed."
                log_change(request.user, rule, message)
            groups = sorted({*before, *affected_groups(rule)}, key=lambda group: group.name)
            messages.success(request, gettext("File access rule %(rule)s saved.") % {"rule": rule} + " "
                             + profile_message(groups))
            return drawer_done(request, "console:file_access_rules")
    return render_drawer(request, "console/file_access/form.html", "console/file_access/drawer_form.html", {
        "form": form, "formset": formset, "rule": rule, "can_change": can_change,
    })


@staff_required
@require_POST
def file_access_rule_delete(request, pk):
    require_perms(request, "view_fileaccessrule", "delete_fileaccessrule")
    rule = get_object_or_404(FileAccessRule, pk=pk)
    groups = affected_groups(rule)
    log_deletion(request.user, rule)
    rule.delete()
    messages.success(request, gettext("File access rule %(rule)s deleted.") % {"rule": rule} + " "
                     + profile_message(groups))
    return redirect("console:file_access_rules")
