"""Allowlist the binaries of new releases of unsigned tools (colima, …).

Every release asset is downloaded, the Mach-O executables inside are hashed, and one BINARY rule
is created per hash. Santa then allows exactly these files, and nothing else built by someone else.
"""
import base64
import fnmatch
import hashlib
import logging
import re
import tarfile
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime

import requests
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.utils.http import urlencode

from . import macho, notifications, semver
from .models import DEPENDENCY_KINDS, HISTORY_KINDS, ReleaseSource, ReleaseVersion, Rule, RuleType
from .services import chunked

logger = logging.getLogger(__name__)

SKIPPED_ASSET_SUFFIXES = (".sha256", ".sha256sum", ".sha512", ".md5", ".txt", ".sig", ".asc", ".pem",
                          ".sbom", ".json", ".spdx", ".intoto.jsonl", ".bundle", ".deb", ".rpm", ".apk", ".exe",
                          ".msi")
UNSUPPORTED_SUFFIXES = (".dmg", ".pkg")
# anonymous token accepted by ghcr.io for the Homebrew bottles
HOMEBREW_BOTTLE_TOKEN = "QQ=="
MAX_ARCHIVE_MEMBERS = 20000


class ReleaseError(Exception):
    def __init__(self, message, new_versions=()):
        super().__init__(message)
        # the versions the other identifiers of the source found before one failed
        self.new_versions = list(new_versions)


@dataclass
class Asset:
    name: str
    url: str
    sha256: str = ""
    headers: dict = None
    # npm only publishes a SHA-512
    sha512: str = ""


@dataclass
class Release:
    version: str
    assets: list
    published_at: object = None
    # what the release needs: {name: version range} (npm), [name, …] (Homebrew formula)
    dependencies: object = None


@dataclass
class FoundBinary:
    asset: str
    path: str
    info: macho.MachOInfo
    # name@version of the dependency it came from, empty for the package itself
    dependency: str = ""


# Downloads and archives


def http_session():
    session = requests.Session()
    session.headers["User-Agent"] = "santa-server"
    return session


def download(session, asset):
    """Download the asset to a temporary file, check its SHA-256 if known"""
    tmp = tempfile.TemporaryFile()
    digest = hashlib.sha256()
    digest512 = hashlib.sha512()
    size = 0
    with session.get(asset.url, headers=asset.headers or {}, stream=True,
                     timeout=settings.RELEASE_HTTP_TIMEOUT) as response:
        response.raise_for_status()
        for chunk in response.iter_content(macho.CHUNK_SIZE):
            size += len(chunk)
            if size > settings.RELEASE_MAX_DOWNLOAD_BYTES:
                tmp.close()
                raise ReleaseError(f"{asset.name}: bigger than {settings.RELEASE_MAX_DOWNLOAD_BYTES} bytes")
            digest.update(chunk)
            digest512.update(chunk)
            tmp.write(chunk)
    if asset.sha256 and digest.hexdigest() != asset.sha256.lower():
        tmp.close()
        raise ReleaseError(f"{asset.name}: SHA-256 mismatch, expected {asset.sha256}, got {digest.hexdigest()}")
    if asset.sha512 and digest512.hexdigest() != asset.sha512.lower():
        tmp.close()
        raise ReleaseError(f"{asset.name}: SHA-512 mismatch, expected {asset.sha512}, got {digest512.hexdigest()}")
    tmp.seek(0)
    return tmp


class _UnpackBudget:
    """The bytes an archive may still unpack: a small download must not fill the disk (decompression bomb)"""

    def __init__(self, name):
        self.name = name
        self.remaining = settings.RELEASE_MAX_UNPACKED_BYTES

    def consume(self, size):
        self.remaining -= size
        if self.remaining < 0:
            raise ReleaseError(f"{self.name}: unpacks to more than {settings.RELEASE_MAX_UNPACKED_BYTES} bytes")


def _spool(member_fileobj, budget):
    spooled = tempfile.SpooledTemporaryFile(max_size=16 * 1024 * 1024)
    try:
        while chunk := member_fileobj.read(macho.CHUNK_SIZE):
            budget.consume(len(chunk))
            spooled.write(chunk)
    except BaseException:
        spooled.close()
        raise
    spooled.seek(0)
    return spooled


def _worth_unpacking(path, size, pattern):
    # small files are no executables, too big ones are skipped instead of filling the disk
    return 4096 <= size <= settings.RELEASE_MAX_FILE_BYTES and _matches(path, pattern)


def _matches(path, pattern):
    return not pattern or fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(path.rsplit("/", 1)[-1], pattern)


def _inspect_candidate(fileobj, path, pattern):
    if not _matches(path, pattern):
        return None
    try:
        info = macho.inspect(fileobj)
    except macho.NotMachO:
        return None
    # the dylibs and bundles are never executed on their own, Santa does not check them
    if not info.is_executable:
        return None
    return info


