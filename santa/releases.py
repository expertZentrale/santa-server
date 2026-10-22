"""Allowlist the binaries of new releases of unsigned tools (colima, …).

Every release asset is downloaded, the Mach-O executables inside are hashed, and one BINARY rule
is created per hash. Santa then allows exactly these files, and nothing else built by someone else.
"""
import base64
import copy
import fnmatch
import hashlib
import json
import logging
import lzma
import re
import tarfile
import tempfile
import threading
import weakref
import zipfile
import zlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from urllib.parse import parse_qs, urljoin, urlsplit

import requests
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.utils.http import urlencode
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

from . import archives, bottles, macho, notifications, semver
from .models import (
    DEPENDENCY_KINDS,
    HISTORY_KINDS,
    MACOS_RELEASES,
    ReleaseSource,
    ReleaseVersion,
    Rule,
    RuleType,
    pattern_lines,
)
from .progress import Cancelled, Progress
from .services import chunked

logger = logging.getLogger(__name__)

SKIPPED_ASSET_SUFFIXES = (".sha256", ".sha256sum", ".sha512", ".md5", ".txt", ".sig", ".asc", ".pem",
                          ".sbom", ".json", ".spdx", ".intoto.jsonl", ".bundle", ".deb", ".rpm", ".apk", ".exe",
                          ".msi")
# read inside an archive, package or disk image: the .pkg in a .dmg, …
NESTED_SUFFIXES = (".pkg", ".dmg", ".zip", ".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz", ".tar.xz", ".txz")
# anonymous token accepted by ghcr.io for the Homebrew bottles
HOMEBREW_BOTTLE_TOKEN = "QQ=="
MAX_ARCHIVE_MEMBERS = 20000


class ReleaseError(Exception):
    def __init__(self, message, new_versions=()):
        super().__init__(message)
        # the versions the other identifiers of the source found before one failed
        self.new_versions = list(new_versions)


class RetryLater(ReleaseError):
    """A release that failed for now (the network, a rate limit, a full disk): nothing is recorded, the next check
    tries it again"""


class Errors(list):
    """The error lines of a release; retry: one of them is temporary, the release is tried again at the next check"""
    retry = False


def is_transient(error):
    """A failure that can be gone at the next check: a connection, a timeout, a rate limit, a server error, the disk.
    Not a missing file (404) or a refused request."""
    if isinstance(error, requests.HTTPError) and error.response is not None:
        return error.response.status_code in (408, 429) or error.response.status_code >= 500
    return isinstance(error, (requests.RequestException, OSError))


@dataclass
class Asset:
    name: str
    url: str
    sha256: str = ""
    headers: dict = None
    # npm only publishes a SHA-512
    sha512: str = ""
    # what the asset pattern is matched against: the name (GitHub), the bottle tags the bottle serves, the URL
    # (cask), the target platform (VS Code), the compatible products (JetBrains). Empty: the name.
    match: tuple = ()
    # a Homebrew bottle: its executables are changed as Homebrew installs them on the Macs of the tag
    bottle_tag: str = ""
    # only over https, also its redirects (a download list)
    https_only: bool = False


@dataclass
class Release:
    version: str
    assets: list
    published_at: object = None
    # what the release needs: {name: version range} (npm); [name, …] (Homebrew formula, the current one) or
    # [(name, version), …] (the versions the bottles were built with; a name can come in several versions);
    # None: not known
    dependencies: object = None
    # noted with the version (a Homebrew tap formula that signs its program again when it installs it)
    warnings: tuple = ()


@dataclass
class FoundBinary:
    asset: str
    path: str
    info: macho.MachOInfo
    # name@version of the dependency it came from, empty for the package itself
    dependency: str = ""
    asset_match: tuple = ()


# Downloads and archives


# the longest wait a rate limit (Retry-After) gets inside a check
MAX_RETRY_AFTER = 60


class _Retry(Retry):
    """A rate limit (429, 503) names how long to wait: waited for, but at most MAX_RETRY_AFTER seconds (it can name an
    hour; the next check comes sooner)"""

    def get_retry_after(self, response):
        seconds = super().get_retry_after(response)
        return None if seconds is None else min(seconds, MAX_RETRY_AFTER)


# {session: {kind: {key: value}}}: what one check (a session each) looked up already, gone with its session
_RUN_CACHES = weakref.WeakKeyDictionary()
_RUN_CACHES_LOCK = threading.Lock()


def run_cache(session, kind):
    with _RUN_CACHES_LOCK:
        return _RUN_CACHES.setdefault(session, {}).setdefault(kind, {})


def http_session():
    session = requests.Session()
    session.headers["User-Agent"] = "santa-server"
    # a dropped connection or a busy registry (ghcr.io under many requests) is asked again, a moment later
    retry = _Retry(total=5, backoff_factor=0.5, status_forcelist=(429, 500, 502, 503, 504),
                   allowed_methods=frozenset({"GET", "HEAD", "POST"}), respect_retry_after_header=True)
    # one connection per download of the pool (DOWNLOAD_WORKERS) and host
    adapter = HTTPAdapter(max_retries=retry, pool_maxsize=DOWNLOAD_WORKERS * 2)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


MAX_REDIRECTS = 10


def https_get(session, url, **kwargs):
    """A GET that stays on https: the redirects are followed here, each one checked, not by requests (it would follow
    one to http)"""
    for _hop in range(MAX_REDIRECTS + 1):
        if urlsplit(url).scheme != "https":
            raise ReleaseError(f"{url}: only https URLs")
        response = session.get(url, allow_redirects=False, **kwargs)
        if not getattr(response, "is_redirect", False):
            return response
        url = urljoin(url, response.headers["location"])
        response.close()
    raise ReleaseError(f"{url}: more than {MAX_REDIRECTS} redirects")


def download(session, asset):
    """Download the asset to a temporary file, check its SHA-256 if known"""
    tmp = tempfile.TemporaryFile()
    digest = hashlib.sha256()
    digest512 = hashlib.sha512()
    size = 0
    get = partial(https_get, session) if asset.https_only else session.get
    with get(asset.url, headers=asset.headers or {}, stream=True, timeout=settings.RELEASE_HTTP_TIMEOUT) as response:
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

    def __init__(self, name, skipped=None):
        self.name = name
        self.remaining = settings.RELEASE_MAX_UNPACKED_BYTES
        # the executables left out for their size: named in the errors, else the release looks empty
        self.skipped = skipped if skipped is not None else []

    def consume(self, size):
        self.remaining -= size
        if self.remaining < 0:
            raise ReleaseError(f"{self.name}: unpacks to more than {settings.RELEASE_MAX_UNPACKED_BYTES} bytes")


def _spool(member_fileobj, budget, start=b""):
    """The member in a temporary file, counted against the budget; start: its first bytes, read before"""
    spooled = tempfile.SpooledTemporaryFile(max_size=16 * 1024 * 1024)
    try:
        budget.consume(len(start))
        spooled.write(start)
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
    """The binary pattern (globs, one per line) against a path, or its file name"""
    globs = pattern_lines(pattern)
    name = path.rsplit("/", 1)[-1]
    return not globs or any(fnmatch.fnmatch(path, glob) or fnmatch.fnmatch(name, glob) for glob in globs)


def binary_path_matches(path, pattern):
    """The binary pattern against a path found by find_binaries: inside a nested archive ("x.dmg/x.pkg/bin/tool")
    the pattern was matched against the path inside it"""
    if _matches(path, pattern):
        return True
    parts = path.split("/")
    return any(_matches("/".join(parts[index + 1:]), pattern)
               for index, part in enumerate(parts[:-1]) if part.lower().endswith(NESTED_SUFFIXES))


def _inspect_candidate(fileobj, path, pattern, bottle_tag=""):
    if not _matches(path, pattern):
        return None
    warning = ""
    if bottle_tag:
        try:
            bottles.relocate(fileobj, bottle_tag)
        except bottles.Unpredictable as e:
            # unchanged: the hash of the bottle
            warning = (f"{path}: Homebrew changes it on install in a way the server can't reproduce ({e}), "
                       "its rule has the hash of the bottle: allow the blocked event instead")
    try:
        info = macho.inspect(fileobj)
    except macho.NotMachO:
        return None
    # the dylibs and bundles are never executed on their own, Santa does not check them
    if not info.is_executable:
        return None
    info.warning = warning
    return info


def _is_nested(path, size):
    return path.lower().endswith(NESTED_SUFFIXES) and size <= settings.RELEASE_MAX_FILE_BYTES


def find_binaries(fileobj, name, pattern="", bottle_tag="", skipped=None):
    """Yield (path, MachOInfo) for the Mach-O executables in a raw binary, a zip or tar archive, a flat package
    (.pkg) or a disk image (.dmg). An archive inside one (the .pkg in a .dmg) is read too, one level deep.
    bottle_tag: a Homebrew bottle, its executables as Homebrew installs them (santa/bottles.py).
    skipped: a list that gets the executables bigger than RELEASE_MAX_FILE_BYTES, not checked."""
    try:
        yield from _find_binaries(fileobj, name, pattern, _UnpackBudget(name, skipped), nested=False,
                                  bottle_tag=bottle_tag)
    except archives.ArchiveError as e:
        raise ReleaseError(f"{name}: {e}") from e


def _find_binaries(fileobj, name, pattern, budget, nested, bottle_tag=""):
    fileobj.seek(0)
    header = fileobj.read(8)
    fileobj.seek(0)
    if macho.is_macho_header(header):
        info = _inspect_candidate(fileobj, name, pattern)
        if info:
            yield name, info
        return

    # a bottle is installed as it is: an archive in it is data (the test files of go), never executed
    descend = not nested and not bottle_tag

    def wanted(path, size, executable):
        # packages and disk images keep the x bit: only executables are worth unpacking there
        return ((descend and _is_nested(path, size))
                or (executable is not False and _worth_unpacking(path, size, pattern)))

    def wanted_in_image(path, size, executable):
        # a disk image only unpacks what is wanted: a too big executable is named from the listing (its x bit),
        # its first bytes can't be read without unpacking it
        if wanted(path, size, executable):
            return True
        if executable and size > settings.RELEASE_MAX_FILE_BYTES and _matches(path, pattern):
            budget.skipped.append(_too_big(path, size))
        return False

    if archives.is_pkg(header):
        members = archives.pkg_members(fileobj, budget.consume, MAX_ARCHIVE_MEMBERS)
    elif archives.is_dmg(fileobj):
        members = archives.dmg_members(fileobj, wanted_in_image, budget.consume, MAX_ARCHIVE_MEMBERS,
                                       settings.RELEASE_UNPACK_TIMEOUT)
    elif zipfile.is_zipfile(fileobj):
        members = _zip_members(fileobj)
    else:
        try:
            members = _tar_members(tarfile.open(fileobj=fileobj, mode="r:*"))
        except tarfile.TarError:
            if nested:
                return
            raise ReleaseError(f"{name}: not a Mach-O binary, zip or tar archive, package or disk image") from None
    for path, size, executable, open_member in members:
        if not wanted(path, size, executable):
            if size > settings.RELEASE_MAX_FILE_BYTES and executable is not False and _matches(path, pattern):
                with open_member() as member_fileobj:
                    if macho.may_be_executable(member_fileobj.read(16)):
                        budget.skipped.append(_too_big(path, size))
            continue
        with open_member() as member_fileobj:
            archive = descend and _is_nested(path, size)
            # only executables are unpacked: the libraries, headers and data of a package (the gigabytes of a
            # compiler) neither use up the budget nor the time
            start = member_fileobj.read(16)
            if not archive and not macho.may_be_executable(start):
                continue
            spooled_member = _spool(member_fileobj, budget, start)
        with spooled_member as spooled:
            if archive:
                for inner_path, info in _find_binaries(spooled, path, pattern, budget, nested=True):
                    yield f"{path}/{inner_path}", info
                continue
            info = _inspect_candidate(spooled, path, pattern, bottle_tag)
            if info:
                yield path, info


def _too_big(path, size):
    return f"{path}: {size} bytes, bigger than RELEASE_MAX_FILE_BYTES ({settings.RELEASE_MAX_FILE_BYTES}), not checked"


def _zip_members(fileobj):
    fileobj.seek(0)
    with zipfile.ZipFile(fileobj) as archive:
        for member in archive.infolist()[:MAX_ARCHIVE_MEMBERS]:
            if not member.is_dir():
                yield member.filename, member.file_size, None, lambda member=member: archive.open(member)


