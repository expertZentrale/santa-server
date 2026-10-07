from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from django.contrib.auth.models import User
from django.http import QueryDict
from django.test import RequestFactory, SimpleTestCase
from django.urls import resolve, reverse
from django.utils import timezone

from santa.console.filters import (
    MAX_SAVED_FILTERS,
    TIME_DAYS,
    chosen,
    date_range,
    filter_query,
    remember_filters,
)
from santa.models import AccessRequest, Machine, Policy, ReleaseSource, Rule, RuleType, SavedFilter

from .test_console import SHA_A, SHA_B, ConsoleBase


class FilterHelpersTestCase(SimpleTestCase):
    def test_chosen(self):
        params = QueryDict("scope=global&scope=3&scope=&scope=global&scope=evil")
        self.assertEqual(chosen(params, "scope"), ["global", "3", "evil"])
        self.assertEqual(chosen(params, "scope", ["global", 3]), ["global", "3"])
        self.assertEqual(chosen(params, "missing"), [])

    def test_date_range(self):
        berlin = ZoneInfo("Europe/Berlin")
        with timezone.override(berlin):
            start, end = date_range(QueryDict("days=range&from=2026-03-01&to=2026-03-02"), "days", TIME_DAYS, "7")
            self.assertEqual((start, end), (datetime(2026, 3, 1, tzinfo=berlin), datetime(2026, 3, 3, tzinfo=berlin)))
            # open ends and invalid dates
            self.assertEqual(date_range(QueryDict("days=range&to=2026-03-02"), "days", TIME_DAYS, "7")[0], None)
            self.assertEqual(date_range(QueryDict("days=range&from=03/01/2026"), "days", TIME_DAYS, "7"),
                             (None, None))
        start, end = date_range(QueryDict(), "days", TIME_DAYS, "7")
        self.assertAlmostEqual(start, timezone.now() - timedelta(days=7), delta=timedelta(seconds=5))
        self.assertIsNone(end)
        self.assertEqual(date_range(QueryDict("days="), "days", TIME_DAYS, "7"), (None, None))
        # unknown presets: the default
        self.assertIsNotNone(date_range(QueryDict("days=12"), "days", TIME_DAYS, "7")[0])

    def test_filter_query(self):
        self.assertEqual(filter_query(QueryDict("view=all&page=2&per_page=25&q=x&group=1&group=2"), keep=("view",)),
                         "q=x&group=1&group=2")