def find_binaries(fileobj, name, pattern=""):
    """Yield (path, MachOInfo) for the Mach-O executables in a raw binary, a zip or a tar archive"""
    if name.lower().endswith(UNSUPPORTED_SUFFIXES):
        raise ReleaseError(f"{name}: .dmg and .pkg files cannot be unpacked here. "
                           "Signed apps are better allowed with a Team ID or Signing ID rule.")
    fileobj.seek(0)
    header = fileobj.read(8)
    fileobj.seek(0)
    if macho.is_macho_header(header):
        info = _inspect_candidate(fileobj, name, pattern)
        if info:
            yield name, info
    elif zipfile.is_zipfile(fileobj):
        fileobj.seek(0)
        budget = _UnpackBudget(name)
        with zipfile.ZipFile(fileobj) as archive:
            for member in archive.infolist()[:MAX_ARCHIVE_MEMBERS]:
                if member.is_dir() or not _worth_unpacking(member.filename, member.file_size, pattern):
                    continue
                with archive.open(member) as member_fileobj, _spool(member_fileobj, budget) as spooled:
                    info = _inspect_candidate(spooled, member.filename, pattern)
                    if info:
                        yield member.filename, info
    else:
        fileobj.seek(0)
        try:
            archive = tarfile.open(fileobj=fileobj, mode="r:*")
        except tarfile.TarError:
            raise ReleaseError(f"{name}: not a Mach-O binary, zip or tar archive")
        budget = _UnpackBudget(name)
        with archive:
            for index, member in enumerate(archive):
                if index >= MAX_ARCHIVE_MEMBERS:
                    break
                if not member.isfile() or not _worth_unpacking(member.name, member.size, pattern):
                    continue
                member_fileobj = archive.extractfile(member)
                if member_fileobj is None:
                    continue
                with member_fileobj, _spool(member_fileobj, budget) as spooled:
                    info = _inspect_candidate(spooled, member.name, pattern)
                    if info:
                        yield member.name, info


# Release providers


def _asset_regex(source):
    try:
        return re.compile(source.asset_pattern) if source.asset_pattern else None
    except re.error as e:
        raise ReleaseError(f"Invalid asset pattern: {e}")


def latest_github_release(session, source, identifier):
    headers = _github_headers()
    base = f"https://api.github.com/repos/{identifier}/releases"
    if source.include_prereleases:
        response = session.get(base, params={"per_page": 10}, headers=headers, timeout=settings.RELEASE_HTTP_TIMEOUT)
        response.raise_for_status()
        releases = [r for r in response.json() if not r.get("draft")]
        if not releases:
            raise ReleaseError("No release found")
        data = releases[0]
    else:
        response = session.get(f"{base}/latest", headers=headers, timeout=settings.RELEASE_HTTP_TIMEOUT)
        response.raise_for_status()
        data = response.json()
    return _github_release(source, data)


def _github_headers():
    headers = {"Accept": "application/vnd.github+json"}
    if settings.GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {settings.GITHUB_TOKEN}"
    return headers


def github_releases(session, source, identifier, limit):
    """The releases of the repository, newest first"""
    response = session.get(f"https://api.github.com/repos/{identifier}/releases",
                           params={"per_page": min(100, max(10, limit * 2))}, headers=_github_headers(),
                           timeout=settings.RELEASE_HTTP_TIMEOUT)
    response.raise_for_status()
    return [_github_release(source, data) for data in response.json()
            if not data.get("draft") and (source.include_prereleases or not data.get("prerelease"))]


def _github_release(source, data):
    regex = _asset_regex(source)
    assets = []
    for asset in data.get("assets", []):
        name = asset["name"]
        if name.lower().endswith(SKIPPED_ASSET_SUFFIXES):
            continue
        if regex and not regex.search(name):
            continue
        digest = asset.get("digest") or ""
        assets.append(Asset(name=name, url=asset["browser_download_url"],
                            sha256=digest.removeprefix("sha256:") if digest.startswith("sha256:") else ""))
    published_at = parse_datetime(data.get("published_at") or "")
    return Release(version=data["tag_name"], assets=assets, published_at=published_at)


def latest_homebrew_formula(session, source, identifier):
    response = session.get(f"https://formulae.brew.sh/api/formula/{identifier}.json",
                           timeout=settings.RELEASE_HTTP_TIMEOUT)
    response.raise_for_status()
    data = response.json()
    version = data["versions"]["stable"]
    if data.get("revision"):
        version = f"{version}_{data['revision']}"
    regex = _asset_regex(source)
    assets = []
    for tag, bottle in sorted(data.get("bottle", {}).get("stable", {}).get("files", {}).items()):
        if tag.startswith(("x86_64_linux", "arm64_linux")) or (regex and not regex.search(tag)):
            continue
        assets.append(Asset(name=f"{identifier}-{version}.{tag}.bottle.tar.gz", url=bottle["url"],
                            sha256=bottle["sha256"], headers={"Authorization": f"Bearer {HOMEBREW_BOTTLE_TOKEN}"}))
    return Release(version=version, assets=assets, dependencies=list(data.get("dependencies") or []))


