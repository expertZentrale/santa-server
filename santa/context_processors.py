from django.conf import settings
from django.templatetags.static import static

from .seasonal import christmas_active
from .users import gravatar_url, profile_for


def favicon_url():
    return settings.SANTA_FAVICON_URL or static("santa/favicon.svg")


def ui(request):
    """Name and favicon of the server, the Christmas theme, theme and picture of the signed-in user, for the base
    template"""
    context = {"server_name": settings.SANTA_SERVER_NAME, "favicon_url": favicon_url(),
               "default_favicon": not settings.SANTA_FAVICON_URL, "christmas": christmas_active()}
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return {**context, "ui_theme": "auto"}
    return {**context, "ui_theme": profile_for(user).theme, "avatar_url": gravatar_url(user, 32)}
