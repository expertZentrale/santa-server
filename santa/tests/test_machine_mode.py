from datetime import timedelta

from django.contrib.admin.models import LogEntry
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone

from santa.models import Group, Machine

from .test_console import ConsoleBase
from .test_sync import SyncBase


class PreflightModeTestCase(SyncBase):
    def machine(self):
        return Machine.objects.get(machine_id=self.machine_id)

    def test_the_mode_of_the_mac_wins_over_its_group(self):
        self.assertEqual(self.preflight()["client_mode"], "LOCKDOWN")
        Machine.objects.filter(machine_id=self.machine_id).update(client_mode_override="MONITOR")
        self.assertEqual(self.preflight()["client_mode"], "MONITOR")

    def test_until_a_date_then_the_group_again(self):
        self.preflight()
        Machine.objects.filter(machine_id=self.machine_id).update(
            client_mode_override="MONITOR", client_mode_override_until=timezone.now() + timedelta(hours=1))
        self.assertEqual(self.preflight()["client_mode"], "MONITOR")
        Machine.objects.filter(machine_id=self.machine_id).update(
            client_mode_override_until=timezone.now() - timedelta(minutes=1))
        self.assertEqual(self.preflight()["client_mode"], "LOCKDOWN")
        # reset: the Mac shows the group's mode again in the console
        machine = self.machine()
        self.assertEqual((machine.client_mode_override, machine.client_mode_override_until), ("", None))

    def test_the_mode_inherited_from_a_parent_group(self):
        # its own value is not used: it takes the parent's (no overridden settings)
        child = Group.objects.create(name="Child", parent=self.dev, client_mode="MONITOR")
        self.assertEqual(self.preflight(child)["client_mode"], "LOCKDOWN")
        self.assertEqual(self.machine().effective_client_mode(), "LOCKDOWN")


class MachineModeViewTestCase(ConsoleBase):
    def url(self):
        return reverse("console:machine_mode", args=[self.machine.pk])

    def test_set_for_a_week_and_back_to_the_group(self):
        before = timezone.now()
        response = self.client.post(self.url(), {"client_mode_override": "LOCKDOWN",
                                                 "client_mode_override_until_0": "7d"})
        self.assertRedirects(response, reverse("console:machine", args=[self.machine.pk]))
        self.machine.refresh_from_db()
        self.assertEqual(self.machine.client_mode_override, "LOCKDOWN")
        self.assertGreater(self.machine.client_mode_override_until, before + timedelta(days=6, hours=23))
        self.assertTrue(LogEntry.objects.filter(object_id=str(self.machine.pk)).exists())
        page = self.client.get(reverse("console:machine", args=[self.machine.pk])).content.decode()
        self.assertIn("this Mac: Lockdown", page)
        # as the group: the end goes too
        self.client.post(self.url(), {"client_mode_override": "", "client_mode_override_until_0": "7d"})
        self.machine.refresh_from_db()
        self.assertEqual((self.machine.client_mode_override, self.machine.client_mode_override_until), ("", None))

    def test_a_date_in_the_past_is_refused(self):
        response = self.client.post(self.url(), {
            "client_mode_override": "MONITOR", "client_mode_override_until_0": "date",
            "client_mode_override_until_1": (timezone.localtime() - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "The date must be in the future.")
        self.machine.refresh_from_db()
        self.assertEqual(self.machine.client_mode_override, "")

    def test_needs_the_right_to_change_macs(self):
        viewer = User.objects.create_user("viewer", is_staff=True)
        self.client.force_login(viewer)
        self.assertEqual(self.client.get(self.url()).status_code, 403)

    def test_back_to_the_group_with_an_old_end_in_the_hidden_field(self):
        self.machine.client_mode_override = "LOCKDOWN"
        self.machine.client_mode_override_until = timezone.now() - timedelta(minutes=5)
        self.machine.save()
        response = self.client.post(self.url(), {
            "client_mode_override": "", "client_mode_override_until_0": "date",
            "client_mode_override_until_1": timezone.localtime(self.machine.client_mode_override_until)
            .strftime("%Y-%m-%dT%H:%M")})
        self.assertEqual(response.status_code, 302)
        self.machine.refresh_from_db()
        self.assertEqual(self.machine.client_mode_override, "")

    def test_the_date_is_shown_in_the_time_zone_of_the_user(self):
        until = timezone.now().replace(second=0, microsecond=0) + timedelta(days=2)
        Machine.objects.filter(pk=self.machine.pk).update(client_mode_override="MONITOR",
                                                          client_mode_override_until=until)
        with timezone.override("Europe/Berlin"):
            page = self.client.get(self.url()).content.decode()
            shown = timezone.localtime(until, timezone.get_current_timezone()).strftime("%Y-%m-%dT%H:%M")
        self.assertIn(f'value="{shown}"', page)