def _tar_members(archive):
    with archive:
        for index, member in enumerate(archive):
            if index >= MAX_ARCHIVE_MEMBERS:
                break
            if member.isfile():
                member_fileobj = archive.extractfile(member)
                if member_fileobj is not None:
                    yield member.name, member.size, None, lambda member_fileobj=member_fileobj: member_fileobj


# Release providers


def compile_patterns(text, label="pattern"):
    """The regexes of a pattern field, one per line; None when it is empty (everything matches)"""
    lines = pattern_lines(text)
    try:
        return [re.compile(line) for line in lines] or None
    except re.error as e:
        raise ReleaseError(f"Invalid {label}: {e}")


def _asset_regex(source):
    return compile_patterns(source.asset_pattern, "asset pattern")


def version_matches(source, version):
    """The version pattern: one of its regexes matches (an empty pattern takes every version)"""
    try:
        patterns = compile_patterns(source.version_pattern)
    except ReleaseError:
        return True
    return patterns is None or not version or any(pattern.search(version) for pattern in patterns)


# the kinds that use the asset pattern (npm and URL have one file per release)
ASSET_PATTERN_KINDS = (ReleaseSource.Kind.GITHUB_RELEASE, ReleaseSource.Kind.HOMEBREW_FORMULA,
                       ReleaseSource.Kind.HOMEBREW_CASK, ReleaseSource.Kind.VSCODE_EXTENSION,
                       ReleaseSource.Kind.JETBRAINS_PLUGIN)


def asset_matches(patterns, values):
    """The asset pattern takes an asset when one of its regexes matches one of its values (Asset.match)"""
    return patterns is None or any(pattern.search(value) for pattern in patterns for value in values)


# the macOS releases of the bottle tags, newest first
MACOS_NAMES = tuple(name for name, _label in MACOS_RELEASES)


def bottle_coverage(tags):
    """{bottle tag: the tags of the Macs it serves}. Like Homebrew: a Mac gets the bottle of the newest macOS that
    is not newer than its own, of the same architecture; "all" serves every Mac. A formula built only for
    arm64_big_sur (portable-ruby) is installed on arm64_sequoia too."""
    coverage = {tag: [tag] for tag in tags}
    for prefix in ("arm64_", ""):
        current = None
        for release in reversed(MACOS_NAMES):
            tag = prefix + release
            if tag in coverage:
                current = tag
            elif current:
                coverage[current].append(tag)
    if "all" in coverage:
        coverage["all"] += [prefix + release for prefix in ("arm64_", "") for release in MACOS_NAMES
                            if prefix + release not in coverage]
    return coverage


def bottle_served(source, served):
    """The tags of the Macs a bottle is taken for. served: its coverage, its own tag first (Asset.match).
    Without "nearest bottle" only its own tag counts; the minimum macOS leaves out the older Macs."""
    tags = list(served) if source.macos_nearest else list(served[:1])
    if source.macos_minimum in MACOS_NAMES:
        newer = MACOS_NAMES[:MACOS_NAMES.index(source.macos_minimum) + 1]
        wanted = {prefix + name for prefix in ("arm64_", "") for name in newer}
        tags = [tag for tag in tags if tag == "all" or tag in wanted]
    return tags


def bottle_taken(source, patterns, served):
    tags = bottle_served(source, served)
    return bool(tags) and asset_matches(patterns, tags)


def _taken_bottles(source, bottles):
    """The tags of the bottles a source takes, of {tag: …}, with the macOS versions each one serves"""
    patterns = _asset_regex(source)
    coverage = bottle_coverage(bottles)
    return {tag: coverage[tag] for tag in bottles if bottle_taken(source, patterns, coverage[tag])}


def _bottle_assets(source, name, version, bottles):
    """The assets of the bottles a source takes. bottles: {tag: (url, sha256, headers)}"""
    taken = _taken_bottles(source, bottles)
    return [Asset(name=f"{name}-{version}.{tag}.bottle.tar.gz", url=url, sha256=sha256, headers=headers,
                  match=tuple(taken[tag]), bottle_tag=tag)
            for tag, (url, sha256, headers) in sorted(bottles.items()) if tag in taken]


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


def _published(value):
    """A publication date of a catalog, always aware: the older Homebrew bottles only give the day ("2021-04-06"),
    which compared with the cutoff of the range to keep failed"""
    try:
        published = parse_datetime(value or "")
    except ValueError:
        return None
    if published and timezone.is_naive(published):
        published = timezone.make_aware(published, UTC)
    return published


def _github_release(source, data):
    regex = _asset_regex(source)
    assets = []
    for asset in data.get("assets", []):
        name = asset["name"]
        if name.lower().endswith(SKIPPED_ASSET_SUFFIXES):
            continue
        if not asset_matches(regex, (name,)):
            continue
        digest = asset.get("digest") or ""
        assets.append(Asset(name=name, url=asset["browser_download_url"],
                            sha256=digest.removeprefix("sha256:") if digest.startswith("sha256:") else "",
                            match=(name,)))
    published_at = _published(data.get("published_at"))
    return Release(version=data["tag_name"], assets=assets, published_at=published_at)


# the console offers to remove such a package from the package rule (views_sources.removable_packages)
LINUX_ONLY_ERROR = ("Only for Linux, there is no bottle for macOS: nothing for Santa to allow. Remove it from the "
                    "package rule.")


def homebrew_linux_only(data):
    """A formula only for Linux (acl, afuse): it says so, or it only has Linux bottles. Nothing for Santa."""
    files = ((data.get("bottle") or {}).get("stable") or {}).get("files") or {}
    return ("linux" in [requirement.get("name") for requirement in data.get("requirements") or []]
            or (bool(files) and all("linux" in tag for tag in files)))


def latest_homebrew_formula(session, source, identifier):
    if is_tap_identifier(identifier):
        release = latest_homebrew_tap(session, source, identifier, cask=False)
        if semver.is_prerelease(release.version) and not source.include_prereleases:
            # the newest release that isn't one, from the history of the tap (like "latest" of GitHub)
            stable = homebrew_tap_releases(session, source, identifier, 1) if settings.GITHUB_TOKEN else []
            if not stable:
                raise ReleaseError(f"{identifier}: the current version {release.version} is a prerelease; "
                                   "allow prereleases, or wait for a release (older releases need GITHUB_TOKEN)")
            return stable[0]
        return release
    response = session.get(f"https://formulae.brew.sh/api/formula/{identifier}.json",
                           timeout=settings.RELEASE_HTTP_TIMEOUT)
    response.raise_for_status()
    data = response.json()
    if homebrew_linux_only(data):
        raise ReleaseError(LINUX_ONLY_ERROR)
    version = data["versions"]["stable"]
    if data.get("revision"):
        version = f"{version}_{data['revision']}"
    headers = {"Authorization": f"Bearer {HOMEBREW_BOTTLE_TOKEN}"}
    bottles = {tag: (bottle["url"], bottle["sha256"], headers)
               for tag, bottle in data.get("bottle", {}).get("stable", {}).get("files", {}).items()
               if not tag.startswith(("x86_64_linux", "arm64_linux"))}
    return Release(version=version, assets=_bottle_assets(source, identifier, version, bottles),
                   dependencies=list(data.get("dependencies") or []))


def _ghcr_repository(name):
    # the image name of a formula in the Homebrew registry: python@3.12 -> python/3.12, gtk+3 -> gtkx3
    return name.replace("@", "/").replace("+", "x")


GHCR_TAGS_PAGE = 1000
GHCR_MAX_TAG_PAGES = 20


def _ghcr_base(name):
    return f"https://ghcr.io/v2/homebrew/core/{_ghcr_repository(name)}"


def _ghcr_tags(session, name):
    """The tags of a formula in the Homebrew registry, newest first. The registry pages them (100 by default, node has
    more): asked for a large page, and the next ones it links."""
    url, params, tags = f"{_ghcr_base(name)}/tags/list", {"n": GHCR_TAGS_PAGE}, []
    for _page in range(GHCR_MAX_TAG_PAGES):
        response = session.get(url, params=params, timeout=settings.RELEASE_HTTP_TIMEOUT,
                               headers={"Authorization": f"Bearer {HOMEBREW_BOTTLE_TOKEN}"})
        response.raise_for_status()
        tags += response.json().get("tags") or []
        last = parse_qs(urlsplit((response.links or {}).get("next", {}).get("url", "")).query).get("last")
        if not last:
            break
        params = {"n": GHCR_TAGS_PAGE, "last": last[0]}
    return list(reversed(tags))


def _ghcr_index(session, name, tag):
    # a tag never changes; the same dependency is looked up for many versions in one sync
    cache = run_cache(session, "ghcr-index")
    if (name, tag) not in cache:
        response = session.get(f"{_ghcr_base(name)}/manifests/{tag}", timeout=settings.RELEASE_HTTP_TIMEOUT,
                               headers={"Authorization": f"Bearer {HOMEBREW_BOTTLE_TOKEN}",
                                        "Accept": "application/vnd.oci.image.index.v1+json"})
        response.raise_for_status()
        cache[(name, tag)] = response.json()
    return cache[(name, tag)]


def _ghcr_release(source, name, tag, index):
    """The Release of a version's index in the Homebrew registry: its macOS bottles, the date, and the versions of the
    runtime dependencies the bottles were built with (their "tab"; all of them, also the indirect ones).

    A bottle "all" (scripts, the same for every system) is built on Linux: its tab names the Linux dependencies, of
    which only the direct ones are taken; the indirect ones come from their own macOS bottles.

    The dependencies are the ones of the bottles the source takes: the bottles of a version are not always built
    with the same dependency versions (a rebuild for one macOS), and a Mac gets the bottle of its macOS. The pairs
    (name, version) keep each version a taken bottle needs."""
    headers = {"Authorization": f"Bearer {HOMEBREW_BOTTLE_TOKEN}"}
    annotations = index.get("annotations") or {}
    version = annotations.get("org.opencontainers.image.version") or tag
    digests, tabs = {}, {}
    for manifest in index.get("manifests") or []:
        bottle = manifest.get("annotations") or {}
        # "0.9.1.arm64_sequoia.1": version, bottle tag, rebuild
        bottle_tag = bottle.get("org.opencontainers.image.ref.name", "").removeprefix(f"{version}.").split(".")[0]
        if (manifest.get("platform") or {}).get("os") != "darwin" and bottle_tag != "all":
            continue
        if digest := bottle.get("sh.brew.bottle.digest"):
            digests[bottle_tag] = digest
        try:
            tab = json.loads(bottle.get("sh.brew.tab") or "{}")
        except ValueError:
            tab = {}
        runtime = tab.get("runtime_dependencies") if isinstance(tab, dict) else None
        # an empty list is a bottle without dependencies, not one that doesn't say
        if isinstance(runtime, list):
            tabs[bottle_tag] = [(dependency["full_name"], str(dependency["pkg_version"])) for dependency in runtime
                                if isinstance(dependency, dict) and dependency.get("full_name")
                                and dependency.get("pkg_version")
                                and (bottle_tag != "all" or dependency.get("declared_directly"))]
    bottles = {bottle_tag: (f"{_ghcr_base(name)}/blobs/sha256:{digest}", digest, headers)
               for bottle_tag, digest in digests.items()}
    # none taken (a dependency without a bottle for these macOS versions): what any of them needs
    taken = _taken_bottles(source, bottles) or tabs
    known = [tabs[bottle_tag] for bottle_tag in sorted(taken) if bottle_tag in tabs]
    return Release(version=version, assets=_bottle_assets(source, name, version, bottles),
                   published_at=_published(annotations.get("org.opencontainers.image.created")),
                   dependencies=list(dict.fromkeys(pair for pairs in known for pair in pairs)) if known else None)


