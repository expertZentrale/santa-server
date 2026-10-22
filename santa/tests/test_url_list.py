import json
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.urls import reverse

from santa.config_io import export_config, import_config
from santa.console.views_sources import latest_versions
from santa.models import ReleaseSource, ReleaseVersion, Rule, legacy_list_sources
from santa.releases import (
    ReleaseError,
    cleanup_release_source,
    list_file_taken,
    list_state_version,
    list_version_order,
    preview_release_source,
    stale_rules,
    sync_release_source,
    url_list_releases,
)

from .test_console import ConsoleBase
from .test_releases import FakeResponse, ReleaseSourceBase, fake_session, zip_file
from .utils import build_macho

LIST_URL = "https://downloads.example/list.json"


def at(path, platform="mac-arm64", **options):
    """A row of the downloads: the downloads at a path of the entry, only those of a platform"""
    row = {"url_from": "path", "path": path, "url_field": "url"}
    if platform:
        row.update(download_field="platform", download_value=platform)
    return {**row, **options}


def built(template, name=None):
    """A row of the downloads: the URL built from the entry, only for the entry of a name"""
    row = {"url_from": "template", "template": template}
    return {**row, "entry_field": "name", "entry_value": name} if name else row


PATHS = [at("downloads.headless"), at("downloads.app")]


class Redirect(FakeResponse):
    is_redirect = True

    def __init__(self, location):
        super().__init__()
        self.headers = {"location": location}

    def close(self):
        pass


def entry(version, platforms=("mac-arm64", "mac-x64", "linux64"), products=("app", "headless")):
    return {"version": version, "revision": "1", "downloads": {
        product: [{"platform": platform, "url": f"https://downloads.example/{version}/{platform}/{product}.zip"}
                  for platform in platforms]
        for product in products}}


def list_routes(versions, document=None):
    """The list (oldest first, as such lists often are) and a zip with its own executable per download"""
    routes = {LIST_URL: FakeResponse(content=json.dumps(document or {"versions": [entry(v) for v in versions]})
                                     .encode())}
    for version in versions:
        for platform in ("mac-arm64", "mac-x64", "linux64"):
            for product in ("app", "headless"):
                binary = build_macho(identifier=f"com.example.{product}.{version}.{platform}", team_id="", adhoc=True)
                routes[f"https://downloads.example/{version}/{platform}/{product}.zip"] = FakeResponse(
                    content=zip_file({f"{product}/{product}": binary}))
    return routes


