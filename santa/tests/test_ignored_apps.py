import io
import json
from datetime import datetime, timedelta
from unittest.mock import patch

from django.contrib.admin.models import ADDITION, DELETION, LogEntry
from django.contrib.auth.models import Permission, User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.db.models import QuerySet
from django.urls import reverse
from django.utils import timezone

from santa.config_io import export_config, import_config
from santa.ignores import ignored_q, ignores_of, without_ignored
from santa.models import Event, IgnoredApp

from .test_console import SHA_A, SHA_B, ConsoleBase

SIGNED = {"signing_id": "ABCDE12345:com.example.tool", "team_id": "ABCDE12345"}


class IgnoredQTestCase(ConsoleBase):
    def ignore(self, kind, value, **kwargs):
        return IgnoredApp.objects.create(kind=kind, value=value, **kwargs)

    def shown(self):
        return sorted(without_ignored(Event.objects.all()).values_list("file_name", flat=True))

    def test_by_each_kind(self):
        self.make_event(file_name="signed", **SIGNED)
        # a new version of the same app: another hash, the same signing ID
        self.make_event(sha256=SHA_B, file_name="signed-new", **SIGNED)
        installer = self.make_event(sha256="c" * 64, file_name="installer")
        Event.objects.filter(pk=installer.pk).update(file_path="/private/var/folders/x/install")
        self.make_event(sha256="d" * 64, file_name="other")
        self.assertIsNone(ignored_q())
        self.ignore(IgnoredApp.Kind.SIGNINGID, SIGNED["signing_id"])
        self.assertEqual(self.shown(), ["installer", "other"])
        self.ignore(IgnoredApp.Kind.PATH, "/private/var/folders/")
        self.assertEqual(self.shown(), ["other"])
        IgnoredApp.objects.all().delete()
        self.ignore(IgnoredApp.Kind.TEAMID, SIGNED["team_id"])
        self.assertEqual(self.shown(), ["installer", "other"])
        IgnoredApp.objects.all().delete()
        self.ignore(IgnoredApp.Kind.SHA256, SHA_A)
        self.assertEqual(self.shown(), ["installer", "other", "signed-new"])

    def test_an_expired_one_and_an_empty_value_take_nothing(self):
        self.make_event(file_name="unsigned")
        self.ignore(IgnoredApp.Kind.SHA256, SHA_A, expires_at=timezone.now() - timedelta(minutes=1))
        # an unsigned binary has no Team ID: "no Team ID" is not one
        self.ignore(IgnoredApp.Kind.TEAMID, "")
        self.assertEqual(self.shown(), ["unsigned"])

    def test_many_values_in_chunks(self):
        IgnoredApp.objects.bulk_create([IgnoredApp(kind=IgnoredApp.Kind.SHA256, value=f"{index:064x}")
                                        for index in range(2500)])
        self.make_event(sha256=f"{7:064x}", file_name="ignored")
        self.make_event(file_name="shown")
        self.assertEqual(self.shown(), ["shown"])


