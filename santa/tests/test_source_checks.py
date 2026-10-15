import io
import time
import uuid
from unittest.mock import patch

from django.contrib.admin.models import LogEntry
from django.contrib.auth.models import Permission, User
from django.core.cache import cache
from django.core.management import call_command
from django.test import RequestFactory
from django.urls import reverse
from django.utils import timezone

from santa.console.views_sources import check_summary
from santa.models import ReleaseSource, ReleaseVersion
from santa.progress import (
    AlreadyRunning,
    Cancelled,
    Progress,
    cancel_progress,
    cancel_source_check,
    progress_for,
    read_progress,
    read_source_check,
    run_source_check,
)
from santa.releases import preview_release_source, sync_release_source

from .test_console import ConsoleBase
from .test_releases import fake_session


class SourceCheckBase(ConsoleBase):
    def setUp(self):
        super().setUp()
        self.source = ReleaseSource.objects.create(name="tools", kind=ReleaseSource.Kind.GITHUB_RELEASE,
                                                   identifier="a/one\nb/two\nc/three", is_global=True)
        self.addCleanup(cache.delete_many, [f"santa:source-check:{self.source.pk}",
                                            f"santa:source-check:{self.source.pk}:cancel"])


class RunSourceCheckTestCase(SourceCheckBase):
    def test_one_check_at_a_time_seen_by_everyone(self):
        with run_source_check(self.source, self.admin) as progress:
            progress.add_total(3)
            progress.start("a/one")
            check = read_source_check(self.source.pk)
            self.assertEqual((check["user"], check["job"], check["current"], check["label"]),
                             ("admin", False, 1, "a/one"))
            with self.assertRaises(AlreadyRunning) as cm, run_source_check(self.source, self.user):
                pass
            self.assertEqual(cm.exception.check["user"], "admin")
        self.assertIsNone(read_source_check(self.source.pk))

    def test_a_dead_check_is_gone(self):
        with patch("santa.progress.time.time", return_value=1000.0):
            cache.add(f"santa:source-check:{self.source.pk}", {"beat": 1000.0, "user": "admin"}, 600)
        with patch("santa.progress.time.time", return_value=1036.0):
            self.assertIsNone(read_source_check(self.source.pk))
        # a new one starts
        with run_source_check(self.source, self.admin):
            self.assertEqual(read_source_check(self.source.pk)["user"], "admin")

    def test_the_heartbeat_goes_on_beyond_the_timeout(self):
        with patch("santa.progress.HEARTBEAT", 0.01), patch("santa.progress.TIMEOUT", 0.02), \
                run_source_check(self.source) as progress:
            first = cache.get(progress.keys[0])["beat"]
            time.sleep(0.1)
            self.assertGreater(cache.get(progress.keys[0])["beat"], first)
            self.assertTrue(progress._thread.is_alive())
            self.assertTrue(read_source_check(self.source.pk)["job"])

    def test_stopped_by_someone_else_after_the_current_package(self):
        calls = []

        def sync_identifier(session, source, identifier):
            calls.append(identifier)
            # meanwhile, another admin stops it
            cancel_source_check(source.pk, self.user)
            return [], []

        with patch("santa.releases._sync_identifier", side_effect=sync_identifier), \
                run_source_check(self.source, self.admin) as progress:
            new_versions = sync_release_source(self.source, fake_session({}), progress=progress)
        self.assertEqual((calls, new_versions), (["a/one"], []))
        self.assertEqual(progress.cancelled_by, "jdoe@example.com")
        self.source.refresh_from_db()
        # recorded as a check, the next one starts fresh
        self.assertIsNotNone(self.source.last_checked_at)
        self.assertFalse(cache.get(f"santa:source-check:{self.source.pk}:cancel"))

    def test_nothing_to_stop(self):
        self.assertFalse(cancel_source_check(self.source.pk, self.admin))

    def test_the_scheduled_job_skips_a_check_running_in_the_console(self):
        out = io.StringIO()
        with run_source_check(self.source, self.admin), \
                patch("santa.management.commands.sync_release_sources.sync_release_source") as sync:
            call_command("sync_release_sources", stdout=out)
        sync.assert_not_called()
        self.assertIn("checked in the console right now", out.getvalue())

    def test_a_preview_stops_with_what_it_has(self):
        progress_id = str(uuid.uuid4())
        request = RequestFactory().post("/", {"progress_id": progress_id})
        request.user = self.admin
        seen = []

        def releases(session, source, identifier, limit):
            seen.append(identifier)
            cancel_progress(self.admin, progress_id)
            return []

        with patch.dict("santa.releases.HISTORY", {ReleaseSource.Kind.GITHUB_RELEASE: releases}), \
                progress_for(request) as progress:
            results = preview_release_source(self.source, fake_session({}), progress=progress)
        self.assertEqual(([result["identifier"] for result in results], progress.cancelled_by), (["a/one"], "admin"))
        self.assertTrue(read_progress(self.admin, progress_id)["done"])
        cache.delete_many([f"santa:progress:{self.admin.pk}:{progress_id}",
                           f"santa:progress:{self.admin.pk}:{progress_id}:cancel"])

    def test_cancelled_only_with_a_flag(self):
        progress = Progress()
        progress.start("x")
        progress.cancelled_by = "admin"
        with self.assertRaises(Cancelled):
            progress.note("y")


