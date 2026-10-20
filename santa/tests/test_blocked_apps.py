from django.contrib.auth.models import User
from django.urls import reverse

from .test_console import SHA_A, SHA_B, ConsoleBase


class BlockedAppDrawersTestCase(ConsoleBase):
    def setUp(self):
        super().setUp()
        self.make_event(executing_user="jdoe", minutes_ago=30)
        self.make_event(executing_user="jdoe", minutes_ago=20)
        self.make_event(machine=self.other_machine, executing_user="someone", minutes_ago=10)
        # another binary, and one outside the time range of the list
        self.make_event(sha256=SHA_B, executing_user="jdoe")
        self.make_event(executing_user="old", minutes_ago=60 * 24 * 10)

    def url(self, part, query=""):
        return reverse("console:blocked_app", args=[SHA_A, part]) + query

    def test_the_counts_open_their_rows_in_the_drawer(self):
        page = self.client.get(reverse("console:events") + "?rows=now").content.decode()
        self.assertIn(f'hx-get="{self.url("events")}?', page)
        self.assertIn(f'hx-get="{self.url("macs")}?', page)
        self.assertIn(f'hx-get="{self.url("users")}?', page)

    def test_the_blocks_with_the_filters_of_the_list(self):
        response = self.client.get(self.url("events", "?days=7"), HTTP_HX_REQUEST="true")
        self.assertEqual(len(response.context["rows"]), 3)
        self.assertEqual(response.context["total"], 3)
        self.assertNotContains(response, ">old<")
        # all times: also the old one
        self.assertEqual(self.client.get(self.url("events", "?days=")).context["total"], 4)

    def test_the_macs_and_the_users(self):
        macs = self.client.get(self.url("macs", "?days=7"), HTTP_HX_REQUEST="true")
        self.assertEqual([(row["machine"], row["count"]) for row in macs.context["rows"]],
                         [(self.other_machine, 1), (self.machine, 2)])
        self.assertContains(macs, f'hx-get="{reverse("console:machine", args=[self.machine.pk])}"')
        users = self.client.get(self.url("users", "?days=7"), HTTP_HX_REQUEST="true")
        self.assertEqual([(row["executing_user"], row["count"]) for row in users.context["rows"]],
                         [("someone", 1), ("jdoe", 2)])

    def test_the_last_one_who_ran_it(self):
        page = self.client.get(reverse("console:events") + "?rows=now").content.decode()
        row = page[page.index(f'data-key="{SHA_A}"'):]
        row = row[:row.index("</tr>")]
        self.assertIn("someone", row)
        self.assertIn(self.other_machine.hostname, row)

    def test_unknown_parts_and_binaries_and_the_permission(self):
        self.assertEqual(self.client.get(self.url("rules")).status_code, 404)
        self.assertEqual(self.client.get(reverse("console:blocked_app", args=["c" * 64, "events"])).status_code, 404)
        self.client.force_login(User.objects.create_user("viewer", is_staff=True))
        self.assertEqual(self.client.get(self.url("events")).status_code, 403)
