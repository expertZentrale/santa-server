import json
import time
from unittest.mock import MagicMock, patch

from django.core.cache import cache
from django.test import TestCase, override_settings

from santa import catalog
from santa.models import ReleaseSource

LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "catalog-tests"}}
Kind = ReleaseSource.Kind


def response(data, status_code=200):
    mock = MagicMock(status_code=status_code)
    mock.json.return_value = data
    return mock


def session(get=None, post=None):
    mock = MagicMock()
    mock.get.return_value = get
    mock.post.return_value = post
    return mock


VSCODE = {"results": [{"extensions": [{
    "extensionName": "rust-analyzer", "displayName": "rust-analyzer", "shortDescription": "Rust language support",
    "publisher": {"publisherName": "rust-lang", "flags": "verified"},
    "versions": [{"files": [{"assetType": "Microsoft.VisualStudio.Services.Icons.Default",
                             "source": "https://cdn.example/icon.png"}]}],
}]}]}


def streamed(data):
    mock = response(None)
    mock.__enter__.return_value = mock
    mock.iter_content.return_value = [json.dumps(data).encode()]
    return mock


FORMULAE = [
    {"name": "colima", "aliases": [], "desc": "Container runtimes on macOS", "homepage": "https://github.com/abiosoft/colima"},
    {"name": "lima", "aliases": [], "desc": "Linux virtual machines, used by colima", "homepage": "https://lima-vm.io"},
    {"name": "colima-extra", "aliases": [], "desc": "", "homepage": ""},
    {"name": "podman", "aliases": ["kolima"], "desc": "", "homepage": ""},
    {"name": "colima-old", "desc": "", "homepage": "", "disabled": True},
]
CASKS = [{"token": "firefox", "name": ["Mozilla Firefox"], "desc": "Web browser", "homepage": "https://www.mozilla.org"},
         {"token": "visual-studio-code", "name": ["Microsoft Visual Studio Code", "VS Code"], "desc": "Editor",
          "homepage": ""}]


