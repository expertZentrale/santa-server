from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.utils import translation
from django.views.decorators.http import require_POST

from ..models import UserProfile
from ..users import gravatar_url, profile_for
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