def _ghcr_repository(name):
    # the image name of a formula in the Homebrew registry: python@3.12 -> python/3.12, gtk+3 -> gtkx3
    return name.replace("@", "/").replace("+", "x")


def homebrew_formula_releases(session, source, identifier, limit):
    """The bottles of the older versions of a formula, newest first, from the Homebrew registry (ghcr.io).

    formulae.brew.sh only knows the current version. Every tag of the registry is a version (a rebuild of the same
    bottle gets "-1", "-2"…); its index lists the bottles per macOS with their SHA-256 and the publication date.
    """
    headers = {"Authorization": f"Bearer {HOMEBREW_BOTTLE_TOKEN}"}
    base = f"https://ghcr.io/v2/homebrew/core/{_ghcr_repository(identifier)}"
    response = session.get(f"{base}/tags/list", headers=headers, timeout=settings.RELEASE_HTTP_TIMEOUT)
    response.raise_for_status()
    regex = _asset_regex(source)
    cutoff = source.keep_cutoff()
    releases = {}
    # the tags come oldest first; a looked-up version older than the range ends the search
    for tag in reversed(response.json().get("tags") or []):
        if len(releases) >= limit:
            break
        response = session.get(f"{base}/manifests/{tag}", timeout=settings.RELEASE_HTTP_TIMEOUT,
                               headers={**headers, "Accept": "application/vnd.oci.image.index.v1+json"})
        response.raise_for_status()
        index = response.json()
        annotations = index.get("annotations") or {}
        version = annotations.get("org.opencontainers.image.version") or tag
        published_at = parse_datetime(annotations.get("org.opencontainers.image.created") or "")
        if cutoff and published_at and published_at < cutoff:
            break
        if version in releases:
            # an older build of a version already found
            continue
        assets = []
        for manifest in index.get("manifests") or []:
            if (manifest.get("platform") or {}).get("os") != "darwin":
                continue
            bottle = manifest.get("annotations") or {}
            # "0.9.1.arm64_sequoia.1": version, bottle tag, rebuild
            bottle_tag = bottle.get("org.opencontainers.image.ref.name", "").removeprefix(f"{version}.").split(".")[0]
            digest = bottle.get("sh.brew.bottle.digest")
            if not digest or (regex and not regex.search(bottle_tag)):
                continue
            assets.append(Asset(name=f"{identifier}-{version}.{bottle_tag}.bottle.tar.gz",
                                url=f"{base}/blobs/sha256:{digest}", sha256=digest, headers=headers))
        releases[version] = Release(version=version, assets=sorted(assets, key=lambda asset: asset.name),
                                    published_at=published_at)
    return list(releases.values())


def latest_homebrew_cask(session, source, identifier):
    response = session.get(f"https://formulae.brew.sh/api/cask/{identifier}.json",
                           timeout=settings.RELEASE_HTTP_TIMEOUT)
    response.raise_for_status()
    data = response.json()
    candidates = {data["url"]: data.get("sha256")}
    for variation in (data.get("variations") or {}).values():
        if variation.get("url"):
            candidates.setdefault(variation["url"], variation.get("sha256") or data.get("sha256"))
    regex = _asset_regex(source)
    assets = []
    for url, sha256 in sorted(candidates.items()):
        if regex and not regex.search(url):
            continue
        name = url.split("?")[0].rsplit("/", 1)[-1]
        assets.append(Asset(name=name, url=url, sha256="" if sha256 in (None, "no_check") else sha256))
    return Release(version=str(data["version"]), assets=assets)


def latest_url(session, source, identifier):
    # no version information, the content hash is the version
    asset = Asset(name=identifier.split("?")[0].rsplit("/", 1)[-1] or "download", url=identifier)
    return Release(version="", assets=[asset])


def _npm_url(identifier):
    # scoped packages: the registry wants @scope%2Fname
    return f"https://registry.npmjs.org/{identifier.replace('/', '%2F')}"


def latest_npm_package(session, source, identifier):
    response = session.get(f"{_npm_url(identifier)}/latest", timeout=settings.RELEASE_HTTP_TIMEOUT)
    response.raise_for_status()
    return _npm_release(identifier, response.json())


def _npm_release(identifier, data, published_at=None):
    """A release of one version document of the registry"""
    dist = data["dist"]
    sha512 = ""
    integrity = dist.get("integrity") or ""
    if integrity.startswith("sha512-"):
        sha512 = base64.b64decode(integrity.removeprefix("sha512-")).hex()
    name = identifier.rsplit("/", 1)[-1]
    dependencies = {**(data.get("dependencies") or {}), **(data.get("optionalDependencies") or {})}
    return Release(version=data["version"], published_at=published_at, dependencies=dependencies,
                   assets=[Asset(name=f"{name}-{data['version']}.tgz", url=dist["tarball"], sha512=sha512)])