class IgnoredInTheConsoleTestCase(ConsoleBase):
    def setUp(self):
        super().setUp()
        self.make_event(file_name="tool", **SIGNED)
        self.make_event(sha256=SHA_B, file_name="other")
        IgnoredApp.objects.create(kind=IgnoredApp.Kind.SIGNINGID, value=SIGNED["signing_id"])

    def apps(self, **params):
        response = self.client.get(reverse("console:events"), {"rows": "now", **params})
        return sorted(row["file_name"] for row in response.context["page"])

    def test_the_comment_shows_with_the_ignored_event(self):
        IgnoredApp.objects.update(note="Updater <b>noise</b>", created_by=self.admin)
        event = Event.objects.get(file_name="tool")
        response = self.client.get(reverse("console:event", args=(event.pk,)), headers={"HX-Request": "true"})
        self.assertContains(response, "Updater &lt;b&gt;noise&lt;/b&gt;")
        self.assertContains(response, f"by {self.admin.get_username()} on")
        response = self.client.get(reverse("console:events"), {"rows": "now", "resolved": "ignored"})
        self.assertContains(response, '<small class="muted ignore-note" title="Updater &lt;b&gt;noise&lt;/b&gt;">')

    def test_the_lists_leave_them_out_also_with_new_events(self):
        self.assertEqual(self.apps(), ["other"])
        # a new event after it was ignored, also of a new version
        self.make_event(sha256="c" * 64, file_name="tool-2", **SIGNED)
        self.assertEqual(self.apps(), ["other"])
        self.assertEqual(self.apps(resolved="ignored"), ["tool", "tool-2"])
        self.assertEqual(self.apps(resolved="all"), ["other", "tool", "tool-2"])
        # the drawer of a blocked app and the open blocks of the Mac
        drawer = self.client.get(reverse("console:blocked_app", args=[SHA_A, "events"]))
        self.assertEqual(drawer.status_code, 404)
        detail = self.client.get(reverse("console:machine", args=[self.machine.pk]))
        self.assertEqual(detail.context["open_blocks"], 1)

    def test_the_live_update_leaves_them_out(self):
        after = Event.objects.order_by("-pk").first().pk
        self.make_event(sha256="c" * 64, file_name="tool-2", **SIGNED)
        response = self.client.get(reverse("console:event_updates"), {"after": after, "view": "blocked"})
        self.assertNotContains(response, "tool-2")

    def test_ignore_from_the_list_with_the_signing_id_by_default(self):
        IgnoredApp.objects.all().delete()
        form = self.client.get(reverse("console:events_ignore"), {"shas": SHA_A}).context["form"]
        self.assertEqual(form[f"kind_{SHA_A}"].initial, IgnoredApp.Kind.SIGNINGID)
        self.assertEqual(form[f"path_{SHA_A}"].initial, "/opt/bin/")
        unsigned = self.client.get(reverse("console:events_ignore"), {"shas": SHA_B}).context["form"]
        self.assertEqual(unsigned[f"kind_{SHA_B}"].initial, IgnoredApp.Kind.SHA256)
        response = self.client.post(reverse("console:events_ignore"), {
            "shas": [SHA_A, SHA_B], "apply": "1", f"kind_{SHA_A}": "SIGNINGID", f"kind_{SHA_B}": "PATH",
            f"path_{SHA_B}": "/opt/bin/", "expires_at_0": "7d", "note": "noise"}, headers={"HX-Request": "true"})
        self.assertEqual(response.status_code, 204)
        ignores = {(i.kind, i.value): i for i in IgnoredApp.objects.all()}
        self.assertEqual(set(ignores), {("SIGNINGID", SIGNED["signing_id"]), ("PATH", "/opt/bin/")})
        self.assertGreater(ignores[("PATH", "/opt/bin/")].expires_at, timezone.now() + timedelta(days=6))
        self.assertEqual(ignores[("PATH", "/opt/bin/")].created_by, self.admin)
        self.assertEqual(LogEntry.objects.filter(action_flag=ADDITION).count(), 2)
        self.assertEqual(self.apps(), [])

    def test_ignored_again_keeps_the_later_end(self):
        ignore = IgnoredApp.objects.get()
        ignore.expires_at = timezone.now() + timedelta(days=1)
        ignore.save()
        self.client.post(reverse("console:events_ignore"), {
            "shas": [SHA_A], "resolved": "ignored", "apply": "1", f"kind_{SHA_A}": "SIGNINGID",
            "expires_at_0": "30d"})
        ignore.refresh_from_db()
        self.assertGreater(ignore.expires_at, timezone.now() + timedelta(days=29))
        # for good: no end any more
        self.client.post(reverse("console:events_ignore"), {
            "shas": [SHA_A], "resolved": "ignored", "apply": "1", f"kind_{SHA_A}": "SIGNINGID", "expires_at_0": ""})
        ignore.refresh_from_db()
        self.assertIsNone(ignore.expires_at)

    def test_a_relative_path_is_refused(self):
        response = self.client.post(reverse("console:events_ignore"), {
            "shas": [SHA_B], "apply": "1", f"kind_{SHA_B}": "PATH", f"path_{SHA_B}": "opt/bin"})
        self.assertContains(response, "Must be an absolute path")
        self.assertEqual(IgnoredApp.objects.count(), 1)

    def test_stop_ignoring(self):
        response = self.client.post(reverse("console:events_unignore"), {"shas": [SHA_A], "resolved": "ignored"})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(IgnoredApp.objects.exists())
        self.assertTrue(LogEntry.objects.filter(action_flag=DELETION).exists())
        self.assertEqual(self.apps(), ["other", "tool"])

    def test_stop_ignoring_takes_every_path_of_the_binary(self):
        IgnoredApp.objects.all().delete()
        Event.objects.filter(file_sha256=SHA_A).update(file_path="/Users/a/Downloads/tool")
        self.make_event(file_name="tool", **SIGNED)
        Event.objects.filter(file_sha256=SHA_A, file_path="/opt/bin/tool").update(file_path="/private/var/x/tool")
        IgnoredApp.objects.create(kind=IgnoredApp.Kind.PATH, value="/Users/a/Downloads/")
        IgnoredApp.objects.create(kind=IgnoredApp.Kind.PATH, value="/private/var/")
        self.client.post(reverse("console:events_unignore"), {"shas": [SHA_A], "resolved": "ignored"})
        self.assertFalse(IgnoredApp.objects.exists())

    def test_ignoring_an_app_another_request_just_ignored(self):
        IgnoredApp.objects.all().delete()
        other = IgnoredApp(kind=IgnoredApp.Kind.SIGNINGID, value=SIGNED["signing_id"])
        real_first = QuerySet.first

        def first(queryset):
            # the other request saves between the look-up and the insert
            found = real_first(queryset)
            if found is None and queryset.model is IgnoredApp and other.pk is None:
                other.save()
            return found
        with patch.object(QuerySet, "first", first):
            response = self.client.post(reverse("console:events_ignore"), {
                "shas": [SHA_A], "apply": "1", f"kind_{SHA_A}": "SIGNINGID", "expires_at_0": "", "note": "noise"},
                headers={"HX-Request": "true"})
        self.assertEqual(response.status_code, 204)
        self.assertEqual(list(IgnoredApp.objects.values_list("pk", "note")), [(other.pk, "noise")])

    def test_the_event_drawer_and_the_badge(self):
        event = Event.objects.get(file_sha256=SHA_A)
        response = self.client.get(reverse("console:event", args=[event.pk]), headers={"HX-Request": "true"})
        self.assertEqual(response.context["ignores"], list(IgnoredApp.objects.all()))
        self.assertContains(response, reverse("console:events_unignore"))
        rows = self.client.get(reverse("console:events"), {"rows": "now", "resolved": "ignored"}).content.decode()
        self.assertIn(">ignored</span>", rows)

    def test_the_permissions(self):
        viewer = User.objects.create_user("viewer", is_staff=True)
        viewer.user_permissions.add(*Permission.objects.filter(codename="view_event"))
        self.client.force_login(viewer)
        self.assertEqual(self.client.get(reverse("console:events_ignore"), {"shas": SHA_B}).status_code, 403)
        self.assertEqual(self.client.post(reverse("console:events_unignore"), {"shas": [SHA_A]}).status_code, 403)

    def test_the_users_can_still_request_it(self):
        Event.objects.update(executing_user="jdoe@example.com")
        self.client.force_login(self.user)
        response = self.client.get(reverse("requests:new"))
        self.assertContains(response, "tool")


