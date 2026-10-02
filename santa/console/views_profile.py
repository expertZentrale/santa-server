from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import QueryDict
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import translation
from django.views.decorators.http import require_POST

from ..models import SavedFilter, UserProfile
from ..users import gravatar_url, profile_for
from .filters import MAX_QUERY_LENGTH, MAX_SAVED_FILTERS, filter_query
from .forms import ProfileForm
from .utils import safe_next


@login_required
def profile(request):
    user_profile = profile_for(request.user)
    form = ProfileForm(request.POST or None, instance=user_profile)
    if request.method == "POST" and form.is_valid():
        form.save()
        _activate(request, user_profile)
        messages.success(request, translation.gettext("Preferences saved."))
        return redirect("profile")
    return render(request, "console/profile.html", {
        "form": form, "avatar_large": gravatar_url(request.user, 96),
        "groups": request.user.groups.order_by("name"),
    })


@login_required
@require_POST
def set_preference(request):
    """Theme or language from the user menu, back to the page"""
    user_profile = profile_for(request.user)
    theme = request.POST.get("theme")
    if theme in UserProfile.Theme.values:
        user_profile.theme = theme
    language = request.POST.get("language")
    if language is not None and language in {"", *dict(UserProfile._meta.get_field("language").choices)}:
        user_profile.language = language
    user_profile.save()
    _activate(request, user_profile)
    return redirect(safe_next(request, "home"))


def _activate(request, user_profile):
    if user_profile.language:
        translation.activate(user_profile.language)


@login_required
@require_POST
def save_filter(request):
    """The current filter of a list under a name: a new one, or the one of that name replaced"""
    page = request.POST.get("page", "").strip()[:32]
    name = request.POST.get("name", "").strip()[:100]
    query = filter_query(QueryDict(request.POST.get("query", "").lstrip("?")))
    back = safe_next(request, "home")
    if not page or not name:
        messages.error(request, translation.gettext("Enter a name for the view."))
        return redirect(back)
    if len(query) > MAX_QUERY_LENGTH:
        messages.error(request, translation.gettext("This view is too long to save."))
        return redirect(back)
    saved = SavedFilter.objects.filter(user=request.user, page=page)
    if not saved.filter(name=name).exists() and saved.count() >= MAX_SAVED_FILTERS:
        messages.error(request, translation.gettext("At most %(count)s saved views per list. Delete one first.")
                       % {"count": MAX_SAVED_FILTERS})
        return redirect(back)
    SavedFilter.objects.update_or_create(user=request.user, page=page, name=name, defaults={"query": query})
    messages.success(request, translation.gettext("View “%(name)s” saved.") % {"name": name})
    return redirect(back)


@login_required
@require_POST
def delete_filter(request, pk):
    saved_filter = get_object_or_404(SavedFilter, pk=pk, user=request.user)
    saved_filter.delete()
    messages.success(request, translation.gettext("View “%(name)s” deleted.") % {"name": saved_filter.name})
    return redirect(safe_next(request, "home"))


@login_required
@require_POST
def rename_filter(request, pk):
    saved_filter = get_object_or_404(SavedFilter, pk=pk, user=request.user)
    name = request.POST.get("name", "").strip()[:100]
    if not name:
        messages.error(request, translation.gettext("Enter a name for the view."))
    elif SavedFilter.objects.filter(user=request.user, page=saved_filter.page, name=name).exclude(pk=pk).exists():
        messages.error(request, translation.gettext("There is a view “%(name)s” already.") % {"name": name})
    else:
        saved_filter.name = name
        saved_filter.save(update_fields=["name"])
        messages.success(request, translation.gettext("View renamed to “%(name)s”.") % {"name": name})
    return redirect(safe_next(request, "home"))
