import base64
import hashlib
import importlib
import importlib.util
import io
import json
import signal
import time
import zlib
from datetime import timedelta
from unittest.mock import MagicMock, patch

import requests
from django.apps import apps as django_apps
from django.contrib.admin.models import LogEntry
from django.contrib.auth.models import Permission, User
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import connection
from django.test import SimpleTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from santa import catalog, macho, semver
from santa import releases as release_module
from santa.console.views_sources import error_groups, removable_packages
from santa.models import ReleaseSource, ReleaseVersion, Rule, RuleType, Tag
from santa.progress import SHUTDOWN, SHUTTING_DOWN, Cancelled, Progress
from santa.releases import (
    LINUX_ONLY_ERROR,
    Asset,
    FoundBinary,
    Release,
    ReleaseError,
    _cut_lines,
    _ghcr_tags,
    _npm_platform_ok,
    cleanup_preview,
    cleanup_release_source,
    collect_binaries,
    create_release_rules,
    find_binaries,
    http_session,
    preview_release_source,
    prune_old_versions,
    stale_rules,
    sync_release_source,
    unchecked_identifiers,
)

from .test_console import ConsoleBase
from .test_package_sources import VSCodeTestCase
from .test_releases import FakeResponse, ReleaseSourceBase, fake_session, tar_gz, zip_file
from .utils import ARM64, X86_64, build_fat, build_macho


class SemverTestCase(SimpleTestCase):
    def test_ranges(self):
        cases = [
            ("1.2.3", "^1.0.0", True), ("2.0.0", "^1.0.0", False), ("0.2.5", "^0.2.3", True),
            ("0.3.0", "^0.2.3", False), ("0.0.4", "^0.0.3", False), ("1.2.9", "~1.2.3", True),
            ("1.3.0", "~1.2", False), ("1.5.0", "1.x", True), ("1.0.0", "*", True), ("1.0.0", "", True),
            ("1.4.0", "1.2 - 1.4", True), ("1.5.0", "1.2 - 1.4", False), ("3.0.0", ">2 <4 || 5", True),
            ("5.1.0", ">2 <4 || 5", True), ("4.0.0", ">2 <4 || 5", False), ("1.2.3", "=1.2.3", True),
            ("1.2.3", ">= 1.2.3", True), ("1.2.2", ">=1.2.3", False), ("1.2.0", "<=1.2", True),
            ("1.3.0", "<=1.2", False),
            # prereleases only for a range that names one of the same version
            ("2.0.0-beta.1", ">=1", False), ("1.2.3-rc.1", "^1.2.3-rc.0", True), ("1.2.4-rc.1", "^1.2.3-rc.0", False),
            # no version range: nothing matches
            ("1.0.0", "git+https://example.com/x.git", False), ("1.0.0", "file:../x", False),
        ]
        for version, text, expected in cases:
            with self.subTest(version=version, range=text):
                self.assertEqual(semver.satisfies(version, text), expected)

    def test_max_satisfying(self):
        versions = ["1.0.0", "1.2.0", "1.10.0", "2.0.0", "1.11.0-beta"]
        self.assertEqual(semver.max_satisfying(versions, "^1"), "1.10.0")
        self.assertEqual(semver.max_satisfying(versions, "~1.2"), "1.2.0")
        self.assertIsNone(semver.max_satisfying(versions, "^3"))


def unsigned(name):
    return build_macho(identifier=name, team_id="", adhoc=True)


def github_release(tag, published_at, binary_name="colima-Darwin-arm64"):
    return {"tag_name": tag, "published_at": published_at, "draft": False, "prerelease": False,
            "assets": [{"name": binary_name, "browser_download_url": f"https://github.example/{tag}/{binary_name}"}]}


