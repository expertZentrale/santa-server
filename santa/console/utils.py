from functools import wraps

from django.contrib.admin.models import ADDITION, CHANGE, DELETION, LogEntry
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.utils.http import url_has_allowed_host_and_scheme

PAGE_SIZE = 50


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
    if not request.user.has_perms([f"santa.{perm}" for perm in perms]):
        raise PermissionDenied


def is_htmx(request):
    return request.headers.get("HX-Request") == "true"


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


def log_deletion(user, obj, message="Deleted in the console"):
    _log(user, [obj], DELETION, message)


def paginate(request, queryset, per_page=PAGE_SIZE):
    return Paginator(queryset, per_page).get_page(request.GET.get("page"))


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
