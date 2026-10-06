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

from . import macho, notifications
from .models import ReleaseSource, ReleaseVersion, Rule, RuleType

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


@dataclass
class FoundBinary:
    asset: str
    path: str
    info: macho.MachOInfo


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
    headers = {"Accept": "application/vnd.github+json"}
    if settings.GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {settings.GITHUB_TOKEN}"
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
    return Release(version=version, assets=assets)


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


def latest_npm_package(session, source, identifier):
    # scoped packages: the registry wants @scope%2Fname
    response = session.get(f"https://registry.npmjs.org/{identifier.replace('/', '%2F')}/latest",
                           timeout=settings.RELEASE_HTTP_TIMEOUT)
    response.raise_for_status()
    data = response.json()
    dist = data["dist"]
    sha512 = ""
    integrity = dist.get("integrity") or ""
    if integrity.startswith("sha512-"):
        sha512 = base64.b64decode(integrity.removeprefix("sha512-")).hex()
    name = identifier.rsplit("/", 1)[-1]
    return Release(version=data["version"], assets=[Asset(name=f"{name}-{data['version']}.tgz", url=dist["tarball"],
                                                          sha512=sha512)])


VSCODE_GALLERY_URL = "https://marketplace.visualstudio.com/_apis/public/gallery/extensionquery"
# IncludeVersions | IncludeFiles | IncludeVersionProperties | IncludeAssetUri
# | IncludeLatestPrereleaseAndStableVersionOnly: the full version list can be many megabytes
VSCODE_QUERY_FLAGS = 0x1 | 0x2 | 0x10 | 0x80 | 0x10000
VSCODE_MAC_PLATFORMS = ("darwin-arm64", "darwin-x64", "universal")


def latest_vscode_extension(session, source, identifier):
    response = session.post(
        VSCODE_GALLERY_URL,
        json={"filters": [{"criteria": [{"filterType": 7, "value": identifier}], "pageSize": 1}],
              "flags": VSCODE_QUERY_FLAGS},
        headers={"Accept": "application/json;api-version=7.2-preview.1"},
        timeout=settings.RELEASE_HTTP_TIMEOUT,
    )
    response.raise_for_status()
    extensions = [e for result in response.json().get("results", []) for e in result.get("extensions", [])]
    if not extensions:
        raise ReleaseError(f"Extension {identifier} not found")
    regex = _asset_regex(source)
    candidates = []
    for version in extensions[0].get("versions", []):
        properties = {p["key"]: p["value"] for p in version.get("properties", [])}
        if properties.get("Microsoft.VisualStudio.Code.PreRelease") == "true" and not source.include_prereleases:
            continue
        platform = version.get("targetPlatform") or "universal"
        if platform in VSCODE_MAC_PLATFORMS:
            candidates.append((version, platform))
    if not candidates:
        raise ReleaseError(f"No macOS version of {identifier}")
    # the versions come newest first, one entry per target platform
    latest = candidates[0][0]["version"]
    assets = []
    published_at = None
    for version, platform in candidates:
        if version["version"] != latest or (regex and not regex.search(platform)):
            continue
        url = next((f["source"] for f in version.get("files", [])
                    if f["assetType"] == "Microsoft.VisualStudio.Services.VSIXPackage"), None)
        if url:
            assets.append(Asset(name=f"{identifier}-{latest}@{platform}.vsix", url=url))
            published_at = max(filter(None, [published_at, parse_datetime(version.get("lastUpdated") or "")]),
                               default=None)
    return Release(version=latest, assets=assets, published_at=published_at)


def latest_jetbrains_plugin(session, source, identifier):
    base = "https://plugins.jetbrains.com/api/plugins"
    plugin_id = identifier
    if not identifier.isdigit():
        response = session.get(f"{base}/intellij/{identifier}", timeout=settings.RELEASE_HTTP_TIMEOUT)
        response.raise_for_status()
        plugin_id = response.json()["id"]
    params = {"size": 20}
    if not source.include_prereleases:
        params["channel"] = ""
    response = session.get(f"{base}/{plugin_id}/updates?{urlencode(params)}", timeout=settings.RELEASE_HTTP_TIMEOUT)
    response.raise_for_status()
    regex = _asset_regex(source)
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
        return Release(version=update["version"], assets=[asset], published_at=published_at)
    raise ReleaseError(f"No compatible update of {identifier}")


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


# Sync


def collect_binaries(session, source, release):
    found = []
    errors = []
    for asset in release.assets:
        try:
            with download(session, asset) as fileobj:
                for path, info in find_binaries(fileobj, asset.name, source.binary_pattern):
                    found.append(FoundBinary(asset=asset.name, path=path, info=info))
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


def create_release_rules(source, release_version, binaries):
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
                is_enabled=source.auto_approve and not release_version.auto_enable_pending,
                release_source=source,
                release_version=release_version,
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


def prune_old_versions(source):
    """Keep the last keep_versions versions of every identifier, drop the identifiers removed from the source"""
    versions = source.versions.order_by("-created_at")
    removed = list(versions.exclude(identifier__in=source.identifiers))
    old_versions = []
    if source.keep_versions:
        for identifier in source.identifiers:
            old_versions += versions.filter(identifier=identifier)[source.keep_versions:]
    for version in removed + old_versions:
        # the rules are deleted with their version, the next sync removes them from the Macs
        version.delete()
    return len(removed) + len(old_versions)


def _sync_identifier(session, source, identifier):
    """Check one identifier of the source. Return (the new ReleaseVersion or None, the non-fatal errors)."""
    versions = source.versions.filter(identifier=identifier)
    release = PROVIDERS[source.kind](session, source, identifier)
    if not release.assets:
        raise ReleaseError(f"No matching asset in release {release.version or '-'}")
    if release.version and versions.filter(version=release.version).exists():
        return None, []
    if release.version and source.version_pattern and not re.search(source.version_pattern, release.version):
        logger.info("Release source %s: %s version %s skipped by the version pattern",
                    source.name, identifier, release.version)
        return None, []
    binaries, errors = collect_binaries(session, source, release)
    if not binaries and (errors or source.kind not in KINDS_OFTEN_WITHOUT_EXECUTABLES):
        raise ReleaseError("No Mach-O executable found. " + " ".join(errors))
    version = release.version or binaries[0].info.sha256[:12]
    if versions.filter(version=version).exists():
        return None, []
    with transaction.atomic():
        release_version = ReleaseVersion.objects.create(
            source=source, identifier=identifier, version=version, published_at=release.published_at,
            binary_count=len({b.info.sha256 for b in binaries}),
            auto_enable_pending=source.auto_approve and source.auto_approve_delay_days > 0,
            notes="\n".join([f"{b.asset}: {b.path} {b.info.sha256}" for b in binaries] + errors)[:10000]
                  or NO_EXECUTABLES_NOTE,
        )
        create_release_rules(source, release_version, binaries)
    logger.info("Release source %s: %s version %s, %s binaries",
                source.name, identifier, version, release_version.binary_count)
    return release_version, errors


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
            release_version, warnings = _sync_identifier(session, source, identifier)
        except (ReleaseError, requests.RequestException, KeyError, ValueError) as e:
            logger.warning("Release source %s: %s%s", source.name, prefix, e)
            failed.append(f"{prefix}{e}")
            continue
        errors += [f"{prefix}{warning}" for warning in warnings]
        if release_version:
            new_versions.append(release_version)
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
