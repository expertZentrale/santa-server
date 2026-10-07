from django import template
from django.utils.html import format_html

from ..models import AccessRequest, Policy, RuleType, Tag

register = template.Library()

POLICY_CLASSES = {
    Policy.ALLOWLIST: "ok",
    Policy.ALLOWLIST_COMPILER: "ok",
    Policy.BLOCKLIST: "bad",
    Policy.SILENT_BLOCKLIST: "bad",
    Policy.CEL: "info",
}


@register.filter
def as_switch(field):
    """A yes / no field as a switch: screen readers say on / off instead of checked"""
    return field.as_widget(attrs={"role": "switch"})


@register.filter
def policy_class(policy):
    return POLICY_CLASSES.get(policy, "")


@register.filter
def identifier_parts(rule):
    """A signing ID as (team ID with the colon, bundle ID), so the bundle ID stands out; others (None, identifier)"""
    if rule.rule_type == RuleType.SIGNINGID and ":" in rule.identifier:
        team, rest = rule.identifier.split(":", 1)
        return f"{team}:", rest
    return None, rule.identifier


@register.filter
def decision_class(decision):
    if decision.startswith("BLOCK_"):
        return "bad"
    # monitor mode: allowed only because it is unknown, lockdown would block it
    return "warn" if decision == "ALLOW_UNKNOWN" else "ok"


@register.filter
def short_hash(value, length=12):
    return value if len(value) <= length + 4 else f"{value[:length]}…"


@register.simple_tag(takes_context=True)
def pending_requests(context):
    user = context["request"].user
    if not user.has_perm("santa.view_accessrequest"):
        return 0
    return AccessRequest.objects.filter(status=AccessRequest.Status.PENDING).count()


@register.filter
def get_item(mapping, key):
    return (mapping or {}).get(key) or {}


@register.simple_tag(takes_context=True)
def tag_names(context):
    """The existing tags, for the autocomplete of the tag inputs (staff only)"""
    user = context["request"].user
    if not (user.is_staff and user.has_perm("santa.view_rule")):
        return []
    return list(Tag.objects.order_by("name").values_list("name", flat=True))


@register.simple_tag(takes_context=True)
def sort_th(context, label, name, css_class=""):
    """A sortable column header: a click sorts by it, a second click reverses"""
    request = context["request"]
    current = context.get("sort") or ""
    params = request.GET.copy()
    params.pop("page", None)
    if current == name:
        params["sort"], state = f"-{name}", "ascending"
    elif current == f"-{name}":
        params["sort"], state = name, "descending"
    else:
        params["sort"], state = name, None
    aria = format_html(' aria-sort="{}"', state) if state else ""
    classes = format_html(' class="{}"', css_class) if css_class else ""
    # data-col: the column for the widths and the column chooser (console/table.js)
    return format_html('<th data-col="{}"{}{}><a class="sort" href="?{}">{}</a></th>', name, classes, aria,
                       params.urlencode(), label)