class UrlListTestCase(ReleaseSourceBase):
    def list_source(self, **kwargs):
        options = {"name": "tools", "identifier": LIST_URL, "list_sources": PATHS, "keep_versions": 3, **kwargs}
        source = ReleaseSource.objects.create(kind=ReleaseSource.Kind.URL_LIST, **options)
        source.groups.add(self.dev)
        return source

    def downloaded(self, session):
        return sorted(call.args[0] for call in session.get.call_args_list if call.args[0] != LIST_URL)

    def test_the_newest_version_with_its_downloads(self):
        source = self.list_source()
        session = fake_session(list_routes(["9.0.1.5", "10.0.0.1", "10.0.0.12"]))
        sync_release_source(source, session)
        source.refresh_from_db()
        self.assertEqual(source.last_error, "")
        # 10.0.0.12 is newer than 10.0.0.1 and 9.x: compared as numbers, not as text
        self.assertEqual(list(source.versions.values_list("version", flat=True)), ["10.0.0.12"])
        self.assertEqual(self.downloaded(session), ["https://downloads.example/10.0.0.12/mac-arm64/app.zip",
                                                    "https://downloads.example/10.0.0.12/mac-arm64/headless.zip"])
        self.assertEqual(ReleaseVersion.objects.get().binary_count, 2)
        self.assertEqual(Rule.objects.filter(release_source=source).count(), 2)

    def test_keep_the_last_versions(self):
        source = self.list_source(approve_kept_versions=True)
        sync_release_source(source, fake_session(list_routes(["1.0", "1.1", "1.2", "1.3"])))
        self.assertEqual(sorted(source.versions.values_list("version", flat=True)), ["1.1", "1.2", "1.3"])
        # a new version pushes out the oldest, not the newest one recorded first
        sync_release_source(source, fake_session(list_routes(["1.0", "1.1", "1.2", "1.3", "1.4"])))
        self.assertEqual(sorted(source.versions.values_list("version", flat=True)), ["1.2", "1.3", "1.4"])

    def test_newest_first_by_version(self):
        # the older kept versions are recorded after the newest: the lists sort by the version, not by the record
        source = self.list_source(approve_kept_versions=True)
        sync_release_source(source, fake_session(list_routes(["1.9", "1.10", "1.11"])))
        self.client.force_login(User.objects.create_superuser("admin", password="x"))
        response = self.client.get(reverse("console:source", args=(source.pk,)),
                                   headers={"HX-Request": "true", "X-Santa-Rows": "1"})
        self.assertEqual([version.version for _identifier, _item, versions in response.context["per_identifier"]
                          for version in versions], ["1.11", "1.10", "1.9"])
        self.assertEqual(latest_versions(source)[0][1].version, "1.11")

    def test_entries_of_an_object_only_some_of_them(self):
        document = {"channels": {
            "Stable": {"channel": "Stable", **entry("2.0")},
            "Beta": {"channel": "Beta", **entry("3.0")},
        }}
        source = self.list_source(list_entries="channels.*",
                                  list_sources=[at("downloads.app", entry_field="channel", entry_value="Stable")])
        session = fake_session(list_routes(["2.0", "3.0"], document))
        sync_release_source(source, session)
        self.assertEqual(list(source.versions.values_list("version", flat=True)), ["2.0"])
        self.assertEqual(self.downloaded(session), ["https://downloads.example/2.0/mac-arm64/app.zip"])

    def test_the_version_pattern_before_the_limit(self):
        source = self.list_source(version_pattern="^1\\.")
        sync_release_source(source, fake_session(list_routes(["1.0", "1.1", "2.0"])))
        self.assertEqual(list(source.versions.values_list("version", flat=True)), ["1.1"])

    def test_only_https_downloads(self):
        document = {"versions": [{"version": "1.0", "downloads": {"app": [
            {"platform": "mac-arm64", "url": "ftp://downloads.example/1.0/mac-arm64/app.zip"}]}}]}
        source = self.list_source(list_sources=[at("downloads.app")])
        session = fake_session(list_routes(["1.0"], document))
        with self.assertRaisesMessage(ReleaseError, "only URLs other than https, not taken (e.g. ftp://downloads."):
            sync_release_source(source, session)
        self.assertEqual(self.downloaded(session), [])

    def test_redirects_stay_on_https(self):
        routes = list_routes(["1.0"])
        # the list moved, on https: followed
        routes["https://downloads.example/old.json"] = Redirect("/list.json")
        source = self.list_source(identifier="https://downloads.example/old.json")
        sync_release_source(source, fake_session(routes))
        self.assertEqual(list(source.versions.values_list("version", flat=True)), ["1.0"])
        # a list or a download sent to http: not fetched
        routes["https://downloads.example/old.json"] = Redirect("http://downloads.example/list.json")
        with self.assertRaisesMessage(ReleaseError, "http://downloads.example/list.json: only https URLs"):
            url_list_releases(fake_session(routes), source, "https://downloads.example/old.json", 1)
        routes = list_routes(["2.0"])
        routes["https://downloads.example/2.0/mac-arm64/app.zip"] = Redirect("http://mirror.example/app.zip")
        session = fake_session(routes)
        other = self.list_source(name="other")
        try:
            sync_release_source(other, session)
        except ReleaseError:
            pass
        other.refresh_from_db()
        self.assertIn("http://mirror.example/app.zip: only https URLs",
                      other.last_error + "".join(other.versions.values_list("notes", flat=True)))
        self.assertNotIn("http://mirror.example/app.zip", self.downloaded(session))

    def test_a_list_too_big_or_broken(self):
        source = self.list_source()
        with self.settings(RELEASE_MAX_LIST_BYTES=10), self.assertRaisesMessage(ReleaseError, "bigger than 10 bytes"):
            sync_release_source(source, fake_session(list_routes(["1.0"])))
        with self.assertRaisesMessage(ReleaseError, "not valid JSON"):
            sync_release_source(source, fake_session({LIST_URL: FakeResponse(content=b"<html>")}))
        source.refresh_from_db()
        self.assertIn("not valid JSON", source.last_error)
        self.assertFalse(source.versions.exists())

    def explain(self, rows=None, **options):
        with self.assertRaises(ReleaseError) as raised:
            source = ReleaseSource(kind=ReleaseSource.Kind.URL_LIST, identifier=LIST_URL,
                                   list_sources=rows or PATHS, **options)
            url_list_releases(fake_session(list_routes(["1.0", "1.1"])), source, LIST_URL, 3)
        return str(raised.exception)

    def test_why_nothing_is_found(self):
        self.assertIn('versions in the list "versionz" finds nothing; the list has: versions',
                      self.explain(list_entries="versionz"))
        self.assertIn('2 entries, none has the field "vers"; the first has: downloads, revision, version',
                      self.explain(list_version_key="vers"))
        self.assertIn('download 1: no downloads at "downloads.ap"; "downloads" of the first entry has: app, headless',
                      self.explain([at("downloads.ap")]))
        self.assertIn('download 1: no download has the field "link"; they have: platform, url',
                      self.explain([at("downloads.app", url_field="link")]))
        self.assertIn("download 1: no download has platform = mac-arm65; platform has: linux64, mac-arm64, mac-x64",
                      self.explain([at("downloads.app", platform="mac-arm65")]))
        self.assertIn('download 1: no download has the field "os"; they have: platform, url',
                      self.explain([at("downloads.app", platform=None, download_field="os", download_value="mac")]))
        self.assertIn('download 1: no entry has the field "channel"; the first has: downloads, revision, version',
                      self.explain([at("downloads.app", entry_field="channel", entry_value="Stable")]))
        self.assertIn("no version matches the version pattern; the newest: 1.1, 1.0",
                      self.explain(version_pattern="^2\\."))

    def test_the_preview_says_why_and_shows_the_entries(self):
        source = self.list_source(list_sources=[at("downloads.ap"), at("downloads.app")])
        result = preview_release_source(source, fake_session(list_routes(["1.0"])))[0]
        # one row gives nothing: named, the other one gives its version
        self.assertEqual(result["error"], "")
        self.assertEqual(result["row_problems"],
                         [(1, 'no downloads at "downloads.ap"; "downloads" of the first entry has: app, headless')])
        self.assertEqual(result["entries"], [[("version", "1.0"), ("revision", "1")]])
        self.assertEqual(result["kind"], ReleaseSource.Kind.URL_LIST)

    def test_preview(self):
        source = self.list_source()
        result = preview_release_source(source, fake_session(list_routes(["1.0", "1.1"])))
        releases = result[0]["releases"]
        self.assertEqual([release["version"] for release in releases], ["1.1", "1.0"])
        self.assertEqual([asset["name"] for asset in releases[0]["assets"]], ["headless.zip", "app.zip"])
        # no version pattern: no mark for the version
        self.assertFalse(result[0]["version_pattern"])

    def test_version_order(self):
        versions = ["9.0", "10.0.0.1", "10.0.0.12", "10.0.0.2", "10.0.0.2-beta"]
        self.assertEqual(sorted(versions, key=list_version_order, reverse=True),
                         ["10.0.0.12", "10.0.0.2-beta", "10.0.0.2", "10.0.0.1", "9.0"])

    def test_checks_of_the_fields(self):
        source = ReleaseSource(name="tools", kind=ReleaseSource.Kind.URL_LIST, identifier="http://downloads.example/x",
                               keep_unit=ReleaseSource.KeepUnit.WEEKS, list_sources=[
                                   {"url_from": "path"},
                                   {"url_from": "template", "template": "http://cdn.example/{revision}/a.zip"},
                                   {"url_from": "template", "template": "https://cdn.example/{revision/a.zip"},
                                   {"url_from": "path", "path": "a", "entry_field": "name"},
                                   {"url_from": "path", "path": "a", "download_value": "mac"},
                                   {}])
        with self.assertRaises(ValidationError) as raised:
            source.full_clean()
        errors = raised.exception.message_dict
        self.assertIn("only https URLs", errors["identifier"][0])
        self.assertEqual(set(errors), {"identifier", "list_sources", "keep_unit"})
        self.assertEqual(errors["list_sources"], [
            "Download 1: Enter where the downloads are in the entry.",
            "Download 2: The URL must start with https://.",
            "Download 3: Use {field} for the fields of the entry.",
            "Download 4: Enter both the field and the value of the entries, or neither.",
            "Download 5: Enter both the field and the value of the downloads, or neither.",
            "Download 6: Choose where the URL comes from.",
        ])
        source.list_sources = []
        with self.assertRaises(ValidationError) as raised:
            source.full_clean()
        self.assertEqual(raised.exception.message_dict["list_sources"], ["Add at least one download."])

    def test_round_trip(self):
        rows = [at("downloads.app", entry_field="channel", entry_value="Stable"),
                built("https://cdn.example/{revision}/a.zip", "a")]
        self.list_source(list_entries="channels.*", list_sources=rows)
        data = json.loads(json.dumps(export_config()))
        ReleaseSource.objects.all().delete()
        import_config(data)
        source = ReleaseSource.objects.get()
        self.assertEqual((source.kind, source.list_format, source.list_entries, source.list_version_key,
                          source.list_sources), (ReleaseSource.Kind.URL_LIST, "json", "channels.*", "version", rows))

    def test_files_of_older_versions(self):
        """The text fields before the table: paths, URL field, filters (the first for the downloads)"""
        rows, left_out = legacy_list_sources("downloads.chrome\ndownloads.headless", "url",
                                             "platform=^mac\\-arm64$\nkind=^mac-(arm64|x64)$\nchannel=^Stable$\n"
                                             "other=^x$")
        self.assertEqual(rows, [at("downloads.chrome", entry_field="channel", entry_value="Stable"),
                                at("downloads.headless", entry_field="channel", entry_value="Stable")])
        # a regex of several values, and a third filter: named, not changed into something else
        self.assertEqual(left_out, ["kind=^mac-(arm64|x64)$", "other=^x$"])
        data = json.loads(json.dumps(export_config()))
        data["release_sources"] = [{"name": "old", "kind": "URL_LIST", "identifier": LIST_URL, "groups": [],
                                    "list_downloads": "downloads.app", "list_url_key": "url",
                                    "list_filters": "platform=^mac-arm64$"}]
        import_config(data)
        self.assertEqual(ReleaseSource.objects.get(name="old").list_sources, [at("downloads.app")])
        self.assertTrue(ReleaseSource.objects.get(name="old").is_enabled)
        data["release_sources"][0].update(name="regex", list_filters="platform=mac")
        report = import_config(data)
        source = ReleaseSource.objects.get(name="regex")
        self.assertFalse(source.is_enabled)
        self.assertEqual(source.list_sources, [at("downloads.app", platform=None)])
        self.assertTrue(any("filters not taken over" in warning and "platform=mac" in warning
                            for warning in report["warnings"]))

    def test_the_conditions_look_at_the_download_and_its_entry(self):
        """As the filters before the table: a condition of the downloads also finds a field of the entry"""
        source = self.list_source(list_sources=[at("downloads.app", platform=None, download_field="revision",
                                                   download_value="1")])
        session = fake_session(list_routes(["1.0"]))
        sync_release_source(source, session)
        self.assertEqual(len(self.downloaded(session)), 3)

    def test_malformed_rows_are_errors(self):
        source = ReleaseSource(name="x", kind=ReleaseSource.Kind.URL_LIST, identifier=LIST_URL, list_sources=["bad"])
        with self.assertRaises(ValidationError) as raised:
            source.full_clean()
        self.assertIn("Download 1: not a row.", raised.exception.message_dict["list_sources"])
        source.list_sources = {"a": 1}
        with self.assertRaises(ValidationError) as raised:
            source.full_clean()
        self.assertIn("The downloads must be a list of rows.", raised.exception.message_dict["list_sources"])

    def test_other_downloads_under_the_same_version_record_it_again(self):
        source = self.list_source(list_sources=[at("downloads.app")], auto_approve=False)
        sync_release_source(source, fake_session(list_routes(["1.0"])))
        version = source.versions.get()
        version.rules.update(is_enabled=True)
        # the same check again: nothing new
        sync_release_source(source, fake_session(list_routes(["1.0"])))
        self.assertEqual(source.versions.get().pk, version.pk)
        # a row changed: the files of the same version name are others, recorded again and still approved
        source.list_sources = [at("downloads.headless")]
        source.save()
        session = fake_session(list_routes(["1.0"]))
        sync_release_source(source, session)
        again = source.versions.get()
        self.assertNotEqual(again.pk, version.pk)
        self.assertEqual(self.downloaded(session), ["https://downloads.example/1.0/mac-arm64/headless.zip"])
        self.assertEqual([file["asset"] for rule in again.rules.all() for file in rule.release_files],
                         ["headless.zip"])
        self.assertTrue(all(rule.is_enabled for rule in again.rules.all()))

    def test_the_list_needs_https(self):
        with self.assertRaisesMessage(ReleaseError, "only https"):
            url_list_releases(fake_session({}), self.list_source(), "http://downloads.example/x", 1)


