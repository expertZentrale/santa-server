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
        self.assertFalse(User.objects.get(username="demo.anna@example.com").has_usable_password())
        # no rules or package rules
        self.assertFalse(Rule.objects.exists())
        self.assertFalse(ReleaseSource.objects.exists())
        # a second run adds Macs, it reuses the groups and users
        self.run_command("--force", "--macs", "2", "--events", "0")
        self.assertEqual(Machine.objects.filter(machine_id__startswith="DEMO-").count(), 7)
        self.assertEqual(Group.objects.filter(name__startswith="Demo: ").count(), 3)

        self.run_command("--force", "--delete")
        self.assertEqual(list(Machine.objects.values_list("machine_id", flat=True)), ["REAL-1"])
        self.assertFalse(Event.objects.exists())
        self.assertFalse(AccessRequest.objects.exists())
        self.assertFalse(User.objects.filter(username__startswith="demo.").exists())
        self.assertEqual(list(Group.objects.values_list("name", flat=True)), ["Office"])