def homebrew_formula_releases(session, source, identifier, limit, known=None):
    """The bottles of the older versions of a formula, newest first, from the Homebrew registry (ghcr.io).

    formulae.brew.sh only knows the current version. Every tag of the registry is a version (a rebuild of the same
    bottle gets "-1", "-2"…); its index lists the bottles per macOS with their SHA-256 and the publication date.
    known: {version: published_at} recorded already: their index is not asked (a request each), they come without
    assets, with the date recorded.
    """
    if is_tap_identifier(identifier):
        return homebrew_tap_releases(session, source, identifier, limit, known)
    cutoff = source.keep_cutoff()
    releases = {}
    # a looked-up version older than the range ends the search
    for tag in _ghcr_tags(session, identifier):
        if len(releases) >= limit:
            break
        # a rebuild of a version: "-1"; a version can end like that itself (ca-certificates 2026-09-25)
        version = tag if not known or tag in known else re.sub(r"-\d+$", "", tag)
        if known and version in known:
            published_at = known[version]
            if cutoff and published_at and published_at < cutoff:
                break
            releases.setdefault(version, Release(version=version, assets=[], published_at=published_at))
            continue
        release = _ghcr_release(source, identifier, tag, _ghcr_index(session, identifier, tag))
        if cutoff and release.published_at and release.published_at < cutoff:
            break
        # an older build of a version already found is skipped
        releases.setdefault(release.version, release)
    return list(releases.values())


def _tap_release_at(session, source, name, version, before):
    """A version of a tap formula, or the newest one of the day of `before`, from the history of its file"""
    if not settings.GITHUB_TOKEN:
        # only the current version (homebrew_tap_releases): the dependencies of an older version take it, as before
        current = latest_homebrew_tap(session, source, name, cask=False)
        if version and current.version != version:
            raise ReleaseError(f"{name}: version {version} is not the current one, the older versions of a tap "
                               "need GITHUB_TOKEN (they come from the history of the tap on GitHub)")
        return current
    if not version and not before:
        return latest_homebrew_tap(session, source, name, cask=False)
    # the version a dependency had then can be older than the range to keep
    releases = homebrew_tap_releases(session, source, name, HISTORY_LIMIT, in_range=False)
    end_of_day = before and datetime.combine(before.astimezone(UTC).date(), datetime.max.time(), tzinfo=UTC)
    for release in releases:
        if version and release.version != version:
            continue
        if not end_of_day or (release.published_at and release.published_at <= end_of_day):
            return release
    raise ReleaseError(f"{name}: no version {version or f'up to {before:%Y-%m-%d}'} in the history of the tap")


def homebrew_formula_at(session, source, name, version=None, before=None):
    """A formula from the Homebrew registry, as it was on the day of `before` (the dependency of an older version):
    the given version or else the newest one published by then. A version is rebuilt now and then (a new tag "-1",
    other files, often for other macOS versions): its build of that day, the one the Macs got then."""
    if is_tap_identifier(name):
        return _tap_release_at(session, source, name, version, before)
    # the same dependencies (python, openssl…) come up for many versions in one sync
    cache = run_cache(session, "ghcr-tags")
    if name not in cache:
        cache[name] = _ghcr_tags(session, name)
    tags = cache[name]
    if version:
        tags = [tag for tag in tags if tag == version or re.fullmatch(rf"{re.escape(version)}-\d+", tag)]
    # the whole day: the registry gives the older bottles only a day, and a release of the same day counts
    end_of_day = before and datetime.combine(before.astimezone(UTC).date(), datetime.max.time(), tzinfo=UTC)
    release = None
    for tag in tags[:HISTORY_LIMIT]:
        release = _ghcr_release(source, name, tag, _ghcr_index(session, name, tag))
        if not end_of_day or (release.published_at and release.published_at <= end_of_day):
            return release
    if version and release:
        # every build is younger (a date missing): the oldest one, the closest
        return release
    raise ReleaseError(f"{name}: no version {version or f'up to {before:%Y-%m-%d}'} in the Homebrew registry")


def latest_homebrew_cask(session, source, identifier):
    if is_tap_identifier(identifier):
        return latest_homebrew_tap(session, source, identifier, cask=True)
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
        if not asset_matches(regex, (url,)):
            continue
        name = url.split("?")[0].rsplit("/", 1)[-1]
        assets.append(Asset(name=name, url=url, sha256="" if sha256 in (None, "no_check") else sha256,
                            match=(url,)))
    return Release(version=str(data["version"]), assets=assets)


# Homebrew taps (owner/tap/name): no JSON API, the Ruby file of the formula or cask in the GitHub repository
# owner/homebrew-tap is read with regexes. It covers the usual forms (url + sha256, per OS and architecture,
# bottle blocks), not every Ruby expression; a download it can't read is left out and named in the error.
TAP_FILE_MAX_BYTES = 512 * 1024
_TAP_SKIPPED_BLOCKS = ("patch", "head", "livecheck", "test", "service", "caveats")
# a resource is mostly something to build with (a Python package); one for a platform is the program built for it
# (rustfs/tap/rustfs: on_macos do on_arm do resource "binary" do url …)
_TAP_PLATFORM_WORDS = ("on_macos", "on_arm", "on_intel", "os.mac?", "hardware::cpu")
_RB_END = re.compile(r"^\s*end\b")
_RB_CONDITION = re.compile(r"^\s*(?:if|unless)\s+(.+)$")
_RB_BLOCK = re.compile(r"^\s*(\w+)\b.*\bdo\b(?:\s*\|[^|]*\|)?\s*$")
_RB_OPENERS = re.compile(r"^\s*(?:case|begin|def|class|module|while|until)\b")
_RB_URL = re.compile(r"""^\s*url\s+["']([^"']+)["']""")
_RB_SHA256 = re.compile(r"""^\s*sha256\s+(?:["']([0-9a-f]{64})["']|:no_check)""")
# a comment after the code ("#{…}" is an interpolation)
_RB_COMMENT = re.compile(r"\s+#(?!\{).*$")
_RB_SHA256_ARCH = re.compile(r'\b(arm|intel):\s*"([0-9a-f]{64})"')
_RB_ARCH = re.compile(r'^\s*arch\s+(.+)$')
_RB_ARCH_VALUE = re.compile(r'\b(arm|intel):\s*"([^"]*)"')
_RB_BOTTLE_SHA = re.compile(r'\b(\w+):\s*"([0-9a-f]{64})"')
_RB_DEPENDS = re.compile(r'^\s*depends_on\s+"([^"]+)"\s*(=>.*)?$')
# constants of the formula: VERSION = "1.0.3".freeze, used as #{VERSION}; BINARIES = { "macos-aarch64" => "…" }
# with sha256 BINARIES["macos-aarch64"]
_RB_CONSTANT = re.compile(r"""^\s*([A-Z][A-Z0-9_]*)\s*=\s*["']([^"'#]*)["'](?:\.freeze)?\s*$""")
_RB_CONSTANT_HASH = re.compile(r"^\s*([A-Z][A-Z0-9_]*)\s*=\s*\{\s*$")
_RB_HASH_ENTRY = re.compile(r"""^\s*["']([^"']+)["']\s*=>\s*["']([^"']*)["']\s*,?\s*$""")
_RB_SHA256_CONSTANT = re.compile(r"""^\s*sha256\s+([A-Za-z_]\w*)(?:\[["']([^"']+)["']\])?\s*$""")
# local variables: download_prefix = "…", url darwin_url; arch_string = Hardware::CPU.intel? ? "amd64" : "arm64"
_RB_LOCAL = re.compile(r'^\s*([a-z_]\w*)\s*=\s*"([^"]*)"\s*$')
_RB_LOCAL_ARCH = re.compile(r'^\s*([a-z_]\w*)\s*=\s*Hardware::CPU\.(intel|arm)\?\s*\?\s*"([^"]*)"\s*:\s*"([^"]*)"\s*$')
_RB_URL_VARIABLE = re.compile(r"^\s*url\s+([a-z_]\w*)\s*$")
# a URL put together in a method from a key of a hash constant (rustfs/tap/rc before it declared its resources):
# url = "…/rustfs-cli-#{target}-v#{VERSION}.tar.gz" with sha256 = BINARIES[target]
_RB_URL_TEMPLATE = re.compile(r'^\s*[a-z_]\w*\s*=\s*"(https?://[^"]*#\{([a-z_]\w*)\}[^"]*)"\s*$', re.M)
_RB_HASH_LOOKUP = re.compile(r"=\s*([A-Z][A-Z0-9_]*)\[\s*([a-z_]\w*)\s*\]")
# the formula signs its program again when it installs it (post_install): another hash than the download's
_RB_CODESIGN = re.compile(r"""["']codesign["']""")


def is_tap_identifier(identifier):
    parts = identifier.split("/")
    return len(parts) == 3 and all(parts)


def _tap_file(session, identifier, cask):
    return _tap_path_and_file(session, identifier, cask)[1]


def _tap_path_and_file(session, identifier, cask, ref="HEAD", paths=None):
    """(path, text) of the formula or cask file in the tap's repository, at a commit (ref)"""
    owner, tap, name = identifier.split("/")
    folder = "Casks" if cask else "Formula"
    for path in paths or (f"{folder}/{name}.rb", f"{folder}/{name[0]}/{name}.rb", f"{name}.rb"):
        text = _raw_tap_file(session, identifier, ref, path)
        if text is not None:
            return path, text
    raise ReleaseError(f"{identifier}: not found in the tap {owner}/{tap}")


def _raw_tap_file(session, identifier, ref, path):
    """The text of a file of the tap's repository, None when it isn't there"""
    owner, tap, _name = identifier.split("/")
    headers = {"Authorization": f"Bearer {settings.GITHUB_TOKEN}"} if settings.GITHUB_TOKEN else {}
    with session.get(f"https://raw.githubusercontent.com/{owner}/homebrew-{tap}/{ref}/{path}", headers=headers,
                     stream=True, timeout=settings.RELEASE_HTTP_TIMEOUT) as response:
        if response.status_code == 404:
            return None
        response.raise_for_status()
        data = b""
        for chunk in response.iter_content(64 * 1024):
            data += chunk
            if len(data) > TAP_FILE_MAX_BYTES:
                raise ReleaseError(f"{identifier}: the file in the tap is bigger than {TAP_FILE_MAX_BYTES} bytes")
        return data.decode("utf-8", "replace")


