"""The filters of the lists: several values per filter, time ranges, the last used and the saved filters (views).

The filter bar (console/widgets/_filter_bar.html) shows each active filter as a chip; the facets describe them.
"""
import operator
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from functools import reduce

from django.http import QueryDict
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import formats, timezone
from django.utils.translation import gettext
from django.utils.translation import gettext_lazy as _

from ..models import SavedFilter
from ..users import profile_for

RANGE = "range"
# the page of the list and its length are not part of a filter
NOT_FILTERS = ("page", "per_page", "reset", "rows")
MAX_SAVED_FILTERS = 30
# the lists with a filter bar (the page of filter_bar()), and whether they belong to the console (staff only)
FILTER_PAGES = {"rules": True, "events-blocked": True, "events-all": True, "machines": True, "sources": True,
                "file-access": True, "groups": True, "users": True, "tags": True, "requests": True,
                "my-requests": False}
MAX_QUERY_LENGTH = 4000
# the time filters of the lists ("" = all time), and their days for date_range()
TIME_PRESETS = [("1", _("24 hours")), ("7", _("7 days")), ("30", _("30 days")), ("365", _("1 year")),
                ("", _("All time"))]
TIME_DAYS = {"1": 1, "7": 7, "30": 30, "365": 365, "": None}


def chosen(params, name, allowed=None):
    """The values of a filter with several values (?scope=global&scope=3), unknown ones left out"""
    values = list(dict.fromkeys(value for value in params.getlist(name) if value))
    if allowed is not None:
        allowed = {str(value) for value in allowed}
        values = [value for value in values if value in allowed]
    return values


def any_of(conditions):
    """Q objects combined with OR, None without any (no filter)"""
    return reduce(operator.or_, conditions) if conditions else None


def _day_start(value, days_after=0):
    try:
        day = date.fromisoformat(value or "")
    except ValueError:
        return None
    return timezone.make_aware(datetime.combine(day + timedelta(days=days_after), time.min))


def date_range(params, name, presets, default, from_name="from", to_name="to"):
    """(start, end) of a time filter, each None for an open end.

    presets: {value: days, or None for all time}. "range": the dates from / to, both days included, in the time
    zone of the user.
    """
    value = params.get(name, default)
    if value == RANGE:
        return _day_start(params.get(from_name)), _day_start(params.get(to_name), days_after=1)
    days = presets[value] if value in presets else presets[default]
    return (timezone.now() - timedelta(days=days) if days else None), None


def in_range(queryset, field, start, end):
    if start:
        queryset = queryset.filter(**{f"{field}__gte": start})
    if end:
        queryset = queryset.filter(**{f"{field}__lt": end})
    return queryset


def filter_query(params, keep=()):
    """The filter of a query string, without the page and the parameters in keep (e.g. the tab)"""
    query = QueryDict(mutable=True)
    for name, values in params.lists():
        if name not in NOT_FILTERS and name not in keep:
            query.setlist(name, values)
    return query.urlencode()


def remember_filters(request, page, keep=()):
    """The last used filter of a list, kept in the profile of the user.

    Opened without a filter (e.g. from the menu), the list redirects to the last one; ?reset forgets it. A
    submitted filter form always has a value (the search field), so an empty filter means "not chosen".
    Returns the redirect or None.
    """
    profile = profile_for(request.user)
    stored = dict(profile.last_filters or {})
    kept = {name: request.GET.getlist(name) for name in keep if name in request.GET}
    if "reset" in request.GET:
        if stored.pop(page, None) is not None:
            profile.last_filters = stored
            profile.save(update_fields=["last_filters"])
        return redirect(_url(request, kept, ""))
    current = filter_query(request.GET, keep)
    if not current:
        return redirect(_url(request, kept, stored[page])) if stored.get(page) else None
    if len(current) <= MAX_QUERY_LENGTH and stored.get(page) != current:
        stored[page] = current
        profile.last_filters = stored
        profile.save(update_fields=["last_filters"])
    return None


def _url(request, kept, query):
    """The list again with this query: the path comes from the URL patterns, never from the request (no redirect to
    another site), the query is encoded again"""
    match = request.resolver_match
    path = reverse(match.view_name, args=match.args, kwargs=match.kwargs)
    params = QueryDict(query, mutable=True)
    for name, values in kept.items():
        params.setlist(name, values)
    return f"{path}?{params.urlencode()}" if params else path


@dataclass
class Facet:
    """One filter of the filter bar.

    kind: "multi" (checkboxes, nothing ticked = everything), "time" (presets or a date range), "choice" (one of
    some values) or "fixed" (set by a link, e.g. the events of one binary: shown, only removable).
    default: the value when the parameter is missing; for "multi" only before the form was sent (no "q").
    unfiltered: the value of "time" and "choice" that doesn't filter (the × of the chip).
    """
    name: str
    label: str
    kind: str = "multi"
    choices: list = field(default_factory=list)
    default: object = None
    unfiltered: str = ""
    from_name: str = "from"
    to_name: str = "to"
    text: str = ""

    def __post_init__(self):
        # compared with the strings of the query
        self.choices = [(str(value), label) for value, label in self.choices]


