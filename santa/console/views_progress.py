from django.http import HttpResponse, JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from ..progress import cancel_progress, read_progress
from .utils import staff_required


@staff_required
@never_cache
def progress(request, progress_id):
    """How far a long request of the user is (asked by core.js while it waits); empty before it starts"""
    return JsonResponse(read_progress(request.user, progress_id) or {})


@staff_required
@require_POST
def cancel(request, progress_id):
    """Stop a long request of the user at its next step (the preview of a package rule)"""
    cancel_progress(request.user, progress_id)
    return HttpResponse(status=204)