@override_settings(CACHES=LOCMEM)
class CatalogTestCase(TestCase):
    def setUp(self):
        cache.clear()

    def test_vscode(self):
        s = session(post=response(VSCODE))
        [found] = catalog.search(Kind.VSCODE_EXTENSION, "rust", s)
        self.assertEqual((found.identifier, found.name, found.icon_url, found.verified),
                         ("rust-lang.rust-analyzer", "rust-analyzer", "https://cdn.example/icon.png", True))
        criteria = s.post.call_args.kwargs["json"]["filters"][0]["criteria"]
        self.assertIn({"filterType": 10, "value": "rust"}, criteria)

    def test_github(self):
        s = session(get=response({"items": [{"full_name": "BurntSushi/ripgrep", "name": "ripgrep",
                                             "description": "fast grep",
                                             "owner": {"avatar_url": "https://avatars.example/1"}}]}))
        [found] = catalog.search(Kind.GITHUB_RELEASE, "ripgrep", s)
        self.assertEqual((found.identifier, found.icon_url), ("BurntSushi/ripgrep", "https://avatars.example/1"))

    def test_github_query_matches_parts_of_names(self):
        self.assertEqual(catalog.github_query("ersitzt/intelli"), "intelli in:name user:ersitzt")
        self.assertEqual(catalog.github_query("ersitzt/"), "user:ersitzt")
        self.assertEqual(catalog.github_query("ripgre"), "ripgre in:name")
        s = session(get=response({"items": []}))
        catalog.search(Kind.GITHUB_RELEASE, "erSitzt/intelligent-cl", s)
        self.assertEqual(s.get.call_args.kwargs["params"]["q"], "intelligent-cl in:name user:erSitzt")

    def test_github_rate_limit(self):
        with self.assertRaises(catalog.CatalogError):
            catalog.search(Kind.GITHUB_RELEASE, "ripgrep", session(get=response({}, status_code=403)))

    def test_npm_uses_the_github_avatar(self):
        s = session(get=response({"objects": [
            {"package": {"name": "esbuild", "description": "bundler",
                         "links": {"repository": "https://github.com/evanw/esbuild"}}},
            {"package": {"name": "left-pad", "links": {}}},
        ]}))
        found = catalog.search(Kind.NPM_PACKAGE, "esbuild", s)
        self.assertEqual(found[0].icon_url, "https://github.com/evanw.png?size=64")
        self.assertEqual(found[1].icon_url, "")

    def test_homebrew_formula_ranks_names_before_descriptions(self):
        s = session(get=streamed(FORMULAE))
        found = catalog.search(Kind.HOMEBREW_FORMULA, "Colima", s)
        self.assertEqual([f.identifier for f in found], ["colima", "colima-extra", "lima"])
        self.assertEqual(found[0].icon_url, "https://github.com/abiosoft.png?size=64")
        self.assertEqual(s.get.call_args.args[0], "https://formulae.brew.sh/api/formula.json")
        # aliases are names too
        self.assertEqual([f.identifier for f in catalog.search(Kind.HOMEBREW_FORMULA, "kolima", s)], ["podman"])
        # the list is loaded once, not for every search
        self.assertEqual(s.get.call_count, 1)

    def test_homebrew_cask(self):
        s = session(get=streamed(CASKS))
        [found] = catalog.search(Kind.HOMEBREW_CASK, "firef", s)
        self.assertEqual((found.identifier, found.name, found.description),
                         ("firefox", "Mozilla Firefox", "Web browser"))
        self.assertEqual(s.get.call_args.args[0], "https://formulae.brew.sh/api/cask.json")

    def test_homebrew_cask_other_names_are_searched(self):
        found = catalog.search(Kind.HOMEBREW_CASK, "vs code", session(get=streamed(CASKS)))
        self.assertEqual([(f.identifier, f.name) for f in found],
                         [("visual-studio-code", "Microsoft Visual Studio Code")])

    def homebrew_index_key(self, kind=Kind.HOMEBREW_FORMULA):
        return f"santa:catalog:v{catalog.CACHE_VERSION}:homebrew-index:{kind}"

    def store_old_index(self):
        cache.set(self.homebrew_index_key(), {
            "loaded_at": time.time() - catalog.HOMEBREW_INDEX_TIMEOUT - 1,
            "entries": [catalog._homebrew_entry(Kind.HOMEBREW_FORMULA, FORMULAE[0])]}, None)

    def test_homebrew_list_is_loaded_by_one_worker(self):
        # another worker loads the list: no second download, the search tries again later
        cache.add(self.homebrew_index_key() + ":lock", "1")
        s = session(get=streamed(FORMULAE))
        with self.assertRaises(catalog.CatalogError):
            catalog.search(Kind.HOMEBREW_FORMULA, "colima", s)
        # meanwhile, the older list is used
        self.store_old_index()
        self.assertEqual([f.identifier for f in catalog.search(Kind.HOMEBREW_FORMULA, "colima", s)], ["colima"])
        s.get.assert_not_called()

    def test_older_homebrew_list_is_replaced_or_kept(self):
        self.store_old_index()
        s = MagicMock()
        s.get.side_effect = catalog.requests.ConnectionError("down")
        self.assertEqual([f.identifier for f in catalog.search(Kind.HOMEBREW_FORMULA, "lima", s)], ["colima"])
        s = session(get=streamed(FORMULAE))
        self.assertEqual([f.identifier for f in catalog.search(Kind.HOMEBREW_FORMULA, "podman", s)], ["podman"])
        self.assertIsNone(cache.get(self.homebrew_index_key() + ":lock"))

    def test_homebrew_list_size_is_limited(self):
        with self.assertRaises(catalog.CatalogError):
            with patch.object(catalog, "HOMEBREW_INDEX_MAX_BYTES", 10):
                catalog.search(Kind.HOMEBREW_FORMULA, "colima", session(get=streamed(FORMULAE)))

    def test_homebrew_lookup(self):
        s = session(get=response({"token": "firefox", "name": ["Mozilla Firefox"], "homepage": "https://www.mozilla.org"}))
        found = catalog.lookup(Kind.HOMEBREW_CASK, "firefox", s)
        self.assertEqual(found.name, "Mozilla Firefox")
        self.assertEqual(s.get.call_args.args[0], "https://formulae.brew.sh/api/cask/firefox.json")

    def test_jetbrains(self):
        s = session(get=response({"plugins": [
            {"id": 10080, "xmlId": "izhangzhihao.rainbow.brackets", "name": "Rainbow Brackets",
             "icon": "/files/10080/1/icon/default.svg", "vendor": {"isVerified": True}},
            {"id": 8214, "xmlId": "com.example.noicon", "name": "No icon"},
        ]}))
        found = catalog.search(Kind.JETBRAINS_PLUGIN, "rainbow", s)
        self.assertEqual(found[0].icon_url, "https://plugins.jetbrains.com/files/10080/1/icon/default.svg")
        self.assertEqual(found[1].icon_url, "")

    def test_not_searchable_and_short_queries(self):
        s = session()
        self.assertEqual(catalog.search(Kind.URL, "https://example.com", s), [])
        self.assertEqual(catalog.search(Kind.NPM_PACKAGE, "e", s), [])
        s.get.assert_not_called()

    def test_results_are_cached(self):
        s = session(post=response(VSCODE))
        catalog.search(Kind.VSCODE_EXTENSION, "Rust", s)
        catalog.search(Kind.VSCODE_EXTENSION, "rust ", s)
        self.assertEqual(s.post.call_count, 1)

    def test_update_identifier_icons(self):
        source = ReleaseSource(name="tools", kind=Kind.VSCODE_EXTENSION,
                               identifier="rust-lang.rust-analyzer\nexample.picked\nexample.unknown")
        s = MagicMock()
        s.post.side_effect = [response(VSCODE), response({"results": [{"extensions": []}]})]
        catalog.update_identifier_icons(source, {"example.picked": {"name": "Picked", "icon_url": ""}}, s)
        self.assertEqual(source.identifier_icons, {
            "rust-lang.rust-analyzer": {"name": "rust-analyzer", "icon_url": "https://cdn.example/icon.png"},
            "example.picked": {"name": "Picked", "icon_url": ""},
        })

    def test_lookup_failure_is_not_an_error(self):
        s = MagicMock()
        s.get.side_effect = catalog.requests.ConnectionError("down")
        self.assertIsNone(catalog.lookup(Kind.NPM_PACKAGE, "esbuild", s))
