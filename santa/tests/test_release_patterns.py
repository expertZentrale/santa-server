import hashlib
import importlib
import importlib.util
import io
import logging
import time
import uuid
from types import SimpleNamespace
from unittest.mock import patch

from django.apps import apps
from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from santa.log_redaction import RedactSyncTokens, redact_sync_path
from santa.models import Policy, ReleaseSource, ReleaseVersion, Rule, RuleType
from santa.progress import Progress, progress_for, read_progress
from santa.releases import (
    Asset,
    Release,
    ReleaseError,
    _tap_release,
    bottle_coverage,
    cleanup_release_source,
    collect_many,
    find_binaries,
    homebrew_formula_at,
    homebrew_formula_releases,
    latest_homebrew_formula,
    latest_homebrew_tap,
    parse_tap_file,
    pattern_stale_versions,
    stale_rules,
    stale_versions,
    sync_release_source,
    version_matches,
)

from .test_console import ConsoleBase
from .test_releases import FakeResponse, fake_session, tar_gz
from .utils import build_macho

GITHUB_LATEST = "https://api.github.com/repos/abiosoft/colima/releases/latest"


def unsigned(name):
    return build_macho(identifier=name, team_id="", adhoc=True)


def github_routes(assets, tag="v1.0"):
    """The latest release with these assets {name: content} (also as the list of releases), and their downloads"""
    release = {"tag_name": tag, "published_at": "2026-10-01T00:00:00Z", "draft": False, "prerelease": False,
               "assets": [{"name": name, "browser_download_url": f"https://github.example/{tag}/{name}"}
                          for name in assets]}
    routes = {GITHUB_LATEST: FakeResponse(release), GITHUB_LATEST.removesuffix("/latest"): FakeResponse([release])}
    routes.update({f"https://github.example/{tag}/{name}": FakeResponse(content=data)
                   for name, data in assets.items()})
    return routes


def formula_routes(name, tags, version="1.0", revision=0):
    bottles = {tag: {"url": f"https://ghcr.example/{name}/{tag}", "sha256": hashlib.sha256(tag.encode()).hexdigest()}
               for tag in tags}
    return {f"https://formulae.brew.sh/api/formula/{name}.json": FakeResponse({
        "versions": {"stable": version}, "revision": revision, "dependencies": [],
        "bottle": {"stable": {"files": bottles}}})}


class BottleTestCase(SimpleTestCase):
    def test_coverage_like_homebrew(self):
        coverage = bottle_coverage(["arm64_big_sur", "big_sur", "arm64_sonoma"])
        self.assertEqual(coverage["arm64_big_sur"], ["arm64_big_sur", "arm64_monterey", "arm64_ventura"])
        self.assertEqual(coverage["arm64_sonoma"],
                         ["arm64_sonoma", "arm64_sequoia", "arm64_tahoe", "arm64_golden_gate"])
        # the architectures stay apart
        self.assertNotIn("arm64_monterey", coverage["big_sur"])
        self.assertIn("tahoe", coverage["big_sur"])
        self.assertIn("arm64_tahoe", bottle_coverage(["all"])["all"])

    def formula(self, tags, **options):
        source = ReleaseSource(kind=ReleaseSource.Kind.HOMEBREW_FORMULA, **options)
        release = latest_homebrew_formula(fake_session(formula_routes("portable-ruby", tags)), source,
                                          "portable-ruby")
        return [asset.name.split(".")[-4] for asset in release.assets]

    def test_the_nearest_bottle_for_a_pattern(self):
        # portable-ruby: only big_sur bottles, which Homebrew installs on newer macOS too
        tags = ["arm64_big_sur", "big_sur"]
        self.assertEqual(self.formula(tags, asset_pattern="^arm64_sequoia"), ["arm64_big_sur"])
        self.assertEqual(self.formula(tags, asset_pattern="^arm64_sequoia", macos_nearest=False), [])

    def test_the_minimum_macos(self):
        tags = ["arm64_sonoma", "arm64_sequoia", "arm64_tahoe", "sonoma"]
        self.assertEqual(self.formula(tags, macos_minimum="sequoia"), ["arm64_sequoia", "arm64_tahoe", "sonoma"])
        self.assertEqual(self.formula(tags, macos_minimum="sequoia", macos_nearest=False),
                         ["arm64_sequoia", "arm64_tahoe"])
        self.assertEqual(self.formula(["arm64_big_sur"], macos_minimum="tahoe"), ["arm64_big_sur"])
        self.assertEqual(self.formula(["arm64_big_sur"], macos_minimum="tahoe", macos_nearest=False), [])
        self.assertEqual(self.formula(["all"], macos_minimum="tahoe", macos_nearest=False), ["all"])
        self.assertEqual(self.formula(tags, macos_minimum="sequoia", asset_pattern="^arm64_"),
                         ["arm64_sequoia", "arm64_tahoe"])