class IgnoredAdministrationTestCase(ConsoleBase):
    def test_list_add_edit_delete(self):
        response = self.client.post(reverse("console:admin_ignored_add"), {
            "kind": "PATH", "value": "/private/var/folders/", "name": "Installers", "expires_at_0": ""})
        self.assertEqual(response.status_code, 302)
        ignore = IgnoredApp.objects.get()
        self.assertEqual(ignore.created_by, self.admin)
        self.assertContains(self.client.get(reverse("console:admin_ignored")), "/private/var/folders/")
        self.client.post(reverse("console:admin_ignored_app", args=[ignore.pk]), {
            "kind": "PATH", "value": "/private/var/folders/", "name": "Installers", "note": "IT",
            "expires_at_0": "1d"})
        ignore.refresh_from_db()
        self.assertEqual(ignore.note, "IT")
        self.assertIsNotNone(ignore.expires_at)
        self.client.post(reverse("console:admin_ignored_delete", args=[ignore.pk]))
        self.assertFalse(IgnoredApp.objects.exists())

    def test_export_import_and_the_clean_up(self):
        IgnoredApp.objects.create(kind="SIGNINGID", value=SIGNED["signing_id"], name="tool",
                                  expires_at=timezone.now() + timedelta(days=3))
        IgnoredApp.objects.create(kind="SHA256", value=SHA_B, expires_at=timezone.now() - timedelta(minutes=1))
        data = json.loads(json.dumps(export_config()))
        IgnoredApp.objects.all().delete()
        report = import_config(data)
        self.assertEqual(list(IgnoredApp.objects.values_list("kind", "name")), [("SIGNINGID", "tool")])
        self.assertTrue(any("expired" in warning for warning in report["warnings"]))
        IgnoredApp.objects.create(kind="SHA256", value=SHA_B, expires_at=timezone.now() - timedelta(minutes=1))
        output = io.StringIO()
        call_command("cleanup_expired_rules", stdout=output)
        self.assertIn("1 expired ignored app(s) deleted", output.getvalue())
        self.assertEqual(IgnoredApp.objects.count(), 1)

    def test_import_without_offset_and_in_the_preview(self):
        IgnoredApp.objects.create(kind="PATH", value="/private/var/folders/", name="Installers")
        data = json.loads(json.dumps(export_config()))
        IgnoredApp.objects.all().delete()
        data["ignored_apps"][0]["expires_at"] = "2099-12-01T12:00:00"
        upload = SimpleUploadedFile("config.json", json.dumps(data).encode(), content_type="application/json")
        response = self.client.post(reverse("console:admin_config_import"), {"file": upload})
        [section] = [s for s in response.context["preview_sections"] if s["name"] == "ignored_apps"]
        self.assertEqual([(row["token"], row["label"], row["status"]) for row in section["rows"]],
                         [("ignored_apps:0", "/private/var/folders/", "created")])
        self.assertIn("Installers", section["rows"][0]["detail"])
        # skipped in the preview: not imported
        post = {"payload": response.context["payload"], "edited": "on", "skip": ["ignored_apps:0"]}
        self.client.post(reverse("console:admin_config_import"), {**post, "action": "import"})
        self.assertFalse(IgnoredApp.objects.exists())
        import_config(data)
        self.assertEqual(IgnoredApp.objects.get().expires_at,
                         timezone.make_aware(datetime(2099, 12, 1, 12), timezone.get_default_timezone()))

    def test_ignores_of_an_event(self):
        event = self.make_event(**SIGNED)
        event.file_path = "/private/var/folders/x/install"
        path = IgnoredApp.objects.create(kind="PATH", value="/private/var/folders/")
        IgnoredApp.objects.create(kind="PATH", value="/Applications/")
        self.assertEqual(ignores_of(event), [path])


