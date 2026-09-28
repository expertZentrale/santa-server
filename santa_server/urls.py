from django.contrib import admin
from django.urls import include, path

from santa.console import views_auth, views_profile

admin.site.site_header = "Santa Server"
admin.site.site_title = "Santa Server"
admin.site.index_title = "Binary authorization for the Macs"

urlpatterns = [
    path("", views_auth.home, name="home"),
    path("login/", views_auth.LoginView.as_view(), name="login"),
    path("logout/", views_auth.LogoutView.as_view(), name="logout"),
    path("profile/", views_profile.profile, name="profile"),
    path("profile/preferences/", views_profile.set_preference, name="set_preference"),
    path("oidc/", include("mozilla_django_oidc.urls")),
    path("console/", include("santa.console.urls")),
    path("request/", include("santa.console.request_urls")),
    path("admin/", admin.site.urls),
    path("sync/", include("santa.sync_urls")),
    path("", include("django_prometheus.urls")),
]