def parse_tap_file(text):
    """The parts of a formula or cask file: {"version", "revision", "downloads": [(url, sha256, context)],
    "arch": {arm, intel}, "arch_sha256": {arm, intel}, "root_url", "rebuild", "bottles": {tag: sha256},
    "dependencies"}.
    context: the conditions around a download (on_linux, OS.mac? && Hardware::CPU.arm?, …), lower case."""
    found = {"version": "", "revision": 0, "downloads": [], "arch": {}, "arch_sha256": {}, "root_url": "",
             "rebuild": 0, "bottles": {}, "dependencies": []}
    # the open blocks: (kind, condition); kind "root" (the class of the formula, the cask block), "if", "else",
    # "skip", "bottle" or "" (any other block)
    stack = []
    pending = None
    # a sha256 written before its url (some casks)
    loose_sha = None
    constants, hashes, arch_variables = {}, {}, {}
    found["codesigns"] = False
    # the constant hash being read
    in_hash = None
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        line = _RB_COMMENT.sub("", line)
        if in_hash is not None:
            if entry := _RB_HASH_ENTRY.match(line):
                hashes[in_hash][entry[1]] = entry[2]
            elif line.strip().startswith("}"):
                in_hash = None
            continue
        if constant := _RB_CONSTANT_HASH.match(line):
            in_hash = constant[1]
            hashes[in_hash] = {}
            continue
        if constant := _RB_CONSTANT.match(line):
            constants[constant[1]] = constant[2]
            continue
        if _RB_CODESIGN.search(line):
            found["codesigns"] = True
        if not stack or all(kind == "root" for kind, _condition in stack):
            if local := _RB_LOCAL_ARCH.match(line):
                first, other = ("intel", "arm") if local[2] == "intel" else ("arm", "intel")
                arch_variables[local[1]] = {first: local[3], other: local[4]}
                continue
            if local := _RB_LOCAL.match(line):
                constants[local[1]] = local[2]
                continue
        if _RB_END.match(line):
            if stack:
                stack.pop()
            continue
        if re.match(r"^\s*(?:elsif)\b", line) and stack:
            stack[-1] = ("if", line.strip()[5:].lower())
            continue
        if re.match(r"^\s*else\b", line) and stack:
            # the other branch: the condition of the if doesn't hold, its words say nothing
            stack[-1] = ("else", "")
            continue
        if condition := _RB_CONDITION.match(line):
            stack.append(("if", condition[1].lower()))
            continue
        if block := _RB_BLOCK.match(line):
            word = block[1]
            if word == "cask" and not stack:
                stack.append(("root", ""))
            elif word == "bottle":
                stack.append(("bottle", ""))
            elif word == "resource":
                stack.append(("resource", ""))
            elif word in _TAP_SKIPPED_BLOCKS:
                stack.append(("skip", ""))
            else:
                # on_arm, on_macos, on_linux, on_sonoma :or_newer, stable, …
                stack.append(("", line.strip().lower()))
            continue
        if _RB_OPENERS.match(line):
            stack.append(("root" if not stack and line.strip().startswith("class ") else "", ""))
            continue
        kinds = [kind for kind, _condition in stack]
        if "skip" in kinds:
            continue
        context = " ".join(condition for _kind, condition in stack)
        if "resource" in kinds and not any(word in context for word in _TAP_PLATFORM_WORDS):
            continue
        top_level = all(kind == "root" for kind in kinds)
        if "bottle" in kinds:
            if root := re.match(r'^\s*root_url\s+"([^"]+)"', line):
                found["root_url"] = root[1].rstrip("/")
            elif rebuild := re.match(r"^\s*rebuild\s+(\d+)", line):
                found["rebuild"] = int(rebuild[1])
            elif line.strip().startswith("sha256"):
                found["bottles"].update({tag: sha for tag, sha in _RB_BOTTLE_SHA.findall(line) if tag != "cellar"})
            continue
        if top_level and (version := re.match(r"""^\s*version\s+["']([^"']+)["']""", line)) and not found["version"]:
            found["version"] = version[1]
        elif top_level and (revision := re.match(r"^\s*revision\s+(\d+)", line)):
            found["revision"] = int(revision[1])
        elif arch := _RB_ARCH.match(line):
            found["arch"] = dict(_RB_ARCH_VALUE.findall(arch[1]))
        elif (url := _RB_URL.match(line)) or ((named_url := _RB_URL_VARIABLE.match(line))
                                               and named_url[1] in constants):
            pending = [url[1] if url else constants[named_url[1]], loose_sha or "", context]
            loose_sha = None
            found["downloads"].append(pending)
        elif (sha := _RB_SHA256.match(line)) or (named := _RB_SHA256_CONSTANT.match(line)):
            digest = (sha[1] or "") if sha else (hashes.get(named[1], {}).get(named[2]) if named[2]
                                                  else constants.get(named[1])) or ""
            if not re.fullmatch(r"[0-9a-f]{64}", digest):
                digest = ""
            if pending is not None and not pending[1]:
                pending[1] = digest
            else:
                loose_sha = digest
        elif re.match(r"^\s*(?:sha256\s+)?(?:arm|intel):", line):
            # sha256 arm: "…", intel: "…" (a cask with #{arch}), also over two lines
            found["arch_sha256"].update(_RB_SHA256_ARCH.findall(line))
        elif (depends := _RB_DEPENDS.match(line)) and not re.search(r":(?:build|test)\b", depends[2] or "") \
                and "linux" not in context:
            found["dependencies"].append(depends[1])
    lookups = dict((variable, constant) for constant, variable in _RB_HASH_LOOKUP.findall(text))
    known = {download[0] for download in found["downloads"]}
    for template, variable in _RB_URL_TEMPLATE.findall(text):
        if variable in constants or lookups.get(variable) not in hashes:
            continue
        # one download per key (macos-arm64, linux-amd64, …): the ones for other systems drop out by their URL
        for key, sha in hashes[lookups[variable]].items():
            url = template.replace(f"#{{{variable}}}", key)
            if url not in known:
                found["downloads"].append([url, sha if re.fullmatch(r"[0-9a-f]{64}", sha) else "", ""])
    for download in found["downloads"]:
        # a variable made of others ("#{download_prefix}/v#{version}/…"): filled in until nothing changes
        for _round in range(5):
            before = download[0]
            for name, value in constants.items():
                download[0] = download[0].replace(f"#{{{name}}}", value)
            for name, values in arch_variables.items():
                # the architecture the url is for: the condition around it
                arch = "arm" if "cpu.arm?" in download[2] else "intel" if "cpu.intel?" in download[2] else ""
                if arch:
                    download[0] = download[0].replace(f"#{{{name}}}", values[arch])
            if download[0] == before:
                break
    if not found["version"] and constants.get("VERSION"):
        found["version"] = constants["VERSION"]
    found["downloads"] = [tuple(download) for download in found["downloads"]]
    return found


def _tap_assets(found, version):
    """The macOS downloads of a tap file, the variables filled in; the ones left with a variable are named"""
    assets = []
    unreadable = []
    for url, sha256, context in found["downloads"]:
        if "linux" in context or re.search(r"linux|windows|win64|\.exe\b|\.msi\b", url, re.I):
            continue
        # the source code GitHub packs for a tag: nothing to run, only to build from
        if re.match(r"https://github\.com/[^/]+/[^/]+/archive/", url):
            continue
        variants = [(url, sha256)]
        if "#{arch}" in url and found["arch"]:
            variants = [(url.replace("#{arch}", value), sha256 or found["arch_sha256"].get(key, ""))
                        for key, value in found["arch"].items()]
        for variant, digest in variants:
            variant = variant.replace("#{version}", version)
            if "#{" in variant:
                unreadable.append(variant)
                continue
            name = variant.split("?")[0].rsplit("/", 1)[-1] or "download"
            assets.append(Asset(name=name, url=variant, sha256=digest, match=(variant,)))
    return assets, unreadable


def latest_homebrew_tap(session, source, identifier, cask):
    return _tap_release(source, identifier, parse_tap_file(_tap_file(session, identifier, cask)), cask)


def _tap_release(source, identifier, found, cask, published_at=None):
    """The Release of a formula or cask file of a tap (parse_tap_file), the current one or one of its history"""
    name = identifier.rsplit("/", 1)[-1]
    version = found["version"]
    if not version:
        first = next((url for url, _sha, _context in found["downloads"]), "")
        guessed = re.search(r"(\d+(?:\.\d+)+)", first)
        version = guessed[1] if guessed else ""
    package_version = f"{version}_{found['revision']}" if found["revision"] else version
    if not cask and found["bottles"] and found["root_url"]:
        headers = None
        if "ghcr.io/v2/" in found["root_url"]:
            headers = {"Authorization": f"Bearer {HOMEBREW_BOTTLE_TOKEN}"}
        rebuild = f".{found['rebuild']}" if found["rebuild"] else ""
        bottles = {}
        for tag, sha256 in found["bottles"].items():
            if tag.endswith("_linux"):
                continue
            if headers:
                url = f"{found['root_url']}/{_ghcr_repository(name)}/blobs/sha256:{sha256}"
            else:
                url = f"{found['root_url']}/{name}-{package_version}.{tag}.bottle{rebuild}.tar.gz"
            bottles[tag] = (url, sha256, headers)
        assets = _bottle_assets(source, name, package_version, bottles)
    else:
        assets, unreadable = _tap_assets(found, version)
        patterns = _asset_regex(source)
        assets = [asset for asset in assets if asset_matches(patterns, asset.match)]
        if not assets and unreadable:
            raise ReleaseError(f"{identifier}: no download the server can read, e.g. {unreadable[0]}")
    warnings = ()
    if found["codesigns"] and not cask:
        warnings = (f"{identifier}: the formula signs its program again when it installs it (codesign), the Macs see "
                    "another hash than the download's: allow the blocked event instead",)
    return Release(version=package_version, assets=assets, dependencies=found["dependencies"], warnings=warnings,
                   published_at=published_at)


# the commits of a tap formula looked at for its older versions, at most (each one is a file to read)
TAP_HISTORY_COMMITS = 100


def homebrew_tap_releases(session, source, identifier, limit, known=None, in_range=True):
    """The versions of a tap formula, newest first, from the history of its file in the tap's GitHub repository:
    taps have no registry of older bottles. A version's date is that of the commit that brought it.

    Only with GITHUB_TOKEN, else only the current version: without it GitHub allows 60 API requests an hour.
    One request to the GitHub API (the commits of the file), then the file of each
    commit from raw.githubusercontent.com until `limit` versions are found, or the commits are older than the range
    to keep (in_range=False: all `limit` versions). known: {version: published_at} recorded already, they come
    without their files read again when the commit names them (the message "tool 1.2.3", as Homebrew writes it);
    else the file is read to know its version.
    """
    if not settings.GITHUB_TOKEN:
        # without a token GitHub allows 60 API requests an hour for the whole server (the catalog search too): only
        # the current version, as before the history was read
        current = latest_homebrew_tap(session, source, identifier, cask=False)
        return [] if semver.is_prerelease(current.version) and not source.include_prereleases else [current]
    cache = run_cache(session, "tap-history")
    # one session checks every source: the files, the prereleases and the range are the source's
    key = (source.pk, identifier, limit, in_range, frozenset(known or ()))
    if key in cache:
        return cache[key]
    owner, tap, name = identifier.split("/")
    path, _text = _tap_path_and_file(session, identifier, cask=False)
    response = session.get(f"https://api.github.com/repos/{owner}/homebrew-{tap}/commits",
                           params={"path": path, "per_page": TAP_HISTORY_COMMITS}, headers=_github_headers(),
                           timeout=settings.RELEASE_HTTP_TIMEOUT)
    response.raise_for_status()
    cutoff = source.keep_cutoff() if in_range else None
    releases = {}
    for commit in response.json()[:TAP_HISTORY_COMMITS]:
        details = commit.get("commit") or {}
        date = _published((details.get("committer") or {}).get("date"))
        named = re.match(rf"{re.escape(name)}:? v?(\S+)", (details.get("message") or "").strip())
        if known and named and named[1] in known:
            # recorded already: no file to read for it
            release = Release(version=named[1], assets=[], published_at=date)
        else:
            found = parse_tap_file(_raw_tap_file(session, identifier, commit["sha"], path) or "")
            release = _tap_release(source, identifier, found, cask=False, published_at=date)
        if not release.version or (semver.is_prerelease(release.version) and not source.include_prereleases):
            continue
        if release.version in releases:
            # an older commit of the same version (a new bottle, a fix): the version came with the oldest one
            releases[release.version].published_at = date or releases[release.version].published_at
            continue
        if len(releases) >= limit or (cutoff and date and date < cutoff and releases):
            break
        releases[release.version] = release
    result = list(releases.values())
    cache[key] = result
    return result


def latest_url(session, source, identifier):
    # no version information, the content hash is the version
    asset = Asset(name=identifier.split("?")[0].rsplit("/", 1)[-1] or "download", url=identifier)
    return Release(version="", assets=[asset])


def _list_document(session, identifier):
    """The download list, read once per check: only https, at most RELEASE_MAX_LIST_BYTES"""
    cache = run_cache(session, "url_lists")
    if identifier in cache:
        return cache[identifier]
    content = bytearray()
    with https_get(session, identifier, stream=True, timeout=settings.RELEASE_HTTP_TIMEOUT) as response:
        response.raise_for_status()
        for chunk in response.iter_content(macho.CHUNK_SIZE):
            content += chunk
            if len(content) > settings.RELEASE_MAX_LIST_BYTES:
                raise ReleaseError(f"{identifier}: bigger than {settings.RELEASE_MAX_LIST_BYTES} bytes")
    try:
        document = json.loads(bytes(content))
    except ValueError as e:
        raise ReleaseError(f"{identifier}: not valid JSON ({e})")
    cache[identifier] = document
    return document


def _list_path(value, path):
    """The values at a path of a download list: keys separated by dots, * takes every value of an object; a list on
    the way is gone through"""
    values = [value]
    for key in [part for part in path.split(".") if part]:
        found = []
        for item in values:
            items = item if isinstance(item, list) else [item]
            for element in items:
                if not isinstance(element, dict):
                    continue
                if key == "*":
                    found += list(element.values())
                elif key in element:
                    found.append(element[key])
        values = found
    return [element for item in values for element in (item if isinstance(item, list) else [item])]


def _list_field(objects, field):
    """The text of a field in the first object that has it (the download, then its entry); numbers count too"""
    for item in objects:
        value = item.get(field)
        if isinstance(value, (str, int, float)) and not isinstance(value, bool):
            return str(value)
    return None


def list_version_order(version):
    """A version of a download list to sort by: dotted numbers as numbers (131.0.6778.85, four parts are common),
    the rest as text"""
    return tuple((0, int(part), "") if part.isdigit() else (1, 0, part)
                 for part in re.split(r"[.\-+_]", version or ""))


