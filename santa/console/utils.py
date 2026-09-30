from functools import wraps

from django.contrib.admin.models import ADDITION, CHANGE, DELETION, LogEntry
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.utils.http import url_has_allowed_host_and_scheme

PAGE_SIZE = 50
PAGE_SIZES = (25, 50, 100, 200)


def staff_required(view):
    """The console is for the administrators (OIDC role, or local staff accounts)"""
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path())
        if not (request.user.is_active and request.user.is_staff):
            raise PermissionDenied
        return view(request, *args, **kwargs)
    return wrapper


def require_perms(request, *perms):
    """The permissions of Santa by codename ("add_rule"), others with their app ("auth.change_user")"""
    if not request.user.has_perms([perm if "." in perm else f"santa.{perm}" for perm in perms]):
        raise PermissionDenied


def is_htmx(request):
    return request.headers.get("HX-Request") == "true"


def render_drawer(request, page_template, drawer_template, context):
    """The side drawer for the links with hx-target="#drawer", the full page for everything else"""
    return render(request, drawer_template if is_htmx(request) else page_template, context)


def drawer_done(request, url):
    """After a form was saved in the drawer: console.js goes back to the previous view of the drawer and updates it,
    or reloads the page behind it (both show the change and the message)"""
    if is_htmx(request):
        response = HttpResponse(status=204)
        response["HX-Trigger"] = "drawerSaved"
        return response
    return redirect(url)


def _log(user, objects, flag, message):
    objects = [obj for obj in objects if obj is not None]
    if not objects:
        return
    model = type(objects[0])
    pks = [obj.pk for obj in objects]
    # SQL Server: at most 2100 parameters per query
    for start in range(0, len(pks), 1000):
        queryset = model._default_manager.filter(pk__in=pks[start:start + 1000])
        LogEntry.objects.log_actions(user.pk, queryset, flag, change_message=message,
                                     single_object=len(objects) == 1)


def log_addition(user, obj, message="Added in the console"):
    _log(user, [obj], ADDITION, message)


def log_change(user, objects, message):
    """Log a change for one object or a list of objects of the same model"""
    _log(user, objects if isinstance(objects, (list, tuple)) else [objects], CHANGE, message)


def changed_message(form):
    """The log message of a saved console form: the fields that changed"""
    if not form.changed_data:
        return "No fields changed."
    return f"Changed in the console: {', '.join(form.changed_data)}"


def log_deletion(user, obj, message="Deleted in the console"):
    _log(user, [obj], DELETION, message)


def page_size(request):
    """The ?per_page= of the request, else the last one of the session: one size for every list"""
    try:
        size = int(request.GET.get("per_page", ""))
    except ValueError:
        size = None
    if size in PAGE_SIZES:
        request.session["per_page"] = size
        return size
    size = request.session.get("per_page")
    return size if size in PAGE_SIZES else PAGE_SIZE


def paginate(request, queryset, per_page=None):
    """A page of the queryset, with the page size the user chose (or a fixed per_page)"""
    page = Paginator(queryset, per_page or page_size(request)).get_page(request.GET.get("page"))
    page.page_sizes = PAGE_SIZES
    return page


def safe_next(request, default):
    """The ?next= URL if it points to this site, else the default"""
    next_url = request.POST.get("next") or request.GET.get("next")
    if next_url and url_has_allowed_host_and_scheme(next_url, {request.get_host()}, request.is_secure()):
        return next_url
    return default


def sort_by(request, queryset, columns, default, tiebreak="pk"):
    """Order by the ?sort= column ("name" or "-name"), only one of the columns: {name: field or [fields]}

    tiebreak makes the order stable for the pagination (for a GROUP BY: one of the grouped fields).
    """
    key = request.GET.get("sort") or default
    if key.lstrip("-") not in columns:
        key = default
    fields = columns[key.lstrip("-")]
    fields = fields if isinstance(fields, (list, tuple)) else [fields]
    prefix = "-" if key.startswith("-") else ""
    return queryset.order_by(*[prefix + field for field in fields], prefix + tiebreak), key