TOOLS_URL = "https://registry.example/tools@latest/tools.json"
TOOLS = {"tools": [
    {"name": "app", "revision": "1248", "browserVersion": "156.0.1.4"},
    {"name": "app-shell", "revision": "1248", "browserVersion": "156.0.1.4"},
    {"name": "other", "revision": "1555", "browserVersion": "157.0"},
    {"name": "kit", "revision": "2370", "browserVersion": "27.2", "revisionOverrides": {"mac14-arm64": "2251"}},
    {"name": "helper", "revision": "1013"},
]}
CDN = "https://cdn.example/builds"


def tools_routes(files):
    """The list without URLs, and a zip with its own executable for each of the files"""
    routes = {TOOLS_URL: FakeResponse(content=json.dumps(TOOLS).encode())}
    for path in files:
        binary = build_macho(identifier=f"com.example.{path}", team_id="", adhoc=True)
        routes[f"{CDN}/{path}"] = FakeResponse(content=zip_file({f"{path}/bin": binary}))
    return routes


class UrlTemplatesTestCase(ReleaseSourceBase):
    def tools_source(self, name, **options):
        source = ReleaseSource.objects.create(name=name, kind=ReleaseSource.Kind.URL_LIST, identifier=TOOLS_URL,
                                              list_entries="tools", keep_versions=2, **options)
        source.groups.add(self.dev)
        return source

    def downloaded(self, session):
        return sorted(call.args[0] for call in session.get.call_args_list if call.args[0] != TOOLS_URL)

    def test_the_urls_built_from_the_entries(self):
        files = ["cft/156.0.1.4/app-mac-arm64.zip", "cft/156.0.1.4/app-shell-mac-arm64.zip", "other/1555/other.zip",
                 "kit/2370/kit-mac-15-arm64.zip", "kit/2251/kit-mac-14-arm64.zip", "helper/1013/helper.zip"]
        # one entry per tool: the version of the app, both of its files
        source = self.tools_source("app", list_version_key="browserVersion", list_sources=[
            built(f"{CDN}/cft/{{browserVersion}}/app-mac-arm64.zip", "app"),
            built(f"{CDN}/cft/{{browserVersion}}/app-shell-mac-arm64.zip", "app-shell")])
        session = fake_session(tools_routes(files))
        sync_release_source(source, session)
        self.assertEqual(list(source.versions.values_list("version", flat=True)), ["156.0.1.4"])
        self.assertEqual(self.downloaded(session), [f"{CDN}/{path}" for path in files[:2]])
        self.assertEqual(source.versions.get().binary_count, 2)
        # by build, a nested field for an older macOS; an entry without it leaves that row out
        source = self.tools_source("kit", list_version_key="revision", list_sources=[
            built(f"{CDN}/kit/{{revision}}/kit-mac-15-arm64.zip", "kit"),
            built(f"{CDN}/kit/{{revisionOverrides.mac14-arm64}}/kit-mac-14-arm64.zip")])
        session = fake_session(tools_routes(files))
        sync_release_source(source, session)
        self.assertEqual(list(source.versions.values_list("version", flat=True)), ["2370"])
        self.assertEqual(self.downloaded(session), [f"{CDN}/{files[4]}", f"{CDN}/{files[3]}"])
        # an entry without the version field of the others (no browserVersion)
        source = self.tools_source("helper", list_version_key="revision", list_sources=[
            built(f"{CDN}/helper/{{revision}}/helper.zip", "helper")])
        sync_release_source(source, fake_session(tools_routes(files)))
        self.assertEqual(list(source.versions.values_list("version", flat=True)), ["1013"])

    def test_the_whole_list_as_one_version(self):
        files = ["cft/156.0.1.4/app-mac-arm64.zip", "other/1555/other.zip", "helper/1013/helper.zip",
                 "other/1556/other.zip"]
        source = self.tools_source("tools", list_version_key="revision", list_one_version=True, list_sources=[
            built(f"{CDN}/cft/{{browserVersion}}/app-mac-arm64.zip", "app"),
            built(f"{CDN}/other/{{revision}}/other.zip", "other"),
            built(f"{CDN}/helper/{{revision}}/helper.zip", "helper")])
        session = fake_session(tools_routes(files))
        sync_release_source(source, session)
        version = source.versions.get()
        self.assertEqual(version.version, "app 1248 · helper 1013 · other 1555")
        self.assertEqual(version.binary_count, 3)
        # each row only for its entry: no URL for the others
        self.assertEqual(self.downloaded(session), sorted(f"{CDN}/{path}" for path in files[:3]))
        # a new build of one of them: a new state of the list, the newest stays when the older one goes
        TOOLS["tools"][2]["revision"] = "1556"
        try:
            sync_release_source(source, fake_session(tools_routes(files)))
            source.keep_versions = 1
            source.save()
            sync_release_source(source, fake_session(tools_routes(files)))
        finally:
            TOOLS["tools"][2]["revision"] = "1555"
        self.assertEqual(list(source.versions.values_list("version", flat=True)),
                         ["app 1248 · helper 1013 · other 1556"])

    def test_clean_up_after_a_download_was_removed(self):
        files = ["cft/156.0.1.4/app-mac-arm64.zip", "other/1555/other.zip", "helper/1013/helper.zip"]
        rows = [built(f"{CDN}/cft/{{browserVersion}}/app-mac-arm64.zip", "app"),
                built(f"{CDN}/other/{{revision}}/other.zip", "other"),
                built(f"{CDN}/helper/{{revision}}/helper.zip", "helper")]
        source = self.tools_source("tools", list_version_key="revision", list_one_version=True, list_sources=rows)
        sync_release_source(source, fake_session(tools_routes(files)))
        self.assertEqual(stale_rules(source), [])
        # "other" no longer wanted: its rule goes, the version and the others stay
        source.list_sources = [rows[0], rows[2]]
        source.save()
        stale = stale_rules(source)
        self.assertEqual([rule.release_files[0]["asset"] for rule, _reason in stale], ["other.zip"])
        cleanup_release_source(source)
        self.assertEqual(sorted(rule.release_files[0]["asset"] for rule in source.rules.all()),
                         ["app-mac-arm64.zip", "helper.zip"])
        self.assertEqual(source.versions.get().binary_count, 2)

    def test_a_file_of_two_rows_stays_with_one_of_them(self):
        files = ["other/1555/other.zip"]
        rows = [built(f"{CDN}/other/{{revision}}/other.zip", "other"), built(f"{CDN}/other/1555/other.zip", "other")]
        source = self.tools_source("tools", list_version_key="revision", list_one_version=True, list_sources=rows)
        sync_release_source(source, fake_session(tools_routes(files)))
        source.list_sources = [rows[1]]
        source.save()
        self.assertEqual(stale_rules(source), [])

    def test_clean_up_of_files_recorded_before_the_rows_were_kept(self):
        """Without the mark of their row: by the name of the file and the condition of the row"""
        source = ReleaseSource(kind=ReleaseSource.Kind.URL_LIST, list_sources=[
            built(f"{CDN}/other/{{revision}}/other-mac-arm64.zip", "other"), at("downloads.app")])
        self.assertTrue(list_file_taken(source, {"asset": "other-mac-arm64.zip", "match": ["other", "1555"]}))
        self.assertFalse(list_file_taken(source, {"asset": "kit-mac-arm64.zip", "match": ["kit", "1555"]}))
        self.assertFalse(list_file_taken(source, {"asset": "other-mac-arm64.zip", "match": ["kit", "1555"]}))
        self.assertTrue(list_file_taken(source, {"asset": "app.zip", "match": ["mac-arm64", "https://x/app.zip"]}))
        self.assertFalse(list_file_taken(source, {"asset": "app.zip", "match": ["linux64", "https://x/app.zip"]}))

    def test_a_long_state_of_the_list_is_shortened(self):
        version = list_state_version([f"tool-{index} 1.0.{index}" for index in range(40)])
        self.assertLessEqual(len(version), 200)
        self.assertNotEqual(version, list_state_version([f"tool-{index} 1.0.{index}" for index in range(41)]))

    def test_why_a_row_gives_nothing(self):
        source = ReleaseSource(kind=ReleaseSource.Kind.URL_LIST, identifier=TOOLS_URL, list_entries="tools",
                               list_version_key="revision", list_sources=[built(f"{CDN}/{{revision}}/app.zip", "ap")])
        with self.assertRaisesMessage(ReleaseError, "download 1: no entry has name = ap; name has: app, app-shell, "
                                                    "helper, kit, other"):
            url_list_releases(fake_session(tools_routes([])), source, TOOLS_URL, 1)
        source.list_sources = [built(f"{CDN}/{{build}}/app.zip")]
        with self.assertRaisesMessage(ReleaseError, 'download 1: no entry has the field "build" of the URL; the first '
                                                    "has: browserVersion, name, revision"):
            url_list_releases(fake_session(tools_routes([])), source, TOOLS_URL, 1)

    def test_the_preview_asks_for_the_files(self):
        # a row for every entry: URLs for each of them, most don't exist
        source = ReleaseSource(name="x", kind=ReleaseSource.Kind.URL_LIST, identifier=TOOLS_URL, list_entries="tools",
                               list_version_key="revision",
                               list_sources=[built(f"{CDN}/other/{{revision}}/other.zip")])
        routes = tools_routes(["other/1555/other.zip"])
        session = fake_session(routes)
        session.head.side_effect = lambda url, **kwargs: routes.get(url) or FakeResponse(status_code=404)
        releases = preview_release_source(source, session)[0]["releases"]
        self.assertEqual([(release["version"], [(asset["where"], asset["ok"], asset.get("error", ""))
                                                 for asset in release["assets"]]) for release in releases][:2],
                         [("2370", [("cdn.example/builds/other/2370/other.zip", False, "HTTP 404")]),
                          ("1555", [("cdn.example/builds/other/1555/other.zip", True, "")])])


