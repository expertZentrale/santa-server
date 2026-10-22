import json

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.urls import reverse

from santa.config_io import export_config, import_config
from santa.console.views_sources import latest_versions
from santa.models import ReleaseSource, ReleaseVersion, Rule
from santa.releases import (
    ReleaseError,
    list_version_order,
    preview_release_source,
    sync_release_source,
    url_list_releases,
)

from .test_console import ConsoleBase
from .test_releases import FakeResponse, ReleaseSourceBase, fake_session, zip_file
from .utils import build_macho

LIST_URL = "https://downloads.example/list.json"


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
        options = {"name": "tools", "identifier": LIST_URL, "list_downloads": "downloads.headless\ndownloads.app",
                   "list_filters": "platform=^mac-arm64$", "keep_versions": 3, **kwargs}
        source = ReleaseSource.objects.create(kind=ReleaseSource.Kind.URL_LIST, **options)
        source.groups.add(self.dev)
        return source

    def downloaded(self, session):
        return sorted(call.args[0] for call in session.get.call_args_list if call.args[0] != LIST_URL)

    def test_the_newest_version_with_the_filtered_downloads(self):
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

    def test_entries_of_an_object_and_a_filter_on_the_entry(self):
        document = {"channels": {
            "Stable": {"channel": "Stable", **entry("2.0")},
            "Beta": {"channel": "Beta", **entry("3.0")},
        }}
        source = self.list_source(list_entries="channels.*", list_filters="platform=^mac-arm64$\nchannel=^Stable$",
                                  list_downloads="downloads.app")
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
        source = self.list_source(list_downloads="downloads.app")
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

    def explain(self, **options):
        with self.assertRaises(ReleaseError) as raised:
            source = ReleaseSource(kind=ReleaseSource.Kind.URL_LIST, identifier=LIST_URL, **{
                "list_downloads": "downloads.headless\ndownloads.app", "list_filters": "platform=^mac-arm64$",
                **options})
            url_list_releases(fake_session(list_routes(["1.0", "1.1"])), source, LIST_URL, 3)
        return str(raised.exception)

    def test_why_nothing_is_found(self):
        self.assertIn('versions in the list "versionz" finds nothing; the list has: versions',
                      self.explain(list_entries="versionz"))
        self.assertIn('2 entries, none has the field "vers"; the first has: downloads, revision, version',
                      self.explain(list_version_key="vers"))
        self.assertIn('no downloads at "downloads.ap"; "downloads" of the first entry has: app, headless',
                      self.explain(list_downloads="downloads.ap"))
        self.assertIn('none has the field "link"; they have: platform, url', self.explain(list_url_key="link"))
        self.assertIn("the filter platform=^mac-arm65$ matches none of 12 downloads; platform has: linux64, "
                      "mac-arm64, mac-x64", self.explain(list_filters="platform=^mac-arm65$"))
        self.assertIn('no download or entry has the field "os"', self.explain(list_filters="os=mac"))
        self.assertIn("no version matches the version pattern; the newest: 1.1, 1.0",
                      self.explain(version_pattern="^2\\."))

    def test_the_preview_says_why(self):
        source = self.list_source(list_downloads="downloads.ap")
        result = preview_release_source(source, fake_session(list_routes(["1.0"])))
        self.assertIn('no downloads at "downloads.ap"', result[0]["error"])
        self.assertEqual(result[0]["kind"], ReleaseSource.Kind.URL_LIST)

    def test_preview(self):
        source = self.list_source()
        result = preview_release_source(source, fake_session(list_routes(["1.0", "1.1"])))
        releases = result[0]["releases"]
        self.assertEqual([release["version"] for release in releases], ["1.1", "1.0"])
        self.assertEqual([asset["name"] for asset in releases[0]["assets"]], ["headless.zip", "app.zip"])

    def test_version_order(self):
        versions = ["9.0", "10.0.0.1", "10.0.0.12", "10.0.0.2", "10.0.0.2-beta"]
        self.assertEqual(sorted(versions, key=list_version_order, reverse=True),
                         ["10.0.0.12", "10.0.0.2-beta", "10.0.0.2", "10.0.0.1", "9.0"])

    def test_checks_of_the_fields(self):
        source = ReleaseSource(name="tools", kind=ReleaseSource.Kind.URL_LIST, identifier="http://downloads.example/x",
                               list_filters="platform\nchannel=[", keep_unit=ReleaseSource.KeepUnit.WEEKS)
        with self.assertRaises(ValidationError) as raised:
            source.full_clean()
        errors = raised.exception.message_dict
        self.assertIn("only https URLs", errors["identifier"][0])
        self.assertEqual(set(errors), {"identifier", "list_downloads", "list_filters", "keep_unit"})
        self.assertEqual(len(errors["list_filters"]), 2)

    def test_round_trip(self):
        self.list_source(list_entries="channels.*", list_filters="platform=^mac-arm64$\nchannel=^Stable$")
        data = json.loads(json.dumps(export_config()))
        ReleaseSource.objects.all().delete()
        import_config(data)
        source = ReleaseSource.objects.get()
        self.assertEqual((source.kind, source.list_format, source.list_entries, source.list_version_key,
                          source.list_downloads, source.list_url_key, source.list_filters),
                         (ReleaseSource.Kind.URL_LIST, "json", "channels.*", "version",
                          "downloads.headless\ndownloads.app", "url", "platform=^mac-arm64$\nchannel=^Stable$"))

    def test_the_list_needs_https(self):
        with self.assertRaisesMessage(ReleaseError, "only https"):
            url_list_releases(fake_session({}), self.list_source(), "http://downloads.example/x", 1)