class SourceCheckViewsTestCase(SourceCheckBase):
    def test_the_drawer_shows_a_running_check_and_stops_it(self):
        url = reverse("console:source", args=(self.source.pk,))
        self.assertIsNone(self.client.get(url, HTTP_HX_REQUEST="true").context["check"])
        with run_source_check(self.source, self.user) as progress:
            progress.add_total(3)
            progress.start("b/two")
            response = self.client.get(url, HTTP_HX_REQUEST="true")
            self.assertContains(response, "Check running")
            self.assertContains(response, "started by jdoe@example.com at")
            self.assertContains(response, "1 / 3 · b/two")
            self.assertContains(response, reverse("console:source_check_cancel", args=(self.source.pk,)))
            status = self.client.get(reverse("console:source_check_status", args=(self.source.pk,))).json()
            self.assertEqual((status["running"], status["step"]), (True, "1 / 3 · b/two"))
            response = self.client.post(reverse("console:source_check_cancel", args=(self.source.pk,)),
                                        HTTP_HX_REQUEST="true")
            self.assertContains(response, "Stopping after the current package (admin)")
            self.assertTrue(LogEntry.objects.filter(object_id=str(self.source.pk), change_message="Check stopped")
                            .exists())
            # the list too
            self.assertContains(self.client.get(reverse("console:sources"), {"rows": "now"}), "being checked")
        self.assertEqual(self.client.get(reverse("console:source_check_status", args=(self.source.pk,))).json(),
                         {"running": False})

    def test_read_only_viewers_see_it_but_cannot_stop_it(self):
        viewer = User.objects.create_user("viewer", is_staff=True)
        viewer.user_permissions.set(Permission.objects.filter(codename="view_releasesource"))
        self.client.force_login(viewer)
        with run_source_check(self.source, self.admin):
            response = self.client.get(reverse("console:source", args=(self.source.pk,)))
            self.assertContains(response, "Check running")
            self.assertNotContains(response, reverse("console:source_check_cancel", args=(self.source.pk,)))
            response = self.client.post(reverse("console:source_check_cancel", args=(self.source.pk,)))
            self.assertEqual(response.status_code, 403)
            self.assertIsNone(read_source_check(self.source.pk)["cancelled_by"])

    def test_a_second_check_is_not_started(self):
        with run_source_check(self.source, self.user), \
                patch("santa.console.views_sources.sync_release_source") as sync:
            response = self.client.post(reverse("console:source_check", args=(self.source.pk,)), follow=True)
        sync.assert_not_called()
        self.assertContains(response, "is being checked already (started by jdoe@example.com at")

    def test_stopped_by_someone_else(self):
        def sync(source, progress, only=None):
            progress.add_total(3)
            progress.start("a/one")
            cancel_source_check(source.pk, self.user)
            # as sync_release_source() does
            try:
                progress.start("b/two")
            except Cancelled:
                return []

        with patch("santa.console.views_sources.sync_release_source", side_effect=sync):
            response = self.client.post(reverse("console:source_check", args=(self.source.pk,)), follow=True)
        self.assertContains(response, "check stopped by jdoe@example.com after 1 of 3 packages")
        self.assertNotContains(response, "up to date")

    def test_one_message_for_many_new_versions(self):
        self.source.auto_approve = False
        versions = [ReleaseVersion.objects.create(source=self.source, identifier="a/one", version=f"1.{n}",
                                                  binary_count=1 if n else 0) for n in range(6)]
        message = check_summary(self.source, versions)
        self.assertEqual(message, "tools: 6 new versions (a/one 1.0, a/one 1.1, a/one 1.2, 3 more). 5 wait for "
                                  "approval. 1 has no executable (nothing for Santa to allow).")
        with patch("santa.console.views_sources.sync_release_source", return_value=versions):
            self.client.post(reverse("console:source_check", args=(self.source.pk,)))
        self.assertEqual(self.admin.console_notifications.count(), 1)

    def test_enabled_automatically_later(self):
        self.source.identifier = "a/one"
        version = ReleaseVersion(source=self.source, identifier="a/one", version="2.0", binary_count=1,
                                 auto_enable_pending=True, created_at=timezone.now())
        self.assertIn("1 is enabled automatically on", check_summary(self.source, [version]))