def npm_releases(session, source, identifier, limit):
    """The versions of the package, newest first (the full document: only it has the publication dates)"""
    response = session.get(_npm_url(identifier), timeout=settings.RELEASE_HTTP_TIMEOUT)
    response.raise_for_status()
    data = response.json()
    times = data.get("time") or {}
    releases = []
    for version, manifest in (data.get("versions") or {}).items():
        if semver.is_prerelease(version) and not source.include_prereleases:
            continue
        releases.append(_npm_release(identifier, manifest, parse_datetime(times.get(version) or "")))
    releases.sort(key=lambda release: release.published_at or datetime.min.replace(tzinfo=UTC), reverse=True)
    return releases


VSCODE_GALLERY_URL = "https://marketplace.visualstudio.com/_apis/public/gallery/extensionquery"
# IncludeVersions | IncludeFiles | IncludeVersionProperties | IncludeAssetUri
# | IncludeLatestPrereleaseAndStableVersionOnly: the full version list can be many megabytes
VSCODE_QUERY_FLAGS = 0x1 | 0x2 | 0x10 | 0x80 | 0x10000
VSCODE_MAC_PLATFORMS = ("darwin-arm64", "darwin-x64", "universal")


def latest_vscode_extension(session, source, identifier):
    releases = _vscode_releases(session, source, identifier, VSCODE_QUERY_FLAGS)
    if not releases:
        raise ReleaseError(f"No macOS version of {identifier}")
    return releases[0]


def vscode_releases(session, source, identifier, limit):
    """All versions of the extension, newest first (without the "latest only" flag: a bigger answer)"""
    return _vscode_releases(session, source, identifier, VSCODE_QUERY_FLAGS & ~0x10000)[:limit]


def _vscode_releases(session, source, identifier, flags):
    response = session.post(
        VSCODE_GALLERY_URL,
        json={"filters": [{"criteria": [{"filterType": 7, "value": identifier}], "pageSize": 1}], "flags": flags},
        headers={"Accept": "application/json;api-version=7.2-preview.1"},
        timeout=settings.RELEASE_HTTP_TIMEOUT,
    )
    response.raise_for_status()
    extensions = [e for result in response.json().get("results", []) for e in result.get("extensions", [])]
    if not extensions:
        raise ReleaseError(f"Extension {identifier} not found")
    regex = _asset_regex(source)
    # the versions come newest first, one entry per target platform
    releases = {}
    for version in extensions[0].get("versions", []):
        properties = {p["key"]: p["value"] for p in version.get("properties", [])}
        if properties.get("Microsoft.VisualStudio.Code.PreRelease") == "true" and not source.include_prereleases:
            continue
        platform = version.get("targetPlatform") or "universal"
        if platform not in VSCODE_MAC_PLATFORMS:
            continue
        release = releases.setdefault(version["version"], Release(version=version["version"], assets=[]))
        if regex and not regex.search(platform):
            continue
        url = next((f["source"] for f in version.get("files", [])
                    if f["assetType"] == "Microsoft.VisualStudio.Services.VSIXPackage"), None)
        if url:
            release.assets.append(Asset(name=f"{identifier}-{version['version']}@{platform}.vsix", url=url))
            release.published_at = max(filter(None, [release.published_at,
                                                     parse_datetime(version.get("lastUpdated") or "")]), default=None)
    return list(releases.values())


def latest_jetbrains_plugin(session, source, identifier):
    releases = jetbrains_releases(session, source, identifier, 20)
    if not releases:
        raise ReleaseError(f"No compatible update of {identifier}")
    return releases[0]


def jetbrains_releases(session, source, identifier, limit):
    """The compatible updates of the plugin, newest first"""
    base = "https://plugins.jetbrains.com/api/plugins"
    plugin_id = identifier
    if not identifier.isdigit():
        response = session.get(f"{base}/intellij/{identifier}", timeout=settings.RELEASE_HTTP_TIMEOUT)
        response.raise_for_status()
        plugin_id = response.json()["id"]
    params = {"size": max(20, min(100, limit))}
    if not source.include_prereleases:
        params["channel"] = ""
    response = session.get(f"{base}/{plugin_id}/updates?{urlencode(params)}", timeout=settings.RELEASE_HTTP_TIMEOUT)
    response.raise_for_status()
    regex = _asset_regex(source)
    releases = []
    for update in response.json():
        if not update.get("approve", True) or update.get("hidden"):
            continue
        if regex and not any(regex.search(product) for product in update.get("compatibleVersions") or {}):
            continue
        published_at = None
        if update.get("cdate"):
            published_at = datetime.fromtimestamp(int(update["cdate"]) / 1000, tz=UTC)
        asset = Asset(name=update["file"].rsplit("/", 1)[-1],
                      url=f"https://downloads.marketplace.jetbrains.com/files/{update['file']}")
        releases.append(Release(version=update["version"], assets=[asset], published_at=published_at))
    return releases