class PatternsTestCase(SimpleTestCase):
    def test_several_version_patterns(self):
        source = ReleaseSource(version_pattern="^1\\.\n^3\\.")
        self.assertEqual([version for version in ("1.2", "2.0", "3.1") if version_matches(source, version)],
                         ["1.2", "3.1"])
        self.assertTrue(version_matches(ReleaseSource(version_pattern=""), "2.0"))

    def test_several_binary_patterns(self):
        data = tar_gz({f"tool/bin/{name}": unsigned(name) for name in ("a", "b", "c")})
        found = list(find_binaries(io.BytesIO(data), "tool.tar.gz", "*/a\n  \nb"))
        self.assertEqual(sorted(path for path, _info in found), ["tool/bin/a", "tool/bin/b"])

    def test_every_regex_is_checked(self):
        source = ReleaseSource(name="x", kind=ReleaseSource.Kind.GITHUB_RELEASE, identifier="a/b",
                               version_pattern=" ^1\\. \n(", asset_pattern="Darwin")
        with self.assertRaises(ValidationError) as caught:
            source.clean()
        self.assertIn("Invalid regex (", caught.exception.message_dict["version_pattern"][0])
        source.version_pattern = " ^1\\. \n\n^2\\."
        source.clean()
        self.assertEqual(source.version_pattern, "^1\\.\n^2\\.")


class PatternCleanupTestCase(TestCase):
    def sync(self, assets, **options):
        source = ReleaseSource.objects.create(name="colima", kind=ReleaseSource.Kind.GITHUB_RELEASE,
                                              identifier="abiosoft/colima", is_global=True, **options)
        sync_release_source(source, fake_session(github_routes(assets)))
        return source, source.versions.get()

    def test_the_rules_outside_a_new_asset_pattern(self):
        source, version = self.sync({"colima-Darwin-arm64": unsigned("arm"), "colima-Darwin-x86_64": unsigned("x86")},
                                    rule_type=RuleType.BINARY)
        rule = Rule.objects.get(identifier=hashlib.sha256(unsigned("x86")).hexdigest())
        self.assertEqual(rule.release_files, [{"asset": "colima-Darwin-x86_64", "match": ["colima-Darwin-x86_64"],
                                               "path": "colima-Darwin-x86_64", "sha256": rule.identifier}])
        source.asset_pattern = "arm64"
        self.assertEqual([(item.pk, reason) for item, reason in stale_rules(source)], [(rule.pk, "asset")])
        self.assertEqual(stale_versions(source, check_filters=True), [])
        self.assertEqual(cleanup_release_source(source), (0, 1))
        self.assertFalse(Rule.objects.filter(pk=rule.pk).exists())
        version.refresh_from_db()
        self.assertEqual(version.binary_count, 1)

    def test_a_shared_rule_stays_while_one_file_is_taken(self):
        assets = {"colima-Darwin-arm64": build_macho(identifier="colima.arm"),
                  "colima-Darwin-x86_64": build_macho(identifier="colima.x86")}
        source, version = self.sync(assets, rule_type=RuleType.TEAMID)
        rule = Rule.objects.get(release_version=version)
        self.assertEqual(len(rule.release_files), 2)
        source.asset_pattern = "arm64"
        self.assertEqual(stale_rules(source), [])
        # no file of the version is taken any more: the version goes as a whole
        source.asset_pattern = "windows"
        self.assertEqual(pattern_stale_versions(source), {version.pk})
        self.assertEqual(stale_rules(source), [])
        self.assertEqual(cleanup_release_source(source), (1, 1))

    def test_the_binary_pattern_and_the_dependencies(self):
        source, version = self.sync({"colima-Darwin-arm64": unsigned("arm"), "colima-Darwin-x86_64": unsigned("x86")},
                                    rule_type=RuleType.BINARY)
        Rule.objects.create(rule_type=RuleType.BINARY, identifier="d" * 64, release_source=source,
                            release_version=version, release_dependency="dep@1.0", is_global=True,
                            release_files=[{"asset": "dep.tgz", "match": [], "path": "bin/dep", "sha256": "d" * 64}])
        source.binary_pattern = "*arm64"
        self.assertEqual([reason for _rule, reason in stale_rules(source)], ["binary"])

    def test_the_bottle_settings(self):
        routes = formula_routes("portable-ruby", ["arm64_big_sur", "big_sur"])
        routes.update({f"https://ghcr.example/portable-ruby/{tag}": FakeResponse(content=unsigned(tag))
                       for tag in ("arm64_big_sur", "big_sur")})
        # the bottle names its own content: the SHA-256 of the route is not the one of the content
        with patch("santa.releases.download", side_effect=lambda session, asset: io.BytesIO(
                unsigned(asset.name.split(".")[-4]))):
            source = ReleaseSource.objects.create(name="ruby", kind=ReleaseSource.Kind.HOMEBREW_FORMULA,
                                                  identifier="portable-ruby", is_global=True)
            sync_release_source(source, fake_session(routes))
        self.assertEqual(Rule.objects.filter(release_source=source).count(), 2)
        source.macos_minimum = "sequoia"
        self.assertEqual(stale_rules(source), [])
        source.macos_nearest = False
        self.assertEqual(pattern_stale_versions(source), {source.versions.get().pk})


