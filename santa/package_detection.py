"""Which package a blocked binary comes from: suggestions for the package rules (events → "Allow as package").

The sources, the most reliable first: the same file in a release a package rule recorded (its SHA-256), the bundle
ID or the app name in the Homebrew cask data, the install path (Cellar, Caskroom, node_modules, VS Code extensions).
A suggestion is only a proposal: the console shows its source and lets the administrator change it.
"""
import re
from dataclasses import dataclass, field

from django.utils.translation import gettext_lazy as _

from . import catalog
from .models import ReleaseSource, Rule, RuleType
from .services import chunked

Kind = ReleaseSource.Kind

# the catalogs in a few letters: a chip in a list, the catalog of a suggestion (the long names wrap)
SHORT_KIND_LABELS = {
    Kind.GITHUB_RELEASE: _("GitHub"),
    Kind.HOMEBREW_FORMULA: _("Formula"),
    Kind.HOMEBREW_CASK: _("Cask"),
    Kind.URL: _("URL"),
    Kind.URL_LIST: _("List"),
    Kind.NPM_PACKAGE: _("npm"),
    Kind.VSCODE_EXTENSION: _("VS Code"),
    Kind.JETBRAINS_PLUGIN: _("JetBrains"),
}

SOURCE_RELEASE = "release"
SOURCE_BUNDLE_ID = "bundle_id"
SOURCE_APP_NAME = "app_name"
SOURCE_PATH = "path"

# the install path of a package: (kind, regex with name and, if it has one, version)
HOMEBREW_PREFIX = r"(?:/opt/homebrew|/usr/local|/home/linuxbrew/\.linuxbrew)"
PATH_PATTERNS = (
    (Kind.HOMEBREW_FORMULA, re.compile(rf"{HOMEBREW_PREFIX}/Cellar/(?P<name>[^/]+)/(?P<version>[^/]+)/")),
    (Kind.HOMEBREW_FORMULA, re.compile(rf"{HOMEBREW_PREFIX}/opt/(?P<name>[^/]+)/")),
    (Kind.HOMEBREW_CASK, re.compile(rf"{HOMEBREW_PREFIX}/Caskroom/(?P<name>[^/]+)/(?P<version>[^/]+)/")),
    (Kind.VSCODE_EXTENSION, re.compile(
        r"/\.vscode(?:-insiders|-server)?/extensions/(?P<name>[\w-]+\.[\w-]+?)-(?P<version>\d+\.\d+[^/]*?)"
        r"(?:-(?:darwin|universal)[\w-]*)?/")),
)
# the package in a node_modules folder (@scope/name or name), after the innermost one
NODE_PACKAGE = re.compile(r"^((?:@[^/]+/)?[^/.@][^/]*)/")


@dataclass
class Suggestion:
    kind: str
    identifier: str
    source: str
    version: str = ""
    name: str = ""
    icon_url: str = ""
    # the catalog knows the identifier (a path can name a package that is not in the catalog, e.g. from a tap)
    confirmed: bool = False
    # the current version in the catalog, when it differs from the one the path names
    latest_version: str = ""
    # other casks for the same app name: the administrator chooses
    alternatives: list = field(default_factory=list)
    events: list = field(default_factory=list)
    shas: set = field(default_factory=set)
    example_path: str = ""
    # the package rules that contain the identifier already
    contained_in: list = field(default_factory=list)

    @property
    def key(self):
        return (self.kind, self.identifier)

    def get_kind_label(self):
        return ReleaseSource.Kind(self.kind).label if self.kind else ""

    def get_short_kind_label(self):
        return SHORT_KIND_LABELS.get(self.kind, "")


def full_path(event):
    """Santa reports the folder in file_path and the name in file_name"""
    path = event.file_path or ""
    if event.file_name and not path.endswith("/" + event.file_name):
        path = f"{path.rstrip('/')}/{event.file_name}"
    return path


def package_from_path(path):
    """(kind, identifier, version) of an install path, or None"""
    for kind, pattern in PATH_PATTERNS:
        if found := pattern.search(path):
            return kind, found["name"], found.groupdict().get("version") or ""
    if "/node_modules/" in path and (found := NODE_PACKAGE.match(path.rsplit("/node_modules/", 1)[1])):
        return Kind.NPM_PACKAGE, found[1], ""
    return None


def _released_files(shas, all_rule_types=True):
    """{sha256: (source, version)} of the binaries the package rules recorded. Binary rules by their identifier;
    all_rule_types: also the files of the other rules (a scan of their documents, not for every list)."""
    found = {}
    for chunk in chunked(list(shas)):
        for rule in (Rule.objects.filter(rule_type=RuleType.BINARY, identifier__in=chunk, release_dependency="")
                     .exclude(release_version=None).select_related("release_source", "release_version")):
            found[rule.identifier] = (rule.release_source, rule.release_version)
    missing = set(shas) - set(found)
    if missing and all_rule_types:
        # the other rule types keep their files as a document: compared here, not in the database
        for rule in (Rule.objects.exclude(rule_type=RuleType.BINARY).exclude(release_version=None)
                     .filter(release_dependency="").select_related("release_source", "release_version")
                     .only("release_files", "release_source", "release_version")):
            for file in rule.release_files:
                if file.get("sha256") in missing:
                    found[file["sha256"]] = (rule.release_source, rule.release_version)
    return found