def url_list_releases(session, source, identifier, limit):
    """The versions of a download list with their downloads that pass the filters and the version pattern, newest
    first. None: an error that says which step finds nothing."""
    releases, skipped = _url_list_releases(session, source, identifier)
    if not releases:
        raise ReleaseError(f"{identifier}: {_explain_url_list(_list_document(session, identifier), source, skipped)}")
    return releases[:limit]


def _url_list_releases(session, source, identifier):
    """(the releases, newest first; the download URLs left out for not being https)"""
    document = _list_document(session, identifier)
    filters = source.list_filter_patterns()
    releases = {}
    skipped = []
    for entry in _list_path(document, source.list_entries):
        if not isinstance(entry, dict):
            continue
        version = _list_field([entry], source.list_version_key)
        if not version:
            continue
        release = releases.setdefault(version, Release(version=version, assets=[]))
        for path in pattern_lines(source.list_downloads):
            for item in _list_path(entry, path):
                if not isinstance(item, dict):
                    continue
                url = _list_field([item], source.list_url_key)
                if not url or not all((value := _list_field([item, entry], field)) is not None and regex.search(value)
                                      for field, regex in filters):
                    continue
                if not url.startswith("https://"):
                    skipped.append(url)
                    continue
                if any(asset.url == url for asset in release.assets):
                    continue
                values = tuple(value for value in (_list_field([item], key) for key in item) if value)
                release.assets.append(Asset(name=url.split("?")[0].rsplit("/", 1)[-1] or "download", url=url,
                                            match=values, https_only=True))
    # the version pattern before the limit: the newest versions it takes, not the newest of all
    found = [release for release in releases.values() if release.assets and version_matches(source, release.version)]
    found.sort(key=lambda release: list_version_order(release.version), reverse=True)
    return found, skipped


def latest_url_list(session, source, identifier):
    return url_list_releases(session, source, identifier, 1)[0]


LIST_HINT_VALUES = 10


def _some(values):
    """A few of the values for a message: sorted, at most LIST_HINT_VALUES"""
    values = sorted({str(value) for value in values})
    more = ", …" if len(values) > LIST_HINT_VALUES else ""
    return ", ".join(values[:LIST_HINT_VALUES]) + more if values else "nothing"


def _path_hint(value, path):
    """Where a path of a download list stops finding anything: (the part found, the keys there)"""
    values = [value]
    done = []
    for key in [part for part in path.split(".") if part]:
        found = _list_path(values, key)
        if not found:
            objects = [element for item in values for element in (item if isinstance(item, list) else [item])
                       if isinstance(element, dict)]
            return ".".join(done), (list(objects[0]) if objects else [])
        done.append(key)
        values = found
    return ".".join(done), []


def _explain_url_list(document, source, skipped):
    """Why a download list gives no version: the first step that finds nothing, with what is there instead (a wrong
    key in the package rule)"""
    entries = [entry for entry in _list_path(document, source.list_entries) if isinstance(entry, dict)]
    if not entries:
        where, keys = _path_hint(document, source.list_entries)
        return (f'versions in the list "{source.list_entries}" finds nothing; '
                f'{where or "the list"} has: {_some(keys)}')
    versioned = [entry for entry in entries if _list_field([entry], source.list_version_key)]
    if not versioned:
        return (f'{len(entries)} entries, none has the field "{source.list_version_key}"; '
                f"the first has: {_some(entries[0])}")
    pairs = []
    for path in pattern_lines(source.list_downloads):
        items = [(item, entry) for entry in versioned for item in _list_path(entry, path) if isinstance(item, dict)]
        if not items:
            where, keys = _path_hint(versioned[0], path)
            where = f'"{where}" of ' if where else ""
            return f'no downloads at "{path}"; {where}the first entry has: {_some(keys)}'
        pairs += items
    with_url = [(item, entry) for item, entry in pairs if _list_field([item], source.list_url_key)]
    if not with_url:
        return (f'{len(pairs)} downloads, none has the field "{source.list_url_key}"; '
                f"they have: {_some(key for item, _entry in pairs for key in item)}")
    for field, regex in source.list_filter_patterns():
        values = [value for item, entry in with_url if (value := _list_field([item, entry], field)) is not None]
        if not values:
            return (f'no download or entry has the field "{field}" of the filter {field}={regex.pattern}; '
                    f"the downloads have: {_some(key for item, _entry in with_url for key in item)}")
        if not any(regex.search(value) for value in values):
            return (f"the filter {field}={regex.pattern} matches none of {len(with_url)} downloads; "
                    f"{field} has: {_some(values)}")
    if skipped:
        return f"only URLs other than https, not taken (e.g. {skipped[0]})"
    if source.version_pattern:
        newest = sorted({_list_field([entry], source.list_version_key) for entry in versioned},
                        key=list_version_order, reverse=True)
        return f"no version matches the version pattern; the newest: {', '.join(newest[:5])}"
    return "no download matches all filters together"


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
        releases.append(_npm_release(identifier, manifest, _published(times.get(version))))
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
        if not asset_matches(regex, (platform,)):
            continue
        url = next((f["source"] for f in version.get("files", [])
                    if f["assetType"] == "Microsoft.VisualStudio.Services.VSIXPackage"), None)
        if url:
            release.assets.append(Asset(name=f"{identifier}-{version['version']}@{platform}.vsix", url=url,
                                        match=(platform,)))
            release.published_at = max(filter(None, [release.published_at,
                                                     _published(version.get("lastUpdated"))]), default=None)
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
        products = tuple(update.get("compatibleVersions") or {})
        if not asset_matches(regex, products):
            continue
        published_at = None
        if update.get("cdate"):
            published_at = datetime.fromtimestamp(int(update["cdate"]) / 1000, tz=UTC)
        asset = Asset(name=update["file"].rsplit("/", 1)[-1],
                      url=f"https://downloads.marketplace.jetbrains.com/files/{update['file']}", match=products)
        releases.append(Release(version=update["version"], assets=[asset], published_at=published_at))
    return releases


# Most packages of these catalogs are scripts (JavaScript, Python, Java), and many formulae only libraries, headers
# or data (ca-certificates, libyaml, the dependencies of others): Santa has nothing to allow in them. A release
# without executables is recorded with 0 binaries instead of being an error.
KINDS_OFTEN_WITHOUT_EXECUTABLES = (ReleaseSource.Kind.NPM_PACKAGE, ReleaseSource.Kind.VSCODE_EXTENSION,
                                   ReleaseSource.Kind.JETBRAINS_PLUGIN, ReleaseSource.Kind.HOMEBREW_FORMULA)
NO_EXECUTABLES_NOTE = "No Mach-O executable in this release: Santa does not check scripts or libraries, " \
                      "nothing to allow."


PROVIDERS = {
    ReleaseSource.Kind.GITHUB_RELEASE: latest_github_release,
    ReleaseSource.Kind.HOMEBREW_FORMULA: latest_homebrew_formula,
    ReleaseSource.Kind.HOMEBREW_CASK: latest_homebrew_cask,
    ReleaseSource.Kind.URL: latest_url,
    ReleaseSource.Kind.URL_LIST: latest_url_list,
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
    ReleaseSource.Kind.URL_LIST: url_list_releases,
}
assert set(HISTORY) == set(HISTORY_KINDS)
# releases looked at for a time range, and the older versions added per identifier and sync (the rest follows)
HISTORY_LIMIT = 100
MAX_KEPT_VERSIONS_PER_SYNC = 10


# Dependencies


def _npm_platform_ok(manifest):
    # optional dependencies are often one package per platform (esbuild, swc, …): only the macOS ones
    platforms = manifest.get("os") or []
    if "!darwin" in platforms:
        return False
    # the systems named without "!" are the only ones; only exclusions: every other system
    allowed = [platform for platform in platforms if not platform.startswith("!")]
    return not allowed or "darwin" in allowed


def npm_dependencies(session, release, errors):
    """The releases of the dependencies of an npm release, also the indirect ones"""
    found = {}
    queue = [_npm_alias(name, spec) for name, spec in (release.dependencies or {}).items()]
    documents = {}
    while queue:
        name, spec = queue.pop(0)
        if not name:
            continue
        if name not in documents:
            if len(documents) >= settings.RELEASE_MAX_DEPENDENCIES:
                errors.append(f"More than {settings.RELEASE_MAX_DEPENDENCIES} dependencies, the others are skipped")
                break
            # the documents of the names waiting, at once (a level of the tree), within the limit
            names = list(dict.fromkeys(other for other, _spec in [(name, spec), *queue]
                                       if other and other not in documents))
            documents.update(_npm_documents(session, names[:settings.RELEASE_MAX_DEPENDENCIES - len(documents)]))
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
        # a name can come in several versions: the limit is on what is downloaded
        if len(found) >= settings.RELEASE_MAX_DEPENDENCIES:
            errors.append(f"More than {settings.RELEASE_MAX_DEPENDENCIES} dependencies, the others are skipped")
            break
        dependency = _npm_release(name, versions[version])
        found[f"{name}@{version}"] = dependency
        queue += [_npm_alias(other, other_spec) for other, other_spec in (dependency.dependencies or {}).items()]
    return found


def _npm_alias(name, spec):
    """(name, range) of a dependency; an alias npm:real-name@range is the real name"""
    if spec.startswith("npm:"):
        name, _, spec = spec.removeprefix("npm:").rpartition("@")
    return name, spec


def _npm_document(session, name):
    response = session.get(_npm_url(name), timeout=settings.RELEASE_HTTP_TIMEOUT,
                           headers={"Accept": "application/vnd.npm.install-v1+json"})
    response.raise_for_status()
    return response.json()


def _npm_documents(session, names):
    """{name: abbreviated registry document}, asked at once; the first failure is raised, as one after the other"""
    with ThreadPoolExecutor(max_workers=max(1, min(DOWNLOAD_WORKERS, len(names))),
                            thread_name_prefix="santa-npm") as pool:
        futures = [(name, pool.submit(_npm_document, session, name)) for name in names]
    return {name: future.result() for name, future in futures}


def homebrew_dependencies(session, source, release, errors):
    """The releases (bottles) of the runtime dependencies of a formula, also the indirect ones.

    The current formula names them: their current versions. An older version gets the versions its bottles were
    built with, or, without them, the newest ones published on or before its day."""
    found = {}
    # every version the bottles need, of the release and of its dependencies. The release's own name the indirect
    # ones mostly, with the versions its bottles were built with: theirs win over what a dependency names (not the
    # ones of a bottle "all", which names only the direct ones). A name without a version (the current formula)
    # adds nothing to a version of it already queued.
    queue = _dependency_pairs(release.dependencies)
    own = {name for name, _version in queue}
    before = release.published_at
    seen, names = set(), set()
    while queue:
        name, version = queue.pop(0)
        if (name, version) in seen or (version is None and name in names):
            continue
        if len(seen) >= settings.RELEASE_MAX_DEPENDENCIES:
            errors.append(f"More than {settings.RELEASE_MAX_DEPENDENCIES} dependencies, the others are skipped")
            break
        seen.add((name, version))
        names.add(name)
        try:
            if version or before:
                dependency = homebrew_formula_at(session, source, name, version=version, before=before)
            else:
                dependency = latest_homebrew_formula(session, source, name)
        except (ReleaseError, requests.RequestException, KeyError, ValueError) as e:
            if not (version or before):
                raise
            _dependency_error(errors, f"Dependency {name}", e)
            continue
        if dependency.dependencies is None and not is_tap_identifier(name):
            # an older bottle without its versions: the names of the current formula, in the newest versions of the
            # day (before) as for the release itself; none at all would take away the indirect ones
            try:
                dependency.dependencies = list(latest_homebrew_formula(session, source, name).dependencies or [])
            except (ReleaseError, requests.RequestException, KeyError, ValueError) as e:
                _dependency_error(errors, f"Dependencies of {name}", e)
        found[f"{name}@{dependency.version}"] = dependency
        queue += [(other, other_version) for other, other_version in _dependency_pairs(dependency.dependencies)
                  if other not in own]
    return found


def _dependency_pairs(dependencies):
    """[(name, version or None)] of the dependencies of a Homebrew release: pairs, or names (the current formula)"""
    if isinstance(dependencies, dict):
        return list(dependencies.items())
    return [tuple(item) if isinstance(item, tuple | list) else (item, None) for item in dependencies or []]


