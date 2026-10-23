from io import StringIO

from django.contrib.auth.models import User
from django.core.management import CommandError, call_command
from django.test import TestCase, override_settings

from santa.models import AccessRequest, Event, Group, Machine, ReleaseSource, Rule


class DemoDataTests(TestCase):
    def run_command(self, *args):
        out = StringIO()
        call_command("create_demo_data", *args, stdout=out)
        return out.getvalue()

    @override_settings(DEBUG=False)
    def test_needs_debug_or_force(self):
        with self.assertRaises(CommandError):
            self.run_command()
        self.assertFalse(Machine.objects.exists())

    def test_creates_and_deletes_the_demo_data(self):
        real = Group.objects.create(name="Office")
        Machine.objects.create(machine_id="REAL-1", serial_number="C02REAL", group=real)
        self.run_command("--force", "--macs", "5", "--events", "60", "--seed", "1")
        self.assertEqual(Machine.objects.filter(machine_id__startswith="DEMO-").count(), 5)
        self.assertEqual(Event.objects.count(), 60)
        self.assertEqual(Group.objects.filter(name__startswith="Demo: ").count(), 3)
        self.assertTrue(AccessRequest.objects.filter(kind=AccessRequest.Kind.EVENT).exists())
        self.assertFalse(User.objects.get(username="anna@demo.invalid").has_usable_password())
        # no rules or package rules
        self.assertFalse(Rule.objects.exists())
        self.assertFalse(ReleaseSource.objects.exists())
        # a second run adds Macs, it reuses the groups and users
        self.run_command("--force", "--macs", "2", "--events", "0")
        self.assertEqual(Machine.objects.filter(machine_id__startswith="DEMO-").count(), 7)
        self.assertEqual(Group.objects.filter(name__startswith="Demo: ").count(), 3)

        # look-alikes that this command didn't create stay
        lookalike_user = User.objects.create_user("demo.someone@example.com", password="x")
        lookalike_mac = Machine.objects.create(machine_id="DEMO-REAL", serial_number="C02DEMO", group=real)
        lookalike_group = Group.objects.create(name="Demo: Real", description="Our demo Macs")
        AccessRequest.objects.create(requester=lookalike_user, kind=AccessRequest.Kind.OTHER, title="x",
                                     justification="x")

        self.run_command("--force", "--delete")
        self.assertEqual(set(Machine.objects.values_list("machine_id", flat=True)),
                         {"REAL-1", lookalike_mac.machine_id})
        self.assertFalse(Event.objects.exists())
        self.assertEqual(list(AccessRequest.objects.values_list("requester", flat=True)), [lookalike_user.pk])
        self.assertEqual(list(User.objects.values_list("username", flat=True)), [lookalike_user.username])
        self.assertEqual(set(Group.objects.values_list("name", flat=True)), {"Office", lookalike_group.name})

    def test_more_macs_than_sql_server_parameters(self):
        self.run_command("--force", "--macs", "2200", "--events", "10")
        self.assertEqual(Machine.objects.count(), 2200)
        self.assertEqual(Event.objects.count(), 10)
