from django.urls import re_path

from . import sync_views

app_name = "santa_sync"

# Santa appends "<stage>/<machine id>" to the SyncBaseURL of the group
urlpatterns = [
    re_path(rf"^(?P<sync_token>[\w-]+)/{stage}/(?P<machine_id>[^/]+)/?$", getattr(sync_views, stage), name=stage)
    for stage in ("preflight", "eventupload", "ruledownload", "postflight")
]