class UrlListPagesTestCase(ConsoleBase):
    def test_the_overview_names_the_list_by_its_file(self):
        source = ReleaseSource.objects.create(
            name="tools", kind=ReleaseSource.Kind.URL_LIST, identifier=LIST_URL, keep_versions=2,
            list_sources=PATHS + [built("https://cdn.example/{version}/a.zip", "a")])
        source.groups.add(self.dev)
        sync_release_source(source, fake_session(list_routes(["1.0"])))
        response = self.client.get(reverse("console:source", args=(source.pk,)), headers={"HX-Request": "true"})
        self.assertContains(response, f'<span class="identifier-title" title="{LIST_URL}">list.json</span>')
        self.assertContains(response, f'href="{LIST_URL}" rel="noopener noreferrer" target="_blank">downloads.example')
        self.assertNotContains(response, f"<code>{LIST_URL}</code>")
        self.assertContains(response, "the downloads at <code>downloads.headless</code>, URL in <code>url</code> · "
                                      "<code>platform = mac-arm64</code>")
        self.assertContains(response, '<span class="muted">only name = a:</span> '
                                      "<code>https://cdn.example/{version}/a.zip</code>")
        # the versions, rendered with the rows of the drawer
        response = self.client.get(reverse("console:source", args=(source.pk,)),
                                   headers={"HX-Request": "true", "X-Santa-Rows": "1"})
        self.assertContains(response, '<div class="version-files"><span class="chip small">app.zip</span>'
                                      '<span class="chip small">headless.zip</span></div>')
        response = self.client.get(reverse("console:sources"), {"rows": "now"})
        self.assertContains(response, f'<span class="mono" title="{LIST_URL}">list.json</span>')


