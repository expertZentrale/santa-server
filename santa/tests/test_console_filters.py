"""The filters Mac and User of the events and the execution rules: several values, "is" or "is not"."""
from django.contrib.auth.models import Permission, User
from django.test import RequestFactory
from django.urls import resolve, reverse

from santa.console.filters import Facet, filter_bar, lookup_values
from santa.models import Event, Group, Machine, Rule, RuleType, SavedFilter

from .test_console import SHA_A, SHA_B, ConsoleBase


class LookupFacetTestCase(ConsoleBase):
    def bar(self, query):
        request = RequestFactory().get(reverse("console:events"), query)
        request.user = self.admin
        request.resolver_match = resolve(reverse("console:events"))
        facet = Facet("machine", "Mac", kind="lookup", choices=[("1", "mbp-01"), ("2", "mbp-02"), ("3", "mbp-03")])
        return filter_bar(request, "events-all", [facet])["bar"]

    def test_values_and_their_exclusion(self):
        request = RequestFactory().get("/", {"machine": ["3", "7"], "machine_not": "1"})
        self.assertEqual(lookup_values(request.GET, "machine"), (["3", "7"], True))
        [chip] = self.bar({"q": "", "machine": ["1", "2", "3"], "machine_not": "1"})["chips"]
        self.assertEqual(chip["text"], "is not mbp-01, mbp-02 +1")
        self.assertTrue(chip["exclude"])
        # × removes the values and "is not"
        self.assertEqual(chip["remove_url"], "?q=")

    def test_a_saved_view_keeps_is_not(self):
        bar = self.bar({"q": "", "machine": "1", "machine_not": "1"})
        self.assertEqual(bar["query"], "q=&machine=1&machine_not=1")
        SavedFilter.objects.create(user=self.admin, page="events-all", name="no test Mac", query=bar["query"])
        self.assertEqual(self.bar({"q": "", "machine": "1", "machine_not": "1"})["active_view"].name, "no test Mac")


class EventFiltersTestCase(ConsoleBase):
    def names(self, **params):
        response = self.client.get(reverse("console:events"), {"view": "all", "q": "", "days": "", **params})
        return sorted((event.machine.hostname, event.executing_user) for event in response.context["page"])

    def setUp(self):
        super().setUp()
        self.make_event(executing_user="jdoe")
        self.make_event(machine=self.other_machine, executing_user="someone")

    def test_mac(self):
        self.assertEqual(self.names(machine=self.machine.pk), [("jdoe-mbp", "jdoe")])
        self.assertEqual(self.names(machine=self.machine.pk, machine_not="1"), [("other", "someone")])
        self.assertEqual(len(self.names(machine=[self.machine.pk, self.other_machine.pk])), 2)

    def test_user(self):
        self.assertEqual(self.names(user="someone"), [("other", "someone")])
        self.assertEqual(self.names(user="someone", user_not="1"), [("jdoe-mbp", "jdoe")])

    def test_the_link_of_a_mac_shows_its_name(self):
        # the "events" link of a Mac: one value, with its hostname in the chip
        response = self.client.get(reverse("console:events"), {"machine": self.machine.pk})
        chip = next(chip for chip in response.context["bar"]["chips"] if chip["facet"].name == "machine")
        self.assertEqual((chip["text"], chip["active"]), ("jdoe-mbp", True))

    def test_the_blocked_apps_without_the_excluded_mac(self):
        response = self.client.get(reverse("console:events"), {
            "q": "", "machine": self.other_machine.pk, "machine_not": "1"})
        [row] = response.context["page"]
        self.assertEqual((row["event_count"], row["machine_count"]), (1, 1))