def _dependency_error(errors, label, error):
    # a warning keeps the rules the version has; a temporary one has the version resolved again at the next check
    if is_transient(error) and isinstance(errors, Errors):
        errors.retry = True
    errors.append(f"{label}: {error}")


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


# Downloads at once: a check waits on the network (the bottles of every macOS, the kept versions, the dependencies),
# not on the server. Every download keeps its own limits; at most this many temporary files at a time.
DOWNLOAD_WORKERS = 4


def _inspect(session, asset, pattern, progress=None, label=""):
    # in a thread of the pool: only the network, hashing and unpacking, never the database
    if progress and label:
        progress.show(label)
    skipped = []
    with download(session, asset) as fileobj:
        try:
            return list(find_binaries(fileobj, asset.name, pattern, asset.bottle_tag, skipped)), skipped
        except ReleaseError:
            raise
        except (EOFError, ValueError, zlib.error, lzma.LZMAError, zipfile.BadZipFile, tarfile.TarError) as e:
            # a broken or truncated archive (its hash matched: it is broken at the source, not tried again)
            raise ReleaseError(f"{asset.name}: broken archive ({type(e).__name__}: {e})") from e


def collect_many(session, source, releases, progress=None, labels=None):
    """[(binaries, errors)] for the releases [(release, dependency label)], their assets downloaded and inspected at
    once (DOWNLOAD_WORKERS). The same file (its hash, or else its URL) is downloaded once.
    labels: what the progress shows for each release while its files download ("colima 0.9.1 · lima@2.0.3")."""
    jobs = []
    labels = labels or [""] * len(releases)
    for index, (release, dependency) in enumerate(releases):
        # the binary pattern is for the package itself, the dependencies have their own paths
        pattern = "" if dependency else source.binary_pattern
        jobs += [(index, asset, pattern, dependency) for asset in release.assets]
    # inspected before in this check (a dependency of several versions or packages): not downloaded again
    scanned = run_cache(session, "scans")
    files = {}
    for index, asset, pattern, _dependency in jobs:
        key = (asset.sha256 or asset.sha512 or asset.url, pattern)
        if key not in scanned:
            files.setdefault(key, (asset, labels[index]))
    futures = {}
    if files:
        with ThreadPoolExecutor(max_workers=min(DOWNLOAD_WORKERS, len(files)), thread_name_prefix="santa-download") \
                as pool:
            futures = {key: pool.submit(_inspect, session, asset, key[1], progress, label)
                       for key, (asset, label) in files.items()}
        for key, future in futures.items():
            if not future.exception():
                scanned[key] = future.result()
    # a release's own warnings (a tap formula that signs its program again), also those of a dependency
    results = [([], Errors(release.warnings)) for release, _dependency in releases]
    for index, asset, pattern, dependency in jobs:
        errors = results[index][1]
        key = (asset.sha256 or asset.sha512 or asset.url, pattern)
        try:
            found, skipped = scanned[key] if key in scanned else futures[key].result()
        except ReleaseError as e:
            errors.append(str(e))
            continue
        except Exception as e:
            # the network, the disk, or something unexpected: one file never stops the check. Tried again at the
            # next check, all but a refused request (404): an unexpected error can be gone with the next version
            # of the server, a version recorded without the file would stay so
            if not isinstance(e, requests.HTTPError) or is_transient(e):
                errors.retry = True
            if not is_transient(e):
                logger.exception("Release source %s: %s", source.name, asset.name)
            errors.append(f"{asset.name}: {type(e).__name__}: {e}")
            continue
        results[index][0].extend(FoundBinary(asset=asset.name, path=path, info=info, dependency=dependency,
                                             asset_match=asset.match) for path, info in found)
        errors.extend(f"{asset.name}: {info.warning}" for _path, info in found if info.warning)
        errors.extend(f"{asset.name}: {line}" for line in skipped)
    return results


def collect_binaries(session, source, release, dependency=""):
    return collect_many(session, source, [(release, dependency)])[0]


def progress_label(identifier, release, dependency=""):
    """What the progress of a check shows: the package, its version and the dependency it works on"""
    return " · ".join(filter(None, [f"{identifier} {release.version}".strip(), dependency]))


def _collect_releases(session, source, releases, progress=None, identifier=""):
    """{id(release): (binaries, errors, the dependency labels)} for the releases, with the binaries of their
    dependencies: every file of all of them downloaded at once"""
    batch, owners, resolve_errors, labels = [], [], {}, []
    for release in releases:
        if progress:
            # only the label: a cancel stops between the versions recorded, after their files are downloaded
            progress.show(progress_label(identifier, release))
        errors = Errors()
        dependencies = dependency_releases(session, source, release, errors)
        resolve_errors[id(release)] = errors
        batch.append((release, ""))
        owners.append((release, ""))
        labels.append(progress_label(identifier, release))
        for label, dependency in dependencies.items():
            batch.append((dependency, label))
            owners.append((release, label))
            labels.append(progress_label(identifier, release, label))
    collected = {id(release): ([], Errors(), []) for release in releases}
    own_errors = {}
    for (release, label), (found, errors) in zip(owners, collect_many(session, source, batch, progress, labels),
                                                 strict=True):
        binaries, dependency_errors, included = collected[id(release)]
        binaries += found
        if label:
            included.append(label)
            dependency_errors.extend(f"{label}: {error}" for error in errors)
            dependency_errors.retry |= errors.retry
        else:
            own_errors[id(release)] = errors
    # as before: the errors of the release, of finding its dependencies, then of the dependencies
    results = {}
    for key, (binaries, errors, included) in collected.items():
        parts = [own_errors.get(key, Errors()), resolve_errors[key], errors]
        combined = Errors(line for part in parts for line in part)
        combined.retry = any(part.retry for part in parts)
        results[key] = (binaries, combined, included)
    return results


def rule_identifiers(rule_type, info):
    """The (rule type, identifier) pairs for an executable, falling back to its hash when it is not signed"""
    if rule_type == RuleType.CDHASH and info.cdhashes:
        return [(RuleType.CDHASH, cdhash) for cdhash in info.cdhashes]
    value = {RuleType.SIGNINGID: info.signing_id, RuleType.TEAMID: info.team_id,
             RuleType.CERTIFICATE: info.cert_sha256}.get(rule_type)
    if value:
        return [(rule_type, value)]
    return [(RuleType.BINARY, info.sha256)]


def release_file(asset, match, path, sha256):
    """An entry of Rule.release_files"""
    return {"asset": asset, "match": list(match), "path": path, "sha256": sha256}


# rows per INSERT: SQL Server takes 2100 parameters (a rule has 17 columns) and 1000 rows per statement
RULE_BATCH = 100
LINK_BATCH = 500


def create_release_rules(source, release_version, binaries, enabled=None, existing=()):
    """enabled: None = as the source approves new versions; existing: (rule type, identifier) of rules the version has
    already, not created again. In batches: a version with its dependencies can have a thousand rules."""
    if enabled is None:
        enabled = source.auto_approve and not release_version.auto_enable_pending
    rules = []
    # one rule for the binaries sharing an identifier (a Team ID), it keeps all their files
    binaries_of = {}
    for binary in binaries:
        for key in rule_identifiers(source.rule_type, binary.info):
            binaries_of.setdefault(key, []).append(binary)
    for (rule_type, identifier), same in binaries_of.items():
        if (rule_type, identifier) in existing:
            continue
        binary = same[0]
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
            release_files=[release_file(item.asset, item.asset_match, item.path, item.info.sha256)
                           for item in same],
        )
        try:
            rule.full_clean(exclude=["release_source", "release_version"])
        except ValidationError as e:
            raise ReleaseError(f"{binary.path}: {' '.join(e.messages)}")
        rules.append(rule)
    if not rules:
        return []
    before = set(release_version.rules.values_list("pk", flat=True))
    Rule.objects.bulk_create(rules, batch_size=RULE_BATCH)
    # SQL Server returns no keys from a bulk insert: the new rules of the version, read back
    created = [rule for rule in release_version.rules.order_by("pk") if rule.pk not in before]
    group_ids = list(source.groups.values_list("pk", flat=True))
    tag_ids = list(source.tags.values_list("pk", flat=True))
    Rule.groups.through.objects.bulk_create(
        [Rule.groups.through(rule_id=rule.pk, group_id=group_id) for rule in created for group_id in group_ids],
        batch_size=LINK_BATCH)
    Rule.tags.through.objects.bulk_create(
        [Rule.tags.through(rule_id=rule.pk, tag_id=tag_id) for rule in created for tag_id in tag_ids],
        batch_size=LINK_BATCH)
    return created


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


def version_order(source):
    """The key that sorts the recorded versions of a source by their release: the date, or for a download list
    (no dates; its older versions are recorded after the newest) the version itself"""
    if source.kind == ReleaseSource.Kind.URL_LIST:
        return lambda version: list_version_order(version.version)
    return _version_date


RULE_OUTSIDE_ASSET_PATTERN = "asset"
RULE_OUTSIDE_BINARY_PATTERN = "binary"


def _asset_still_taken(source, patterns, file):
    if not file.get("match") or source.kind not in ASSET_PATTERN_KINDS:
        return True
    if source.kind == ReleaseSource.Kind.HOMEBREW_FORMULA and file.get("asset", "").endswith(".bottle.tar.gz"):
        # the bottles: also the minimum macOS and the nearest bottle
        return bottle_taken(source, patterns, file["match"])
    return asset_matches(patterns, file["match"])


def _rule_outside_patterns(source, patterns, rule):
    """Why the asset or binary pattern no longer covers any file of a rule of the package itself, None when one
    file is covered or its files are not known (recorded before they were kept)"""
    files = rule.release_files or []
    if not files or rule.release_dependency:
        return None
    in_assets = [file for file in files if _asset_still_taken(source, patterns, file)]
    if not in_assets:
        return RULE_OUTSIDE_ASSET_PATTERN
    if not any(not file.get("path") or binary_path_matches(file["path"], source.binary_pattern)
               for file in in_assets):
        return RULE_OUTSIDE_BINARY_PATTERN
    return None


def _pattern_stale(source):
    """({version pk: [(rule, reason)]} of the rules the asset or binary pattern no longer covers, the pks of the
    versions none of whose own rules is covered any more)"""
    try:
        patterns = _asset_regex(source)
    except ReleaseError:
        patterns = None
    bottle_settings = source.kind == ReleaseSource.Kind.HOMEBREW_FORMULA and (
        source.macos_minimum or not source.macos_nearest)
    if patterns is None and not source.binary_pattern and not bottle_settings:
        return {}, set()
    own = {}
    for rule in (source.rules.filter(release_dependency="").exclude(release_version=None)
                 .select_related("release_version")
                 # release_source too: the rule reads it (deferred, it would be one query per rule)
                 .only("pk", "rule_type", "identifier", "release_dependency", "release_files", "release_source",
                       "release_version__identifier", "release_version__version")):
        own.setdefault(rule.release_version_id, []).append(rule)
    stale = {}
    whole = set()
    for version_pk, rules in own.items():
        found = [(rule, reason) for rule in rules if (reason := _rule_outside_patterns(source, patterns, rule))]
        if found:
            stale[version_pk] = found
            if len(found) == len(rules):
                whole.add(version_pk)
    return stale, whole


def pattern_stale_versions(source):
    """The pks of the versions none of whose own rules the asset or binary pattern covers any more"""
    return _pattern_stale(source)[1]


def unwanted_prerelease(source, version):
    """A prerelease of a tap recorded before they were left out (taps have no flag of their own, the version says)"""
    return (source.kind == ReleaseSource.Kind.HOMEBREW_FORMULA and not source.include_prereleases
            and is_tap_identifier(version.identifier) and semver.is_prerelease(version.version))