class EventsBulkTestCase(ConsoleBase):
    def setUp(self):
        super().setUp()
        self.make_event(file_name="tool", **SIGNED)

    def bulk(self, action, htmx=True, **data):
        headers = {"HX-Request": "true"} if htmx else {}
        return self.client.post(reverse("console:events_bulk"), {"action": action, "shas": [SHA_A], **data},
                                headers=headers)

    def test_the_drawers_of_the_bar(self):
        self.assertContains(self.bulk("create_rules"), "data-create-rules")
        self.assertContains(self.bulk("ignore"), f'name="kind_{SHA_A}"')

    def test_resolve_and_stop_ignoring_reload_the_list(self):
        response = self.bulk("resolve")
        self.assertEqual(response.status_code, 204)
        self.assertIn("drawerSaved", response["HX-Trigger"])
        self.assertIsNotNone(Event.objects.get().resolved_at)
        IgnoredApp.objects.create(kind=IgnoredApp.Kind.SIGNINGID, value=SIGNED["signing_id"])
        response = self.bulk("unignore", resolved="ignored resolved")
        self.assertEqual(response.status_code, 204)
        self.assertFalse(IgnoredApp.objects.exists())
        # without JavaScript: back to the list
        self.assertEqual(self.bulk("resolve", htmx=False).status_code, 302)

    def test_an_unknown_action(self):
        self.assertEqual(self.bulk("delete", htmx=False).status_code, 302)

    def test_the_bar_offers_stop_ignoring_only_for_the_ignored(self):
        page = self.client.get(reverse("console:events"), {"rows": "now"}).content.decode()
        self.assertIn('value="ignore"', page)
        self.assertNotIn('value="unignore"', page)
        page = self.client.get(reverse("console:events"), {"rows": "now", "resolved": "ignored"}).content.decode()
        self.assertIn('value="unignore"', page)

    def test_room_below_the_facts_of_an_event(self):
        event = Event.objects.get()
        response = self.client.get(reverse("console:event", args=[event.pk]), headers={"HX-Request": "true"})
        self.assertContains(response, "facts-actions")