# Most packages of these catalogs are scripts (JavaScript, Python, Java): Santa has nothing to allow in them.
# A release without executables is recorded with 0 binaries instead of being an error.
KINDS_OFTEN_WITHOUT_EXECUTABLES = (ReleaseSource.Kind.NPM_PACKAGE, ReleaseSource.Kind.VSCODE_EXTENSION,
                                   ReleaseSource.Kind.JETBRAINS_PLUGIN)
NO_EXECUTABLES_NOTE = "No Mach-O executable in this release: Santa does not check scripts or libraries, " \
                      "nothing to allow."


PROVIDERS = {
    ReleaseSource.Kind.GITHUB_RELEASE: latest_github_release,
    ReleaseSource.Kind.HOMEBREW_FORMULA: latest_homebrew_formula,
    ReleaseSource.Kind.HOMEBREW_CASK: latest_homebrew_cask,
    ReleaseSource.Kind.URL: latest_url,
    ReleaseSource.Kind.NPM_PACKAGE: latest_npm_package,
    ReleaseSource.Kind.VSCODE_EXTENSION: latest_vscode_extension,
    ReleaseSource.Kind.JETBRAINS_PLUGIN: latest_jetbrains_plugin,
}
# the older versions, newest first: for "also allow the older kept versions"
HISTORY = {
    ReleaseSource.Kind.GITHUB_RELEASE: github_releases,
    ReleaseSource.Kind.HOMEBREW_FORMULA: homebrew_formula_releases,
    ReleaseSource.Kind.NPM_PACKAGE: npm_releases,
    ReleaseSource.Kind.VSCODE_EXTENSION: vscode_releases,
    ReleaseSource.Kind.JETBRAINS_PLUGIN: jetbrains_releases,
}
assert set(HISTORY) == set(HISTORY_KINDS)
# releases looked at for a time range, and the older versions added per identifier and sync (the rest follows)
HISTORY_LIMIT = 100
MAX_KEPT_VERSIONS_PER_SYNC = 10


# Dependencies


def _npm_platform_ok(manifest):
    # optional dependencies are often one package per platform (esbuild, swc, …): only the macOS ones
    platforms = manifest.get("os") or []
    if not platforms:
        return True
    if any(platform.startswith("!") for platform in platforms):
        return "!darwin" not in platforms
    return "darwin" in platforms


def npm_dependencies(session, release, errors):
    """The releases of the dependencies of an npm release, also the indirect ones"""
    found = {}
    queue = list((release.dependencies or {}).items())
    documents = {}
    while queue:
        name, spec = queue.pop(0)
        if spec.startswith("npm:"):
            # an alias: npm:real-name@range
            name, _, spec = spec.removeprefix("npm:").rpartition("@")
        if not name:
            continue
        if name not in documents:
            if len(documents) >= settings.RELEASE_MAX_DEPENDENCIES:
                errors.append(f"More than {settings.RELEASE_MAX_DEPENDENCIES} dependencies, the others are skipped")
                break
            response = session.get(_npm_url(name), timeout=settings.RELEASE_HTTP_TIMEOUT,
                                   headers={"Accept": "application/vnd.npm.install-v1+json"})
            response.raise_for_status()
            documents[name] = response.json()
        versions = documents[name].get("versions") or {}
        version = semver.max_satisfying(versions, spec)
        if version is None:
            tag = (documents[name].get("dist-tags") or {}).get(spec)
            version = tag if tag in versions else None
        if version is None:
            errors.append(f"Dependency {name}@{spec}: no matching version")
            continue
        if f"{name}@{version}" in found or not _npm_platform_ok(versions[version]):
            continue
        dependency = _npm_release(name, versions[version])
        found[f"{name}@{version}"] = dependency
        queue += list((dependency.dependencies or {}).items())
    return found


def homebrew_dependencies(session, source, release, errors):
    """The releases (bottles) of the runtime dependencies of a formula, also the indirect ones"""
    found = {}
    queue = list(release.dependencies or [])
    seen = set()
    while queue:
        name = queue.pop(0)
        if name in seen:
            continue
        if len(seen) >= settings.RELEASE_MAX_DEPENDENCIES:
            errors.append(f"More than {settings.RELEASE_MAX_DEPENDENCIES} dependencies, the others are skipped")
            break
        seen.add(name)
        dependency = latest_homebrew_formula(session, source, name)
        found[f"{name}@{dependency.version}"] = dependency
        queue += dependency.dependencies or []
    return found


