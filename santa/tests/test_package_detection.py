from unittest.mock import ANY, patch

from django.contrib.admin.models import LogEntry
from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import SimpleTestCase
from django.urls import reverse
from django.utils import timezone

from santa import catalog
from santa.models import Event, ReleaseSource, ReleaseVersion, Rule, RuleType
from santa.package_detection import (
    SOURCE_APP_NAME,
    SOURCE_BUNDLE_ID,
    SOURCE_PATH,
    SOURCE_RELEASE,
    package_from_path,
    same_version,
    suggest_packages,
)

from .test_console import ConsoleBase

Kind = ReleaseSource.Kind
SHA_REDIS = "1" * 64
SHA_CLI = "2" * 64
SHA_BRAVE = "3" * 64
SHA_TOOL = "4" * 64

CASKS = [
    ["brave-browser", "Brave", [], "", "", {"apps": ["Brave Browser.app"], "bundle_ids": ["com.brave.Browser"]}],
    ["firefox", "Firefox", [], "", "", {"apps": ["Firefox.app"], "bundle_ids": ["org.mozilla.firefox"]}],
    ["firefox@esr", "Firefox ESR", [], "", "", {"apps": ["Firefox.app"], "bundle_ids": ["org.mozilla.firefox"]}],
]
FORMULAE = [["redis", "redis", [], "", "", {"version": "8.10.1"}], ["git", "git", [], "", "", {"version": "2.51.0"}]]


class PathTestCase(SimpleTestCase):
    def test_install_paths(self):
        cases = [
            ("/opt/homebrew/Cellar/redis/8.10.1/bin/redis-server", (Kind.HOMEBREW_FORMULA, "redis", "8.10.1")),
            ("/usr/local/Cellar/python@3.14/3.14.0_1/bin/python3", (Kind.HOMEBREW_FORMULA, "python@3.14", "3.14.0_1")),
            ("/home/linuxbrew/.linuxbrew/Cellar/git/2.51.0/bin/git", (Kind.HOMEBREW_FORMULA, "git", "2.51.0")),
            ("/opt/homebrew/opt/redis/bin/redis-cli", (Kind.HOMEBREW_FORMULA, "redis", "")),
            ("/opt/homebrew/Caskroom/codex/0.40.0/codex", (Kind.HOMEBREW_CASK, "codex", "0.40.0")),
            ("/Users/x/app/node_modules/@esbuild/darwin-arm64/bin/esbuild",
             (Kind.NPM_PACKAGE, "@esbuild/darwin-arm64", "")),
            ("/Users/x/app/node_modules/vite/node_modules/esbuild/bin/esbuild", (Kind.NPM_PACKAGE, "esbuild", "")),
            ("/Users/x/.vscode/extensions/ms-python.vscode-pylance-2024.1.1/dist/bin/tool",
             (Kind.VSCODE_EXTENSION, "ms-python.vscode-pylance", "2024.1.1")),
            ("/Users/x/.vscode/extensions/rust-lang.rust-analyzer-0.3.2-darwin-arm64/server/rust-analyzer",
             (Kind.VSCODE_EXTENSION, "rust-lang.rust-analyzer", "0.3.2")),
        ]
        for path, expected in cases:
            with self.subTest(path=path):
                self.assertEqual(package_from_path(path), expected)
        for path in ("/Applications/Brave Browser.app/Contents/MacOS/Brave Browser", "/usr/local/bin/colima",
                     "/Users/x/Downloads/tool"):
            with self.subTest(path=path):
                self.assertIsNone(package_from_path(path))


class DetectionBase(ConsoleBase):
    def setUp(self):
        super().setUp()
        cache.set(f"santa:catalog:v{catalog.CACHE_VERSION}:homebrew-index:{Kind.HOMEBREW_CASK}",
                  {"loaded_at": 0, "entries": CASKS}, None)
        cache.set(f"santa:catalog:v{catalog.CACHE_VERSION}:homebrew-index:{Kind.HOMEBREW_FORMULA}",
                  {"loaded_at": 0, "entries": FORMULAE}, None)
        self.addCleanup(cache.clear)
        # the cached lists are taken as they are, even if old
        patcher = patch("santa.catalog.HOMEBREW_INDEX_TIMEOUT", 10 ** 12)
        patcher.start()
        self.addCleanup(patcher.stop)

    def event(self, sha256, path, name, **kwargs):
        return Event.objects.create(machine=self.machine, group=self.machine.group, execution_time=timezone.now(),
                                    decision="BLOCK_UNKNOWN", file_sha256=sha256, file_path=path, file_name=name,
                                    **kwargs)