class SavedAndLastFiltersTestCase(ConsoleBase):
    def test_the_last_filter_is_remembered_per_list_and_user(self):
        url = reverse("console:rules")
        self.client.get(url, {"q": "", "scope": ["global", "machines"]})
        self.assertRedirects(self.client.get(url), f"{url}?q=&scope=global&scope=machines")
        # the page and its size don't count, the tab is kept
        self.client.get(url, {"q": "", "page": "2"})
        self.assertRedirects(self.client.get(url), f"{url}?q=")
        events = reverse("console:events")
        self.client.get(events, {"view": "all", "q": "colima"})
        self.assertRedirects(self.client.get(events, {"view": "all"}), f"{events}?q=colima&view=all")
        self.assertEqual(self.client.get(events, {"view": "blocked"}).status_code, 200)
        # another user has their own
        other = User.objects.create_superuser("other", "other@example.com", "pw")
        self.client.force_login(other)
        self.assertEqual(self.client.get(url).status_code, 200)
        # reset forgets it
        self.client.force_login(self.admin)
        self.assertRedirects(self.client.get(url, {"reset": "1"}), url)
        self.assertEqual(self.client.get(url).status_code, 200)

    def test_remembered_filter_never_redirects_to_another_site(self):
        self.client.get(reverse("console:rules"), {"q": "x", "scope": "global"})
        # the path of the request is not used: a // path would be a URL of another host
        request = RequestFactory().get("//evil.example/console/rules/")
        request.user = self.admin
        request.resolver_match = resolve(reverse("console:rules"))
        self.assertEqual(remember_filters(request, "rules")["Location"], "/console/rules/?q=x&scope=global")
        request = RequestFactory().get("//evil.example/console/rules/", {"reset": "1"})
        request.user = self.admin
        request.resolver_match = resolve(reverse("console:rules"))
        self.assertEqual(remember_filters(request, "rules")["Location"], "/console/rules/")

    def test_save_overwrite_and_delete_filters(self):
        url = reverse("console:rules")
        response = self.client.post(reverse("save_filter"), {
            "page": "rules", "name": " Global ", "query": "?q=&scope=global&page=3", "next": url})
        self.assertRedirects(response, url, fetch_redirect_response=False)
        saved = SavedFilter.objects.get()
        self.assertEqual((saved.name, saved.query, saved.user), ("Global", "q=&scope=global", self.admin))
        self.client.post(reverse("save_filter"), {"page": "rules", "name": "Global", "query": "scope=machines"})
        self.assertEqual(SavedFilter.objects.get().query, "scope=machines")
        response = self.client.get(url, {"q": ""})
        self.assertContains(response, '<a href="?scope=machines">Global</a>', html=True)
        self.assertContains(response, 'id="save-filter-form"')
        # an empty name, the limit
        self.client.post(reverse("save_filter"), {"page": "rules", "name": " ", "query": "q="})
        for index in range(MAX_SAVED_FILTERS):
            SavedFilter.objects.create(user=self.admin, page="events-all", name=f"f{index}", query="q=")
        response = self.client.post(reverse("save_filter"), {"page": "events-all", "name": "one more", "query": "q="},
                                    follow=True)
        self.assertContains(response, "At most 30 saved views")
        self.assertEqual(SavedFilter.objects.filter(page="rules").count(), 1)
        # only your own can be deleted
        other = User.objects.create_user("other")
        theirs = SavedFilter.objects.create(user=other, page="rules", name="Theirs", query="q=")
        self.assertEqual(self.client.post(reverse("delete_filter", args=(theirs.pk,))).status_code, 404)
        self.client.post(reverse("delete_filter", args=(saved.pk,)))
        self.assertFalse(SavedFilter.objects.filter(pk=saved.pk).exists())
        self.assertNotContains(self.client.get(url, {"q": ""}), "Theirs")

    def test_requesters_save_filters_of_their_list(self):
        self.client.force_login(self.user)
        self.client.post(reverse("save_filter"), {"page": "my-requests", "name": "Open", "query": "status=PENDING"})
        self.assertContains(self.client.get(reverse("requests:list"), {"q": ""}), "?status=PENDING")