class KeepVersionsTestCase(ReleaseSourceBase):
    def github_routes(self, releases):
        routes = {
            "https://api.github.com/repos/abiosoft/colima/releases/latest": FakeResponse(releases[0]),
            "https://api.github.com/repos/abiosoft/colima/releases": FakeResponse(releases),
        }
        for release in releases:
            for asset in release["assets"]:
                routes[asset["browser_download_url"]] = FakeResponse(content=unsigned(release["tag_name"]))
        return routes

    def test_the_older_kept_versions_are_allowed_too(self):
        source = self.github_source(keep_versions=2, approve_kept_versions=True, asset_pattern="")
        releases = [github_release("v3", "2026-09-03T10:00:00Z"), github_release("v2", "2026-09-02T10:00:00Z"),
                    github_release("v1", "2026-09-01T10:00:00Z")]
        new_versions = sync_release_source(source, fake_session(self.github_routes(releases)))
        self.assertEqual([version.version for version in new_versions], ["v3", "v2"])
        self.assertTrue(all(rule.is_enabled for rule in Rule.objects.filter(release_source=source)))
        # v2 was added after v3, it still is the older one: the next check keeps both
        self.assertEqual(sync_release_source(source, fake_session(self.github_routes(releases))), [])
        self.assertEqual(sorted(source.versions.values_list("version", flat=True)), ["v2", "v3"])

    def test_a_cancel_stops_before_the_next_older_version(self):
        source = self.github_source(keep_versions=3, approve_kept_versions=True, asset_pattern="")
        releases = [github_release("v3", "2026-09-03T10:00:00Z"), github_release("v2", "2026-09-02T10:00:00Z"),
                    github_release("v1", "2026-09-01T10:00:00Z")]

        class CancelAtV1(Progress):
            def note(self, label):
                if label.endswith(" v1"):
                    self.cancelled_by = "test"
                super().note(label)
        new_versions = sync_release_source(source, fake_session(self.github_routes(releases)), progress=CancelAtV1())
        self.assertEqual([version.version for version in new_versions], ["v3", "v2"])
        self.assertEqual(sorted(source.versions.values_list("version", flat=True)), ["v2", "v3"])

    def test_without_the_option_only_the_latest_version(self):
        source = self.github_source(keep_versions=2, asset_pattern="")
        releases = [github_release("v3", "2026-09-03T10:00:00Z"), github_release("v2", "2026-09-02T10:00:00Z")]
        new_versions = sync_release_source(source, fake_session(self.github_routes(releases)))
        self.assertEqual([version.version for version in new_versions], ["v3"])

    def test_keep_the_versions_of_the_last_weeks(self):
        now = timezone.now()
        source = self.github_source(keep_versions=2, keep_unit=ReleaseSource.KeepUnit.WEEKS,
                                    approve_kept_versions=True, asset_pattern="")
        releases = [github_release(f"v{n}", (now - timedelta(days=days)).isoformat())
                    for n, days in ((4, 1), (3, 10), (2, 20), (1, 40))]
        new_versions = sync_release_source(source, fake_session(self.github_routes(releases)))
        # 2 weeks: v4 and v3
        self.assertEqual([version.version for version in new_versions], ["v4", "v3"])

    def test_the_newest_version_stays_when_it_is_older_than_the_range(self):
        source = self.github_source(keep_versions=1, keep_unit=ReleaseSource.KeepUnit.MONTHS)
        old = timezone.now() - timedelta(days=100)
        for version, published_at in (("v1", old - timedelta(days=1)), ("v2", old)):
            ReleaseVersion.objects.create(source=source, identifier="abiosoft/colima", version=version,
                                          published_at=published_at)
        self.assertEqual(prune_old_versions(source), 1)
        self.assertEqual(list(source.versions.values_list("version", flat=True)), ["v2"])

    def test_older_homebrew_bottles_from_the_registry(self):
        base = "https://ghcr.io/v2/homebrew/core/colima"
        current = tar_gz({"colima/0.10.3/bin/colima": unsigned("colima-0.10.3")})
        older = tar_gz({"colima/0.10.2/bin/colima": unsigned("colima-0.10.2")})
        rebuilt = tar_gz({"colima/0.10.2/bin/colima": unsigned("colima-0.10.2-1")})

        def index(version, created, tag, bottle):
            digest = hashlib.sha256(bottle).hexdigest()
            return FakeResponse({
                "annotations": {"org.opencontainers.image.version": version,
                                "org.opencontainers.image.created": created},
                "manifests": [
                    {"platform": {"os": "darwin"}, "annotations": {
                        "org.opencontainers.image.ref.name": f"{version}.arm64_sequoia{tag}",
                        "sh.brew.bottle.digest": digest}},
                    {"platform": {"os": "linux"}, "annotations": {
                        "org.opencontainers.image.ref.name": f"{version}.x86_64_linux{tag}",
                        "sh.brew.bottle.digest": "0" * 64}},
                ]})
        routes = {
            "https://formulae.brew.sh/api/formula/colima.json": FakeResponse({
                "versions": {"stable": "0.10.3"}, "revision": 0, "bottle": {"stable": {"files": {
                    "arm64_sequoia": {"url": "https://ghcr.example/colima",
                                      "sha256": hashlib.sha256(current).hexdigest()},
                }}}}),
            "https://ghcr.example/colima": FakeResponse(content=current),
            f"{base}/tags/list": FakeResponse({"tags": ["0.10.1", "0.10.2", "0.10.2-1", "0.10.3"]}),
            f"{base}/manifests/0.10.3": index("0.10.3", "2026-09-20T10:00:00Z", "", current),
            # a rebuild of 0.10.2: the newest build counts
            f"{base}/manifests/0.10.2-1": index("0.10.2", "2026-09-10T10:00:00Z", ".1", rebuilt),
            f"{base}/manifests/0.10.2": index("0.10.2", "2026-09-01T10:00:00Z", "", older),
            f"{base}/blobs/sha256:{hashlib.sha256(rebuilt).hexdigest()}": FakeResponse(content=rebuilt),
        }
        source = ReleaseSource.objects.create(name="colima", kind=ReleaseSource.Kind.HOMEBREW_FORMULA,
                                              identifier="colima", is_global=True, keep_versions=2,
                                              approve_kept_versions=True)
        new_versions = sync_release_source(source, fake_session(routes))
        self.assertEqual([(version.version, version.binary_count) for version in new_versions],
                         [("0.10.3", 1), ("0.10.2", 1)])
        self.assertTrue(Rule.objects.filter(identifier=hashlib.sha256(unsigned("colima-0.10.2-1")).hexdigest())
                        .exists())

    def test_older_bottles_with_only_a_day_as_their_date(self):
        # the registry gives the older bottles a day ("2021-04-06"), the newer ones a time in UTC
        base = "https://ghcr.io/v2/homebrew/core/colima"
        current = tar_gz({"colima/0.10.3/bin/colima": unsigned("colima-0.10.3")})
        older = tar_gz({"colima/0.10.2/bin/colima": unsigned("colima-0.10.2")})
        now = timezone.now()

        def index(version, created, bottle):
            return FakeResponse({
                "annotations": {"org.opencontainers.image.version": version,
                                "org.opencontainers.image.created": created},
                "manifests": [{"platform": {"os": "darwin"}, "annotations": {
                    "org.opencontainers.image.ref.name": f"{version}.arm64_sequoia",
                    "sh.brew.bottle.digest": hashlib.sha256(bottle).hexdigest()}}]})
        routes = {
            "https://formulae.brew.sh/api/formula/colima.json": FakeResponse({
                "versions": {"stable": "0.10.3"}, "revision": 0, "bottle": {"stable": {"files": {
                    "arm64_sequoia": {"url": "https://ghcr.example/colima",
                                      "sha256": hashlib.sha256(current).hexdigest()},
                }}}}),
            "https://ghcr.example/colima": FakeResponse(content=current),
            f"{base}/tags/list": FakeResponse({"tags": ["0.9.0", "0.10.2", "0.10.3"]}),
            f"{base}/manifests/0.10.3": index("0.10.3", f"{now - timedelta(days=1):%Y-%m-%dT%H:%M:%SZ}", current),
            f"{base}/manifests/0.10.2": index("0.10.2", f"{now - timedelta(days=5):%Y-%m-%d}", older),
            f"{base}/manifests/0.9.0": index("0.9.0", "2021-04-06", older),
            f"{base}/blobs/sha256:{hashlib.sha256(older).hexdigest()}": FakeResponse(content=older),
        }
        source = ReleaseSource.objects.create(name="colima", kind=ReleaseSource.Kind.HOMEBREW_FORMULA,
                                              identifier="colima", is_global=True, keep_versions=2,
                                              keep_unit=ReleaseSource.KeepUnit.WEEKS, approve_kept_versions=True)
        new_versions = sync_release_source(source, fake_session(routes))
        self.assertEqual([version.version for version in new_versions], ["0.10.3", "0.10.2"])
        self.assertIsNotNone(ReleaseVersion.objects.get(version="0.10.2").published_at.tzinfo)

    def test_the_preview_shows_releases_older_than_the_range(self):
        base = "https://ghcr.io/v2/homebrew/core/jq"
        routes = {
            f"{base}/tags/list": FakeResponse({"tags": ["1.8.2"]}),
            f"{base}/manifests/1.8.2": FakeResponse({"annotations": {
                "org.opencontainers.image.version": "1.8.2", "org.opencontainers.image.created": "2021-04-06"},
                "manifests": []}),
        }
        source = ReleaseSource(name="jq", kind=ReleaseSource.Kind.HOMEBREW_FORMULA, identifier="jq",
                               keep_versions=3, keep_unit=ReleaseSource.KeepUnit.WEEKS)
        [result] = preview_release_source(source, fake_session(routes))
        self.assertEqual([release["version"] for release in result["releases"]], ["1.8.2"])

    def test_formula_without_executables(self):
        # a library or data only (ca-certificates): recorded with 0 binaries, not an error
        bottle = tar_gz({"ca-certificates/2026-01-01/share/ca-certificates/cacert.pem": b"x" * 5000})
        routes = {
            "https://formulae.brew.sh/api/formula/ca-certificates.json": FakeResponse({
                "versions": {"stable": "2026-01-01"}, "revision": 0, "bottle": {"stable": {"files": {
                    "all": {"url": "https://ghcr.example/ca", "sha256": hashlib.sha256(bottle).hexdigest()},
                }}}}),
            "https://ghcr.example/ca": FakeResponse(content=bottle),
        }
        source = ReleaseSource.objects.create(name="ca", kind=ReleaseSource.Kind.HOMEBREW_FORMULA,
                                              identifier="ca-certificates", is_global=True)
        [version] = sync_release_source(source, fake_session(routes))
        self.assertEqual(version.binary_count, 0)
        source.refresh_from_db()
        self.assertEqual(source.last_error, "")

    def test_older_vscode_versions(self):
        source = ReleaseSource.objects.create(name="tool", kind=ReleaseSource.Kind.VSCODE_EXTENSION,
                                              identifier="example.tool", is_global=True, keep_versions=2,
                                              approve_kept_versions=True)
        gallery = VSCodeTestCase.gallery(None, [("1.2.0", "darwin-arm64", False), ("1.1.0", "darwin-arm64", False),
                                                ("1.0.0", "darwin-arm64", False)])
        routes = {f"https://vsassets.example/{version}/darwin-arm64": FakeResponse(
            content=zip_file({"extension/bin/tool": unsigned(f"tool-{version}")})) for version in ("1.2.0", "1.1.0")}
        session = fake_session(routes)
        session.post.return_value = FakeResponse(gallery)
        new_versions = sync_release_source(source, session)
        self.assertEqual([version.version for version in new_versions], ["1.2.0", "1.1.0"])
        # the whole list only for the older versions
        self.assertTrue(session.post.call_args_list[-1].kwargs["json"]["flags"] & 0x10000 == 0)

    def test_clean(self):
        source = ReleaseSource(name="tools", kind=ReleaseSource.Kind.HOMEBREW_CASK, identifier="colima",
                               keep_versions=0, approve_kept_versions=True, include_dependencies=True)
        with self.assertRaises(ValidationError) as cm:
            source.full_clean()
        self.assertIn("approve_kept_versions", cm.exception.message_dict)
        source.keep_versions = 3
        with self.assertRaises(ValidationError) as cm:
            source.full_clean()
        # a cask only points to the current download
        self.assertIn("approve_kept_versions", cm.exception.message_dict)
        source.kind = ReleaseSource.Kind.GITHUB_RELEASE
        source.identifier = "abiosoft/colima"
        with self.assertRaises(ValidationError) as cm:
            source.full_clean()
        self.assertEqual(list(cm.exception.message_dict), ["include_dependencies"])


def npm_document(name, version, tarball, dependencies=None, optional=None, os=None):
    manifest = {"name": name, "version": version, "dependencies": dependencies or {},
                "optionalDependencies": optional or {},
                "dist": {"tarball": f"https://registry.example/{name}-{version}.tgz",
                         "integrity": "sha512-" + base64.b64encode(hashlib.sha512(tarball).digest()).decode()}}
    if os:
        manifest["os"] = os
    return manifest