class SuggestTestCase(DetectionBase):
    def test_the_sources_most_reliable_first(self):
        source = ReleaseSource.objects.create(name="tools", kind=Kind.GITHUB_RELEASE, identifier="example/tool",
                                              is_global=True)
        version = ReleaseVersion.objects.create(source=source, identifier="example/tool", version="v1")
        Rule.objects.create(rule_type=RuleType.BINARY, identifier=SHA_TOOL, release_source=source,
                            release_version=version, is_global=True)
        events = [
            # a known release wins over the path
            self.event(SHA_TOOL, "/opt/homebrew/Cellar/tool/1.0/bin", "tool"),
            self.event(SHA_REDIS, "/opt/homebrew/Cellar/redis/8.10.1/bin", "redis-server"),
            self.event(SHA_CLI, "/opt/homebrew/Cellar/redis/8.10.1/bin", "redis-cli"),
            self.event(SHA_BRAVE, "/Volumes/Brave Browser/Brave Browser.app/Contents/MacOS", "Brave Browser",
                       bundle_id="com.brave.Browser", bundle_name="Brave Browser"),
            self.event("5" * 64, "/Applications/Firefox.app/Contents/MacOS", "firefox", bundle_name="Firefox"),
            self.event("6" * 64, "/usr/local/bin", "colima"),
        ]
        suggestions, unmatched = suggest_packages(events, cached_only=True)
        found = {(s.kind, s.identifier): s for s in suggestions}
        self.assertEqual(found[(Kind.GITHUB_RELEASE, "example/tool")].source, SOURCE_RELEASE)
        self.assertEqual(found[(Kind.GITHUB_RELEASE, "example/tool")].contained_in, [source])
        redis = found[(Kind.HOMEBREW_FORMULA, "redis")]
        self.assertEqual((redis.source, redis.confirmed, redis.shas), (SOURCE_PATH, True, {SHA_REDIS, SHA_CLI}))
        self.assertEqual(found[(Kind.HOMEBREW_CASK, "brave-browser")].source, SOURCE_BUNDLE_ID)
        firefox = found[(Kind.HOMEBREW_CASK, "firefox")]
        self.assertEqual((firefox.source, firefox.alternatives), (SOURCE_APP_NAME, ["firefox@esr"]))
        self.assertEqual([event.file_name for event in unmatched], ["colima"])

    def test_an_older_version_and_an_unknown_formula(self):
        events = [self.event(SHA_REDIS, "/opt/homebrew/Cellar/redis/7.2.0/bin", "redis-server"),
                  self.event(SHA_CLI, "/opt/homebrew/Cellar/pw-unknown/1.0/bin", "tool")]
        suggestions, _unmatched = suggest_packages(events, cached_only=True)
        found = {s.identifier: s for s in suggestions}
        self.assertEqual(found["redis"].latest_version, "8.10.1")
        self.assertFalse(found["pw-unknown"].confirmed)

    def test_a_version_that_only_starts_like_the_current_one(self):
        events = [self.event(SHA_REDIS, "/opt/homebrew/Cellar/redis/8.10.10/bin", "redis-server")]
        [redis], _unmatched = suggest_packages(events, cached_only=True)
        self.assertEqual(redis.latest_version, "8.10.1")
        self.assertTrue(same_version("8.10.1_1", "8.10.1"))
        self.assertTrue(same_version("8.10.1", "8.10.1_2"))
        self.assertFalse(same_version("8.10.10", "8.10.1"))

    def test_a_page_reads_only_the_lookup_tables(self):
        events = [self.event(SHA_BRAVE, "/Applications/Brave Browser.app/Contents/MacOS", "Brave Browser",
                             bundle_id="com.brave.Browser", bundle_name="Brave Browser")]
        suggest_packages(events, cached_only=True)
        index_key = f"santa:catalog:v{catalog.CACHE_VERSION}:homebrew-index:{Kind.HOMEBREW_CASK}"
        with patch("santa.catalog.cache.get", wraps=cache.get) as get:
            [brave], _unmatched = suggest_packages(events, cached_only=True)
        self.assertEqual(brave.identifier, "brave-browser")
        self.assertNotIn(index_key, [call.args[0] for call in get.call_args_list])