class UrlListPagesTestCase(ConsoleBase):
    def test_the_overview_names_the_list_by_its_file(self):
        source = ReleaseSource.objects.create(
            name="tools", kind=ReleaseSource.Kind.URL_LIST, identifier=LIST_URL, keep_versions=2,
            list_downloads="downloads.headless\ndownloads.app", list_filters="platform=^mac-arm64$")
        source.groups.add(self.dev)
        sync_release_source(source, fake_session(list_routes(["1.0"])))
        response = self.client.get(reverse("console:source", args=(source.pk,)), headers={"HX-Request": "true"})
        self.assertContains(response, f'<span class="identifier-title" title="{LIST_URL}">list.json</span>')
        self.assertContains(response, f'href="{LIST_URL}" rel="noopener noreferrer" target="_blank">downloads.example')
        self.assertNotContains(response, f"<code>{LIST_URL}</code>")
        self.assertContains(response, "downloads.headless<br>downloads.app")
        self.assertContains(response, "platform=^mac-arm64$")
        # the versions, rendered with the rows of the drawer
        response = self.client.get(reverse("console:source", args=(source.pk,)),
                                   headers={"HX-Request": "true", "X-Santa-Rows": "1"})
        self.assertContains(response, '<div class="version-files"><span class="chip small">app.zip</span>'
                                      '<span class="chip small">headless.zip</span></div>')
        response = self.client.get(reverse("console:sources"), {"rows": "now"})
        self.assertContains(response, f'<span class="mono" title="{LIST_URL}">list.json</span>')


class UrlListFormTestCase(ConsoleBase):
    def test_the_form_takes_the_list_fields(self):
        response = self.client.post(reverse("console:source_add"), {
            "name": "tools", "kind": ReleaseSource.Kind.URL_LIST, "identifier": LIST_URL, "is_enabled": "on",
            "list_format": "json", "list_entries": "versions", "list_version_key": "version",
            "list_downloads": "downloads.app", "list_url_key": "url", "list_filters": "platform=^mac-arm64$",
            "rule_type": "BINARY", "policy": "ALLOWLIST", "groups": [self.dev.pk], "auto_approve": "on",
            "auto_approve_delay_days": 0, "keep_versions": 3, "keep_unit": "WEEKS", "approve_kept_versions": "on"})
        self.assertEqual(response.status_code, 302, response.content[:2000])
        source = ReleaseSource.objects.get()
        # no dates in a list: a number of versions; the older ones can be allowed
        self.assertEqual((source.keep_unit, source.approve_kept_versions), ("VERSIONS", True))
        self.assertEqual(source.list_filters, "platform=^mac-arm64$")

    def test_the_fields_show_only_for_a_list(self):
        response = self.client.get(reverse("console:source_add"))
        self.assertContains(response, '<div data-show-when="kind=URL_LIST">')
        self.assertContains(response, 'name="list_downloads"')
        # "List" instead of "Packages", and only the help of the chosen catalog
        self.assertContains(response, '<span data-show-when="kind=URL_LIST">List</span>')
        self.assertContains(response, '<small class="help" data-show-when="kind=URL_LIST">The https URL of the JSON')
        self.assertContains(response, '<small class="help" data-show-when="kind=GITHUB_RELEASE">GitHub: owner/repo')
        self.assertNotContains(response, "GitHub: owner/repo (e.g. abiosoft/colima) · Homebrew")