@override_settings(RELEASE_MAX_DEPENDENCIES=10)
class DependenciesTestCase(ReleaseSourceBase):
    def npm_routes(self):
        main = tar_gz({"package/index.js": b"x" * 5000})
        darwin = tar_gz({"package/bin/esbuild": unsigned("esbuild-darwin")})
        helper = tar_gz({"package/bin/helper": unsigned("helper")})
        documents = {
            "@example/tool": npm_document("@example/tool", "1.0.0", main, dependencies={"dep-a": "^1.0.0"},
                                          optional={"@esbuild/darwin-arm64": "0.20.0",
                                                    "@esbuild/linux-x64": "0.20.0"}),
            "dep-a": {"1.0.0": npm_document("dep-a", "1.0.0", b"", dependencies={"dep-b": "~2.1.0"}),
                      "1.4.0": npm_document("dep-a", "1.4.0", helper, dependencies={"dep-b": "~2.1.0"}),
                      "2.0.0": npm_document("dep-a", "2.0.0", b"")},
            "dep-b": {"2.1.5": npm_document("dep-b", "2.1.5", main), "2.2.0": npm_document("dep-b", "2.2.0", b"")},
            "@esbuild/darwin-arm64": {"0.20.0": npm_document("@esbuild/darwin-arm64", "0.20.0", darwin,
                                                             os=["darwin"])},
            "@esbuild/linux-x64": {"0.20.0": npm_document("@esbuild/linux-x64", "0.20.0", b"", os=["linux"])},
        }
        routes = {"https://registry.npmjs.org/@example%2Ftool/latest": FakeResponse(documents.pop("@example/tool")),
                  "https://registry.example/@example/tool-1.0.0.tgz": FakeResponse(content=main),
                  "https://registry.example/dep-a-1.4.0.tgz": FakeResponse(content=helper),
                  "https://registry.example/dep-b-2.1.5.tgz": FakeResponse(content=main),
                  "https://registry.example/@esbuild/darwin-arm64-0.20.0.tgz": FakeResponse(content=darwin)}
        for name, versions in documents.items():
            routes[f"https://registry.npmjs.org/{name.replace('/', '%2F')}"] = FakeResponse({"versions": versions})
        # the Linux package is never downloaded: no route for its tarball
        return routes

    def test_npm_dependencies_also_the_indirect_ones(self):
        source = ReleaseSource.objects.create(name="tool", kind=ReleaseSource.Kind.NPM_PACKAGE, is_global=True,
                                              identifier="@example/tool", include_dependencies=True)
        [version] = sync_release_source(source, fake_session(self.npm_routes()))
        rules = Rule.objects.filter(release_version=version)
        self.assertEqual(sorted(rules.values_list("release_dependency", flat=True)),
                         ["@esbuild/darwin-arm64@0.20.0", "dep-a@1.4.0"])
        self.assertEqual(version.binary_count, 2)
        self.assertIn("Dependencies: dep-a@1.4.0, @esbuild/darwin-arm64@0.20.0, dep-b@2.1.5", version.notes)

    def test_versions_recorded_before_get_their_dependencies(self):
        routes = self.npm_routes()
        source = ReleaseSource.objects.create(name="tool", kind=ReleaseSource.Kind.NPM_PACKAGE, is_global=True,
                                              identifier="@example/tool")
        [version] = sync_release_source(source, fake_session(routes))
        self.assertEqual((version.binary_count, version.dependencies_resolved), (0, False))
        source.include_dependencies = True
        source.save()
        self.assertEqual(sync_release_source(source, fake_session(routes)), [])
        version.refresh_from_db()
        self.assertEqual((version.binary_count, version.dependencies_resolved), (2, True))
        self.assertEqual(version.dependencies, ["dep-a@1.4.0", "@esbuild/darwin-arm64@0.20.0", "dep-b@2.1.5"])
        self.assertTrue(all(rule.is_enabled for rule in version.rules.all()))
        # looked up once
        session = fake_session(routes)
        sync_release_source(source, session)
        self.assertNotIn("https://registry.npmjs.org/dep-a", [call.args[0] for call in session.get.call_args_list])
        # switched off and cleaned up: looked up again when it is switched on again
        source.include_dependencies = False
        source.save()
        self.assertEqual(cleanup_release_source(source), (0, 2))
        version.refresh_from_db()
        self.assertEqual((version.binary_count, version.dependencies, version.dependencies_resolved), (0, [], False))

    def test_the_detail_lists_the_dependencies(self):
        source = ReleaseSource.objects.create(name="tool", kind=ReleaseSource.Kind.NPM_PACKAGE, is_global=True,
                                              identifier="@example/tool", include_dependencies=True)
        sync_release_source(source, fake_session(self.npm_routes()))
        admin = User.objects.create_superuser("admin", "admin@example.com", "pw")
        self.client.force_login(admin)
        response = self.client.get(reverse("console:source", args=(source.pk,)))
        self.assertContains(response, "3 dependencies")
        self.assertContains(response, "executables allowed", count=2)

    def test_without_the_option_no_dependencies(self):
        source = ReleaseSource.objects.create(name="tool", kind=ReleaseSource.Kind.NPM_PACKAGE, is_global=True,
                                              identifier="@example/tool")
        [version] = sync_release_source(source, fake_session(self.npm_routes()))
        self.assertEqual(version.binary_count, 0)

    def test_only_the_packages_for_macos(self):
        for platforms, expected in (([], True), (["darwin"], True), (["linux"], False), (["!win32"], True),
                                    (["!darwin"], False), (["linux", "!win32"], False), (["darwin", "!win32"], True)):
            with self.subTest(platforms):
                self.assertEqual(_npm_platform_ok({"os": platforms}), expected)

    @override_settings(RELEASE_MAX_DEPENDENCIES=2)
    def test_the_limit_counts_every_version_of_a_name(self):
        empty = tar_gz({"package/index.js": b"x" * 5000})
        documents = {
            "dep-b": {"1.0.0": npm_document("dep-b", "1.0.0", empty), "2.0.0": npm_document("dep-b", "2.0.0", empty)},
            "dep-c": {"1.0.0": npm_document("dep-c", "1.0.0", empty, dependencies={"dep-b": "^2.0.0"})},
        }
        routes = {"https://registry.npmjs.org/tool/latest": FakeResponse(
            npm_document("tool", "1.0.0", empty, dependencies={"dep-b": "^1.0.0", "dep-c": "^1.0.0"})),
            "https://registry.example/tool-1.0.0.tgz": FakeResponse(content=empty)}
        for name, versions in documents.items():
            routes[f"https://registry.npmjs.org/{name}"] = FakeResponse({"versions": versions})
            for version in versions:
                routes[f"https://registry.example/{name}-{version}.tgz"] = FakeResponse(content=empty)
        source = ReleaseSource.objects.create(name="tool", kind=ReleaseSource.Kind.NPM_PACKAGE, is_global=True,
                                              identifier="tool", include_dependencies=True)
        [version] = sync_release_source(source, fake_session(routes))
        # two names, but three name@version: the third is not downloaded
        self.assertEqual(version.dependencies, ["dep-b@1.0.0", "dep-c@1.0.0"])
        self.assertIn("More than 2 dependencies", version.notes)

    def test_the_clean_up_counts_binaries_not_rules(self):
        # one binary of the package with two CDHashes (two rules), and a dependency with one
        fat = build_fat((ARM64, build_macho(identifier="tool-arm64", team_id="", adhoc=True)),
                        (X86_64, build_macho(identifier="tool-x86", team_id="", adhoc=True, cputype=X86_64)))
        main = tar_gz({"package/bin/tool": fat})
        helper = tar_gz({"package/bin/helper": unsigned("helper")})
        routes = {"https://registry.npmjs.org/tool/latest": FakeResponse(
            npm_document("tool", "1.0.0", main, dependencies={"dep-a": "^1.0.0"})),
            "https://registry.example/tool-1.0.0.tgz": FakeResponse(content=main),
            "https://registry.npmjs.org/dep-a": FakeResponse({"versions": {
                "1.0.0": npm_document("dep-a", "1.0.0", helper)}}),
            "https://registry.example/dep-a-1.0.0.tgz": FakeResponse(content=helper)}
        source = ReleaseSource.objects.create(name="tool", kind=ReleaseSource.Kind.NPM_PACKAGE, is_global=True,
                                              identifier="tool", include_dependencies=True,
                                              rule_type=RuleType.CDHASH)
        [version] = sync_release_source(source, fake_session(routes))
        self.assertEqual((version.binary_count, version.dependency_binary_count), (2, 1))
        source.include_dependencies = False
        source.save()
        cleanup_release_source(source)
        version.refresh_from_db()
        # the package's one binary, although it has two rules
        self.assertEqual(version.rules.count(), 2)
        self.assertEqual((version.binary_count, version.dependency_binary_count), (1, 0))

    def test_homebrew_runtime_dependencies(self):
        def formula(name, dependencies, binary):
            bottle = tar_gz({f"{name}/1.0/bin/{name}": binary})
            return {
                f"https://formulae.brew.sh/api/formula/{name}.json": FakeResponse({
                    "versions": {"stable": "1.0"}, "revision": 0, "dependencies": dependencies,
                    "bottle": {"stable": {"files": {"arm64_sequoia": {
                        "url": f"https://ghcr.example/{name}", "sha256": hashlib.sha256(bottle).hexdigest()}}}},
                }),
                f"https://ghcr.example/{name}": FakeResponse(content=bottle),
            }
        routes = {**formula("colima", ["lima"], unsigned("colima")), **formula("lima", ["qemu"], unsigned("lima")),
                  **formula("qemu", [], unsigned("qemu"))}
        source = ReleaseSource.objects.create(name="colima", kind=ReleaseSource.Kind.HOMEBREW_FORMULA,
                                              identifier="colima", is_global=True, include_dependencies=True)
        [version] = sync_release_source(source, fake_session(routes))
        self.assertEqual(version.binary_count, 3)
        self.assertEqual(sorted(Rule.objects.filter(release_version=version)
                                .values_list("release_dependency", flat=True)), ["", "lima@1.0", "qemu@1.0"])

    def homebrew_routes(self, colima, lima, **others):
        """colima 0.10.3 and lima 2.0 are current (formulae.brew.sh); the registry has their older versions.
        colima / lima / others: {tag: (created, tab dependencies or None[, "all"])}; a dependency of the tab is
        {name: version} (direct) or {name: (version, False)} (indirect)"""
        routes = {}

        def formula(name, version, dependencies):
            bottle = tar_gz({f"{name}/{version}/bin/{name}": unsigned(f"{name}-{version}")})
            routes[f"https://formulae.brew.sh/api/formula/{name}.json"] = FakeResponse({
                "versions": {"stable": version}, "revision": 0, "dependencies": dependencies,
                "bottle": {"stable": {"files": {"arm64_sequoia": {
                    "url": f"https://ghcr.example/{name}", "sha256": hashlib.sha256(bottle).hexdigest()}}}}})
            routes[f"https://ghcr.example/{name}"] = FakeResponse(content=bottle)

        def registry(name, tags):
            base = f"https://ghcr.io/v2/homebrew/core/{name}"
            routes[f"{base}/tags/list"] = FakeResponse({"tags": list(tags)})
            for tag, (created, dependencies, *bottle_tag) in tags.items():
                bottle_tag = bottle_tag[0] if bottle_tag else "arm64_sequoia"
                version = tag.split("-")[0]
                bottle = tar_gz({f"{name}/{version}/bin/{name}": unsigned(f"{name}-{tag}")})
                digest = hashlib.sha256(bottle).hexdigest()
                bottle_annotations = {"org.opencontainers.image.ref.name": f"{version}.{bottle_tag}",
                                      "sh.brew.bottle.digest": digest}
                if dependencies is not None:
                    bottle_annotations["sh.brew.tab"] = json.dumps({"runtime_dependencies": [
                        {"full_name": other, "pkg_version": other_version[0] if isinstance(other_version, tuple)
                         else other_version, "declared_directly": not isinstance(other_version, tuple)}
                        for other, other_version in dependencies.items()]})
                # a bottle "all" is built on Linux
                routes[f"{base}/manifests/{tag}"] = FakeResponse({
                    "annotations": {"org.opencontainers.image.version": version,
                                    "org.opencontainers.image.created": created},
                    "manifests": [{"platform": {"os": "linux" if bottle_tag == "all" else "darwin"},
                                   "annotations": bottle_annotations}]})
                routes[f"{base}/blobs/sha256:{digest}"] = FakeResponse(content=bottle)

        formula("colima", "0.10.3", ["lima"])
        formula("lima", "2.0", [])
        registry("colima", colima)
        registry("lima", lima)
        for name, tags in others.items():
            registry(name, tags)
        return routes

    def dependency_labels(self, version):
        return sorted(Rule.objects.filter(release_version__version=version)
                      .values_list("release_dependency", flat=True))

    def colima_source(self, **options):
        return ReleaseSource.objects.create(name="colima", kind=ReleaseSource.Kind.HOMEBREW_FORMULA,
                                            identifier="colima", is_global=True, include_dependencies=True,
                                            **options)

    def test_an_older_formula_gets_the_dependencies_its_bottle_was_built_with(self):
        routes = self.homebrew_routes(
            colima={"0.10.2": ("2026-09-01T08:00:00Z", {"lima": "1.0.2"}), "0.10.3": ("2026-09-20T08:00:00Z", None)},
            # a rebuild of lima 1.0.2: the newest build
            lima={"1.0.2": ("2026-08-01T08:00:00Z", {}), "1.0.2-1": ("2026-08-02T08:00:00Z", {}),
                  "2.0": ("2026-09-10T08:00:00Z", {})})
        source = self.colima_source(keep_versions=2, approve_kept_versions=True)
        sync_release_source(source, fake_session(routes))
        self.assertEqual(self.dependency_labels("0.10.3"), ["", "lima@2.0"])
        self.assertEqual(self.dependency_labels("0.10.2"), ["", "lima@1.0.2"])
        self.assertTrue(Rule.objects.filter(identifier=hashlib.sha256(unsigned("lima-1.0.2-1")).hexdigest(),
                                            release_version__version="0.10.2").exists())

    def test_a_dependency_rebuilt_later_comes_in_the_build_of_that_day(self):
        # lima 1.0.2 was rebuilt after colima 0.10.2: the Macs of then have the first build
        routes = self.homebrew_routes(
            colima={"0.10.2": ("2026-09-01T08:00:00Z", {"lima": "1.0.2"}), "0.10.3": ("2026-09-20T08:00:00Z", None)},
            lima={"1.0.2": ("2026-08-01T08:00:00Z", {}), "1.0.2-1": ("2026-09-15T08:00:00Z", {}),
                  "2.0": ("2026-09-10T08:00:00Z", {})})
        source = self.colima_source(keep_versions=2, approve_kept_versions=True)
        sync_release_source(source, fake_session(routes))
        identifiers = set(Rule.objects.filter(release_version__version="0.10.2", release_dependency="lima@1.0.2")
                          .values_list("identifier", flat=True))
        self.assertEqual(identifiers, {hashlib.sha256(unsigned("lima-1.0.2")).hexdigest()})

    def test_a_bottle_for_all_systems_takes_its_direct_dependencies(self):
        # built on Linux: zlib comes with macOS; qemu comes through the macOS bottle of lima
        routes = self.homebrew_routes(
            colima={"0.10.2": ("2026-09-01T08:00:00Z", {"lima": "1.0.2", "zlib": ("1.3", False)}, "all"),
                    "0.10.3": ("2026-09-20T08:00:00Z", None)},
            lima={"1.0.2": ("2026-08-01T08:00:00Z", {"qemu": "9.0"}), "2.0": ("2026-09-10T08:00:00Z", {})},
            qemu={"9.0": ("2026-07-01T08:00:00Z", {})})
        source = self.colima_source(keep_versions=2, approve_kept_versions=True)
        sync_release_source(source, fake_session(routes))
        self.assertEqual(self.dependency_labels("0.10.2"), ["", "lima@1.0.2", "qemu@9.0"])

    def test_the_tags_of_the_registry_come_in_pages(self):
        base = "https://ghcr.io/v2/homebrew/core/node"
        first, second = FakeResponse({"tags": ["20.0.0", "21.0.0"]}), FakeResponse({"tags": ["22.0.0"]})
        first.links = {"next": {"url": "/v2/homebrew/core/node/tags/list?last=21.0.0&n=0"}}
        session = MagicMock()
        session.get.side_effect = lambda url, params=None, **kwargs: (
            second if (params or {}).get("last") == "21.0.0" else first) if url == f"{base}/tags/list" else None
        self.assertEqual(_ghcr_tags(session, "node"), ["22.0.0", "21.0.0", "20.0.0"])

    def test_without_their_versions_the_dependencies_of_the_day(self):
        # the bottle of colima 0.10.2 names no versions: lima as it was at the end of that day
        routes = self.homebrew_routes(
            colima={"0.10.2": ("2026-09-01T08:00:00Z", None), "0.10.3": ("2026-09-20T08:00:00Z", None)},
            lima={"1.0": ("2026-08-01T08:00:00Z", {}), "1.1": ("2026-09-01T20:00:00Z", {}),
                  "1.2": ("2026-09-05T08:00:00Z", {}), "2.0": ("2026-09-10T08:00:00Z", {})})
        source = self.colima_source(keep_versions=2, approve_kept_versions=True)
        sync_release_source(source, fake_session(routes))
        self.assertEqual(self.dependency_labels("0.10.2"), ["", "lima@1.1"])

    def test_a_recorded_older_formula_gets_the_dependencies_of_its_bottle(self):
        routes = self.homebrew_routes(
            colima={"0.10.2": ("2026-09-01T08:00:00Z", {"lima": "1.0.2"}), "0.10.3": ("2026-09-20T08:00:00Z", None)},
            lima={"1.0.2": ("2026-08-01T08:00:00Z", {}), "2.0": ("2026-09-10T08:00:00Z", {})})
        source = self.colima_source()
        # recorded before the source took the dependencies
        old = ReleaseVersion.objects.create(source=source, identifier="colima", version="0.10.2")
        Rule.objects.create(rule_type=RuleType.BINARY, identifier="a" * 64, release_source=source,
                            release_version=old, is_global=True)
        sync_release_source(source, fake_session(routes))
        self.assertEqual(self.dependency_labels("0.10.2"), ["", "lima@1.0.2"])
        self.assertEqual(self.dependency_labels("0.10.3"), ["", "lima@2.0"])

    def test_resolved_again_the_dependencies_are_replaced(self):
        routes = self.homebrew_routes(
            colima={"0.10.2": ("2026-09-01T08:00:00Z", {"lima": "1.0.2"}), "0.10.3": ("2026-09-20T08:00:00Z", None)},
            lima={"1.0.2": ("2026-08-01T08:00:00Z", {}), "2.0": ("2026-09-10T08:00:00Z", {})})
        source = self.colima_source()
        lima_sha = hashlib.sha256(unsigned("lima-1.0.2")).hexdigest()
        current_sha = hashlib.sha256(unsigned("lima-2.0")).hexdigest()
        # recorded with the dependencies of the current formula, and one binary that stays
        old = ReleaseVersion.objects.create(
            source=source, identifier="colima", version="0.10.2", published_at=timezone.now(),
            dependencies=["lima@2.0"], binary_count=3, dependency_binary_count=2,
            notes=f"colima.tar.gz: bin/colima {'a' * 64}\nlima@2.0 – lima.tar.gz: bin/lima {current_sha}\n"
                  "Dependencies: lima@2.0")
        Rule.objects.create(rule_type=RuleType.BINARY, identifier="a" * 64, release_source=source,
                            release_version=old, is_global=True, is_enabled=True,
                            release_files=[{"sha256": "a" * 64}])
        Rule.objects.create(rule_type=RuleType.BINARY, identifier=current_sha, release_source=source,
                            release_version=old, is_global=True, release_dependency="lima@2.0")
        kept = Rule.objects.create(rule_type=RuleType.BINARY, identifier=lima_sha, release_source=source,
                                   release_version=old, is_global=True, release_dependency="lima@2.0")
        sync_release_source(source, fake_session(routes))
        old.refresh_from_db()
        self.assertEqual(old.dependencies, ["lima@1.0.2"])
        self.assertEqual(set(old.rules.values_list("identifier", flat=True)), {"a" * 64, lima_sha})
        self.assertTrue(Rule.objects.filter(pk=kept.pk).exists())
        # the rule kept belongs to the dependency it comes from now (the versions table matches the labels)
        self.assertEqual(Rule.objects.get(pk=kept.pk).release_dependency, "lima@1.0.2")
        self.assertEqual((old.binary_count, old.dependency_binary_count), (2, 1))
        self.assertNotIn("lima@2.0", old.notes)
        self.assertEqual(old.notes.count("Dependencies: "), 1)
        self.assertIn("colima.tar.gz: bin/colima", old.notes)

    def test_a_dependency_of_several_versions_is_downloaded_once(self):
        routes = self.homebrew_routes(
            colima={"0.10.1": ("2026-08-20T08:00:00Z", {"lima": "1.0.2"}),
                    "0.10.2": ("2026-09-01T08:00:00Z", {"lima": "1.0.2"}), "0.10.3": ("2026-09-20T08:00:00Z", None)},
            lima={"1.0.2": ("2026-08-01T08:00:00Z", {}), "2.0": ("2026-09-10T08:00:00Z", {})})
        source = self.colima_source()
        # recorded before: resolved one after the other, each its own download batch
        for version in ("0.10.1", "0.10.2"):
            ReleaseVersion.objects.create(source=source, identifier="colima", version=version)
        session = fake_session(routes)
        sync_release_source(source, session)
        self.assertEqual(self.dependency_labels("0.10.1"), ["lima@1.0.2"])
        self.assertEqual(self.dependency_labels("0.10.2"), ["lima@1.0.2"])
        urls = [call.args[0] for call in session.get.call_args_list]
        self.assertEqual(len([url for url in urls if url.startswith("https://ghcr.io/v2/homebrew/core/lima/blobs/")]),
                         1)
        self.assertEqual(urls.count("https://ghcr.io/v2/homebrew/core/lima/manifests/1.0.2"), 1)

    def test_dependencies_that_failed_for_now_are_resolved_at_the_next_check(self):
        routes = self.homebrew_routes(
            colima={"0.10.2": ("2026-09-01T08:00:00Z", {"lima": "1.0.2"}), "0.10.3": ("2026-09-20T08:00:00Z", None)},
            lima={"1.0.2": ("2026-08-01T08:00:00Z", {}), "2.0": ("2026-09-10T08:00:00Z", {})})
        source = self.colima_source()
        old = ReleaseVersion.objects.create(source=source, identifier="colima", version="0.10.2")
        working = routes["https://ghcr.io/v2/homebrew/core/lima/manifests/1.0.2"]
        routes["https://ghcr.io/v2/homebrew/core/lima/manifests/1.0.2"] = FailingResponse(429)
        sync_release_source(source, fake_session(routes))
        old.refresh_from_db()
        self.assertFalse(old.dependencies_resolved)
        self.assertEqual(self.dependency_labels("0.10.2"), [])
        routes["https://ghcr.io/v2/homebrew/core/lima/manifests/1.0.2"] = working
        sync_release_source(source, fake_session(routes))
        old.refresh_from_db()
        self.assertTrue(old.dependencies_resolved)
        self.assertEqual(self.dependency_labels("0.10.2"), ["lima@1.0.2"])
    def test_a_bottle_without_dependencies(self):
        # its tab lists none: not the dependencies of the current formula
        routes = self.homebrew_routes(
            colima={"0.10.2": ("2026-09-01T08:00:00Z", {}), "0.10.3": ("2026-09-20T08:00:00Z", None)},
            lima={"1.0": ("2026-08-01T08:00:00Z", {}), "2.0": ("2026-09-10T08:00:00Z", {})})
        source = self.colima_source(keep_versions=2, approve_kept_versions=True)
        sync_release_source(source, fake_session(routes))
        self.assertEqual(self.dependency_labels("0.10.2"), [""])
        self.assertEqual(ReleaseVersion.objects.get(version="0.10.2").dependencies, [])

    def test_a_failed_dependency_keeps_the_rules_the_version_had(self):
        routes = self.homebrew_routes(
            colima={"0.10.2": ("2026-09-01T08:00:00Z", {"lima": "1.0.2"}), "0.10.3": ("2026-09-20T08:00:00Z", None)},
            lima={"1.0.2": ("2026-08-01T08:00:00Z", {}), "2.0": ("2026-09-10T08:00:00Z", {})})
        # lima 1.0.2 can't be looked up
        del routes["https://ghcr.io/v2/homebrew/core/lima/manifests/1.0.2"]
        source = self.colima_source()
        old = ReleaseVersion.objects.create(source=source, identifier="colima", version="0.10.2",
                                            dependencies=["lima@2.0"])
        allowed = Rule.objects.create(rule_type=RuleType.BINARY, identifier="b" * 64, release_source=source,
                                      release_version=old, is_global=True, release_dependency="lima@2.0",
                                      release_files=[{"sha256": "b" * 64}])
        sync_release_source(source, fake_session(routes))
        self.assertTrue(Rule.objects.filter(pk=allowed.pk).exists())
        old.refresh_from_db()
        self.assertIn("Dependency lima", old.notes)

    def test_without_a_tab_the_day_of_the_bottle_in_the_registry(self):
        # recorded as the current version (no date of its own): its bottle is of 2026-09-01, lima 2.0 came later
        routes = self.homebrew_routes(
            colima={"0.10.2": ("2026-09-01T08:00:00Z", None), "0.10.3": ("2026-09-20T08:00:00Z", None)},
            lima={"1.0": ("2026-08-01T08:00:00Z", {}), "2.0": ("2026-09-10T08:00:00Z", {})})
        source = self.colima_source()
        ReleaseVersion.objects.create(source=source, identifier="colima", version="0.10.2")
        sync_release_source(source, fake_session(routes))
        self.assertEqual(self.dependency_labels("0.10.2"), ["lima@1.0"])

    def test_the_migration_resolves_the_older_homebrew_versions_again(self):
        migration = importlib.import_module("santa.migrations.0028_reresolve_homebrew_dependencies")
        source = self.colima_source()
        without = ReleaseSource.objects.create(name="no deps", kind=ReleaseSource.Kind.HOMEBREW_FORMULA,
                                               identifier="colima", is_global=True)
        npm = ReleaseSource.objects.create(name="npm", kind=ReleaseSource.Kind.NPM_PACKAGE, identifier="tool",
                                           is_global=True, include_dependencies=True)
        now = timezone.now()
        versions = {
            "older": ReleaseVersion.objects.create(source=source, identifier="colima", version="1",
                                                   published_at=now, dependencies_resolved=True),
            # recorded as the current version, without a date: a newer one came since
            "undated": ReleaseVersion.objects.create(source=source, identifier="colima", version="2",
                                                     dependencies_resolved=True),
            "without": ReleaseVersion.objects.create(source=without, identifier="colima", version="1",
                                                     published_at=now, dependencies_resolved=True),
            "npm": ReleaseVersion.objects.create(source=npm, identifier="tool", version="1", published_at=now,
                                                 dependencies_resolved=True),
        }
        migration.reresolve_homebrew_dependencies(django_apps, None)
        self.assertEqual({name: ReleaseVersion.objects.get(pk=version.pk).dependencies_resolved
                          for name, version in versions.items()},
                         {"older": False, "undated": False, "without": True, "npm": True})


