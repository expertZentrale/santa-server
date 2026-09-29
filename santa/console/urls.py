from django.urls import path

from . import views_events, views_groups, views_requests, views_rules, views_sources

app_name = "console"

urlpatterns = [
    path("events/", views_events.events, name="events"),
    path("events/rows/", views_events.event_rows, name="event_rows"),
    path("events/new-count/", views_events.new_events_count, name="new_events_count"),
    path("events/allow/", views_events.events_allow, name="events_allow"),
    path("events/resolve/", views_events.events_resolve, name="events_resolve"),
    path("events/<int:pk>/", views_events.event_detail, name="event"),
    path("events/<int:pk>/allow/", views_events.event_allow_one, name="event_allow"),

    path("rules/", views_rules.rules, name="rules"),
    path("rules/bulk/", views_rules.rules_bulk, name="rules_bulk"),
    path("rules/add/", views_rules.rule_form, name="rule_add"),
    path("rules/upload/", views_rules.rule_upload, name="rule_upload"),
    path("rules/<int:pk>/", views_rules.rule_form, name="rule"),
    path("rules/<int:pk>/toggle/", views_rules.rule_toggle, name="rule_toggle"),

    path("packages/", views_sources.sources, name="sources"),
    path("packages/add/", views_sources.source_form, name="source_add"),
    path("packages/catalog/", views_sources.catalog_search, name="catalog_search"),
    path("packages/<int:pk>/", views_sources.source_detail, name="source"),
    path("packages/<int:pk>/edit/", views_sources.source_form, name="source_edit"),
    path("packages/<int:pk>/delete/", views_sources.source_delete, name="source_delete"),
    path("packages/<int:pk>/check/", views_sources.source_check, name="source_check"),
    path("packages/versions/<int:pk>/approve/", views_sources.version_set_enabled, {"enabled": True},
         name="version_approve"),
    path("packages/versions/<int:pk>/disable/", views_sources.version_set_enabled, {"enabled": False},
         name="version_disable"),

    path("groups/", views_groups.groups, name="groups"),
    path("groups/add/", views_groups.group_form, name="group_add"),
    path("groups/base-profile/", views_groups.base_profile_download, name="base_profile"),
    path("groups/<int:pk>/", views_groups.group_form, name="group"),
    path("groups/<int:pk>/profile/", views_groups.group_profile_download, name="group_profile"),
    path("groups/<int:pk>/regenerate-token/", views_groups.group_regenerate_token, name="group_regenerate_token"),
    path("groups/<int:pk>/delete/", views_groups.group_delete, name="group_delete"),

    path("macs/", views_groups.machines, name="machines"),
    path("macs/<int:pk>/", views_groups.machine_detail, name="machine"),
    path("macs/<int:pk>/clean-sync/", views_groups.machine_clean_sync, name="machine_clean_sync"),
    path("macs/<int:pk>/rules/remove/", views_groups.machine_rules_remove, name="machine_rules_remove"),

    path("requests/", views_requests.admin_requests, name="requests"),
    path("requests/<int:pk>/", views_requests.admin_request_detail, name="request"),
    path("requests/<int:pk>/approve/", views_requests.admin_request_approve, name="request_approve"),
    path("requests/<int:pk>/deny/", views_requests.admin_request_deny, name="request_deny"),
]