def _state(facet, params):
    """The value of the facet in the request: a list for "multi", else a string"""
    if facet.kind == "multi":
        if facet.name not in params and "q" not in params:
            return [str(value) for value in facet.default or []]
        return chosen(params, facet.name, [value for value, _label in facet.choices])
    if facet.kind == "fixed":
        return params.get(facet.name, "")
    value = params.get(facet.name, facet.default or facet.unfiltered)
    if facet.kind == "time" and value != RANGE and value not in dict(facet.choices):
        return facet.default or facet.unfiltered
    if facet.kind == "choice" and value not in dict(facet.choices):
        return facet.default or facet.unfiltered
    return value


def _default(facet):
    if facet.kind == "multi":
        return [str(value) for value in facet.default or []]
    if facet.kind == "fixed":
        return ""
    return facet.default or facet.unfiltered


def _restricts(facet, value):
    if facet.kind in ("multi", "fixed"):
        return bool(value)
    return value != facet.unfiltered


def _pairs(facet, value, params):
    if facet.kind == "multi":
        # an empty value keeps a chip without a choice ("Group: all") in the URL; chosen() ignores it
        return [(facet.name, item) for item in sorted(value)] or [(facet.name, "")]
    if facet.kind == "fixed":
        return [(facet.name, value)] if value else []
    pairs = [(facet.name, value)]
    if facet.kind == "time" and value == RANGE:
        pairs += [(name, params.get(name, "")) for name in (facet.from_name, facet.to_name) if params.get(name)]
    return pairs


def _chip_text(facet, value, params):
    labels = dict(facet.choices)
    if facet.kind == "multi":
        texts = [str(labels.get(item, item)) for item in value]
        return ", ".join(texts) if len(texts) <= 2 else f"{', '.join(texts[:2])} +{len(texts) - 2}"
    if facet.kind == "fixed":
        return facet.text or value
    if facet.kind == "time" and value == RANGE:
        start, end = (_display_date(params.get(name)) for name in (facet.from_name, facet.to_name))
        if start and end:
            return f"{start} – {end}"
        if start or end:
            return (gettext("from %(date)s") % {"date": start}) if start else (gettext("until %(date)s")
                                                                               % {"date": end})
        return gettext("any time")
    return str(labels.get(value, value))


def _display_date(value):
    try:
        return formats.date_format(date.fromisoformat(value or ""), "SHORT_DATE_FORMAT")
    except ValueError:
        return ""


def _encode(pairs):
    query = QueryDict(mutable=True)
    for name, value in pairs:
        query.appendlist(name, value)
    return query.urlencode()


def filter_bar(request, page, facets, hidden=(), keep=(), placeholder=""):
    """The context of the filter bar: the chips, the "+ Filter" menu, "reset all" and the views of the user.

    hidden: parameters kept as they are (the tab, the sort). keep: the ones that are not part of the filter, as in
    remember_filters (e.g. the tab of the events).
    """
    params = request.GET
    q = params.get("q", "")
    hidden_pairs = [(name, value) for name in hidden for value in params.getlist(name)]
    states = [(facet, _state(facet, params)) for facet in facets]

    # a chip is shown when it filters, or when it was chosen (its parameter is there): then "all", in grey
    shown = {facet.name: _restricts(facet, value) or (facet.kind != "fixed" and facet.name in params)
             for facet, value in states}

    def query(override=None):
        """The query of the bar; override: the facet whose × it is"""
        # always with q: a filter without it would be "nothing chosen" for remember_filters
        pairs = [("q", q), *hidden_pairs]
        for facet, value in states:
            if facet is override:
                if value != _default(facet) or not _restricts(facet, value):
                    # back to the default
                    continue
                # the default filters (e.g. 7 days): × shows everything, a grey chip
                value = [] if facet.kind == "multi" else facet.unfiltered
            elif not shown[facet.name]:
                continue
            pairs += _pairs(facet, value, params)
        return _encode(pairs)

    chips = []
    for facet, value in states:
        active = _restricts(facet, value)
        chips.append({
            "facet": facet, "value": value, "active": active, "shown": shown[facet.name],
            "text": _chip_text(facet, value, params) if active else gettext("all"),
            "remove_url": f"?{query(override=facet)}",
            "date_from": params.get(facet.from_name, "") if facet.kind == "time" else "",
            "date_to": params.get(facet.to_name, "") if facet.kind == "time" else "",
        })
    # as opened without a filter: the default values, and only the chips of the defaults that filter
    is_default = not q.strip() and all(value == _default(facet) and shown[facet.name] == _restricts(facet, value)
                                       for facet, value in states)
    canonical = query()
    views = list(SavedFilter.objects.filter(user=request.user, page=page))
    kept = {name: params.getlist(name) for name in keep if name in params}
    return {"bar": {
        "page": page, "q": q, "placeholder": placeholder, "hidden": hidden_pairs, "chips": chips,
        "is_default": is_default,
        "reset_url": _url(request, kept, "reset=1"), "query": canonical, "views": views,
        "active_view": next((view for view in views if canonical_query(view.query) == canonical), None),
    }}


def canonical_query(query):
    """A saved query in the order of filter_bar(), to compare it with the current filter"""
    params = QueryDict(query)
    pairs = [("q", params.get("q", ""))] + [(name, value) for name, values in params.lists() if name != "q"
                                            for value in values]
    return _encode(pairs)
