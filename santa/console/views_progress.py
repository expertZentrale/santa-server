from django.http import JsonResponse
from django.views.decorators.cache import never_cache

from ..progress import read_progress
from .utils import staff_required


@staff_required
@never_cache
def progress(request, progress_id):
    """How far a long request of the user is (asked by core.js while it waits); empty before it starts"""
    return JsonResponse(read_progress(request.user, progress_id) or {})