class RuleFiltersTestCase(ConsoleBase):
    def setUp(self):
        super().setUp()
        self.global_rule = Rule.objects.create(rule_type=RuleType.BINARY, identifier=SHA_A, is_global=True)
        self.dev_rule = Rule.objects.create(rule_type=RuleType.TEAMID, identifier="DEVTEAM123")
        self.dev_rule.groups.add(self.dev)
        self.sales_rule = Rule.objects.create(rule_type=RuleType.TEAMID, identifier="SALESTEAM1")
        self.sales_rule.groups.add(self.sales)
        self.mac_rule = Rule.objects.create(rule_type=RuleType.BINARY, identifier=SHA_B)
        self.mac_rule.machines.add(self.machine)

    def rules(self, **params):
        response = self.client.get(reverse("console:rules"), {"q": "", "rows": "now", **params})
        return {rule.pk for rule in response.context["page"]}

    def test_the_rules_a_mac_gets(self):
        self.assertEqual(self.rules(machine=self.machine.pk),
                         {self.global_rule.pk, self.dev_rule.pk, self.mac_rule.pk})
        self.assertEqual(self.rules(machine=self.machine.pk, machine_not="1"), {self.sales_rule.pk})

    def test_the_rules_of_the_macs_of_a_user(self):
        # jdoe@example.com owns jdoe-mbp (MachineOwner)
        self.assertEqual(self.rules(user="jdoe@example.com"),
                         {self.global_rule.pk, self.dev_rule.pk, self.mac_rule.pk})
        self.assertEqual(self.rules(user="nobody@example.com"), set())

    def test_a_parent_group_gives_its_rules(self):
        child = self.sales.__class__.objects.create(name="Sales EU", parent=self.sales, inherit_rules=True)
        mac = Machine.objects.create(machine_id="M3", serial_number="C02EU", hostname="eu", group=child)
        self.assertIn(self.sales_rule.pk, self.rules(machine=mac.pk))

    def test_excluding_macs_that_do_not_exist_keeps_every_rule(self):
        everything = {self.global_rule.pk, self.dev_rule.pk, self.sales_rule.pk, self.mac_rule.pk}
        self.assertEqual(self.rules(user="nobody@example.com", user_not="1"), everything)
        self.assertEqual(self.rules(machine=999999, machine_not="1"), everything)
        self.assertEqual(self.rules(machine=999999), set())

    def test_mac_and_user_together(self):
        self.assertEqual(self.rules(machine=self.machine.pk, user="someone", user_not="1"),
                         {self.dev_rule.pk, self.mac_rule.pk})

    def test_an_owner_of_many_macs(self):
        # more Macs and groups than SQL Server takes parameters in one query, one in an inheriting group
        Group.objects.bulk_create([Group(name=f"Team {index}") for index in range(2200)])
        # SQL Server doesn't return the keys of a bulk insert
        groups = list(Group.objects.filter(name__startswith="Team "))
        child = Group.objects.create(name="Sales EU", parent=self.sales, inherit_rules=True)
        Machine.objects.bulk_create([
            Machine(machine_id=f"BULK{index}", serial_number=f"C02B{index}", group=groups[index % len(groups)],
                    primary_user="fleet@example.com") for index in range(2200)])
        Machine.objects.create(machine_id="BULK-LAST", serial_number="C02LAST", group=child,
                               primary_user="fleet@example.com")
        self.assertEqual(self.rules(user="fleet@example.com"), {self.global_rule.pk, self.sales_rule.pk})
        self.assertEqual(self.rules(user="fleet@example.com", user_not="1"), {self.dev_rule.pk, self.mac_rule.pk})
        # both filters (they apply together) with more values than they take: within the limit of SQL Server
        macs = [self.machine.pk, *range(10**6, 10**6 + 400)]
        users = ["fleet@example.com", *(f"user{index}@example.com" for index in range(400))]
        self.assertEqual(self.rules(machine=macs, user=users), {self.global_rule.pk})


class LookupEndpointsTestCase(ConsoleBase):
    def test_macs(self):
        Machine.objects.create(machine_id="M9", serial_number="C02XSS", hostname="<b>evil</b>", group=self.dev)
        response = self.client.get(reverse("console:filter_macs"), {"q": "evil", "name": "machine"})
        self.assertContains(response, "&lt;b&gt;evil&lt;/b&gt;")
        self.assertNotContains(response, "<b>evil</b>")
        response = self.client.get(reverse("console:filter_macs"), {"q": "C02TEST", "name": "machine"})
        self.assertContains(response, f'name="machine" value="{self.machine.pk}"')
        self.assertContains(response, "jdoe-mbp")

    def test_the_chosen_ones_first_ticked_once(self):
        self.make_event(executing_user="jdoe")
        self.make_event(executing_user="jdoe2")
        response = self.client.get(reverse("console:filter_users"), {"source": "events", "name": "user",
                                                                      "user": "jdoe2", "q": "xyz"})
        html = response.content.decode()
        # chosen: there, ticked, even when the search doesn't find it; the others only unticked
        self.assertIn('name="user" value="jdoe2" checked', html)
        self.assertEqual(html.count('value="jdoe2"'), 1)
        response = self.client.get(reverse("console:filter_macs"), {"name": "machine", "machine": self.machine.pk})
        html = response.content.decode()
        self.assertEqual(html.count(f'value="{self.machine.pk}"'), 1)
        self.assertLess(html.index(f'value="{self.machine.pk}" checked'),
                        html.index(f'value="{self.other_machine.pk}"'))
        # the chip lists the chosen ones in the same place, once
        response = self.client.get(reverse("console:events"), {"view": "all", "machine": self.machine.pk})
        self.assertEqual(response.content.decode().count(f'name="machine" value="{self.machine.pk}"'), 1)

    def test_at_most_twenty(self):
        Machine.objects.bulk_create([Machine(machine_id=f"X{n}", serial_number=f"S{n}", hostname=f"lab-{n:02}",
                                             group=self.dev) for n in range(30)])
        response = self.client.get(reverse("console:filter_macs"), {"q": "lab-"})
        self.assertEqual(response.content.decode().count('type="checkbox"'), 20)

    def test_users(self):
        self.make_event(executing_user="jdoe")
        response = self.client.get(reverse("console:filter_users"), {"source": "events", "q": "jd", "name": "user"})
        self.assertContains(response, 'name="user" value="jdoe"')
        response = self.client.get(reverse("console:filter_users"), {"source": "machines", "q": "jdoe"})
        self.assertContains(response, 'value="jdoe@example.com"')
        # only the names of the lookup filters for the checkboxes
        response = self.client.get(reverse("console:filter_users"),
                                   {"source": "machines", "name": "csrfmiddlewaretoken"})
        self.assertNotContains(response, 'name="csrfmiddlewaretoken"')

    def test_only_with_permission(self):
        viewer = User.objects.create_user("viewer", is_staff=True)
        viewer.user_permissions.set(Permission.objects.filter(codename="view_rule"))
        self.client.force_login(viewer)
        self.assertEqual(self.client.get(reverse("console:filter_macs")).status_code, 403)
        self.assertEqual(self.client.get(reverse("console:filter_users"), {"source": "events"}).status_code, 403)
        self.assertEqual(self.client.get(reverse("console:filter_users"), {"source": "machines"}).status_code, 200)
        self.assertFalse(Event.objects.exists())
