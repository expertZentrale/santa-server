from .users import gravatar_url, profile_for


def ui(request):
    """Theme and picture of the signed-in user, for the base template"""
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return {"ui_theme": "auto"}
    return {"ui_theme": profile_for(user).theme, "avatar_url": gravatar_url(user, 32)}
