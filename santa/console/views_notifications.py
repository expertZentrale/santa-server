"""The bell in the top bar: the messages of the console kept for their user (message_storage.py)"""
from django.contrib.auth.decorators import login_required
from django.db.models import Count
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from ..models import UserNotification
from .utils import is_htmx, paginate, safe_next

PANEL_SIZE = 20


def _mark_read(user):
    user.console_notifications.filter(read_at=None).update(read_at=timezone.now())


@login_required
def notifications(request):
    """All of them, as a page (the full view of the bell)"""
    items = request.user.console_notifications.all()
    level = request.GET.get("level", "")
    if level in UserNotification.Level.values:
        items = items.filter(level=level)
    counts = dict(request.user.console_notifications.values_list("level").annotate(count=Count("pk"))
                  .order_by())
    page = paginate(request, items)
    _mark_read(request.user)
    return render(request, "console/notifications/page.html", {
        "page": page, "level": level,
        "levels": [(value, label, counts.get(value, 0)) for value, label in UserNotification.Level.choices],
        "total": sum(counts.values()),
    })


@login_required
@never_cache
def panel(request):
    """The latest ones, in the menu of the bell (htmx); opening it marks them as read"""
    items = list(request.user.console_notifications.all()[:PANEL_SIZE])
    total = request.user.console_notifications.count()
    _mark_read(request.user)
    return render(request, "console/notifications/_panel.html", {"items": items, "total": total})


@login_required
@require_POST
def dismiss(request, pk):
    get_object_or_404(UserNotification, pk=pk, user=request.user).delete()
    if not is_htmx(request):
        return redirect(safe_next(request, "notifications"))
    if request.POST.get("in_panel"):
        # the menu again: "No notifications." after the last one, the next ones move up
        response = panel(request)
        response["HX-Retarget"] = "[data-notifications-panel]"
        response["HX-Reswap"] = "innerHTML"
        return response
    items = request.user.console_notifications.all()
    if (level := request.POST.get("level", "")) in UserNotification.Level.values:
        items = items.filter(level=level)
    if not items.exists():
        # the full view (of a level): its empty state and counts
        return HttpResponse(headers={"HX-Refresh": "true"})
    return HttpResponse("")


@login_required
@require_POST
def clear(request):
    request.user.console_notifications.all().delete()
    if is_htmx(request):
        return panel(request)
    return redirect(safe_next(request, "notifications"))


@login_required
@never_cache
def unread(request):
    return JsonResponse({"unread": request.user.console_notifications.filter(read_at=None).count()})
