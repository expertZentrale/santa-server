import base64
import hashlib
from datetime import timedelta

from django.contrib.admin.models import LogEntry
from django.contrib.auth.models import Permission, User
from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from santa import semver
from santa.models import ReleaseSource, ReleaseVersion, Rule, RuleType
from santa.releases import cleanup_preview, cleanup_release_source, prune_old_versions, sync_release_source

from .test_console import ConsoleBase
from .test_package_sources import VSCodeTestCase
from .test_releases import FakeResponse, ReleaseSourceBase, fake_session, tar_gz, zip_file
from .utils import build_macho


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
        versions, rule_count, dependency_rules = cleanup_preview(self.source)
        self.assertEqual({version.version for version in versions}, {"v1.0", "v1"})
        self.assertEqual((rule_count, dependency_rules.count()), (2, 1))
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

    def test_saving_the_form_names_the_versions_no_longer_covered(self):
        data = {"name": "tools", "kind": "GITHUB_RELEASE", "identifier": "abiosoft/colima", "rule_type": "BINARY",
                "policy": "ALLOWLIST", "is_global": "on", "keep_versions": "0", "keep_unit": "VERSIONS",
                "auto_approve_delay_days": "0"}
        response = self.client.post(reverse("console:source_edit", args=(self.source.pk,)), data, follow=True)
        self.assertContains(response, "1 version is no longer covered by the package rule")
        # nothing deleted without the clean up
        self.assertEqual(self.source.versions.count(), 4)