class CleanupTestCase(ConsoleBase):
    def setUp(self):
        super().setUp()
        self.source = ReleaseSource.objects.create(name="tools", kind=ReleaseSource.Kind.GITHUB_RELEASE,
                                                   identifier="abiosoft/colima", is_global=True, keep_versions=0)
        now = timezone.now()
        self.versions = {}
        for identifier, version, days in (("abiosoft/colima", "v1.0", 3), ("abiosoft/colima", "v2.0", 2),
                                          ("abiosoft/colima", "v2.1", 1), ("lima-vm/lima", "v1", 1)):
            release_version = ReleaseVersion.objects.create(source=self.source, identifier=identifier,
                                                            version=version, published_at=now - timedelta(days=days))
            Rule.objects.create(rule_type=RuleType.BINARY, identifier=hashlib.sha256(version.encode()).hexdigest(),
                                release_source=self.source, release_version=release_version, is_global=True)
            self.versions[version] = release_version
        Rule.objects.create(rule_type=RuleType.BINARY, identifier="d" * 64, release_source=self.source,
                            release_version=self.versions["v2.1"], release_dependency="dep@1.0", is_global=True)

    def test_preview_and_clean_up(self):
        self.source.version_pattern = r"^v2\."
        self.source.save()
        versions, rule_count, dependency_rule_count = cleanup_preview(self.source)
        self.assertEqual({version.version for version in versions}, {"v1.0", "v1"})
        self.assertEqual((rule_count, dependency_rule_count), (2, 1))
        response = self.client.get(reverse("console:source_cleanup", args=(self.source.pk,)))
        self.assertContains(response, "package removed")
        self.assertContains(response, "outside the version pattern")
        # the preview changes nothing
        self.assertEqual(self.source.versions.count(), 4)
        response = self.client.post(reverse("console:source_cleanup", args=(self.source.pk,)))
        self.assertRedirects(response, reverse("console:source", args=(self.source.pk,)))
        self.assertEqual(sorted(self.source.versions.values_list("version", flat=True)), ["v2.0", "v2.1"])
        # the dependencies are not allowed (any more): their rules go too
        self.assertFalse(Rule.objects.exclude(release_dependency="").exists())
        self.assertEqual(Rule.objects.filter(release_source=self.source).count(), 2)
        self.assertTrue(LogEntry.objects.filter(object_id=str(self.source.pk),
                                                change_message__startswith="Cleaned up").exists())

    def test_the_versions_outside_the_range_to_keep(self):
        self.source.keep_versions = 1
        self.source.include_dependencies = False
        self.source.identifier = "abiosoft/colima\nlima-vm/lima"
        self.source.save()
        self.assertEqual(cleanup_release_source(self.source), (2, 3))
        self.assertEqual(sorted(self.source.versions.values_list("version", flat=True)), ["v1", "v2.1"])

    def test_the_detail_shows_what_is_not_covered(self):
        self.source.version_pattern = r"^v2\."
        self.source.save()
        response = self.client.get(reverse("console:source", args=(self.source.pk,)))
        self.assertContains(response, "not covered")
        self.assertContains(response, reverse("console:source_cleanup", args=(self.source.pk,)))

    def test_nothing_to_clean_up(self):
        self.source.identifier = "abiosoft/colima\nlima-vm/lima"
        self.source.save()
        Rule.objects.exclude(release_dependency="").delete()
        self.assertContains(self.client.get(reverse("console:source_cleanup", args=(self.source.pk,))),
                            "Nothing to clean up")

    def test_needs_the_permission_to_delete_rules(self):
        staff = User.objects.create_user("viewer", password="pw", is_staff=True)
        staff.user_permissions.add(*Permission.objects.filter(codename__in=["view_releasesource",
                                                                            "change_releasesource"]))
        self.client.force_login(staff)
        response = self.client.post(reverse("console:source_cleanup", args=(self.source.pk,)))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.source.versions.count(), 4)

    def test_options_the_catalog_hides_are_switched_off(self):
        # ticked for npm, then the catalog changed to a cask: the browser still sends the hidden checkboxes
        data = {"name": "tools", "kind": "HOMEBREW_CASK", "identifier": "figma", "rule_type": "BINARY",
                "policy": "ALLOWLIST", "is_global": "on", "keep_versions": "3", "keep_unit": "VERSIONS",
                "auto_approve_delay_days": "0", "include_dependencies": "on", "approve_kept_versions": "on"}
        response = self.client.post(reverse("console:source_edit", args=(self.source.pk,)), data)
        self.assertEqual(response.status_code, 302)
        self.source.refresh_from_db()
        self.assertEqual((self.source.include_dependencies, self.source.approve_kept_versions), (False, False))
        # with 0 (keep all) no older versions either
        data.update(kind="GITHUB_RELEASE", identifier="abiosoft/colima", keep_versions="0")
        self.client.post(reverse("console:source_edit", args=(self.source.pk,)), data)
        self.source.refresh_from_db()
        self.assertFalse(self.source.approve_kept_versions)

    def test_saving_the_form_names_the_versions_no_longer_covered(self):
        data = {"name": "tools", "kind": "GITHUB_RELEASE", "identifier": "abiosoft/colima", "rule_type": "BINARY",
                "policy": "ALLOWLIST", "is_global": "on", "keep_versions": "0", "keep_unit": "VERSIONS",
                "auto_approve_delay_days": "0"}
        response = self.client.post(reverse("console:source_edit", args=(self.source.pk,)), data, follow=True)
        self.assertContains(response, "1 version is no longer covered by the package rule")
        # nothing deleted without the clean up
        self.assertEqual(self.source.versions.count(), 4)