def stale_versions(source, now=None, check_filters=False):
    """The versions to delete: of identifiers removed from the source, and outside the range to keep.

    check_filters: also the versions the version pattern no longer matches, the prereleases of taps when they aren't
    allowed, and those whose files the asset or binary pattern no longer covers (the "clean up" of the console; the
    sync leaves them, the patterns only decide about new versions there).
    """
    identifiers = set(source.identifiers)
    check_versions = check_filters and source.kind != ReleaseSource.Kind.URL
    outside_files = pattern_stale_versions(source) if check_filters else set()
    cutoff = source.keep_cutoff(now)
    stale = []
    by_identifier = {}
    for version in source.versions.all():
        if (version.identifier not in identifiers or (check_versions and not version_matches(source, version.version))
                or version.pk in outside_files or (check_filters and unwanted_prerelease(source, version))):
            stale.append(version)
        else:
            by_identifier.setdefault(version.identifier, []).append(version)
    if source.keep_versions:
        for versions in by_identifier.values():
            # by publication: older versions added later must not push out the newest one
            versions.sort(key=version_order(source), reverse=True)
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
    """What the clean up of a package rule deletes: (versions, number of their rules, number of the dependency rules
    left over when the dependencies are no longer allowed)"""
    versions = sorted(stale_versions(source, check_filters=True), key=version_order(source), reverse=True)
    rule_count = sum(Rule.objects.filter(release_version__in=chunk).count()
                     for chunk in chunked([version.pk for version in versions]))
    return versions, rule_count, len(_left_dependency_rules(source, versions))


def _left_dependency_rules(source, versions):
    """The pks of the rules of dependencies no longer allowed, outside the versions deleted anyway (their rules are
    counted there). Compared in Python: an IN list could exceed the 2100 parameters of SQL Server."""
    if uses_dependencies(source):
        return []
    deleted = {version.pk for version in versions}
    return [rule.pk for rule in source.rules.exclude(release_dependency="")
            .only("pk", "release_version", "release_source")
            if rule.release_version_id not in deleted]


def stale_rules(source):
    """The rules of the versions kept whose files the asset or binary pattern no longer covers: [(rule, reason)]
    (the versions with no covered rule left are deleted as a whole, see stale_versions)"""
    stale, _whole = _pattern_stale(source)
    deleted = {version.pk for version in stale_versions(source, check_filters=True)}
    return [item for version_pk, items in stale.items() if version_pk not in deleted for item in items]


def _delete_stale_rules(items):
    """Delete the rules outside the patterns; the binaries no other rule of their version allows leave its count"""
    deleting = {rule.pk for rule, _reason in items}
    by_version = {}
    for rule, _reason in items:
        by_version.setdefault(rule.release_version_id, []).append(rule)
    versions = [version for chunk in chunked(list(by_version))
                for version in ReleaseVersion.objects.filter(pk__in=chunk)]
    for version in versions:
        removed = {file.get("sha256") for rule in by_version[version.pk] for file in rule.release_files} - {"", None}
        for rule in version.rules.only("pk", "rule_type", "identifier", "release_files", "release_version"):
            if rule.pk not in deleting:
                removed -= {file.get("sha256") for file in rule.release_files}
                removed.discard(rule.identifier if rule.rule_type == RuleType.BINARY else None)
        if removed:
            version.binary_count = max(0, version.binary_count - len(removed))
            version.save(update_fields=["binary_count"])
    count = 0
    for chunk in chunked(list(deleting)):
        count += Rule.objects.filter(pk__in=chunk).delete()[1].get("santa.Rule", 0)
    return count


def rerecord_homebrew_versions(session, source, progress=None):
    """Download the recorded versions of a Homebrew formula again and replace the rules of their own executables:
    recorded before the executables were hashed as Homebrew installs them (santa/bottles.py), their rules have the
    hashes of the bottles. Every version is done on its own and keeps what it has until its replacement is there: a
    version the registry no longer has, or a failed download, stays as it is. A rule of an unchanged executable
    stays, the new ones are enabled when the version's rules were. The dependencies are looked up again at the next
    check (they are bottles too). Returns (the versions renewed, the error lines)."""
    renewed, errors = 0, []
    for version in source.versions.order_by("identifier", "-created_at"):
        if progress:
            try:
                progress.note(f"{version.identifier} {version.version}")
            except Cancelled:
                # stopped in the console or by a shutdown: the versions done so far are kept
                break
        label = f"{version.identifier} {version.version}" if source.has_several_identifiers else version.version
        try:
            # a tap from the history of its file, a formula from the registry
            release = homebrew_formula_at(session, source, version.identifier, version=version.version)
        except (ReleaseError, requests.RequestException, KeyError, ValueError) as e:
            errors.append(f"{label}: kept as it is ({e})")
            continue
        [(binaries, warnings)] = collect_many(session, source, [(release, "")], progress,
                                              [progress_label(version.identifier, release)])
        if warnings.retry or (warnings and not binaries):
            errors.append(f"{label}: kept as it is ({'; '.join(warnings)})"[:2000])
            continue
        _replace_own_rules(source, version, binaries, warnings)
        renewed += 1
    return renewed, errors


def _replace_own_rules(source, version, binaries, warnings):
    own = version.rules.filter(release_dependency="")
    enabled = own.filter(is_enabled=True).exists() if own.exists() else None
    keys = {key for binary in binaries for key in rule_identifiers(source.rule_type, binary.info)}
    with transaction.atomic():
        kept = set()
        obsolete = []
        for rule in own.only("pk", "rule_type", "identifier"):
            if (rule.rule_type, rule.identifier) in keys:
                kept.add((rule.rule_type, rule.identifier))
            else:
                obsolete.append(rule.pk)
        for chunk in chunked(obsolete):
            Rule.objects.filter(pk__in=chunk).delete()
        create_release_rules(source, version, binaries, enabled=enabled, existing=kept)
        labels = version.dependencies or []
        old_lines = (version.notes or "").splitlines()
        own_lines = set(_without_dependency_notes(version.notes, labels).splitlines())
        version.notes = "\n".join(
            [f"{b.asset}: {b.path} {b.info.sha256}" for b in binaries] + list(warnings)
            + [line for line in old_lines if line not in own_lines])[:10000] or NO_EXECUTABLES_NOTE
        version.binary_count = len({b.info.sha256 for b in binaries}) + version.dependency_binary_count
        # the bottles of the dependencies: looked up again at the next check, which replaces their rules
        version.dependencies_resolved = False
        version.save(update_fields=["notes", "binary_count", "dependencies_resolved"])


def cleanup_release_source(source):
    """Delete the versions and rules the package rule no longer covers. Returns (versions, rules) deleted."""
    versions, rule_count, _count = cleanup_preview(source)
    outside = stale_rules(source)
    with transaction.atomic():
        other_count = _delete_stale_rules(outside)
        for chunk in chunked(_left_dependency_rules(source, versions)):
            other_count += Rule.objects.filter(pk__in=chunk).delete()[1].get("santa.Rule", 0)
        for version in versions:
            version.delete()
        if not uses_dependencies(source):
            # the binaries of the dependencies leave the count; allowed again later, they are looked up again
            for version in source.versions.filter(dependency_binary_count__gt=0):
                version.binary_count = max(0, version.binary_count - version.dependency_binary_count)
                version.dependency_binary_count = 0
                version.save(update_fields=["binary_count", "dependency_binary_count"])
            source.versions.update(dependencies=[], dependencies_resolved=False)
    return len(versions), rule_count + other_count


def _record_release(session, source, identifier, release, collected=None, progress=None):
    """Download a release and create its version and rules. Return (the new ReleaseVersion or None, errors).

    collected: its (binaries, errors, dependency labels) of _collect_releases(), downloaded with others before.
    """
    versions = source.versions.filter(identifier=identifier)
    if not release.assets:
        raise ReleaseError(f"No matching asset in release {release.version or '-'}")
    if release.version and versions.filter(version=release.version).exists():
        return None, []
    if release.version and not version_matches(source, release.version):
        logger.info("Release source %s: %s version %s skipped by the version pattern",
                    source.name, identifier, release.version)
        return None, []
    binaries, errors, included = collected or _collect_releases(session, source, [release], progress,
                                                                identifier)[id(release)]
    if getattr(errors, "retry", False):
        raise RetryLater(f"Tried again at the next check: {'; '.join(errors)}"[:2000])
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
            dependency_binary_count=len({b.info.sha256 for b in binaries if b.dependency}
                                        - {b.info.sha256 for b in binaries if not b.dependency}),
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
    """The release of a recorded version, for its dependencies: npm has every version; a formula the older ones in
    the Homebrew registry, with the dependency versions of their bottles, else the names of the current formula, for
    the versions of their day"""
    if version.version == latest.version:
        return latest
    if source.kind == ReleaseSource.Kind.HOMEBREW_FORMULA:
        release = None
        try:
            release = homebrew_formula_at(session, source, identifier, version=version.version)
        except (ReleaseError, requests.RequestException, KeyError, ValueError):
            pass
        if release and release.dependencies is not None:
            return release
        # the day of the bottle in the registry: a version recorded as the current one has no date of its own
        published_at = (release.published_at if release else None) or version.published_at or version.created_at
        return Release(version=version.version, assets=[], published_at=published_at,
                       dependencies=list(latest.dependencies or []))
    if source.kind != ReleaseSource.Kind.NPM_PACKAGE:
        return latest
    response = session.get(f"{_npm_url(identifier)}/{version.version}", timeout=settings.RELEASE_HTTP_TIMEOUT)
    response.raise_for_status()
    return _npm_release(identifier, response.json())


def _add_dependencies(session, source, identifier, latest, errors, progress=None):
    """The dependencies of the versions recorded before the source allowed them (or before they were cleaned up, or
    resolved again): they replace the ones the version had"""
    for version in source.versions.filter(identifier=identifier, dependencies_resolved=False).order_by(
            "-created_at")[:MAX_KEPT_VERSIONS_PER_SYNC]:
        if progress:
            # stopped in the console or by a shutdown: before the next version, the ones done are kept
            progress.note(f"{identifier} {version.version}")
        release = _release_of_version(session, source, identifier, version, latest)
        warnings = Errors()
        dependencies = dependency_releases(session, source, release, warnings)
        binaries, included = [], list(dependencies)
        for label, (found, dependency_errors) in zip(
                included, collect_many(session, source, [(dependency, label)
                                                         for label, dependency in dependencies.items()],
                                       progress, [progress_label(identifier, release, label) for label in included]),
                strict=True):
            binaries += found
            warnings += [f"{label}: {error}" for error in dependency_errors]
            warnings.retry |= dependency_errors.retry
        if warnings.retry:
            # resolved again at the next check, the version keeps what it has
            errors.append(f"{version.version}: dependencies tried again at the next check: {'; '.join(warnings)}"
                          [:2000])
            continue
        own_rules = version.rules.filter(release_dependency="")
        binaries_of = {}
        for binary in binaries:
            for key in rule_identifiers(source.rule_type, binary.info):
                binaries_of.setdefault(key, []).append(binary)
        with transaction.atomic():
            # a rule of an unchanged binary stays: the Macs don't get it removed and added again; it takes the
            # dependency it comes from now (its label, files)
            previous = list(version.rules.exclude(release_dependency=""))
            for rule in previous:
                if same := binaries_of.get((rule.rule_type, rule.identifier)):
                    rule.release_dependency = same[0].dependency[:300]
                    rule.description = f"{version} – {same[0].asset}: {same[0].path}"[:500]
                    rule.release_files = [release_file(item.asset, item.asset_match, item.path, item.info.sha256)
                                          for item in same]
                    rule.save(update_fields=["release_dependency", "description", "release_files", "updated_at"])
            # only after a complete result: a dependency that failed doesn't take away what was allowed so far
            obsolete = [] if warnings else [rule.pk for rule in previous
                                             if (rule.rule_type, rule.identifier) not in binaries_of]
            for chunk in chunked(obsolete):
                Rule.objects.filter(pk__in=chunk).delete()
            # the dependencies follow the approval of the version: enabled when its own rules are (or it had none)
            create_release_rules(source, version, binaries, enabled=(
                own_rules.filter(is_enabled=True).exists() if own_rules.exists() else None),
                existing={(rule.rule_type, rule.identifier) for rule in previous})
            own = {file.get("sha256") for rule in own_rules.only("release_files") for file in rule.release_files}
            own.discard(None)
            own.discard("")
            # the binaries of the dependency rules the version has now (kept ones too, after a failure)
            added = {file.get("sha256") for rule in version.rules.exclude(release_dependency="").only("release_files")
                     for file in rule.release_files} - own - {None, ""}
            version.binary_count = len(own) + len(added)
            version.dependency_binary_count = len(added)
            version.notes = "\n".join(filter(None, [
                _without_dependency_notes(version.notes, version.dependencies or []),
                *[f"{b.dependency} – {b.asset}: {b.path} {b.info.sha256}" for b in binaries],
                f"Dependencies: {', '.join(included)}" if included else "", *warnings]))[:10000]
            version.dependencies = included
            version.dependencies_resolved = True
            version.save(update_fields=["dependencies", "dependencies_resolved", "binary_count",
                                        "dependency_binary_count", "notes"])
        errors += [f"{version.version}: {warning}" for warning in warnings]
        logger.info("Release source %s: %s version %s, %s dependencies, %s rules of others removed",
                    source.name, identifier, version.version, len(included), len(obsolete))