class ToggleFiltersTestCase(ConsoleBase):
    def titles(self, url, **params):
        response = self.client.get(url, {"q": "", **params})
        return sorted(str(item) for item in response.context["page"])

    def test_rules_scope_policy_and_created(self):
        global_rule = Rule.objects.create(rule_type=RuleType.BINARY, identifier="1" * 64, is_global=True)
        dev = Rule.objects.create(rule_type=RuleType.BINARY, identifier="2" * 64)
        dev.groups.add(self.dev)
        mac = Rule.objects.create(rule_type=RuleType.BINARY, identifier="3" * 64, policy=Policy.BLOCKLIST)
        mac.machines.add(self.machine)
        old = Rule.objects.create(rule_type=RuleType.BINARY, identifier="4" * 64, is_global=True, is_enabled=False)
        Rule.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(days=400))
        url = reverse("console:rules")
        # global and single Macs, not the groups
        self.assertEqual(self.titles(url, scope=["global", "machines"]),
                         sorted(str(rule) for rule in (global_rule, mac, old)))
        self.assertEqual(self.titles(url, scope=["machines", str(self.dev.pk)]), sorted([str(dev), str(mac)]))
        self.assertEqual(self.titles(url, policy=["BLOCKLIST", "nonsense"]), [str(mac)])
        self.assertEqual(len(self.titles(url, enabled=["yes", "no"])), 4)
        self.assertEqual(self.titles(url, enabled="no"), [str(old)])
        self.assertEqual(len(self.titles(url, created="30")), 3)
        day = (timezone.localtime() - timedelta(days=400)).date().isoformat()
        self.assertEqual(self.titles(url, created="range", created_to=day), [str(old)])
        # the export takes the same filters
        response = self.client.get(reverse("console:rules_export"), {"scope": "machines"})
        self.assertIn("3" * 64, response.content.decode())
        self.assertNotIn("1" * 64, response.content.decode())

    def test_events_groups_and_range(self):
        self.make_event(file_name="dev")
        self.make_event(sha256=SHA_B, file_name="sales", machine=self.other_machine)
        url = reverse("console:events")
        response = self.client.get(url, {"view": "all", "q": "", "group": [self.dev.pk, self.sales.pk]})
        self.assertEqual(len(response.context["page"]), 2)
        response = self.client.get(url, {"view": "all", "q": "", "group": [self.sales.pk]})
        self.assertEqual([e.file_name for e in response.context["page"]], ["sales"])
        today = timezone.localdate().isoformat()
        response = self.client.get(url, {"view": "all", "q": "", "days": "range", "from": today, "to": today})
        self.assertEqual(len(response.context["page"]), 2)
        yesterday = (timezone.localdate() - timedelta(days=1)).isoformat()
        response = self.client.get(url, {"view": "all", "q": "", "days": "range", "to": yesterday})
        self.assertEqual(len(response.context["page"]), 0)
        self.assertContains(response, '<input type="radio" name="days" value="range" checked data-range>', html=True)

    def test_admin_requests(self):
        pending = AccessRequest.objects.create(requester=self.user, kind="OTHER", title="Figma", justification="x")
        AccessRequest.objects.create(requester=self.user, kind="EVENT", title="colima", justification="x",
                                     file_sha256=SHA_A, status=AccessRequest.Status.DENIED)
        url = reverse("console:requests")
        response = self.client.get(url)
        self.assertEqual([r.title for r in response.context["page"]], ["Figma"])
        self.assertContains(response, 'value="PENDING" checked')
        # the form sent without a status: all of them
        self.assertEqual(len(self.client.get(url, {"q": ""}).context["page"]), 2)
        response = self.client.get(url, {"q": "", "kind": "EVENT", "status": ["DENIED", "APPROVED"]})
        self.assertEqual([r.title for r in response.context["page"]], ["colima"])
        self.assertEqual([r.title for r in self.client.get(url, {"q": "jdoe"}).context["page"]], ["colima", "Figma"])
        self.assertEqual(len(self.client.get(url, {"q": "fig"}).context["page"]), 1)
        self.assertEqual(pending.status, AccessRequest.Status.PENDING)

    def test_macs_package_rules_and_users(self):
        Machine.objects.filter(pk=self.machine.pk).update(clean_sync_requested=True,
                                                         last_postflight_at=timezone.now())
        Machine.objects.filter(pk=self.other_machine.pk).update(last_postflight_at=timezone.now())
        machines = reverse("console:machines")
        response = self.client.get(machines, {"q": "", "status": ["clean", "stale"]})
        self.assertEqual([m.hostname for m in response.context["page"]], ["jdoe-mbp"])
        response = self.client.get(machines, {"q": "", "group": [self.dev.pk, self.sales.pk]})
        self.assertEqual(len(response.context["page"]), 2)
        ReleaseSource.objects.create(name="broken", kind="NPM_PACKAGE", identifier="x", last_error="404")
        ReleaseSource.objects.create(name="off", kind="HOMEBREW_CASK", identifier="y", is_enabled=False)
        ReleaseSource.objects.create(name="fine", kind="NPM_PACKAGE", identifier="z")
        sources = reverse("console:sources")
        response = self.client.get(sources, {"q": "", "status": ["error", "disabled"]})
        self.assertEqual([s.name for s in response.context["page"]], ["broken", "off"])
        response = self.client.get(sources, {"q": "", "kind": ["NPM_PACKAGE"]})
        self.assertEqual([s.name for s in response.context["page"]], ["broken", "fine"])
        users = reverse("console:admin_users")
        response = self.client.get(users, {"q": "", "staff": "no"})
        self.assertEqual([u.username for u in response.context["page"]], ["jdoe@example.com"])
        response = self.client.get(users, {"q": "", "staff": ["yes", "no"]})
        self.assertEqual(len(response.context["page"]), 2)