class MigrationTestCase(TestCase):
    def test_the_files_from_the_notes(self):
        migration = importlib.import_module("santa.migrations.0025_rule_release_files")
        source = ReleaseSource.objects.create(name="pkg", kind=ReleaseSource.Kind.HOMEBREW_FORMULA, identifier="pkg")
        sha_a, sha_b, sha_dep = "a" * 64, "b" * 64, "c" * 64
        version = ReleaseVersion.objects.create(source=source, identifier="pkg", version="1.0", notes="\n".join([
            f"pkg-1.0.arm64_sonoma.bottle.tar.gz: pkg/1.0/bin/pkg {sha_a}",
            f"pkg-1.0.arm64_sequoia.bottle.tar.gz: pkg/1.0/bin/pkg {sha_b}",
            f"dep@1 – dep-1.arm64_sequoia.bottle.tar.gz: dep/1/bin/dep {sha_dep}",
        ]))
        for sha, dependency in ((sha_a, ""), (sha_b, ""), (sha_dep, "dep@1")):
            Rule.objects.create(rule_type=RuleType.BINARY, identifier=sha, release_source=source,
                                release_version=version, release_dependency=dependency)
        Rule.objects.create(rule_type=RuleType.TEAMID, identifier="ABCDE12345", release_source=source,
                            release_version=version)
        migration.set_release_files(apps, None)
        self.assertEqual(Rule.objects.get(identifier=sha_a).release_files, [
            {"asset": "pkg-1.0.arm64_sonoma.bottle.tar.gz", "match": ["arm64_sonoma"], "path": "pkg/1.0/bin/pkg",
             "sha256": sha_a}])
        self.assertEqual(Rule.objects.get(identifier=sha_b).release_files[0]["match"],
                         ["arm64_sequoia", "arm64_tahoe", "arm64_golden_gate"])
        self.assertEqual(Rule.objects.get(identifier=sha_dep).release_files, [])
        # two binaries: which one the Team ID rule stands for is not known
        self.assertEqual(Rule.objects.get(rule_type=RuleType.TEAMID).release_files, [])


HASHICORP_FORMULA = '''
class Terraform < Formula
  desc "Terraform"
  version "1.16.6"

  if OS.mac? && Hardware::CPU.intel?
    url "https://releases.example/terraform/1.16.6/terraform_1.16.6_darwin_amd64.zip"
    sha256 "%(a)s"
  end

  if OS.mac? && Hardware::CPU.arm?
    url "https://releases.example/terraform/1.16.6/terraform_1.16.6_darwin_arm64.zip"
    sha256 "%(b)s"
  end

  if OS.linux? && Hardware::CPU.intel?
    url "https://releases.example/terraform/1.16.6/terraform_1.16.6_linux_amd64.zip"
    sha256 "%(c)s"
  end

  depends_on "go" => :build
  depends_on "example/tap/helper"

  livecheck do
    url "https://releases.example/terraform/"
  end

  def install
    if OS.mac?
      bin.install "terraform"
    end
  end
end
''' % {"a": "a" * 64, "b": "b" * 64, "c": "c" * 64}

BOTTLE_FORMULA = '''
class Tool < Formula
  url "https://example.org/tool-2.0.tar.gz"
  sha256 "%(a)s"
  revision 1

  bottle do
    root_url "https://github.com/example/homebrew-tap/releases/download/tool-2.0_1"
    sha256 cellar: :any_skip_relocation, arm64_sonoma: "%(b)s"
    sha256 cellar: :any_skip_relocation, x86_64_linux: "%(c)s"
  end

  on_linux do
    depends_on "glibc"
  end
end
''' % {"a": "a" * 64, "b": "b" * 64, "c": "c" * 64}

TAP_CASK = '''
cask "tool" do # generated
  arch arm: "arm64", intel: "x64"

  version '3.1.0'
  sha256 arm:   "%(a)s",
         intel: "%(b)s"

  url "https://downloads.example/tool-#{version}-#{arch}.dmg"
  name "Tool"
end
''' % {"a": "a" * 64, "b": "b" * 64}


