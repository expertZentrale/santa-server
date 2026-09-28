from datetime import datetime, timezone

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from santa.admin import ExecutingUserFilter
from santa.models import Event, Group, Machine


class EventFilterTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.dev = Group.objects.create(name="Development")
        cls.sales = Group.objects.create(name="Sales")
        dev_mac = Machine.objects.create(machine_id="M1", serial_number="C02DEV", group=cls.dev)
        sales_mac = Machine.objects.create(machine_id="M2", serial_number="C02SALES", group=cls.sales)
        now = datetime.now(tz=timezone.utc)
        cls.events = {}
        for name, machine, user in (("jdoe-dev", dev_mac, "jdoe"), ("asmith-dev", dev_mac, "asmith"),
                                    ("jdoe-sales", sales_mac, "jdoe"), ("mmuster-sales", sales_mac, "mmuster"),
                                    ("root-dev", dev_mac, "")):
            cls.events[name] = Event.objects.create(machine=machine, group=machine.group, decision="BLOCK_UNKNOWN",
                                                    execution_time=now, file_sha256="a" * 64, file_name=name,
                                                    executing_user=user)

    def setUp(self):
        self.client.force_login(User.objects.create_superuser("admin", "admin@example.com", "pw"))

    def changelist(self, **params):
        response = self.client.get(reverse("admin:santa_event_changelist"), params)
        self.assertEqual(response.status_code, 200)
        return response, {e.file_name for e in response.context["cl"].result_list}

    def user_choices(self, response):
        user_filter = next(f for f in response.context["cl"].filter_specs if isinstance(f, ExecutingUserFilter))
        return list(user_filter.lookup_choices)

    def test_filter_by_user(self):
        response, names = self.changelist(executing_user="jdoe")
        self.assertEqual(names, {"jdoe-dev", "jdoe-sales"})
        self.assertEqual(self.user_choices(response), ["asmith", "jdoe", "mmuster"])

    def test_filter_by_group(self):
        _, names = self.changelist(group__id__exact=self.sales.pk)
        self.assertEqual(names, {"jdoe-sales", "mmuster-sales"})

    def test_filter_by_group_and_user(self):
        response, names = self.changelist(group__id__exact=self.dev.pk, executing_user="jdoe")
        self.assertEqual(names, {"jdoe-dev"})
        # only the users of the selected group are offered
        self.assertEqual(self.user_choices(response), ["asmith", "jdoe"])

    def test_show_counts(self):
        # the facet counts are aggregates, SQL Server refuses some forms of them
        response, names = self.changelist(_facets="True", group__id__exact=self.dev.pk)
        self.assertEqual(names, {"jdoe-dev", "asmith-dev", "root-dev"})
        self.assertContains(response, "jdoe (1)")
