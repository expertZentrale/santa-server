from django.conf import settings
from django.contrib import admin
from django.urls import include, path
from django.utils.functional import lazy

from santa.console import views_auth, views_profile

# read when a page is rendered, not when the URLs are loaded
server_name = lazy(lambda: settings.SANTA_SERVER_NAME, str)()
admin.site.site_header = server_name
admin.site.site_title = server_name
admin.site.index_title = "Binary authorization for the Macs"

urlpatterns = [
    path("", views_auth.home, name="home"),
    path("favicon.ico", views_auth.favicon, name="favicon"),
    path("login/", views_auth.LoginView.as_view(), name="login"),
    path("logout/", views_auth.LogoutView.as_view(), name="logout"),
    path("profile/", views_profile.profile, name="profile"),
    path("profile/preferences/", views_profile.set_preference, name="set_preference"),
    path("profile/filters/save/", views_profile.save_filter, name="save_filter"),
    path("profile/filters/<int:pk>/delete/", views_profile.delete_filter, name="delete_filter"),
    path("profile/filters/<int:pk>/rename/", views_profile.rename_filter, name="rename_filter"),
    path("oidc/", include("mozilla_django_oidc.urls")),
    path("console/", include("santa.console.urls")),
    path("request/", include("santa.console.request_urls")),
    path("admin/", admin.site.urls),
    path("sync/", include("santa.sync_urls")),
]