# rustfs/tap/rustfs: constants, a hash of checksums, the program in a resource for the platform
CONSTANTS_FORMULA = '''
class Tool < Formula
  VERSION = "1.0.3".freeze
  GITHUB_REPO = "example/tool".freeze
  BINARIES = {
    "macos-aarch64" => "%(a)s",
    "linux-x86_64-musl" => "%(b)s",
  }.freeze

  url "https://github.com/#{GITHUB_REPO}/archive/refs/tags/#{VERSION}.tar.gz"
  sha256 "%(c)s"

  resource "helper" do
    url "https://files.example/helper-1.0.tar.gz"
    sha256 "%(c)s"
  end

  on_macos do
    on_arm do
      resource "binary" do
        url "https://github.com/#{GITHUB_REPO}/releases/download/#{VERSION}/tool-macos-aarch64-v#{VERSION}.zip"
        sha256 BINARIES["macos-aarch64"]
      end
    end
  end

  on_linux do
    resource "binary" do
      url "https://github.com/#{GITHUB_REPO}/releases/download/#{VERSION}/tool-linux-x86_64-musl-v#{VERSION}.zip"
      sha256 BINARIES["linux-x86_64-musl"]
    end
  end
end
''' % {"a": "a" * 64, "b": "b" * 64, "c": "c" * 64}

# databricks/tap/databricks: local variables, the architecture chosen by the condition around the url
LOCALS_FORMULA = '''
class Tool < Formula
  version "1.21.0"

  download_prefix = "https://github.com/example/tool/releases/download"
  arch_string = Hardware::CPU.intel? ? "amd64" : "arm64"
  darwin_url = "#{download_prefix}/v#{version}/tool_#{version}_darwin_#{arch_string}.zip"
  darwin_amd64_sha = "%(a)s"
  darwin_arm64_sha = "%(b)s"

  if OS.mac? && Hardware::CPU.intel?
    url darwin_url
    sha256 darwin_amd64_sha
  end

  if OS.mac? && Hardware::CPU.arm?
    url darwin_url
    sha256 darwin_arm64_sha
  end

  def post_install
    system "codesign", "--force", "--sign", "-", libexec/"tool"
  end
end
''' % {"a": "a" * 64, "b": "b" * 64}


class TapCleanUpTestCase(TestCase):
    def test_the_clean_up_takes_the_prereleases_of_a_tap_recorded_before(self):
        source = ReleaseSource.objects.create(name="rustfs", kind=ReleaseSource.Kind.HOMEBREW_FORMULA,
                                              identifier="rustfs/tap/rustfs\ngo")
        preview = ReleaseVersion.objects.create(source=source, identifier="rustfs/tap/rustfs",
                                                version="1.0.1-preview.16")
        ReleaseVersion.objects.create(source=source, identifier="rustfs/tap/rustfs", version="1.0.1")
        ReleaseVersion.objects.create(source=source, identifier="go", version="1.27.2")
        self.assertEqual(stale_versions(source, check_filters=True), [preview])
        # not at the hourly check, only in the clean up; and not when they are allowed
        self.assertEqual(stale_versions(source), [])
        source.include_prereleases = True
        self.assertEqual(stale_versions(source, check_filters=True), [])


