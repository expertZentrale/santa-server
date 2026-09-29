"""The history of an object: the admin log entries the console and the admin write for every change"""
from django.contrib.admin.models import LogEntry
from django.contrib.auth.models import Group as AuthGroup
from django.contrib.auth.models import User
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.shortcuts import get_object_or_404
from django.urls import reverse

from ..models import Group, Machine, ReleaseSource, Rule, SignInGroup, Tag
from .utils import paginate, render_drawer, staff_required

# URL name: (model, permission to view it, URL name of its page, whether the page opens in the drawer too)
HISTORY_MODELS = {
    "rule": (Rule, "santa.view_rule", "console:rule", True),
    "releasesource": (ReleaseSource, "santa.view_releasesource", "console:source", True),
    "group": (Group, "santa.view_group", "console:group", False),
    "machine": (Machine, "santa.view_machine", "console:machine", False),
    "signingroup": (SignInGroup, "santa.view_signingroup", "console:admin_sign_in_group", False),
    "tag": (Tag, "santa.view_tag", "console:admin_tag", False),
    "role": (AuthGroup, "auth.view_group", "console:admin_role", False),
    "user": (User, "auth.view_user", "console:admin_user", False),
}


@staff_required
def history(request, model, pk):
    if model not in HISTORY_MODELS:
        raise Http404
    model_class, perm, url_name, has_drawer = HISTORY_MODELS[model]
    if not request.user.has_perm(perm):
        raise PermissionDenied
    obj = get_object_or_404(model_class, pk=pk)
    entries = (LogEntry.objects.filter(content_type=ContentType.objects.get_for_model(model_class), object_id=str(pk))
                               .select_related("user").order_by("-action_time", "-pk"))
    return render_drawer(request, "console/history/page.html", "console/history/drawer.html", {
        "obj": obj, "page": paginate(request, entries), "object_url": reverse(url_name, args=[pk]),
        "object_in_drawer": has_drawer,
    })