def _without_dependency_notes(notes, labels):
    """The notes of a version without the lines of its previous dependencies"""
    prefixes = ("Dependencies: ", "Dependency ", "More than ", *(f"{label} – " for label in labels),
                *(f"{label}: " for label in labels))
    return "\n".join(line for line in (notes or "").splitlines() if not line.startswith(prefixes))


def kept_releases(source, releases, now=None):
    """The releases inside the range the source keeps, newest first"""
    releases = [release for release in releases if version_matches(source, release.version)]
    cutoff = source.keep_cutoff(now)
    if cutoff is None:
        return releases[:source.keep_versions]
    return [release for release in releases if release.published_at and release.published_at >= cutoff]


def _sync_kept_versions(session, source, identifier, errors, latest, progress=None):
    """The older versions in the range to keep that have no rules yet"""
    limit = source.keep_versions if source.keep_unit == ReleaseSource.KeepUnit.VERSIONS else HISTORY_LIMIT
    known = dict(source.versions.filter(identifier=identifier).values_list("version", "published_at"))
    # the registry of Homebrew needs a request per version: not for the ones recorded already
    options = {"known": known} if source.kind == ReleaseSource.Kind.HOMEBREW_FORMULA else {}
    releases = kept_releases(source, HISTORY[source.kind](session, source, identifier, limit, **options))
    new_versions = []
    missing = [release for release in releases if release.version not in known][:MAX_KEPT_VERSIONS_PER_SYNC]
    if source.kind == ReleaseSource.Kind.HOMEBREW_FORMULA:
        for release in missing:
            # a bottle without its dependency versions: the ones of the current formula, in the versions of its day
            if release.dependencies is None and release.published_at:
                release.dependencies = list(latest.dependencies or [])
    # all their files at once, recorded one after the other
    if progress and missing:
        progress.note(f"{identifier}: {len(missing)}")
    collected = _collect_releases(session, source, [release for release in missing if release.assets], progress,
                                  identifier)
    for release in missing:
        if progress:
            progress.note(f"{identifier} {release.version}")
        try:
            release_version, warnings = _record_release(session, source, identifier, release,
                                                        collected=collected.get(id(release)))
        except RetryLater as e:
            errors.append(f"{release.version}: {e}")
            continue
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


def _date_homebrew_version(session, source, identifier, release):
    """The day of the recorded current version of a formula: formulae.brew.sh doesn't give it, the index of its
    bottles in the registry does (a tap: the commit that brought it). Asked once, while the version has none. Only
    the recorded version gets it, not the release: its dependencies are the current ones, not those of its day
    (homebrew_dependencies)."""
    if source.kind != ReleaseSource.Kind.HOMEBREW_FORMULA or release.published_at:
        return
    undated = source.versions.filter(identifier=identifier, version=release.version, published_at__isnull=True)
    if not release.version or not undated.exists():
        return
    try:
        if is_tap_identifier(identifier):
            newest = homebrew_tap_releases(session, source, identifier, 1)
            published_at = newest[0].published_at if newest and newest[0].version == release.version else None
        else:
            index = _ghcr_index(session, identifier, release.version)
            published_at = _published((index.get("annotations") or {}).get("org.opencontainers.image.created"))
    except (ReleaseError, requests.RequestException, ValueError, KeyError):
        return
    if published_at:
        undated.update(published_at=published_at)


def _sync_identifier(session, source, identifier, progress=None):
    """Check one identifier of the source. Return (the new ReleaseVersions, the non-fatal errors)."""
    release = PROVIDERS[source.kind](session, source, identifier)
    release_version, errors = _record_release(session, source, identifier, release, progress=progress)
    _date_homebrew_version(session, source, identifier, release)
    new_versions = [release_version] if release_version else []
    if source.approve_kept_versions and source.keep_versions and source.kind in HISTORY:
        try:
            new_versions += _sync_kept_versions(session, source, identifier, errors, release, progress)
        except (ReleaseError, requests.RequestException, KeyError, ValueError) as e:
            errors.append(f"Older versions: {e}")
    if uses_dependencies(source):
        try:
            _add_dependencies(session, source, identifier, release, errors, progress)
        except (ReleaseError, requests.RequestException, KeyError, ValueError) as e:
            errors.append(f"Dependencies: {e}")
    return new_versions, errors


PREVIEW_RELEASES = 5
PREVIEW_FILES = 20


def preview_release_source(source, session=None, progress=None):
    """What the patterns of a package rule (saved or not) take, without downloading anything: per identifier
    {"identifier", "error", "releases": [{"version", "published_at", "version_ok", "assets": [{"name", "ok",
    "also"}]}], "files": [{"path", "ok"}]}. The files are those of the recorded versions, for the binary pattern."""
    session = session or http_session()
    progress = progress or Progress()
    asset_regex = _asset_regex(source)
    # the providers filter with the asset pattern and the bottle settings: without them they return every asset,
    # marked here
    unfiltered = copy.copy(source)
    unfiltered.asset_pattern = ""
    unfiltered.macos_minimum = ""
    unfiltered.macos_nearest = True
    # the latest releases, also older than the range to keep (the registry of Homebrew stops at its start)
    unfiltered.keep_versions = 0
    files_of = _recorded_files(source) if source.pk else {}
    results = []
    progress.add_total(len(source.identifiers))
    for identifier in source.identifiers:
        try:
            progress.start(identifier)
        except Cancelled:
            # what it has so far (progress.cancelled tells)
            break
        result = {"identifier": identifier, "kind": source.kind, "error": "", "releases": [],
                  "files": files_of.get(identifier, [])[:PREVIEW_FILES]}
        results.append(result)
        try:
            if source.kind in HISTORY:
                releases = HISTORY[source.kind](session, unfiltered, identifier, PREVIEW_RELEASES)
            else:
                releases = [PROVIDERS[source.kind](session, unfiltered, identifier)]
        except (ReleaseError, requests.RequestException, KeyError, ValueError) as e:
            result["error"] = str(e)
            continue
        for release in releases[:PREVIEW_RELEASES]:
            result["releases"].append({
                "version": release.version, "published_at": release.published_at,
                "version_ok": version_matches(source, release.version),
                "assets": [_preview_asset(source, asset_regex, asset) for asset in release.assets],
            })
    return results


def _preview_asset(source, patterns, asset):
    if asset.name.endswith(".bottle.tar.gz") and asset.match:
        served = bottle_served(source, asset.match)
        # a bottle for an older macOS: the newer ones it serves
        return {"name": asset.name, "ok": bottle_taken(source, patterns, asset.match),
                "also": [tag for tag in served if tag != asset.match[0]]}
    return {"name": asset.name, "also": [],
            "ok": source.kind not in ASSET_PATTERN_KINDS or asset_matches(patterns, asset.match or (asset.name,))}


def _recorded_files(source):
    """{identifier: [{"path", "ok"}]}: the files of the recorded versions against the binary pattern"""
    paths = {}
    for rule in (source.rules.filter(release_dependency="").exclude(release_version=None)
                 .select_related("release_version")
                 .only("release_files", "release_source", "release_version__identifier")):
        for file in rule.release_files:
            if file.get("path"):
                paths.setdefault(rule.release_version.identifier, set()).add(file["path"])
    return {identifier: [{"path": path, "ok": binary_path_matches(path, source.binary_pattern)}
                         for path in sorted(found, key=lambda path: (binary_path_matches(path, source.binary_pattern),
                                                                     path))]
            for identifier, found in paths.items()}


LAST_ERROR_MAX = 5000


def _cut_lines(lines, limit):
    """The lines that fit into limit characters, whole, and how many did not"""
    kept, size = [], 0
    for index, line in enumerate(lines):
        more = f"… {len(lines) - index} more"
        if size + len(line) + len(more) + 2 > limit:
            return "\n".join([*kept, more])
        kept.append(line)
        size += len(line) + 1
    return "\n".join(kept)


def _error_owner(source, line):
    """The package of a line of last_error ("<identifier>: …", a source with several packages), or None"""
    return next((identifier for identifier in sorted(source.identifiers, key=len, reverse=True)
                 if line.startswith(f"{identifier}: ")), None)


def unchecked_identifiers(source):
    """The packages of the source never checked: no version recorded and no error of a check (just added)"""
    recorded = set(source.versions.values_list("identifier", flat=True).distinct()) if source.pk else set()
    with_errors = {_error_owner(source, line) for line in source.last_error.splitlines()} \
        if source.has_several_identifiers else ({source.identifiers[0]} if source.last_error and source.identifiers
                                                 else set())
    return [identifier for identifier in source.identifiers if identifier not in recorded | with_errors]


def _recorded_since(source, identifier, started):
    """The versions a package recorded in this check before it stopped (cancelled, or a later step failed): new all
    the same. The check is recorded, the next one would not announce them."""
    return list(source.versions.filter(identifier=identifier, created_at__gte=started, binary_count__gt=0)
                .order_by("created_at"))


def sync_release_source(source, session=None, progress=None, only=None):
    """Check every identifier of the source for a new release. Return the new ReleaseVersions.

    A failing identifier does not stop the others. ReleaseError is raised at the end if one failed,
    the new versions of the other identifiers are kept and in its new_versions.
    The packages without a version yet come first (just added: they are what the check is started for).
    only: check these identifiers of the source, not all of them (the new ones, a package of an approved request).
    """
    session = session or http_session()
    progress = progress or Progress()
    several = source.has_several_identifiers
    started = timezone.now()
    checked_before = source.last_checked_at
    recorded = set(source.versions.values_list("identifier", flat=True).distinct()) if source.pk else set()
    new_versions = []
    errors = []
    failed = []
    identifiers = [identifier for identifier in source.identifiers if only is None or identifier in only]
    checked = set()
    progress.add_total(len(identifiers))
    for identifier in sorted(identifiers, key=lambda identifier: identifier in recorded):
        try:
            progress.start(identifier)
        except Cancelled:
            # stopped in the console: the packages checked so far are kept and recorded as a check
            logger.info("Release source %s: check cancelled by %s", source.name, progress.cancelled_by)
            break
        prefix = f"{identifier}: " if several else ""
        checked.add(identifier)
        try:
            found, warnings = _sync_identifier(session, source, identifier, progress=progress)
        except Cancelled:
            logger.info("Release source %s: check cancelled by %s", source.name, progress.cancelled_by)
            new_versions += _recorded_since(source, identifier, started)
            break
        except (ReleaseError, requests.RequestException, KeyError, ValueError) as e:
            logger.warning("Release source %s: %s%s", source.name, prefix, e)
            failed.append(f"{prefix}{e}")
            new_versions += _recorded_since(source, identifier, started)
            continue
        except Exception as e:
            # something unexpected in one package: the others are checked, the check is recorded
            logger.exception("Release source %s: %s", source.name, identifier)
            failed.append(f"{prefix}{type(e).__name__}: {e}")
            new_versions += _recorded_since(source, identifier, started)
            continue
        errors += [f"{prefix}{warning}" for warning in warnings]
        new_versions += found
    prune_old_versions(source)
    # recorded by a check that never ended (its server killed): announced now, they were not yet
    interrupted = source.versions.filter(created_at__lt=started, binary_count__gt=0)
    if checked_before:
        interrupted = interrupted.filter(created_at__gt=checked_before)
    new_versions = list(interrupted.order_by("created_at")) + new_versions
    worked_before = not source.last_error
    source.last_checked_at = timezone.now()
    # the errors of the packages not checked this time (a partial or stopped check) stay
    kept = [line for line in source.last_error.splitlines()
            if several and (owner := _error_owner(source, line)) and owner not in checked] if source.last_error else []
    # what failed comes first, then the warnings of this check, then the lines kept from before
    source.last_error = _cut_lines(failed + errors + kept, LAST_ERROR_MAX)
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
