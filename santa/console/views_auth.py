from django.conf import settings
from django.contrib.auth import views as auth_views
from django.shortcuts import redirect

from ..context_processors import favicon_url


def favicon(request):
    # browsers ask for /favicon.ico on pages without a <link>, e.g. the JSON ones
    return redirect(favicon_url())


def home(request):
    if not request.user.is_authenticated:
        return redirect("login")
    if request.user.is_staff:
        return redirect("console:events")
    return redirect("requests:list")


class LoginView(auth_views.LoginView):
    template_name = "console/login.html"
    redirect_authenticated_user = True

    def get_context_data(self, **kwargs):
        return {**super().get_context_data(**kwargs), "oidc_enabled": bool(settings.OIDC_RP_CLIENT_ID),
                "provider_name": settings.OIDC_PROVIDER_NAME,
                "failed": "failed" in self.request.GET}


class LogoutView(auth_views.LogoutView):
    pass