class TapTestCase(SimpleTestCase):
    def test_downloads_per_platform(self):
        found = parse_tap_file(HASHICORP_FORMULA)
        self.assertEqual(found["version"], "1.16.6")
        self.assertEqual(found["dependencies"], ["example/tap/helper"])
        session = fake_session({
            "https://raw.githubusercontent.com/hashicorp/homebrew-tap/HEAD/Formula/terraform.rb":
                FakeResponse(content=HASHICORP_FORMULA.encode())})
        release = latest_homebrew_tap(session, ReleaseSource(kind=ReleaseSource.Kind.HOMEBREW_FORMULA),
                                      "hashicorp/tap/terraform", cask=False)
        self.assertEqual([(asset.name, asset.sha256) for asset in release.assets],
                         [("terraform_1.16.6_darwin_amd64.zip", "a" * 64),
                          ("terraform_1.16.6_darwin_arm64.zip", "b" * 64)])
        release = latest_homebrew_tap(session, ReleaseSource(kind=ReleaseSource.Kind.HOMEBREW_FORMULA,
                                                             asset_pattern="arm64"),
                                      "hashicorp/tap/terraform", cask=False)
        self.assertEqual([asset.name for asset in release.assets], ["terraform_1.16.6_darwin_arm64.zip"])

    def test_bottles_from_the_tap(self):
        found = parse_tap_file(BOTTLE_FORMULA)
        self.assertEqual(found["bottles"], {"arm64_sonoma": "b" * 64, "x86_64_linux": "c" * 64})
        self.assertEqual(found["dependencies"], [])
        base = "https://raw.githubusercontent.com/example/homebrew-tap/HEAD/Formula"
        session = fake_session({f"{base}/tool.rb": FakeResponse(status_code=404),
                                f"{base}/t/tool.rb": FakeResponse(content=BOTTLE_FORMULA.encode())})
        release = latest_homebrew_tap(session, ReleaseSource(kind=ReleaseSource.Kind.HOMEBREW_FORMULA,
                                                             macos_minimum="tahoe"), "example/tap/tool", cask=False)
        self.assertEqual(release.version, "2.0_1")
        self.assertEqual([asset.url for asset in release.assets], [
            "https://github.com/example/homebrew-tap/releases/download/tool-2.0_1/"
            "tool-2.0_1.arm64_sonoma.bottle.tar.gz"])

    def test_a_cask_with_architectures(self):
        session = fake_session({"https://raw.githubusercontent.com/example/homebrew-tap/HEAD/Casks/tool.rb":
                                FakeResponse(content=TAP_CASK.encode())})
        release = latest_homebrew_tap(session, ReleaseSource(kind=ReleaseSource.Kind.HOMEBREW_CASK),
                                      "example/tap/tool", cask=True)
        self.assertEqual([(asset.name, asset.sha256) for asset in release.assets],
                         [("tool-3.1.0-arm64.dmg", "a" * 64), ("tool-3.1.0-x64.dmg", "b" * 64)])

    def tap_release(self, text):
        session = fake_session({"https://raw.githubusercontent.com/example/homebrew-tap/HEAD/Formula/tool.rb":
                                FakeResponse(content=text.encode())})
        return latest_homebrew_tap(session, ReleaseSource(kind=ReleaseSource.Kind.HOMEBREW_FORMULA),
                                   "example/tap/tool", cask=False)

    def test_constants_and_the_program_in_a_resource(self):
        release = self.tap_release(CONSTANTS_FORMULA)
        self.assertEqual(release.version, "1.0.3")
        # not the source code of the tag, nor a resource to build with, nor Linux
        self.assertEqual([(asset.url, asset.sha256) for asset in release.assets], [
            ("https://github.com/example/tool/releases/download/1.0.3/tool-macos-aarch64-v1.0.3.zip", "a" * 64)])
        self.assertEqual(release.warnings, ())

    def test_local_variables_and_the_architecture_of_the_condition(self):
        release = self.tap_release(LOCALS_FORMULA)
        base = "https://github.com/example/tool/releases/download/v1.21.0"
        self.assertEqual([(asset.url, asset.sha256) for asset in release.assets], [
            (f"{base}/tool_1.21.0_darwin_amd64.zip", "a" * 64), (f"{base}/tool_1.21.0_darwin_arm64.zip", "b" * 64)])
        # it signs the program again: the hash of the download is not the one the Macs see
        [warning] = release.warnings
        self.assertIn("signs its program again", warning)

    def tap_history_session(self, commits, files):
        """commits: [(sha, date, message)] newest first; files: {sha: version} of Formula/tool.rb"""
        base = "https://raw.githubusercontent.com/example/homebrew-tap"

        def formula(version):
            return (f'class Tool < Formula\n  version "{version}"\n  url "https://dl.example/tool-{version}-darwin.zip"'
                    f'\n  sha256 "{"a" * 64}"\nend\n')
        routes = {f"{base}/HEAD/Formula/tool.rb": FakeResponse(content=formula(files[commits[0][0]]).encode()),
                  "https://api.github.com/repos/example/homebrew-tap/commits": FakeResponse(
                      [{"sha": sha, "commit": {"message": message, "committer": {"date": date}}}
                       for sha, date, message in commits])}
        for sha, version in files.items():
            routes[f"{base}/{sha}/Formula/tool.rb"] = FakeResponse(content=formula(version).encode())
        return fake_session(routes)

    @override_settings(GITHUB_TOKEN="token")
    def test_older_versions_from_the_history_of_the_tap(self):
        session = self.tap_history_session(
            [("c3", "2026-10-10T10:00:00Z", "tool 1.2.0"), ("c2", "2026-10-01T10:00:00Z", "tool: new bottles"),
             ("c1", "2026-09-30T10:00:00Z", "tool 1.1.0"), ("c0", "2026-09-01T10:00:00Z", "tool 1.0.0")],
            {"c3": "1.2.0", "c2": "1.1.0", "c1": "1.1.0", "c0": "1.0.0"})
        source = ReleaseSource(kind=ReleaseSource.Kind.HOMEBREW_FORMULA, keep_versions=2)
        releases = homebrew_formula_releases(session, source, "example/tap/tool", 2)
        # the date of a version: the commit that brought it, not a later one of the same version
        self.assertEqual([(release.version, release.published_at.date().isoformat()) for release in releases],
                         [("1.2.0", "2026-10-10"), ("1.1.0", "2026-09-30")])
        self.assertEqual([asset.url for asset in releases[1].assets], ["https://dl.example/tool-1.1.0-darwin.zip"])
        # one version of the history
        self.assertEqual(homebrew_formula_at(session, source, "example/tap/tool", version="1.0.0").version, "1.0.0")
        with self.assertRaisesMessage(ReleaseError, "no version 0.9 in the history of the tap"):
            homebrew_formula_at(session, source, "example/tap/tool", version="0.9")

    @override_settings(GITHUB_TOKEN="token")
    def test_a_known_version_named_by_its_commit_is_not_read_again(self):
        session = self.tap_history_session(
            [("c3", "2026-10-10T10:00:00Z", "tool 1.2.0"), ("c1", "2026-09-30T10:00:00Z", "tool 1.1.0")],
            {"c3": "1.2.0", "c1": "1.1.0"})
        source = ReleaseSource(kind=ReleaseSource.Kind.HOMEBREW_FORMULA, keep_versions=2)
        releases = homebrew_formula_releases(session, source, "example/tap/tool", 2, known={"1.1.0": None})
        self.assertEqual([release.version for release in releases], ["1.2.0", "1.1.0"])
        urls = [call.args[0] for call in session.get.call_args_list]
        self.assertNotIn("https://raw.githubusercontent.com/example/homebrew-tap/c1/Formula/tool.rb", urls)

    def test_a_url_put_together_from_a_key_of_a_hash(self):
        # rustfs/tap/rc before it declared its resources: the URL in a method, the key of BINARIES in it
        found = parse_tap_file('''
class Rc < Formula
  VERSION = "0.1.35".freeze
  GITHUB_REPO = "rustfs/cli".freeze
  BINARIES = {
    "macos-arm64" => "%(a)s",
    "linux-amd64" => "%(b)s",
  }.freeze
  url "https://github.com/#{GITHUB_REPO}/archive/refs/tags/v#{VERSION}.tar.gz"
  sha256 "%(c)s"

  def binary_url_and_sha
    target = system_target
    sha256 = BINARIES[target]
    url = "https://github.com/#{GITHUB_REPO}/releases/download/v#{VERSION}/rustfs-cli-#{target}-v#{VERSION}.tar.gz"
    [url, sha256]
  end
end
''' % {"a": "a" * 64, "b": "b" * 64, "c": "c" * 64})
        release = _tap_release(ReleaseSource(kind=ReleaseSource.Kind.HOMEBREW_FORMULA), "rustfs/tap/rc", found, False)
        # not Linux, not the source code of the tag
        base = "https://github.com/rustfs/cli/releases/download/v0.1.35"
        self.assertEqual([(asset.url, asset.sha256) for asset in release.assets],
                         [(f"{base}/rustfs-cli-macos-arm64-v0.1.35.tar.gz", "a" * 64)])

    @override_settings(GITHUB_TOKEN="token")
    def test_prereleases_only_when_allowed(self):
        commits = [("c3", "2026-10-16T12:00:00Z", "tool 1.0.2-preview.2"),
                   ("c2", "2026-10-03T10:00:00Z", "tool 1.0.1"),
                   ("c1", "2026-09-20T10:00:00Z", "tool 1.0.1-preview.16")]
        files = {"c3": "1.0.2-preview.2", "c2": "1.0.1", "c1": "1.0.1-preview.16"}
        source = ReleaseSource(kind=ReleaseSource.Kind.HOMEBREW_FORMULA, keep_versions=5)
        session = self.tap_history_session(commits, files)
        self.assertEqual([r.version for r in homebrew_formula_releases(session, source, "example/tap/tool", 5)],
                         ["1.0.1"])
        # the current one is a prerelease: the newest release instead
        self.assertEqual(latest_homebrew_formula(session, source, "example/tap/tool").version, "1.0.1")
        source.include_prereleases = True
        session = self.tap_history_session(commits, files)
        self.assertEqual([r.version for r in homebrew_formula_releases(session, source, "example/tap/tool", 5)],
                         ["1.0.2-preview.2", "1.0.1", "1.0.1-preview.16"])
        self.assertEqual(latest_homebrew_formula(session, source, "example/tap/tool").version, "1.0.2-preview.2")

    @override_settings(GITHUB_TOKEN=None)
    def test_a_prerelease_without_a_token(self):
        session = self.tap_history_session([("c3", "2026-10-16T12:00:00Z", "tool 1.0.2-preview.2")],
                                           {"c3": "1.0.2-preview.2"})
        source = ReleaseSource(kind=ReleaseSource.Kind.HOMEBREW_FORMULA, keep_versions=5)
        self.assertEqual(homebrew_formula_releases(session, source, "example/tap/tool", 5), [])
        with self.assertRaisesMessage(ReleaseError, "the current version 1.0.2-preview.2 is a prerelease"):
            latest_homebrew_formula(session, source, "example/tap/tool")

    @override_settings(GITHUB_TOKEN=None)
    def test_without_a_token_only_the_current_version(self):
        session = self.tap_history_session(
            [("c3", "2026-10-10T10:00:00Z", "tool 1.2.0"), ("c1", "2026-09-30T10:00:00Z", "tool 1.1.0")],
            {"c3": "1.2.0", "c1": "1.1.0"})
        source = ReleaseSource(kind=ReleaseSource.Kind.HOMEBREW_FORMULA, keep_versions=2)
        releases = homebrew_formula_releases(session, source, "example/tap/tool", 2)
        self.assertEqual([release.version for release in releases], ["1.2.0"])
        urls = [call.args[0] for call in session.get.call_args_list]
        self.assertNotIn("https://api.github.com/repos/example/homebrew-tap/commits", urls)
        self.assertEqual(homebrew_formula_at(session, source, "example/tap/tool", version="1.2.0").version, "1.2.0")
        with self.assertRaisesMessage(ReleaseError, "the older versions of a tap need GITHUB_TOKEN"):
            homebrew_formula_at(session, source, "example/tap/tool", version="1.1.0")

    def test_the_warning_of_a_dependency_is_kept(self):
        session = fake_session({"https://example/tool": FakeResponse(content=unsigned("tool")),
                                "https://example/helper": FakeResponse(content=unsigned("helper"))})
        tool = Release(version="1.0", assets=[Asset(name="tool", url="https://example/tool")])
        helper = Release(version="2.0", assets=[Asset(name="helper", url="https://example/helper")],
                         warnings=("example/tap/helper: the formula signs its program again",))
        results = collect_many(session, ReleaseSource(kind=ReleaseSource.Kind.HOMEBREW_FORMULA),
                               [(tool, ""), (helper, "example/tap/helper@2.0")])
        self.assertEqual([list(errors) for _binaries, errors in results],
                         [[], ["example/tap/helper: the formula signs its program again"]])

    def test_not_in_the_tap(self):
        base = "https://raw.githubusercontent.com/example/homebrew-tap/HEAD"
        session = fake_session({f"{base}/Formula/x.rb": FakeResponse(status_code=404),
                                f"{base}/Formula/x/x.rb": FakeResponse(status_code=404),
                                f"{base}/x.rb": FakeResponse(status_code=404)})
        with self.assertRaisesMessage(ReleaseError, "not found in the tap example/tap"):
            latest_homebrew_tap(session, ReleaseSource(kind=ReleaseSource.Kind.HOMEBREW_FORMULA), "example/tap/x",
                                cask=False)