class PreviewDraftTestCase(SourceCheckBase):
    def form_data(self, **changes):
        data = {"name": "tools", "kind": ReleaseSource.Kind.GITHUB_RELEASE, "identifier": "a/one\nb/two",
                "is_enabled": "on", "rule_type": "BINARY", "policy": "ALLOWLIST", "is_global": "on",
                "keep_versions": "1", "keep_unit": "VERSIONS", "auto_approve_delay_days": "0",
                "asset_pattern": r"\.zip$"}
        return {**data, **changes}

    def setUp(self):
        super().setUp()
        self.addCleanup(cache.delete, f"santa:source-preview:{self.admin.pk}:{self.source.pk}")

    def test_the_form_comes_back_with_the_preview(self):
        progress_id = str(uuid.uuid4())
        with patch("santa.console.views_sources.preview_release_source", return_value=[
                {"identifier": "a/one", "error": "", "releases": [], "files": []}]):
            response = self.client.post(reverse("console:source_preview", args=(self.source.pk,)),
                                        self.form_data(progress_id=progress_id), HTTP_HX_REQUEST="true")
        self.assertContains(response, "<code>a/one</code>", html=True)
        # the drawer closed meanwhile, the form opened again
        edit = reverse("console:source_edit", args=(self.source.pk,))
        response = self.client.get(edit, HTTP_HX_REQUEST="true")
        self.assertContains(response, "The unsaved changes of your preview at")
        self.assertEqual(response.context["form"]["asset_pattern"].value(), r"\.zip$")
        self.assertContains(response, "<code>a/one</code>", html=True)
        self.assertContains(self.client.get(reverse("console:source_preview_result", args=(self.source.pk,))),
                            "<code>a/one</code>", html=True)
        # discarded: the saved form again
        response = self.client.post(reverse("console:source_preview_discard", args=(self.source.pk,)),
                                    HTTP_HX_REQUEST="true")
        self.assertEqual((response.status_code, response["HX-Trigger"]), (204, "drawerReload"))
        response = self.client.get(edit, HTTP_HX_REQUEST="true")
        self.assertNotContains(response, "The unsaved changes of your preview")
        self.assertEqual(response.context["form"]["asset_pattern"].value(), "")
        cache.delete(f"santa:progress:{self.admin.pk}:{progress_id}")

    def test_a_running_preview_is_asked_for(self):
        progress_id = str(uuid.uuid4())
        cache.set(f"santa:source-preview:{self.admin.pk}:{self.source.pk}", {
            "data": {name: [value] for name, value in self.form_data().items()}, "started": time.time(),
            "state": "running", "progress_id": progress_id}, 600)
        response = self.client.get(reverse("console:source_edit", args=(self.source.pk,)), HTTP_HX_REQUEST="true")
        self.assertContains(response, "The preview is running")
        self.assertContains(response, reverse("console:progress", args=(progress_id,)))
        self.assertContains(response, f'data-progress-id="{progress_id}"')
        # saving drops it
        self.client.post(reverse("console:source_edit", args=(self.source.pk,)), self.form_data())
        self.assertIsNone(cache.get(f"santa:source-preview:{self.admin.pk}:{self.source.pk}"))

    def test_only_the_own_drafts(self):
        cache.set(f"santa:source-preview:{self.admin.pk}:{self.source.pk}", {
            "data": {name: [value] for name, value in self.form_data().items()}, "started": time.time(),
            "state": "done", "preview": {"results": []}}, 600)
        other = User.objects.create_superuser("other", "other@example.com", "pw")
        self.client.force_login(other)
        response = self.client.get(reverse("console:source_edit", args=(self.source.pk,)))
        self.assertNotContains(response, "The unsaved changes of your preview")

    def test_stop_a_preview_of_the_own(self):
        progress_id = str(uuid.uuid4())
        cache.set(f"santa:progress:{self.admin.pk}:{progress_id}", {"beat": time.time()}, 600)
        self.addCleanup(cache.delete_many, [f"santa:progress:{self.admin.pk}:{progress_id}",
                                            f"santa:progress:{self.admin.pk}:{progress_id}:cancel"])
        other = User.objects.create_superuser("other", "other@example.com", "pw")
        self.client.force_login(other)
        self.client.post(reverse("console:progress_cancel", args=(progress_id,)))
        self.assertIsNone(cache.get(f"santa:progress:{self.admin.pk}:{progress_id}:cancel"))
        self.client.force_login(self.admin)
        self.assertEqual(self.client.post(reverse("console:progress_cancel", args=(progress_id,))).status_code, 204)
        self.assertEqual(cache.get(f"santa:progress:{self.admin.pk}:{progress_id}:cancel"), "admin")