def dependency_releases(session, source, release, errors):
    """{name@version: Release} of the dependencies of the release, if the source allows them"""
    if not uses_dependencies(source) or not release.dependencies:
        return {}
    if source.kind == ReleaseSource.Kind.NPM_PACKAGE:
        return npm_dependencies(session, release, errors)
    if source.kind == ReleaseSource.Kind.HOMEBREW_FORMULA:
        return homebrew_dependencies(session, source, release, errors)
    return {}


# Sync


def collect_binaries(session, source, release, dependency=""):
    found = []
    errors = []
    # the binary pattern is for the package itself, the dependencies have their own paths
    pattern = "" if dependency else source.binary_pattern
    for asset in release.assets:
        try:
            with download(session, asset) as fileobj:
                for path, info in find_binaries(fileobj, asset.name, pattern):
                    found.append(FoundBinary(asset=asset.name, path=path, info=info, dependency=dependency))
        except (ReleaseError, requests.RequestException) as e:
            errors.append(str(e))
    return found, errors


def rule_identifiers(rule_type, info):
    """The (rule type, identifier) pairs for an executable, falling back to its hash when it is not signed"""
    if rule_type == RuleType.CDHASH and info.cdhashes:
        return [(RuleType.CDHASH, cdhash) for cdhash in info.cdhashes]
    value = {RuleType.SIGNINGID: info.signing_id, RuleType.TEAMID: info.team_id,
             RuleType.CERTIFICATE: info.cert_sha256}.get(rule_type)
    if value:
        return [(rule_type, value)]
    return [(RuleType.BINARY, info.sha256)]


def create_release_rules(source, release_version, binaries, enabled=None):
    """enabled: None = as the source approves new versions"""
    if enabled is None:
        enabled = source.auto_approve and not release_version.auto_enable_pending
    rules = []
    seen = set()
    for binary in binaries:
        for rule_type, identifier in rule_identifiers(source.rule_type, binary.info):
            if (rule_type, identifier) in seen:
                continue
            seen.add((rule_type, identifier))
            rule = Rule(
                rule_type=rule_type,
                identifier=identifier,
                policy=source.policy,
                custom_msg=source.custom_msg,
                custom_url=source.custom_url,
                cel_expr=source.cel_expr,
                description=f"{release_version} – {binary.asset}: {binary.path}"[:500],
                is_global=source.is_global,
                is_enabled=enabled,
                release_source=source,
                release_version=release_version,
                release_dependency=binary.dependency[:300],
            )
            try:
                rule.full_clean(exclude=["release_source", "release_version"])
            except ValidationError as e:
                raise ReleaseError(f"{binary.path}: {' '.join(e.messages)}")
            rule.save()
            rule.groups.set(source.groups.all())
            rule.tags.set(source.tags.all())
            rules.append(rule)
    return rules


def enable_due_releases(source=None, now=None):
    """Enable the rules of the releases whose auto approve delay has passed. Returns the enabled versions."""
    now = now or timezone.now()
    versions = ReleaseVersion.objects.select_related("source").filter(auto_enable_pending=True)
    if source is not None:
        versions = versions.filter(source=source)
    enabled = []
    for version in versions:
        if not version.source.auto_approve:
            # auto approve was switched off while the release was waiting, it waits for a manual approval now
            version.auto_enable_pending = False
            version.save(update_fields=["auto_enable_pending"])
        elif version.auto_enable_at <= now:
            with transaction.atomic():
                version.rules.update(is_enabled=True)
                version.auto_enable_pending = False
                version.save(update_fields=["auto_enable_pending"])
            logger.info("Release source %s: version %s enabled after the delay", version.source, version.version)
            enabled.append(version)
    return enabled


def _version_date(version):
    return version.published_at or version.created_at


def stale_versions(source, now=None, check_filters=False):
    """The versions to delete: of identifiers removed from the source, and outside the range to keep.

    check_filters: also the versions the version pattern no longer matches (the "clean up" of the console;
    the sync leaves them, the pattern only decides about new versions there).
    """
    identifiers = set(source.identifiers)
    pattern = None
    if check_filters and source.version_pattern and source.kind != ReleaseSource.Kind.URL:
        try:
            pattern = re.compile(source.version_pattern)
        except re.error:
            pattern = None
    cutoff = source.keep_cutoff(now)
    stale = []
    by_identifier = {}
    for version in source.versions.all():
        if version.identifier not in identifiers or (pattern and not pattern.search(version.version)):
            stale.append(version)
        else:
            by_identifier.setdefault(version.identifier, []).append(version)
    if source.keep_versions:
        for versions in by_identifier.values():
            # by publication: older versions added later must not push out the newest one
            versions.sort(key=_version_date, reverse=True)
            if cutoff is None:
                stale += versions[source.keep_versions:]
            else:
                stale += [version for version in versions[1:] if _version_date(version) < cutoff]
    return stale