def _app_name(event):
    bundle_path = (event.raw or {}).get("file_bundle_path") or ""
    name = bundle_path.rstrip("/").rsplit("/", 1)[-1] if bundle_path.endswith(".app") else ""
    return name or (f"{event.bundle_name}.app" if event.bundle_name else "")


def _suggestion_for(event, released, casks):
    if hit := released.get(event.file_sha256):
        source, version = hit
        return Suggestion(source.kind, version.identifier, SOURCE_RELEASE, version=version.version, confirmed=True)
    bundle_id = event.bundle_id or (event.raw or {}).get("file_bundle_id") or ""
    by_id, by_app = casks
    if bundle_id and len(set(by_id.get(bundle_id, []))) == 1:
        return Suggestion(Kind.HOMEBREW_CASK, by_id[bundle_id][0], SOURCE_BUNDLE_ID, confirmed=True)
    app = _app_name(event)
    if app and (tokens := sorted(set(by_app.get(app.lower(), [])))):
        return Suggestion(Kind.HOMEBREW_CASK, tokens[0], SOURCE_APP_NAME, confirmed=True, alternatives=tokens[1:])
    if found := package_from_path(full_path(event)):
        kind, identifier, version = found
        return Suggestion(kind, identifier, SOURCE_PATH, version=version)
    return None


def same_version(installed, latest):
    """The Cellar names a formula with its revision (8.10.1_1), the catalog may give the version without it; 8.10.10 is
    another version than 8.10.1"""
    return installed == latest or re.sub(r"_\d+$", "", installed) == re.sub(r"_\d+$", "", latest)


def _confirm(suggestion, formulae, cached_only):
    """A suggestion from a path: is it in the catalog? Name and icon for the form."""
    if suggestion.kind == Kind.HOMEBREW_FORMULA:
        if suggestion.identifier in formulae:
            suggestion.confirmed = True
            latest = formulae[suggestion.identifier]
            if latest and suggestion.version and not same_version(suggestion.version, latest):
                suggestion.latest_version = latest
        return
    if cached_only:
        return
    found = catalog.lookup(suggestion.kind, suggestion.identifier)
    if found:
        suggestion.confirmed = suggestion.confirmed or suggestion.source == SOURCE_PATH
        suggestion.name = found.name if found.name != suggestion.identifier else ""
        suggestion.icon_url = found.icon_url


def suggest_packages(events, cached_only=False):
    """(suggestions, the events without one). cached_only: no catalog requests (rendering a list)."""
    events = list(events)
    released = _released_files({event.file_sha256 for event in events}, all_rule_types=not cached_only)
    needs_casks = any(event.bundle_id or event.bundle_name or (event.raw or {}).get("file_bundle_path")
                      for event in events)
    casks = ({}, {})
    if needs_casks:
        tables = catalog.homebrew_lookup(Kind.HOMEBREW_CASK, cached_only=cached_only)
        casks = (tables.get("by_id", {}), tables.get("by_app", {}))
    by_key = {}
    unmatched = []
    for event in events:
        suggestion = _suggestion_for(event, released, casks)
        if suggestion is None:
            unmatched.append(event)
            continue
        suggestion = by_key.setdefault(suggestion.key, suggestion)
        suggestion.events.append(event)
        suggestion.shas.add(event.file_sha256)
        suggestion.example_path = suggestion.example_path or full_path(event)
    suggestions = list(by_key.values())
    formulae = {}
    if any(s.kind == Kind.HOMEBREW_FORMULA and s.source == SOURCE_PATH for s in suggestions):
        formulae = catalog.homebrew_lookup(Kind.HOMEBREW_FORMULA, cached_only=cached_only).get("versions", {})
    sources = list(ReleaseSource.objects.filter(kind__in={s.kind for s in suggestions}).order_by("name"))
    for suggestion in suggestions:
        if suggestion.source == SOURCE_PATH or not cached_only:
            _confirm(suggestion, formulae, cached_only)
        suggestion.contained_in = [source for source in sources
                                   if source.kind == suggestion.kind and suggestion.identifier in source.identifiers]
    return suggestions, unmatched


def package_hints(events):
    """{sha256: suggestion} without catalog requests (the detail and the list of blocked apps)"""
    suggestions, _unmatched = suggest_packages(events, cached_only=True)
    return {sha: suggestion for suggestion in suggestions for sha in suggestion.shas}