class PreviewTestCase(ConsoleBase):
    def setUp(self):
        super().setUp()
        self.source = ReleaseSource.objects.create(name="colima", kind=ReleaseSource.Kind.GITHUB_RELEASE,
                                                   identifier="abiosoft/colima", is_global=True)

    def post(self, user=None, **data):
        if user:
            self.client.force_login(user)
        routes = github_routes({"colima-Darwin-arm64": b"", "colima-Linux-x86_64": b""})
        with patch("santa.releases.http_session", return_value=fake_session(routes)):
            return self.client.post(reverse("console:source_preview", args=(self.source.pk,)), {
                "name": "colima", "kind": ReleaseSource.Kind.GITHUB_RELEASE, "identifier": "abiosoft/colima",
                "rule_type": RuleType.BINARY, "policy": Policy.ALLOWLIST, "is_global": "on",
                "auto_approve_delay_days": 0, "keep_versions": 3, **data})

    def test_what_the_patterns_take(self):
        response = self.post(asset_pattern="Darwin\narm64", version_pattern="^v2")
        self.assertContains(response, "colima-Darwin-arm64")
        self.assertContains(response, "colima-Linux-x86_64")
        self.assertEqual(response.content.decode().count('badge ok'), 1)
        self.assertEqual(response.content.decode().count('badge bad'), 2)
        # nothing saved
        self.source.refresh_from_db()
        self.assertEqual(self.source.asset_pattern, "")

    def test_invalid_patterns_and_permissions(self):
        self.assertContains(self.post(version_pattern="("), "Invalid regex")
        viewer = User.objects.create_user("viewer", is_staff=True)
        self.assertEqual(self.post(user=viewer).status_code, 403)

    def test_cleanup_shows_the_rules(self):
        version = ReleaseVersion.objects.create(source=self.source, identifier="abiosoft/colima", version="v1")
        for name in ("colima-Darwin-arm64", "colima-Linux-x86_64"):
            Rule.objects.create(rule_type=RuleType.BINARY, identifier=hashlib.sha256(name.encode()).hexdigest(),
                                release_source=self.source, release_version=version, is_global=True,
                                release_files=[{"asset": name, "match": [name], "path": name, "sha256": ""}])
        self.source.asset_pattern = "Darwin"
        self.source.save()
        response = self.client.get(reverse("console:source_cleanup", args=(self.source.pk,)))
        self.assertContains(response, "outside the asset pattern")
        self.assertContains(response, "colima-Linux-x86_64")
        response = self.client.get(reverse("console:source", args=(self.source.pk,)))
        self.assertContains(response, '<span class="badge warn">1</span>')


