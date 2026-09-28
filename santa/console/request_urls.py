from django.urls import path

from . import views_requests

app_name = "requests"

urlpatterns = [
    path("", views_requests.my_requests, name="list"),
    path("new/", views_requests.new_request, name="new"),
    path("catalog/", views_requests.request_catalog_search, name="catalog_search"),
    path("<int:pk>/cancel/", views_requests.cancel_request, name="cancel"),
]