class FilterBarTestCase(ConsoleBase):
    def bar(self, url, **params):
        return self.client.get(url, params).context["bar"]

    def chips(self, bar):
        return {chip["facet"].name: chip for chip in bar["chips"] if chip["active"]}

    def test_chips_and_their_remove_links(self):
        url = reverse("console:rules")
        bar = self.bar(url, q="", scope=["global", "machines", str(self.dev.pk), str(self.sales.pk)],
                       created="range", created_from="2026-01-01", created_to="2026-03-31")
        chips = self.chips(bar)
        self.assertEqual(set(chips), {"scope", "created"})
        self.assertEqual(chips["scope"]["text"], "Global, Individual Macs +2")
        self.assertEqual(chips["created"]["text"], "01/01/2026 – 03/31/2026")
        # × keeps the other filters and q: an empty query would bring back the last filter
        self.assertEqual(chips["scope"]["remove_url"],
                         "?q=&created=range&created_from=2026-01-01&created_to=2026-03-31")
        self.assertEqual(chips["created"]["remove_url"],
                         f"?q=&scope={self.dev.pk}&scope={self.sales.pk}&scope=global&scope=machines")
        self.assertFalse(bar["is_default"])
        response = self.client.get(url, {"q": "", "scope": "global"})
        self.assertContains(response, "Reset all")
        self.assertContains(response, 'data-open-facet="policy"')

    def test_defaults_are_chips_but_not_a_reset(self):
        url = reverse("console:events")
        # the bug: on the blocked apps "reset" was always there (view= counted as a filter)
        for view in ("blocked", "all"):
            response = self.client.get(url, {"view": view})
            bar = response.context["bar"]
            self.assertTrue(bar["is_default"], view)
            self.assertNotContains(response, "Reset all")
            self.assertEqual({name: chip["text"] for name, chip in self.chips(bar).items()},
                             {"days": "7 days", "resolved": "Open"})
        # a sent form has the ticked status too (like the status of the requests)
        chips = self.chips(self.bar(url, view="blocked", q="", resolved="open", group=str(self.dev.pk), sha256=SHA_A))
        self.assertEqual(chips["sha256"]["text"], f"{SHA_A[:12]}…")
        # × of a default: no limit
        self.assertIn("days=&", chips["days"]["remove_url"] + "&")
        self.assertIn("resolved=&", chips["resolved"]["remove_url"] + "&")
        self.assertIn("view=blocked", chips["group"]["remove_url"])
        self.assertNotIn("sha256", chips["sha256"]["remove_url"])
        response = self.client.get(url, {"view": "blocked", "q": "", "group": str(self.dev.pk)})
        self.assertContains(response, 'href="/console/events/?reset=1&amp;view=blocked"')
        # the open requests by default, also a chip
        chips = self.chips(self.bar(reverse("console:requests")))
        self.assertEqual(chips["status"]["text"], "Pending")

    def test_chosen_chips_stay_grey_with_all(self):
        url = reverse("console:events")
        # "open and resolved": chosen, but not filtering
        chip = {c["facet"].name: c for c in self.bar(url, view="blocked", q="", resolved="all")["chips"]}["resolved"]
        self.assertEqual((chip["shown"], chip["active"], chip["text"]), (True, False, "all"))
        self.assertNotIn("resolved", chip["remove_url"])
        # a group without a choice (the empty marker of the chip)
        response = self.client.get(url, {"view": "blocked", "q": "", "group": ""})
        group = {c["facet"].name: c for c in response.context["bar"]["chips"]}["group"]
        self.assertEqual((group["shown"], group["active"], group["text"]), (True, False, "all"))
        self.assertContains(response, '<span class="filter-chip" data-facet="group">', html=False)
        self.assertContains(response, 'data-open-facet="group" hidden>')
        self.assertIn("group=&", response.context["bar"]["query"] + "&")
        # × of a default that filters: everything, a grey chip
        days = {c["facet"].name: c for c in self.bar(url, view="blocked", q="")["chips"]}["days"]
        response = self.client.get(url + days["remove_url"] + "&view=blocked")
        days = {c["facet"].name: c for c in response.context["bar"]["chips"]}["days"]
        self.assertEqual((days["shown"], days["active"], days["text"]), (True, False, "all"))
        self.assertContains(response, "Reset all")
        # not chosen: hidden, and its fields are not sent
        rules = self.client.get(reverse("console:rules"), {"q": ""})
        created = {c["facet"].name: c for c in rules.context["bar"]["chips"]}["created"]
        self.assertFalse(created["shown"])
        self.assertContains(rules, '<fieldset class="picker-panel" disabled>', html=False)

    def test_active_view_and_rename(self):
        url = reverse("console:rules")
        query = self.bar(url, q="", scope=["machines", "global"])["query"]
        self.client.post(reverse("save_filter"), {"page": "rules", "name": "Global and Macs", "query": query})
        self.client.post(reverse("save_filter"), {"page": "rules", "name": "Other", "query": "q=x"})
        # the order of the values doesn't matter
        response = self.client.get(url, {"scope": ["global", "machines"], "q": ""})
        self.assertEqual(response.context["bar"]["active_view"].name, "Global and Macs")
        self.assertContains(response, 'aria-current="true">Global and Macs</a>')
        # the buttons of the console: icons, Save and Cancel
        view = SavedFilter.objects.get(name="Global and Macs")
        self.assertContains(response, f'<div class="actions"><button class="button primary" '
                                      f'form="rename-filter-{view.pk}">Save</button><button type="button" '
                                      'class="button" data-rename-cancel>Cancel</button></div>', html=True)
        self.assertContains(response, '<path d="M16 4l4 4L9 19', count=2)
        self.assertNotContains(response, "✎")
        self.assertIsNone(self.bar(url, q="", scope="global")["active_view"])
        view = SavedFilter.objects.get(name="Global and Macs")
        response = self.client.post(reverse("rename_filter", args=(view.pk,)), {"name": "Other"}, follow=True)
        self.assertContains(response, "There is a view “Other” already.")
        self.client.post(reverse("rename_filter", args=(view.pk,)), {"name": " Global + Macs "})
        view.refresh_from_db()
        self.assertEqual(view.name, "Global + Macs")
        other = User.objects.create_user("other")
        theirs = SavedFilter.objects.create(user=other, page="rules", name="Theirs", query="q=")
        self.assertEqual(self.client.post(reverse("rename_filter", args=(theirs.pk,)), {"name": "x"}).status_code,
                         404)