class ProgressTestCase(ConsoleBase):
    def test_the_steps_of_a_check(self):
        progress_id = str(uuid.uuid4())
        request = RequestFactory().post("/", {"progress_id": progress_id})
        request.user = self.admin
        source = ReleaseSource.objects.create(name="tools", kind=ReleaseSource.Kind.GITHUB_RELEASE,
                                              identifier="a/one\nb/two", is_global=True)
        with patch("santa.progress.time.time", return_value=1000.0):
            with self.assertRaises(ReleaseError), progress_for(request) as progress:
                sync_release_source(source, fake_session({}), progress=progress)
        expected = {"total": 2, "current": 2, "label": "b/two", "done": True}
        with patch("santa.progress.time.time", return_value=1004.0):
            self.assertEqual(read_progress(self.admin, progress_id), {**expected, "beat_age": 4.0})
            response = self.client.get(reverse("console:progress", args=(progress_id,)))
        self.assertEqual(response.json(), {**expected, "beat_age": 4.0})
        # only the own requests
        self.client.force_login(User.objects.create_user("other", is_staff=True))
        self.assertEqual(self.client.get(reverse("console:progress", args=(progress_id,))).json(), {})
        cache.delete(f"santa:progress:{self.admin.pk}:{progress_id}")

    def test_the_heartbeat_stops_with_the_request(self):
        progress_id = str(uuid.uuid4())
        request = RequestFactory().post("/", {"progress_id": progress_id})
        request.user = self.admin
        with patch("santa.progress.HEARTBEAT", 0.01), progress_for(request) as progress:
            progress.note("long")
            first = cache.get(progress.keys[0])["beat"]
            time.sleep(0.1)
            # the request is alive: the heartbeat refreshes the entry while it works
            self.assertGreater(cache.get(progress.keys[0])["beat"], first)
            self.assertFalse(read_progress(self.admin, progress_id)["done"])
        self.assertTrue(read_progress(self.admin, progress_id)["done"])
        self.assertFalse(progress._thread.is_alive())

    def test_without_a_valid_id_nothing_is_written(self):
        request = RequestFactory().post("/", {"progress_id": "../../x"})
        request.user = self.admin
        progress = progress_for(request)
        self.assertEqual(progress.keys, [])
        progress.start("x")
        self.assertIsNone(read_progress(self.admin, "../../x"))
        self.assertEqual(Progress().current, 0)