def prune_old_versions(source):
    """Keep the versions of every identifier in the range to keep, drop the identifiers removed from the source"""
    stale = stale_versions(source)
    for version in stale:
        # the rules are deleted with their version, the next sync removes them from the Macs
        version.delete()
    return len(stale)


def cleanup_preview(source):
    """What the clean up of a package rule deletes: (versions, number of their rules, the dependency rules left over
    when the dependencies are no longer allowed)"""
    versions = sorted(stale_versions(source, check_filters=True), key=_version_date, reverse=True)
    rule_count = sum(Rule.objects.filter(release_version__in=chunk).count()
                     for chunk in chunked([version.pk for version in versions]))
    dependency_rules = Rule.objects.none()
    if not uses_dependencies(source):
        dependency_rules = source.rules.exclude(release_dependency="").exclude(
            release_version__in=[version.pk for version in versions][:1000])
    return versions, rule_count, dependency_rules


def cleanup_release_source(source):
    """Delete the versions and rules the package rule no longer covers. Returns (versions, rules) deleted."""
    versions, rule_count, dependency_rules = cleanup_preview(source)
    with transaction.atomic():
        dependency_count = dependency_rules.count()
        changed = set(dependency_rules.values_list("release_version", flat=True))
        dependency_rules.delete()
        for version in versions:
            version.delete()
        if not uses_dependencies(source):
            # allowed again later, the dependencies are looked up again
            for version in source.versions.filter(pk__in=list(changed)[:1000]):
                version.binary_count = version.rules.count()
                version.save(update_fields=["binary_count"])
            source.versions.update(dependencies=[], dependencies_resolved=False)
    return len(versions), rule_count + dependency_count


def _record_release(session, source, identifier, release):
    """Download a release and create its version and rules. Return (the new ReleaseVersion or None, errors)."""
    versions = source.versions.filter(identifier=identifier)
    if not release.assets:
        raise ReleaseError(f"No matching asset in release {release.version or '-'}")
    if release.version and versions.filter(version=release.version).exists():
        return None, []
    if release.version and source.version_pattern and not re.search(source.version_pattern, release.version):
        logger.info("Release source %s: %s version %s skipped by the version pattern",
                    source.name, identifier, release.version)
        return None, []
    binaries, errors = collect_binaries(session, source, release)
    included = []
    for label, dependency in dependency_releases(session, source, release, errors).items():
        found, dependency_errors = collect_binaries(session, source, dependency, dependency=label)
        binaries += found
        errors += [f"{label}: {error}" for error in dependency_errors]
        included.append(label)
    if not binaries and (errors or source.kind not in KINDS_OFTEN_WITHOUT_EXECUTABLES) and not included:
        raise ReleaseError("No Mach-O executable found. " + " ".join(errors))
    version = release.version or binaries[0].info.sha256[:12]
    if versions.filter(version=version).exists():
        return None, []
    notes = [f"{b.dependency + ' – ' if b.dependency else ''}{b.asset}: {b.path} {b.info.sha256}" for b in binaries]
    if included:
        notes.append(f"Dependencies: {', '.join(included)}")
    with transaction.atomic():
        release_version = ReleaseVersion.objects.create(
            source=source, identifier=identifier, version=version, published_at=release.published_at,
            binary_count=len({b.info.sha256 for b in binaries}),
            auto_enable_pending=source.auto_approve and source.auto_approve_delay_days > 0,
            notes="\n".join(notes + errors)[:10000] or NO_EXECUTABLES_NOTE,
            dependencies=included, dependencies_resolved=uses_dependencies(source),
        )
        create_release_rules(source, release_version, binaries)
    logger.info("Release source %s: %s version %s, %s binaries",
                source.name, identifier, version, release_version.binary_count)
    return release_version, errors


def uses_dependencies(source):
    return source.include_dependencies and source.kind in DEPENDENCY_KINDS


def _release_of_version(session, source, identifier, version, latest):
    """The release of a recorded version, for its dependencies: npm has every version; a formula only the current
    one, its dependencies stand for the older versions too"""
    if version.version == latest.version or source.kind != ReleaseSource.Kind.NPM_PACKAGE:
        return latest
    response = session.get(f"{_npm_url(identifier)}/{version.version}", timeout=settings.RELEASE_HTTP_TIMEOUT)
    response.raise_for_status()
    return _npm_release(identifier, response.json())


