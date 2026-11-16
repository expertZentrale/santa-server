from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse, QueryDict
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import translation
from django.views.decorators.http import require_POST

from .. import notifications
from ..models import ListDefault, SavedFilter, UserProfile
from ..services import set_default_filter
from ..users import gravatar_url, profile_for
from .filters import FILTER_PAGES, MAX_QUERY_LENGTH, MAX_SAVED_FILTERS, clean_layout, filter_query
from .forms import ProfileForm
from .utils import log_addition, log_change, log_deletion, safe_next


@login_required
def profile(request):
    user_profile = profile_for(request.user)
    form = ProfileForm(request.POST or None, instance=user_profile, user=request.user)
    if request.method == "POST" and form.is_valid():
        form.save()
        _activate(request, user_profile)
        messages.success(request, translation.gettext("Preferences saved."))
        return redirect("profile")
    return render(request, "console/profile.html", {
        "form": form, "avatar_large": gravatar_url(request.user, 96),
        "groups": request.user.groups.order_by("name"),
        "mail_blocked": notifications.enabled() and notifications.blocked(request.user),
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
    if request.headers.get("X-Santa-Preference") == "1":
        # saved in the background (core.js): the page applies it itself
        return HttpResponse(status=204)
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
    # only the lists there are: the limit is per list
    if page not in FILTER_PAGES or (FILTER_PAGES[page] and not request.user.is_staff):
        messages.error(request, translation.gettext("Unknown list."))
        return redirect(back)
    if not name:
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
    SavedFilter.objects.update_or_create(user=request.user, page=page, name=name,
                                         defaults={"query": query, "layout": clean_layout(request.POST.get("layout"))})
    messages.success(request, translation.gettext("View “%(name)s” saved.") % {"name": name})
    return redirect(back)


@login_required
@require_POST
def default_filter(request):
    """The view a list opens with (from the menu, "Reset all"): a saved one, or without pk the built-in one"""
    page = request.POST.get("page", "").strip()[:32]
    if page not in FILTER_PAGES or (FILTER_PAGES[page] and not request.user.is_staff):
        messages.error(request, translation.gettext("Unknown list."))
        return redirect(safe_next(request, "home"))
    pk = request.POST.get("pk", "")
    chosen = get_object_or_404(SavedFilter, pk=pk, user=request.user, page=page) if pk.isdigit() else None
    set_default_filter(request.user, page, chosen)
    if chosen:
        messages.success(request, translation.gettext("“%(name)s” is now the default view.") % {"name": chosen.name})
    else:
        messages.success(request, translation.gettext("The standard view is now the default."))
    return redirect(safe_next(request, "home"))


@login_required
@require_POST
def delete_filter(request, pk):
    saved_filter = get_object_or_404(SavedFilter, pk=pk, user=request.user)
    saved_filter.delete()
    messages.success(request, translation.gettext("View “%(name)s” deleted.") % {"name": saved_filter.name})
    return redirect(safe_next(request, "home"))


@login_required
@require_POST
def update_filter(request, pk):
    """A saved view gets the current filter and columns of its list, under its name"""
    saved_filter = get_object_or_404(SavedFilter, pk=pk, user=request.user)
    query = filter_query(QueryDict(request.POST.get("query", "").lstrip("?")))
    if len(query) > MAX_QUERY_LENGTH:
        messages.error(request, translation.gettext("This view is too long to save."))
    else:
        saved_filter.query = query
        saved_filter.layout = clean_layout(request.POST.get("layout"))
        saved_filter.save(update_fields=["query", "layout"])
        messages.success(request, translation.gettext("View “%(name)s” updated.") % {"name": saved_filter.name})
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


def _list_default_page(request):
    """The console list of a POST to set its default for everyone: only with the permissions, never the request
    form of the users"""
    if not (request.user.is_staff and request.user.has_perms(["santa.add_listdefault", "santa.change_listdefault"])):
        raise PermissionDenied
    page = request.POST.get("page", "").strip()[:32]
    return page if FILTER_PAGES.get(page) else None


@login_required
@require_POST
def set_list_default(request):
    """The current filters, sort order and columns of a list: the view it opens with for everyone (a user's own
    default view still wins)"""
    page = _list_default_page(request)
    back = safe_next(request, "home")
    if page is None:
        messages.error(request, translation.gettext("Unknown list."))
        return redirect(back)
    query = filter_query(QueryDict(request.POST.get("query", "").lstrip("?")))
    if len(query) > MAX_QUERY_LENGTH:
        messages.error(request, translation.gettext("This view is too long to save."))
        return redirect(back)
    default, created = ListDefault.objects.update_or_create(page=page, defaults={
        "query": query, "layout": clean_layout(request.POST.get("layout")), "updated_by": request.user})
    if created:
        log_addition(request.user, default, f"Default view for everyone: {query}")
    else:
        log_change(request.user, default, f"Default view for everyone: {query}")
    messages.success(request, translation.gettext(
        "This view is now the default of the list for everyone. Users with their own default view keep it."))
    return redirect(back)


@login_required
@require_POST
def clear_list_default(request):
    """The list opens with its built-in defaults again for everyone"""
    if not request.user.has_perm("santa.delete_listdefault"):
        raise PermissionDenied
    page = _list_default_page(request)
    default = ListDefault.objects.filter(page=page).first() if page else None
    if default:
        log_deletion(request.user, default, "Default view for everyone removed")
        default.delete()
        messages.success(request, translation.gettext("The list opens with its built-in defaults again."))
    return redirect(safe_next(request, "home"))
