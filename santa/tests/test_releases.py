import hashlib
import io
import tarfile
import zipfile
from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.contrib.admin.helpers import ACTION_CHECKBOX_NAME
from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from santa.models import Group, ReleaseSource, ReleaseVersion, Rule, Tag
from santa.releases import ReleaseError, enable_due_releases, find_binaries, sync_release_source

from .utils import build_macho


def tar_gz(files):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, data in files.items():
            member = tarfile.TarInfo(name)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
    return buffer.getvalue()


def zip_file(files):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return buffer.getvalue()


class FakeResponse:
    def __init__(self, json_data=None, content=b""):
        self._json = json_data
        self.content = content

    def raise_for_status(self):
        pass

    def json(self):
        return self._json

    def iter_content(self, chunk_size):
        yield self.content

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def fake_session(routes):
    session = MagicMock()

    def get(url, **kwargs):
        return routes[url]

    session.get.side_effect = get
    return session


class FindBinariesTestCase(TestCase):
    def test_tar_archive(self):
        binary = build_macho(identifier="colima", team_id="", adhoc=True)
        data = tar_gz({"colima/0.9.0/bin/colima": binary, "colima/0.9.0/README.md": b"x" * 5000,
                       "colima/0.9.0/lib/libfoo.dylib": build_macho(filetype=6)})
        found = list(find_binaries(io.BytesIO(data), "colima.tar.gz"))
        self.assertEqual([path for path, _ in found], ["colima/0.9.0/bin/colima"])
        self.assertEqual(found[0][1].sha256, hashlib.sha256(binary).hexdigest())

    def test_zip_archive_with_pattern(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("Tool.app/Contents/MacOS/Tool", build_macho(identifier="tool"))
            archive.writestr("Tool.app/Contents/MacOS/helper", build_macho(identifier="helper"))
        found = list(find_binaries(buffer, "tool.zip", "*/MacOS/Tool"))
        self.assertEqual(len(found), 1)

    def test_dmg_not_supported(self):
        with self.assertRaises(ReleaseError):
            list(find_binaries(io.BytesIO(b"x"), "Tool.dmg"))


class ReleaseSourceBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.dev = Group.objects.create(name="Development")

    def github_source(self, **kwargs):
        kwargs.setdefault("asset_pattern", "Darwin")
        source = ReleaseSource.objects.create(name="colima", kind=ReleaseSource.Kind.GITHUB_RELEASE,
                                              identifier="abiosoft/colima", **kwargs)
        source.groups.add(self.dev)
        return source

    def github_routes(self, version, binaries, published_at="2026-09-01T10:00:00Z"):
        assets = [{"name": name, "browser_download_url": f"https://github.example/{version}/{name}"}
                  for name in binaries]
        assets.append({"name": "colima-Linux-x86_64", "browser_download_url": "https://github.example/linux"})
        assets.append({"name": "colima-Darwin-arm64.sha256sum", "browser_download_url": "https://github.example/sum"})
        routes = {"https://api.github.com/repos/abiosoft/colima/releases/latest":
                  FakeResponse({"tag_name": version, "published_at": published_at, "assets": assets})}
        for name, data in binaries.items():
            routes[f"https://github.example/{version}/{name}"] = FakeResponse(content=data)
        return routes



class ReleaseSourceTestCase(ReleaseSourceBase):
    def test_new_github_release_creates_rules(self):
        source = self.github_source()
        source.tags.add(Tag.objects.create(name="homebrew"))
        arm = build_macho(identifier="colima-arm64", team_id="", adhoc=True)
        intel = build_macho(identifier="colima-x86_64", team_id="", adhoc=True, cputype=0x01000007)
        routes = self.github_routes("v0.9.0", {"colima-Darwin-arm64": arm, "colima-Darwin-x86_64": intel})
        [release_version] = sync_release_source(source, fake_session(routes))
        self.assertEqual(release_version.version, "v0.9.0")
        self.assertEqual(release_version.binary_count, 2)
        rules = Rule.objects.filter(release_source=source)
        self.assertEqual({r.identifier for r in rules},
                         {hashlib.sha256(arm).hexdigest(), hashlib.sha256(intel).hexdigest()})
        self.assertTrue(all(r.is_enabled for r in rules))
        self.assertEqual([d.name for d in rules[0].groups.all()], ["Development"])
        self.assertEqual([t.name for t in rules[0].tags.all()], ["homebrew"])
        # same release again
        self.assertEqual(sync_release_source(source, fake_session(routes)), [])
        self.assertEqual(Rule.objects.count(), 2)

    def test_manual_approval_and_old_versions(self):
        source = self.github_source(auto_approve=False, keep_versions=2)
        for index in range(3):
            binary = build_macho(identifier=f"colima-{index}", team_id="", adhoc=True)
            routes = self.github_routes(f"v0.{index}.0", {"colima-Darwin-arm64": binary})
            sync_release_source(source, fake_session(routes))
        self.assertEqual(sorted(source.versions.values_list("version", flat=True)), ["v0.1.0", "v0.2.0"])
        self.assertEqual(Rule.objects.filter(release_source=source).count(), 2)
        self.assertFalse(Rule.objects.filter(is_enabled=True).exists())

    def test_homebrew_formula_checks_the_bottle_hash(self):
        source = ReleaseSource.objects.create(name="colima brew", kind=ReleaseSource.Kind.HOMEBREW_FORMULA,
                                              identifier="colima", asset_pattern="^arm64_", is_global=True)
        bottle = tar_gz({"colima/0.9.0/bin/colima": build_macho(identifier="colima", team_id="", adhoc=True)})
        bottle_sha256 = hashlib.sha256(bottle).hexdigest()
        formula = {"versions": {"stable": "0.9.0"}, "revision": 0, "bottle": {"stable": {"files": {
            "arm64_sequoia": {"url": "https://ghcr.example/arm64_sequoia", "sha256": bottle_sha256},
            "sonoma": {"url": "https://ghcr.example/sonoma", "sha256": "0" * 64},
            "x86_64_linux": {"url": "https://ghcr.example/linux", "sha256": "0" * 64},
        }}}}
        routes = {"https://formulae.brew.sh/api/formula/colima.json": FakeResponse(formula),
                  "https://ghcr.example/arm64_sequoia": FakeResponse(content=bottle)}
        [release_version] = sync_release_source(source, fake_session(routes))
        self.assertEqual(release_version.version, "0.9.0")
        self.assertTrue(Rule.objects.get(release_source=source).is_global)

        # a tampered bottle is refused
        formula["versions"]["stable"] = "0.9.1"
        routes["https://ghcr.example/arm64_sequoia"] = FakeResponse(content=bottle + b"x")
        with self.assertRaises(ReleaseError):
            sync_release_source(source, fake_session(routes))
        source.refresh_from_db()
        self.assertIn("SHA-256 mismatch", source.last_error)

    def test_no_matching_asset(self):
        source = self.github_source(asset_pattern="Windows")
        routes = self.github_routes("v1.0.0", {"colima-Darwin-arm64": build_macho()})
        with self.assertRaises(ReleaseError):
            sync_release_source(source, fake_session(routes))


class AutoApproveDelayTestCase(ReleaseSourceBase):
    def sync_new_release(self, source, published_at):
        binary = build_macho(identifier="colima", team_id="", adhoc=True)
        routes = self.github_routes("v2.0.0", {"colima-Darwin-arm64": binary},
                                    published_at=published_at.isoformat().replace("+00:00", "Z"))
        [release_version] = sync_release_source(source, fake_session(routes))
        return release_version

    def test_recent_release_waits(self):
        source = self.github_source(auto_approve_delay_days=2)
        published_at = timezone.now().replace(microsecond=0) - timedelta(hours=1)
        release_version = self.sync_new_release(source, published_at)
        self.assertTrue(release_version.auto_enable_pending)
        self.assertEqual(release_version.auto_enable_at, published_at + timedelta(days=2))
        self.assertFalse(Rule.objects.get(release_source=source).is_enabled)
        self.assertEqual(enable_due_releases(now=published_at + timedelta(days=1)), [])
        self.assertFalse(Rule.objects.get(release_source=source).is_enabled)
        self.assertEqual(enable_due_releases(now=published_at + timedelta(days=2, minutes=1)), [release_version])
        self.assertTrue(Rule.objects.get(release_source=source).is_enabled)
        release_version.refresh_from_db()
        self.assertFalse(release_version.auto_enable_pending)

    def test_old_release_is_enabled_right_away(self):
        source = self.github_source(auto_approve_delay_days=2)
        release_version = self.sync_new_release(source, timezone.now() - timedelta(days=10))
        self.assertFalse(release_version.auto_enable_pending)
        self.assertTrue(Rule.objects.get(release_source=source).is_enabled)

    def test_without_publication_date_the_first_sighting_counts(self):
        source = self.github_source(auto_approve_delay_days=2)
        routes = self.github_routes("v2.0.0", {"colima-Darwin-arm64": build_macho(team_id="", adhoc=True)},
                                    published_at=None)
        [release_version] = sync_release_source(source, fake_session(routes))
        self.assertIsNone(release_version.published_at)
        self.assertEqual(release_version.auto_enable_at, release_version.created_at + timedelta(days=2))
        self.assertTrue(release_version.auto_enable_pending)

    def test_manual_approval_ignores_the_delay(self):
        source = self.github_source(auto_approve=False, auto_approve_delay_days=2)
        release_version = self.sync_new_release(source, timezone.now())
        self.assertFalse(release_version.auto_enable_pending)
        self.assertEqual(enable_due_releases(now=timezone.now() + timedelta(days=3)), [])
        self.assertFalse(Rule.objects.get(release_source=source).is_enabled)

    def test_auto_approve_switched_off_while_waiting(self):
        source = self.github_source(auto_approve_delay_days=2)
        self.sync_new_release(source, timezone.now())
        source.auto_approve = False
        source.save()
        self.assertEqual(enable_due_releases(now=timezone.now() + timedelta(days=3)), [])
        self.assertFalse(Rule.objects.get(release_source=source).is_enabled)

    def test_disabling_the_rules_cancels_the_wait(self):
        source = self.github_source(auto_approve_delay_days=2)
        release_version = self.sync_new_release(source, timezone.now())
        self.client.force_login(User.objects.create_superuser("admin", "admin@example.com", "pw"))
        rule = Rule.objects.get(release_source=source)
        response = self.client.post(reverse("admin:santa_rule_changelist"),
                                    {"action": "disable_rules", ACTION_CHECKBOX_NAME: [rule.pk]}, follow=True)
        self.assertContains(response, "automatic approval cancelled")
        release_version.refresh_from_db()
        self.assertFalse(release_version.auto_enable_pending)
        self.assertEqual(enable_due_releases(now=timezone.now() + timedelta(days=3)), [])

    def test_the_cronjob_enables_the_due_releases_even_if_a_source_fails(self):
        source = self.github_source(auto_approve_delay_days=2)
        release_version = self.sync_new_release(source, timezone.now() - timedelta(days=1))
        ReleaseVersion.objects.filter(pk=release_version.pk).update(published_at=timezone.now() - timedelta(days=3))
        out = io.StringIO()
        with patch("santa.management.commands.sync_release_sources.sync_release_source",
                   side_effect=ReleaseError("GitHub is down")), self.assertRaises(SystemExit):
            call_command("sync_release_sources", stdout=out, stderr=io.StringIO())
        self.assertIn("v2.0.0 enabled after the delay", out.getvalue())
        self.assertTrue(Rule.objects.get(release_source=source).is_enabled)