class RuleTypeFilterTestCase(ConsoleBase):
    def test_rule_types_are_a_filter(self):
        Rule.objects.create(rule_type=RuleType.BINARY, identifier="1" * 64, is_global=True)
        Rule.objects.create(rule_type=RuleType.TEAMID, identifier="ABCDE12345", is_global=True)
        Rule.objects.create(rule_type=RuleType.SIGNINGID, identifier="ABCDE12345:com.example.a", is_global=True)
        url = reverse("console:rules")
        response = self.client.get(url, {"q": "", "type": [RuleType.TEAMID, RuleType.SIGNINGID]})
        self.assertEqual(sorted(r.rule_type for r in response.context["page"]), [RuleType.SIGNINGID, RuleType.TEAMID])
        # an old link with one type: a chip
        response = self.client.get(url, {"type": RuleType.BINARY})
        self.assertEqual([r.rule_type for r in response.context["page"]], [RuleType.BINARY])
        chip = {c["facet"].name: c for c in response.context["bar"]["chips"]}["type"]
        self.assertTrue(chip["active"])
        self.assertNotContains(response, 'role="tablist"')


class SavedFilterPagesTestCase(ConsoleBase):
    def test_only_known_lists(self):
        # the limit is per list: a made-up one would get around it
        response = self.client.post(reverse("save_filter"), {"page": "made-up-1", "name": "x", "query": "q="},
                                    follow=True)
        self.assertContains(response, "Unknown list.")
        self.assertFalse(SavedFilter.objects.exists())
        # requesters only have their own list
        self.client.force_login(self.user)
        self.client.post(reverse("save_filter"), {"page": "rules", "name": "x", "query": "q="})
        self.assertFalse(SavedFilter.objects.exists())
        self.client.post(reverse("save_filter"), {"page": "my-requests", "name": "x", "query": "q="})
        self.assertEqual(list(SavedFilter.objects.values_list("page", flat=True)), ["my-requests"])