class EventPackagesViewTestCase(DetectionBase):
    def setUp(self):
        super().setUp()
        self.events = [self.event(SHA_REDIS, "/opt/homebrew/Cellar/redis/8.10.1/bin", "redis-server"),
                       self.event(SHA_CLI, "/opt/homebrew/Cellar/redis/8.10.1/bin", "redis-cli")]
        self.ids = [event.pk for event in self.events]

    def form_data(self, **data):
        return {"ids": self.ids, "apply": "1", "approve_0": "on", "kind_0": Kind.HOMEBREW_FORMULA,
                "identifier_0": "redis", "target_0": "new", "new_name": "Datenbanken", "rule_type": RuleType.BINARY,
                "policy": "ALLOWLIST", "groups": [self.dev.pk], "keep_versions": 3, "keep_unit": "VERSIONS",
                "resolve": "on", **data}

    def fake_sync(self, allowed_shas=()):
        """The check of the package rule: rules for these binaries"""
        def sync(source, progress=None, only=None):
            version = ReleaseVersion.objects.create(source=source, identifier=source.identifiers[0], version="8.10.1")
            for sha in allowed_shas:
                rule = Rule.objects.create(rule_type=RuleType.BINARY, identifier=sha, release_source=source,
                                           release_version=version)
                rule.groups.set(source.groups.all())
            return [version]
        return patch("santa.console.views_event_packages.sync_release_source", side_effect=sync)

    def test_the_suggestion_per_package(self):
        response = self.client.get(reverse("console:events_packages"), {"ids": self.ids})
        self.assertContains(response, "install path")
        self.assertContains(response, 'name="identifier_0"')
        self.assertContains(response, 'value="redis"')
        # one package for the two binaries
        self.assertNotContains(response, 'name="identifier_1"')

    def test_a_new_package_rule_checked_and_verified(self):
        with self.fake_sync([SHA_REDIS]) as sync:
            response = self.client.post(reverse("console:events_packages"), self.form_data())
        source = ReleaseSource.objects.get(name="Datenbanken")
        self.assertEqual((source.kind, source.identifiers), (Kind.HOMEBREW_FORMULA, ["redis"]))
        sync.assert_called_once_with(source, progress=ANY, only=None)
        self.assertContains(response, "✓")
        self.assertContains(response, "✗")
        # only the event whose binary is allowed now is resolved
        self.events[0].refresh_from_db()
        self.events[1].refresh_from_db()
        self.assertIsNotNone(self.events[0].resolved_at)
        self.assertIsNone(self.events[1].resolved_at)
        self.assertTrue(LogEntry.objects.filter(object_id=str(source.pk)).exists())

    def test_only_the_chosen_packages_are_verified(self):
        other = self.event("7" * 64, "/Users/x/Downloads", "pw-other")
        with self.fake_sync([SHA_REDIS]):
            response = self.client.post(reverse("console:events_packages"),
                                        self.form_data(ids=[*self.ids, other.pk], kind_1=Kind.GITHUB_RELEASE,
                                                       identifier_1="", target_1="new"))
        self.assertContains(response, "redis-server")
        self.assertNotContains(response, "pw-other")

    def test_changed_before_saving_into_an_existing_rule(self):
        target = ReleaseSource.objects.create(name="Brew", kind=Kind.HOMEBREW_FORMULA, identifier="git",
                                              is_global=True)
        with self.fake_sync():
            self.client.post(reverse("console:events_packages"), self.form_data(
                identifier_0="redis@7.2", target_0=str(target.pk), resolve=""))
        target.refresh_from_db()
        self.assertEqual(target.identifiers, ["git", "redis@7.2"])
        self.assertFalse(ReleaseSource.objects.filter(name="Datenbanken").exists())
        self.assertTrue(LogEntry.objects.filter(object_id=str(target.pk),
                                                change_message__contains="redis@7.2").exists())

    def test_already_contained_and_a_wrong_catalog(self):
        target = ReleaseSource.objects.create(name="Brew", kind=Kind.HOMEBREW_FORMULA, identifier="redis",
                                              is_global=True)
        npm = ReleaseSource.objects.create(name="Node", kind=Kind.NPM_PACKAGE, identifier="vite", is_global=True)
        response = self.client.get(reverse("console:events_packages"), {"ids": self.ids})
        self.assertContains(response, "Already in Brew.")
        response = self.client.post(reverse("console:events_packages"), self.form_data(target_0=str(npm.pk)))
        self.assertContains(response, "Node is a package rule for npm package.")
        with self.fake_sync():
            self.client.post(reverse("console:events_packages"), self.form_data(target_0=str(target.pk)))
        target.refresh_from_db()
        self.assertEqual(target.identifiers, ["redis"])

    def test_permissions(self):
        viewer = User.objects.create_user("viewer", is_staff=True)
        self.client.force_login(viewer)
        self.assertEqual(self.client.get(reverse("console:events_packages"), {"ids": self.ids}).status_code, 403)

    def test_a_tab_of_create_rules(self):
        response = self.client.post(reverse("console:events_create_rules"), {"ids": self.ids}, HTTP_HX_REQUEST="true")
        self.assertContains(response, f'hx-post="{reverse("console:events_packages")}"')
        self.assertContains(response, '<span class="tab-count">1</span>')
        response = self.client.post(reverse("console:events_packages"), {"ids": self.ids}, HTTP_HX_REQUEST="true")
        self.assertContains(response, 'name="identifier_0"')
        # no button of its own in the bulk bar
        response = self.client.get(reverse("console:events"), {"view": "blocked"}, HTTP_X_SANTA_ROWS="1")
        self.assertNotContains(response, f'formaction="{reverse("console:events_packages")}"')

    def test_the_package_field_searches_the_catalog(self):
        response = self.client.get(reverse("console:events_packages"), {"ids": self.ids})
        self.assertContains(response, "data-catalog-input")
        self.assertContains(response, f'data-url="{reverse("console:catalog_search")}"')
        self.assertContains(response, 'data-kind-field="kind_0"')
        # the short catalog names: no line of its own for "Homebrew formula (bottle)"
        self.assertContains(response, '<option value="HOMEBREW_FORMULA" selected>Formula</option>', html=True)
        # a suggestion picked in the browser: its name and icon go into the new package rule
        picked = '{"redis@7.2": {"name": "Redis 7.2", "icon_url": "https://cdn.example/redis.png"}}'
        with self.fake_sync():
            self.client.post(reverse("console:events_packages"), self.form_data(identifier_0="redis@7.2",
                                                                                   picked_0=picked))
        source = ReleaseSource.objects.get(name="Datenbanken")
        self.assertEqual(source.identifier_icons["redis@7.2"],
                         {"name": "Redis 7.2", "icon_url": "https://cdn.example/redis.png"})

    def test_the_bar_for_all_checked_packages(self):
        target = ReleaseSource.objects.create(name="Brew", kind=Kind.HOMEBREW_FORMULA, identifier="git",
                                              is_global=True)
        other = self.event("8" * 64, "/Users/x/Downloads", "pw-other")
        response = self.client.get(reverse("console:events_packages"), {"ids": [*self.ids, other.pk]})
        self.assertContains(response, "data-package-bulk")
        self.assertContains(response, f'<option value="{target.pk}">Add to Brew (Formula)</option>', html=True)
        html = response.content.decode()
        bar = html[html.index("data-package-bulk"):html.index("package-suggestions")]
        # only copied into the rows in the browser: nothing of it is sent
        self.assertNotIn("name=", bar)
        # one package: no bar
        response = self.client.get(reverse("console:events_packages"), {"ids": self.ids})
        self.assertNotContains(response, "data-package-bulk")

    def test_the_hints(self):
        response = self.client.get(reverse("console:event", args=(self.ids[0],)), HTTP_HX_REQUEST="true")
        self.assertContains(response, "Probably from Homebrew formula (bottle) <code>redis</code>.")
        self.assertContains(response, reverse("console:events_packages"))
        response = self.client.get(reverse("console:events"), {"view": "blocked"}, HTTP_X_SANTA_ROWS="1")
        self.assertContains(response, "Formula: redis</span>")