class FasterChecksTestCase(ReleaseSourceBase):
    def test_new_packages_first(self):
        source = ReleaseSource.objects.create(name="tools", kind=ReleaseSource.Kind.GITHUB_RELEASE,
                                              identifier="o/a\no/b\no/c", is_global=True,
                                              last_checked_at=timezone.now())
        ReleaseVersion.objects.create(source=source, identifier="o/b", version="1")
        calls = []
        with patch("santa.releases._sync_identifier",
                   side_effect=lambda session, source, identifier, **kwargs: calls.append(identifier) or ([], [])):
            sync_release_source(source, fake_session({}))
        self.assertEqual(calls, ["o/a", "o/c", "o/b"])

    def test_no_index_for_the_versions_recorded_already(self):
        base = "https://ghcr.io/v2/homebrew/core/colima"
        older = tar_gz({"colima/0.10.1/bin/colima": unsigned("colima-0.10.1")})
        digest = hashlib.sha256(older).hexdigest()
        current = tar_gz({"colima/0.10.3/bin/colima": unsigned("colima-0.10.3")})
        routes = {
            "https://formulae.brew.sh/api/formula/colima.json": FakeResponse({
                "versions": {"stable": "0.10.3"}, "revision": 0, "bottle": {"stable": {"files": {
                    "arm64_sequoia": {"url": "https://ghcr.example/colima",
                                      "sha256": hashlib.sha256(current).hexdigest()}}}}}),
            f"{base}/tags/list": FakeResponse({"tags": ["0.10.1", "0.10.2", "0.10.2-1", "0.10.3"]}),
            # only the version not recorded yet: 0.10.3, 0.10.2 and its rebuild are known
            f"{base}/manifests/0.10.1": FakeResponse({
                "annotations": {"org.opencontainers.image.version": "0.10.1",
                                "org.opencontainers.image.created": "2026-09-01T10:00:00Z"},
                "manifests": [{"platform": {"os": "darwin"}, "annotations": {
                    "org.opencontainers.image.ref.name": "0.10.1.arm64_sequoia", "sh.brew.bottle.digest": digest}}]}),
            f"{base}/blobs/sha256:{digest}": FakeResponse(content=older),
        }
        source = ReleaseSource.objects.create(name="colima", kind=ReleaseSource.Kind.HOMEBREW_FORMULA,
                                              identifier="colima", is_global=True, keep_versions=3,
                                              approve_kept_versions=True)
        for version in ("0.10.3", "0.10.2"):
            ReleaseVersion.objects.create(source=source, identifier="colima", version=version, binary_count=1)
        # recorded by a check that ended
        source.last_checked_at = timezone.now()
        source.save()
        session = fake_session(routes)
        new_versions = sync_release_source(source, session)
        self.assertEqual([version.version for version in new_versions], ["0.10.1"])
        asked = [call.args[0] for call in session.get.call_args_list]
        self.assertNotIn(f"{base}/manifests/0.10.2", asked)
        self.assertNotIn(f"{base}/manifests/0.10.2-1", asked)

    def test_the_files_of_a_release_at_once(self):
        source = ReleaseSource(name="tool", kind=ReleaseSource.Kind.GITHUB_RELEASE, identifier="o/tool")
        same = unsigned("tool-a")
        assets = [Asset(name="a.zip", url="https://x/a", sha256=hashlib.sha256(zip_file({"bin/a": same})).hexdigest()),
                  Asset(name="broken.zip", url="https://x/broken"),
                  Asset(name="b.zip", url="https://x/b")]
        routes = {"https://x/a": FakeResponse(content=zip_file({"bin/a": same})),
                  "https://x/broken": FakeResponse(content=b"x" * 5000),
                  "https://x/b": FakeResponse(content=zip_file({"bin/b": unsigned("tool-b")}))}
        # the same file twice: downloaded once
        assets.append(Asset(name="a-again.zip", url="https://x/a-again", sha256=assets[0].sha256))
        session = fake_session(routes)
        found, errors = collect_binaries(session, source, Release(version="1", assets=assets))
        self.assertEqual([(binary.asset, binary.path) for binary in found],
                         [("a.zip", "bin/a"), ("b.zip", "bin/b"), ("a-again.zip", "bin/a")])
        self.assertEqual(len(errors), 1)
        self.assertIn("broken.zip", errors[0])
        self.assertEqual(sorted(call.args[0] for call in session.get.call_args_list),
                         ["https://x/a", "https://x/b", "https://x/broken"])

    def test_versions_of_a_check_that_never_ended_are_announced(self):
        source = ReleaseSource.objects.create(name="tools", kind=ReleaseSource.Kind.GITHUB_RELEASE,
                                              identifier="o/a", is_global=True,
                                              last_checked_at=timezone.now() - timedelta(hours=2))
        # recorded by a check whose server was killed: never announced
        orphan = ReleaseVersion.objects.create(source=source, identifier="o/a", version="1", binary_count=1)
        with patch("santa.releases._sync_identifier", return_value=([], [])), \
                patch("santa.releases.notifications.versions_found") as found:
            self.assertEqual(sync_release_source(source, fake_session({})), [orphan])
            found.assert_called_once_with(source, [orphan])
            # announced once: the check after it has nothing
            sync_release_source(source, fake_session({}))
            self.assertEqual(found.call_args.args[1], [])

    def test_a_shutdown_ends_the_check_after_the_current_package(self):
        source = ReleaseSource.objects.create(name="tools", kind=ReleaseSource.Kind.GITHUB_RELEASE,
                                              identifier="o/a\no/b", is_global=True)
        self.addCleanup(SHUTTING_DOWN.clear)

        def sync_identifier(session, source, identifier, **kwargs):
            SHUTTING_DOWN.set()
            return [], []

        progress = Progress()
        with patch("santa.releases._sync_identifier", side_effect=sync_identifier) as sync:
            sync_release_source(source, fake_session({}), progress=progress)
        self.assertEqual(sync.call_count, 1)
        self.assertEqual(progress.cancelled_by, SHUTDOWN)
        source.refresh_from_db()
        self.assertIsNotNone(source.last_checked_at)

    def test_the_console_says_the_server_restarts(self):
        source = ReleaseSource.objects.create(name="tools", kind=ReleaseSource.Kind.GITHUB_RELEASE,
                                              identifier="o/a\no/b", is_global=True)
        self.addCleanup(SHUTTING_DOWN.clear)
        self.addCleanup(cache.delete_many, [f"santa:source-check:{source.pk}",
                                            f"santa:source-check:{source.pk}:cancel"])
        admin = User.objects.create_superuser("admin", "admin@example.com", "pw")
        self.client.force_login(admin)

        def sync(source, progress, only=None):
            progress.add_total(2)
            progress.start("o/a")
            SHUTTING_DOWN.set()
            try:
                progress.start("o/b")
            except Cancelled:
                return []

        with patch("santa.console.views_sources.sync_release_source", side_effect=sync):
            response = self.client.post(reverse("console:source_check", args=(source.pk,)), follow=True)
        self.assertContains(response, "the server is restarting, the check stopped after 1 of 2 packages")

    def test_the_gunicorn_worker_stops_the_checks_on_sigterm(self):
        spec = importlib.util.spec_from_file_location("gunicorn_conf", "gunicorn.conf.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        received = []
        previous = signal.signal(signal.SIGTERM, lambda signum, frame: received.append(signum))
        self.addCleanup(signal.signal, signal.SIGTERM, previous)
        self.addCleanup(SHUTTING_DOWN.clear)
        module.post_worker_init(None)
        signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
        self.assertTrue(SHUTTING_DOWN.is_set())
        self.assertEqual(received, [signal.SIGTERM])

    def test_the_lock_of_the_job_is_held_while_it_runs(self):
        from santa.management.commands import sync_release_sources as command
        ReleaseSource.objects.create(name="tools", kind=ReleaseSource.Kind.GITHUB_RELEASE, identifier="o/a",
                                     is_global=True)
        seen = []

        def slow(source, session, progress):
            time.sleep(1.6)
            seen.append(cache.get(command.LOCK_KEY))
            return []

        # the cache counts whole seconds
        with patch.object(command, "LOCK_TIMEOUT", 1), patch.object(command, "LOCK_HEARTBEAT", 0.2), \
                patch.object(command, "sync_release_source", side_effect=slow):
            call_command("sync_release_sources", stdout=io.StringIO())
        # beyond its timeout, refreshed by the heartbeat; deleted at the end
        self.assertEqual(seen, ["1"])
        self.assertIsNone(cache.get(command.LOCK_KEY))


class PackageProblemsTestCase(ReleaseSourceBase):
    @override_settings(RELEASE_MAX_UNPACKED_BYTES=20000)
    def test_only_executables_are_unpacked(self):
        # the libraries and data of a compiler: gigabytes, never unpacked, never counted
        bottle = tar_gz({"ghc/9/lib/libHSbase.a": b"!<arch>\n" + b"x" * 50000,
                         "ghc/9/lib/libffi.dylib": build_macho(filetype=6) + b"x" * 50000,
                         "ghc/9/share/doc.html": b"<html>" + b"x" * 50000,
                         "ghc/9/bin/ghc": unsigned("ghc")})
        found = list(find_binaries(io.BytesIO(bottle), "ghc.bottle.tar.gz"))
        self.assertEqual([path for path, _info in found], ["ghc/9/bin/ghc"])

    def test_a_formula_only_for_linux(self):
        routes = {"https://formulae.brew.sh/api/formula/acl.json": FakeResponse({
            "versions": {"stable": "2.4.0"}, "revision": 0, "requirements": [{"name": "linux"}],
            "bottle": {"stable": {"files": {"x86_64_linux": {"url": "https://x/acl", "sha256": "0" * 64}}}}})}
        source = ReleaseSource.objects.create(name="acl", kind=ReleaseSource.Kind.HOMEBREW_FORMULA,
                                              identifier="acl", is_global=True)
        with self.assertRaises(ReleaseError) as cm:
            sync_release_source(source, fake_session(routes))
        self.assertIn("Only for Linux", str(cm.exception))
        entry = catalog._homebrew_entry(ReleaseSource.Kind.HOMEBREW_FORMULA, {
            "name": "acl", "versions": {"stable": "2.4.0"}, "requirements": [{"name": "linux"}]})
        self.assertTrue(catalog._homebrew_suggestion(entry).linux_only)

    def test_a_dropped_connection_is_asked_again(self):
        adapter = http_session().get_adapter("https://ghcr.io/")
        self.assertEqual(adapter.max_retries.total, 5)
        self.assertIn(503, adapter.max_retries.status_forcelist)

    def test_a_rate_limit_is_waited_for_a_minute_at_most(self):
        retry = http_session().get_adapter("https://ghcr.io/").max_retries
        self.assertTrue(retry.respect_retry_after_header)
        response = MagicMock(status=429)
        response.headers = {"Retry-After": "5"}
        self.assertEqual(retry.get_retry_after(response), 5)
        response.headers = {"Retry-After": "3600"}
        self.assertEqual(retry.get_retry_after(response), 60)
        # the Retry of the next attempt keeps the limit
        self.assertEqual(retry.increment(method="GET", url="/").get_retry_after(response), 60)

    def test_the_errors_per_package_once(self):
        source = ReleaseSource.objects.create(name="tools", kind=ReleaseSource.Kind.HOMEBREW_FORMULA,
                                              identifier="acl\nagda", is_global=True)
        for version in ("2.8.0-r3", "2.8.0.1"):
            ReleaseVersion.objects.create(source=source, identifier="agda", version=version)
        source.last_error = "\n".join([
            "acl: Only for Linux",
            "agda: 2.8.0-r3: ghc@9.14.1: ghc.tar.gz: unpacks to more than 2147483648 bytes",
            "agda: 2.8.0.1: ghc@9.14.1: ghc.tar.gz: unpacks to more than 2147483648 bytes",
            "agda: agda.tar.gz: bigger than 524288000 bytes"])
        source.save()
        self.assertEqual(error_groups(source), [
            ("acl", [("Only for Linux", [])]),
            ("agda", [("ghc@9.14.1: ghc.tar.gz: unpacks to more than 2147483648 bytes", ["2.8.0-r3", "2.8.0.1"]),
                      ("agda.tar.gz: bigger than 524288000 bytes", [])])])
        admin = User.objects.create_superuser("admin", "admin@example.com", "pw")
        self.client.force_login(admin)
        response = self.client.get(reverse("console:source", args=(source.pk,)))
        self.assertContains(response, "Problems at the last check: 2 packages")
        self.assertContains(response, "2 versions: 2.8.0-r3, 2.8.0.1")


class RemoveLinuxPackagesTestCase(ReleaseSourceBase):
    def setUp(self):
        super().setUp()
        self.admin = User.objects.create_superuser("admin", "admin@example.com", "pw")
        self.client.force_login(self.admin)
        self.source = ReleaseSource.objects.create(
            name="tools", kind=ReleaseSource.Kind.HOMEBREW_FORMULA, identifier="acl\nafuse\njq\nagda", is_global=True,
            last_error="\n".join([f"acl: {LINUX_ONLY_ERROR}", f"afuse: {LINUX_ONLY_ERROR}",
                                  "agda: agda.tar.gz: bigger than 524288000 bytes"]))

    def test_only_the_packages_that_cannot_work_are_offered(self):
        self.assertEqual(removable_packages(self.source), ["acl", "afuse"])
        alone = ReleaseSource.objects.create(name="acl", kind=ReleaseSource.Kind.HOMEBREW_FORMULA, identifier="acl",
                                             last_error=LINUX_ONLY_ERROR)
        # a package rule keeps one package
        self.assertEqual(removable_packages(alone), [])
        response = self.client.get(reverse("console:source", args=(self.source.pk,)))
        self.assertContains(response, "Remove the 2 Linux-only packages")
        self.assertContains(response, "Remove acl from the package rule")

    def test_remove_them(self):
        response = self.client.post(reverse("console:source_remove_identifiers", args=(self.source.pk,)),
                                    {"identifier": ["acl", "afuse", "not-in-it"]}, HTTP_HX_REQUEST="true")
        self.source.refresh_from_db()
        self.assertEqual(self.source.identifiers, ["jq", "agda"])
        self.assertEqual(self.source.last_error, "agda: agda.tar.gz: bigger than 524288000 bytes")
        self.assertContains(response, "2 packages removed from tools: acl, afuse.")
        self.assertNotContains(response, "Linux-only")
        self.assertTrue(LogEntry.objects.filter(object_id=str(self.source.pk),
                                                change_message="Removed from the packages: acl, afuse").exists())

    def test_never_all_of_them(self):
        self.client.post(reverse("console:source_remove_identifiers", args=(self.source.pk,)),
                         {"identifier": ["acl", "afuse", "jq", "agda"]})
        self.source.refresh_from_db()
        self.assertEqual(len(self.source.identifiers), 4)

    def test_only_who_may_change_package_rules(self):
        viewer = User.objects.create_user("viewer", is_staff=True)
        viewer.user_permissions.set(Permission.objects.filter(codename="view_releasesource"))
        self.client.force_login(viewer)
        response = self.client.get(reverse("console:source", args=(self.source.pk,)))
        self.assertNotContains(response, "Remove acl from")
        self.assertNotContains(response, "Linux-only packages")
        response = self.client.post(reverse("console:source_remove_identifiers", args=(self.source.pk,)),
                                    {"identifier": ["acl"]})
        self.assertEqual(response.status_code, 403)

    def test_the_drawer_first_the_versions_after_it(self):
        version = ReleaseVersion.objects.create(source=self.source, identifier="jq", version="1.8.2", binary_count=1)
        url = reverse("console:source", args=(self.source.pk,))
        response = self.client.get(url, HTTP_HX_REQUEST="true")
        self.assertContains(response, 'hx-headers=\'{"X-Santa-Rows": "1"}\'')
        self.assertNotContains(response, version.version)
        response = self.client.get(url, HTTP_HX_REQUEST="true", HTTP_X_SANTA_ROWS="1")
        self.assertContains(response, version.version)
        self.assertContains(response, f'id="cleanup-count-{self.source.pk}" hx-swap-oob="true"')
        self.assertNotContains(response, "Problems at the last check")
        # the page at once
        self.assertContains(self.client.get(url), version.version)

    def test_the_first_answer_does_not_grow_with_the_versions(self):
        url = reverse("console:source", args=(self.source.pk,))
        self.client.get(url, HTTP_HX_REQUEST="true")
        with CaptureQueriesContext(connection) as few:
            self.client.get(url, HTTP_HX_REQUEST="true")
        for n in range(30):
            ReleaseVersion.objects.create(source=self.source, identifier="jq", version=f"1.{n}", binary_count=1)
        with CaptureQueriesContext(connection) as many:
            self.client.get(url, HTTP_HX_REQUEST="true")
        self.assertEqual(len(few), len(many))

    def test_the_clean_up_count_does_not_grow_with_the_rules(self):
        self.source.binary_pattern = "bin/*"
        self.source.save()
        version = ReleaseVersion.objects.create(source=self.source, identifier="jq", version="1.8.2", binary_count=1)

        def add(count):
            for n in range(count):
                Rule.objects.create(rule_type=RuleType.BINARY, identifier=f"{n:064x}", release_source=self.source,
                                    release_version=version, release_files=[{"path": f"bin/tool{n}", "sha256": ""}])

        add(2)
        with CaptureQueriesContext(connection) as few:
            stale_rules(self.source)
        add(20)
        with CaptureQueriesContext(connection) as many:
            stale_rules(self.source)
        self.assertEqual(len(few), len(many))


class CheckNewPackagesTestCase(ReleaseSourceBase):
    def setUp(self):
        super().setUp()
        # saving the form looks up the icons in the catalogs: never the network in a test
        patcher = patch("santa.catalog.lookup", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.admin = User.objects.create_superuser("admin", "admin@example.com", "pw")
        self.client.force_login(self.admin)
        self.source = ReleaseSource.objects.create(name="tools", kind=ReleaseSource.Kind.GITHUB_RELEASE,
                                                   identifier="o/a\no/b\no/c", is_global=True,
                                                   last_checked_at=timezone.now(), last_error="o/a: down")
        ReleaseVersion.objects.create(source=self.source, identifier="o/b", version="1", binary_count=1)
        self.addCleanup(cache.delete_many, [f"santa:source-check:{self.source.pk}",
                                            f"santa:source-check:{self.source.pk}:cancel"])

    def test_only_some_packages(self):
        calls = []

        def sync_identifier(session, source, identifier, **kwargs):
            calls.append(identifier)
            raise ReleaseError("not found")

        with patch("santa.releases._sync_identifier", side_effect=sync_identifier), \
                self.assertRaises(ReleaseError):
            sync_release_source(self.source, fake_session({}), only=["o/b"])
        self.assertEqual(calls, ["o/b"])
        self.source.refresh_from_db()
        # the error of the package not checked stays, after the ones of this check
        self.assertEqual(self.source.last_error, "o/b: not found\no/a: down")

    def test_the_errors_are_cut_at_a_line(self):
        lines = [f"o/{n}: {'x' * 90}" for n in range(100)]
        text = _cut_lines(lines, 5000)
        self.assertLessEqual(len(text), 5000)
        self.assertTrue(set(text.splitlines()[:-1]) <= set(lines))
        self.assertRegex(text.splitlines()[-1], r"^… \d+ more$")
        self.assertEqual(_cut_lines(["a", "b"], 5000), "a\nb")

    def test_the_new_ones(self):
        # o/a was checked (its error), o/b has a version: o/c is new
        self.assertEqual(unchecked_identifiers(self.source), ["o/c"])
        response = self.client.get(reverse("console:source", args=(self.source.pk,)))
        self.assertContains(response, "Check the new package")
        with patch("santa.console.views_sources.sync_release_source", return_value=[]) as sync:
            self.client.post(reverse("console:source_check", args=(self.source.pk,)), {"only": "new"})
        self.assertEqual(sync.call_args.kwargs["only"], ["o/c"])

    def test_no_button_when_all_are_new(self):
        fresh = ReleaseSource.objects.create(name="fresh", kind=ReleaseSource.Kind.GITHUB_RELEASE,
                                             identifier="o/x\no/y", is_global=True)
        self.assertEqual(unchecked_identifiers(fresh), ["o/x", "o/y"])
        self.assertNotContains(self.client.get(reverse("console:source", args=(fresh.pk,))), "new package")

    def test_saving_with_added_packages_names_the_button(self):
        data = {"name": "tools", "kind": ReleaseSource.Kind.GITHUB_RELEASE, "identifier": "o/a\no/b\no/c\no/d",
                "is_enabled": "on", "rule_type": "BINARY", "policy": "ALLOWLIST", "is_global": "on",
                "keep_versions": "1", "keep_unit": "VERSIONS", "auto_approve_delay_days": "0"}
        response = self.client.post(reverse("console:source_edit", args=(self.source.pk,)), data, follow=True)
        self.assertContains(response, "1 package added: “Check the new packages” gets its releases.")


class FailingResponse(FakeResponse):
    """A download the server refuses with this status"""

    def __init__(self, status_code):
        super().__init__(status_code=status_code)

    def raise_for_status(self):
        raise requests.HTTPError(f"{self.status_code} Server Error", response=self)


class TemporaryFailuresTestCase(ReleaseSourceBase):
    def routes(self, releases, failing=()):
        routes = KeepVersionsTestCase.github_routes(self, releases)
        for release in releases:
            if release["tag_name"] in failing:
                routes[release["assets"][0]["browser_download_url"]] = FailingResponse(503)
        return routes

    def test_a_temporary_failure_is_tried_again(self):
        source = self.github_source(asset_pattern="")
        releases = [github_release("v3", "2026-09-03T10:00:00Z")]
        with self.assertRaises(ReleaseError):
            sync_release_source(source, fake_session(self.routes(releases, failing=["v3"])))
        self.assertFalse(source.versions.exists())
        source.refresh_from_db()
        self.assertIn("Tried again at the next check", source.last_error)
        [version] = sync_release_source(source, fake_session(self.routes(releases)))
        self.assertEqual((version.version, version.binary_count), ("v3", 1))

    def test_a_missing_file_is_recorded(self):
        # 404: gone at the source, not tried again at every check
        source = self.github_source(keep_versions=2, approve_kept_versions=True, asset_pattern="")
        releases = [github_release("v3", "2026-09-03T10:00:00Z"), github_release("v2", "2026-09-02T10:00:00Z")]
        routes = self.routes(releases)
        routes[releases[1]["assets"][0]["browser_download_url"]] = FailingResponse(404)
        sync_release_source(source, fake_session(routes))
        self.assertEqual(source.versions.get(version="v2").binary_count, 0)

    def test_an_older_version_that_failed_for_now_is_not_recorded(self):
        source = self.github_source(keep_versions=2, approve_kept_versions=True, asset_pattern="")
        releases = [github_release("v3", "2026-09-03T10:00:00Z"), github_release("v2", "2026-09-02T10:00:00Z")]
        sync_release_source(source, fake_session(self.routes(releases, failing=["v2"])))
        self.assertEqual(list(source.versions.values_list("version", flat=True)), ["v3"])
        sync_release_source(source, fake_session(self.routes(releases)))
        self.assertEqual(source.versions.get(version="v2").binary_count, 1)

    def test_a_broken_archive_does_not_stop_the_check(self):
        source = self.github_source(asset_pattern="")
        releases = [github_release("v3", "2026-09-03T10:00:00Z")]
        with patch("santa.releases.find_binaries", side_effect=zlib.error("invalid stored block lengths")), \
                self.assertRaises(ReleaseError):
            sync_release_source(source, fake_session(self.routes(releases)))
        source.refresh_from_db()
        self.assertIn("broken archive", source.last_error)

    def test_an_unexpected_error_inspecting_a_file_is_tried_again(self):
        # not a known problem of the file (a bug, since fixed): the version comes again at the next check
        source = self.github_source(asset_pattern="")
        releases = [github_release("v3", "2026-09-03T10:00:00Z")]
        with patch("santa.releases.find_binaries", side_effect=RuntimeError("unexpected")), \
                self.assertRaises(ReleaseError):
            sync_release_source(source, fake_session(self.routes(releases)))
        self.assertFalse(source.versions.exists())
        [version] = sync_release_source(source, fake_session(self.routes(releases)))
        self.assertEqual(version.binary_count, 1)

    def test_an_unexpected_error_in_one_package_leaves_the_others(self):
        source = ReleaseSource.objects.create(name="tools", kind=ReleaseSource.Kind.GITHUB_RELEASE,
                                              identifier="abiosoft/colima\nlima-vm/lima", is_global=True)
        real = release_module._sync_identifier

        def sync(session, source, identifier, *args, **kwargs):
            if identifier == "abiosoft/colima":
                raise RuntimeError("unexpected")
            return real(session, source, identifier, *args, **kwargs)
        routes = {"https://api.github.com/repos/lima-vm/lima/releases/latest": FakeResponse(
            github_release("v1", "2026-09-01T10:00:00Z", binary_name="limactl")),
            "https://github.example/v1/limactl": FakeResponse(content=unsigned("limactl"))}
        with patch("santa.releases._sync_identifier", side_effect=sync), self.assertRaises(ReleaseError) as raised:
            sync_release_source(source, fake_session(routes))
        self.assertEqual([version.identifier for version in raised.exception.new_versions], ["lima-vm/lima"])
        source.refresh_from_db()
        self.assertIn("abiosoft/colima: RuntimeError: unexpected", source.last_error)

    def test_the_command_goes_on_after_an_unexpected_error(self):
        first = self.github_source(asset_pattern="")
        second = ReleaseSource.objects.create(name="second", kind=ReleaseSource.Kind.GITHUB_RELEASE,
                                              identifier="lima-vm/lima")
        checked = []

        def sync(source, *args, **kwargs):
            checked.append(source.pk)
            if source.pk == first.pk:
                raise RuntimeError("unexpected")
            return []
        with patch("santa.management.commands.sync_release_sources.sync_release_source", side_effect=sync), \
                self.assertRaises(SystemExit):
            call_command("sync_release_sources", stdout=io.StringIO(), stderr=io.StringIO())
        self.assertEqual(sorted(checked), sorted([first.pk, second.pk]))


class BulkRulesTestCase(ReleaseSourceBase):
    def test_many_rules_take_a_few_queries(self):
        source = self.github_source(asset_pattern="")
        tag = Tag.objects.create(name="tools")
        source.tags.add(tag)
        version = ReleaseVersion.objects.create(source=source, identifier="abiosoft/colima", version="v1")
        binaries = [FoundBinary(asset="tools.tar.gz", path=f"bin/tool{n}",
                                info=macho.inspect(io.BytesIO(unsigned(f"tool{n}")))) for n in range(250)]
        with CaptureQueriesContext(connection) as queries:
            rules = create_release_rules(source, version, binaries)
        self.assertEqual(len(rules), 250)
        self.assertLess(len(queries), 20)
        self.assertEqual(version.rules.count(), 250)
        links = Rule.groups.through.objects.filter(rule__release_version=version)
        self.assertEqual(links.filter(group=self.dev).count(), 250)
        self.assertEqual(Rule.tags.through.objects.filter(rule__release_version=version, tag=tag).count(), 250)
        # created again with the same binaries: nothing new
        existing = {(rule.rule_type, rule.identifier) for rule in rules}
        self.assertEqual(create_release_rules(source, version, binaries, existing=existing), [])