class RedactionTestCase(SimpleTestCase):
    def test_the_sync_token_is_masked(self):
        self.assertEqual(redact_sync_path("POST /sync/SeCrEt-123_x/preflight/ABC HTTP/1.1"),
                         "POST /sync/***/preflight/ABC HTTP/1.1")
        self.assertEqual(redact_sync_path("GET /console/rules/ HTTP/1.1"), "GET /console/rules/ HTTP/1.1")
        # malformed and truncated requests too
        self.assertEqual(redact_sync_path("GET /sync/SeCrEt HTTP/1.1"), "GET /sync/*** HTTP/1.1")
        self.assertEqual(redact_sync_path("GET /sync/SeCrEt?x=1 HTTP/1.1"), "GET /sync/***?x=1 HTTP/1.1")
        self.assertEqual(redact_sync_path("Not Found: /sync/SeCrEt"), "Not Found: /sync/***")
        self.assertEqual(redact_sync_path('"GET /sync/SeCrEt"'), '"GET /sync/***"')

    def test_the_django_filter(self):
        record = logging.LogRecord("django.request", logging.WARNING, __file__, 1, "Not Found: %s",
                                   ("/sync/SeCrEt/postflight/ABC",), None)
        self.assertTrue(RedactSyncTokens().filter(record))
        self.assertEqual(record.getMessage(), "Not Found: /sync/***/postflight/ABC")

    def test_the_gunicorn_access_log(self):
        from gunicorn.config import Config
        spec = importlib.util.spec_from_file_location("gunicorn_conf", "gunicorn.conf.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        logger = module.SyncTokenRedactingLogger(Config())
        environ = {"REQUEST_METHOD": "POST", "RAW_URI": "/sync/SeCrEt/ruledownload/ABC",
                   "PATH_INFO": "/sync/SeCrEt/ruledownload/ABC", "SERVER_PROTOCOL": "HTTP/1.1"}
        response = SimpleNamespace(status="200 OK", sent=10, headers=[])
        atoms = logger.atoms(response, SimpleNamespace(headers=[]), environ, SimpleNamespace(seconds=0,
                                                                                           microseconds=5000))
        self.assertEqual(atoms["r"], "POST /sync/***/ruledownload/ABC HTTP/1.1")
        self.assertNotIn("SeCrEt", str(atoms))