class UrlListFormTestCase(ConsoleBase):
    def post(self, rows, **fields):
        return self.client.post(reverse("console:source_add"), {**self.form_data(rows), **fields})

    def form_data(self, rows):
        data = {"name": "tools", "kind": ReleaseSource.Kind.URL_LIST, "identifier": LIST_URL, "is_enabled": "on",
                "list_format": "json", "list_entries": "versions", "list_version_key": "version",
                "rule_type": "BINARY", "policy": "ALLOWLIST", "groups": [self.dev.pk], "auto_approve": "on",
                "auto_approve_delay_days": 0, "keep_versions": 3, "keep_unit": "WEEKS", "approve_kept_versions": "on",
                "downloads-TOTAL_FORMS": len(rows), "downloads-INITIAL_FORMS": 0}
        for index, row in enumerate(rows):
            data.update({f"downloads-{index}-{key}": value for key, value in row.items()})
        return data

    def test_the_downloads_as_rows(self):
        response = self.post([at("downloads.app"), {"url_from": "path"},
                              built("https://cdn.example/{version}/a.zip", "a"), {"url_from": "path", "DELETE": "on",
                                                                                   "path": "x"}])
        # an empty new row is no row, a removed one neither
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Enter where the downloads are in the entry.")
        response = self.post([at("downloads.app"), {"url_from": "path"}, built("https://cdn.example/{version}/a.zip",
                                                                                "a")], **{"downloads-1-url_field": ""})
        self.assertEqual(response.status_code, 200)
        response = self.post([at("downloads.app"), built("https://cdn.example/{version}/a.zip", "a"),
                              {"url_from": "path", "url_field": "url", "DELETE": "on", "path": "x"}])
        self.assertEqual(response.status_code, 302, response.content[:3000])
        source = ReleaseSource.objects.get()
        # no dates in a list: a number of versions; the older ones can be allowed
        self.assertEqual((source.keep_unit, source.approve_kept_versions), ("VERSIONS", True))
        self.assertEqual(source.list_sources, [at("downloads.app"), built("https://cdn.example/{version}/a.zip", "a")])
        # the form of the saved rule shows them as rows; unchanged they stay, for the preview too
        with patch("santa.console.views_sources.preview_release_source", return_value=[]):
            response = self.client.post(reverse("console:source_preview", args=(source.pk,)), {
                **self.form_data([at("downloads.app"), built("https://cdn.example/{version}/a.zip", "a")]),
                "downloads-INITIAL_FORMS": 2})
        self.assertNotContains(response, "Add at least one download.")
        self.assertEqual(response.context["source"].list_sources,
                         [at("downloads.app"), built("https://cdn.example/{version}/a.zip", "a")])
        response = self.client.get(reverse("console:source_edit", args=(source.pk,)))
        self.assertContains(response, 'name="downloads-1-template"')
        self.assertContains(response, ">\nhttps://cdn.example/{version}/a.zip</textarea>")
        self.assertContains(response, 'name="downloads-INITIAL_FORMS" value="2"')

    def test_posted_without_the_table(self):
        """A draft of the preview from before the table, or a script: the saved downloads stay, no error"""
        source = ReleaseSource.objects.create(name="tools", kind=ReleaseSource.Kind.URL_LIST, identifier=LIST_URL,
                                              list_sources=[at("downloads.app")])
        source.groups.add(self.dev)
        response = self.client.post(reverse("console:source_edit", args=(source.pk,)), {
            "name": "tools", "kind": ReleaseSource.Kind.URL_LIST, "identifier": LIST_URL, "is_enabled": "on",
            "list_entries": "versions", "list_version_key": "version", "rule_type": "BINARY",
            "policy": "ALLOWLIST", "groups": [self.dev.pk], "auto_approve": "on", "auto_approve_delay_days": 0,
            "keep_versions": 3})
        self.assertEqual(response.status_code, 302, response.content[:3000])
        source.refresh_from_db()
        self.assertEqual(source.list_sources, [at("downloads.app")])

    def test_the_fields_show_only_for_a_list(self):
        response = self.client.get(reverse("console:source_add"))
        self.assertContains(response, '<div data-show-when="kind=URL_LIST">')
        self.assertContains(response, 'data-formset="downloads"')
        self.assertContains(response, '<div data-show-when="url_from=template">')
        # "List" instead of "Packages", and only the help of the chosen catalog
        self.assertContains(response, '<span data-show-when="kind=URL_LIST">List</span>')
        self.assertContains(response, '<small class="help" data-show-when="kind=URL_LIST">The https URL of the JSON')
        self.assertContains(response, '<small class="help" data-show-when="kind=GITHUB_RELEASE">GitHub: owner/repo')
        self.assertNotContains(response, "GitHub: owner/repo (e.g. abiosoft/colima) · Homebrew")