def _add_dependencies(session, source, identifier, latest, errors):
    """The dependencies of the versions recorded before the source allowed them (or before they were cleaned up)"""
    for version in source.versions.filter(identifier=identifier, dependencies_resolved=False).order_by(
            "-created_at")[:MAX_KEPT_VERSIONS_PER_SYNC]:
        release = _release_of_version(session, source, identifier, version, latest)
        warnings = []
        binaries, included = [], []
        for label, dependency in dependency_releases(session, source, release, warnings).items():
            found, dependency_errors = collect_binaries(session, source, dependency, dependency=label)
            binaries += found
            warnings += [f"{label}: {error}" for error in dependency_errors]
            included.append(label)
        own_rules = version.rules.filter(release_dependency="")
        with transaction.atomic():
            # the dependencies follow the approval of the version: enabled when its own rules are (or it had none)
            create_release_rules(source, version, binaries, enabled=(
                own_rules.filter(is_enabled=True).exists() if own_rules.exists() else None))
            version.dependencies = included
            version.dependencies_resolved = True
            version.binary_count += len({b.info.sha256 for b in binaries})
            notes = [f"{b.dependency} – {b.asset}: {b.path} {b.info.sha256}" for b in binaries]
            if included:
                notes.append(f"Dependencies: {', '.join(included)}")
            version.notes = "\n".join(filter(None, [version.notes, *notes, *warnings]))[:10000]
            version.save(update_fields=["dependencies", "dependencies_resolved", "binary_count", "notes"])
        errors += [f"{version.version}: {warning}" for warning in warnings]
        logger.info("Release source %s: %s version %s, %s dependencies added",
                    source.name, identifier, version.version, len(included))


def kept_releases(source, releases, now=None):
    """The releases inside the range the source keeps, newest first"""
    if source.version_pattern:
        releases = [release for release in releases if re.search(source.version_pattern, release.version)]
    cutoff = source.keep_cutoff(now)
    if cutoff is None:
        return releases[:source.keep_versions]
    return [release for release in releases if release.published_at and release.published_at >= cutoff]


def _sync_kept_versions(session, source, identifier, errors):
    """The older versions in the range to keep that have no rules yet"""
    limit = source.keep_versions if source.keep_unit == ReleaseSource.KeepUnit.VERSIONS else HISTORY_LIMIT
    releases = kept_releases(source, HISTORY[source.kind](session, source, identifier, limit))
    known = set(source.versions.filter(identifier=identifier).values_list("version", flat=True))
    new_versions = []
    for release in [release for release in releases if release.version not in known][:MAX_KEPT_VERSIONS_PER_SYNC]:
        try:
            release_version, warnings = _record_release(session, source, identifier, release)
        except ReleaseError as e:
            # an old release without a matching file: recorded with its reason, not tried again at every sync
            release_version = ReleaseVersion.objects.create(
                source=source, identifier=identifier, version=release.version, published_at=release.published_at,
                notes=str(e)[:10000])
            warnings = []
        errors += [f"{release.version}: {warning}" for warning in warnings]
        if release_version and release_version.binary_count:
            new_versions.append(release_version)
    return new_versions


def _sync_identifier(session, source, identifier):
    """Check one identifier of the source. Return (the new ReleaseVersions, the non-fatal errors)."""
    release = PROVIDERS[source.kind](session, source, identifier)
    release_version, errors = _record_release(session, source, identifier, release)
    new_versions = [release_version] if release_version else []
    if source.approve_kept_versions and source.keep_versions and source.kind in HISTORY:
        try:
            new_versions += _sync_kept_versions(session, source, identifier, errors)
        except (ReleaseError, requests.RequestException, KeyError, ValueError) as e:
            errors.append(f"Older versions: {e}")
    if uses_dependencies(source):
        try:
            _add_dependencies(session, source, identifier, release, errors)
        except (ReleaseError, requests.RequestException, KeyError, ValueError) as e:
            errors.append(f"Dependencies: {e}")
    return new_versions, errors


def sync_release_source(source, session=None):
    """Check every identifier of the source for a new release. Return the new ReleaseVersions.

    A failing identifier does not stop the others. ReleaseError is raised at the end if one failed,
    the new versions of the other identifiers are kept and in its new_versions.
    """
    session = session or http_session()
    several = source.has_several_identifiers
    new_versions = []
    errors = []
    failed = []
    for identifier in source.identifiers:
        prefix = f"{identifier}: " if several else ""
        try:
            found, warnings = _sync_identifier(session, source, identifier)
        except (ReleaseError, requests.RequestException, KeyError, ValueError) as e:
            logger.warning("Release source %s: %s%s", source.name, prefix, e)
            failed.append(f"{prefix}{e}")
            continue
        errors += [f"{prefix}{warning}" for warning in warnings]
        new_versions += found
    prune_old_versions(source)
    worked_before = not source.last_error
    source.last_checked_at = timezone.now()
    source.last_error = "\n".join(failed + errors)[:5000]
    source.save(update_fields=["last_checked_at", "last_error"])
    if worked_before and source.last_error:
        # once, when it starts failing: not at every hourly check
        notifications.source_failed(source)
    # a release published before the delay is enabled right away
    enable_due_releases(source)
    for release_version in new_versions:
        release_version.refresh_from_db()
    notifications.versions_found(source, new_versions)
    if failed:
        raise ReleaseError("\n".join(failed), new_versions)
    return new_versions
