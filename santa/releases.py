"""Allowlist the binaries of new releases of unsigned tools (colima, …).

Every release asset is downloaded, the Mach-O executables inside are hashed, and one BINARY rule
is created per hash. Santa then allows exactly these files, and nothing else built by someone else.
"""
import base64
import copy
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

from . import archives, macho, notifications, semver
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
from .progress import Progress
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
    asset_match: tuple = ()


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


def _is_nested(path, size):
    return path.lower().endswith(NESTED_SUFFIXES) and size <= settings.RELEASE_MAX_FILE_BYTES


def find_binaries(fileobj, name, pattern=""):
    """Yield (path, MachOInfo) for the Mach-O executables in a raw binary, a zip or tar archive, a flat package
    (.pkg) or a disk image (.dmg). An archive inside one (the .pkg in a .dmg) is read too, one level deep."""
    try:
        yield from _find_binaries(fileobj, name, pattern, _UnpackBudget(name), nested=False)
    except archives.ArchiveError as e:
        raise ReleaseError(f"{name}: {e}") from e


def _find_binaries(fileobj, name, pattern, budget, nested):
    fileobj.seek(0)
    header = fileobj.read(8)
    fileobj.seek(0)
    if macho.is_macho_header(header):
        info = _inspect_candidate(fileobj, name, pattern)
        if info:
            yield name, info
        return

    def wanted(path, size, executable):
        # packages and disk images keep the x bit: only executables are worth unpacking there
        return ((not nested and _is_nested(path, size))
                or (executable is not False and _worth_unpacking(path, size, pattern)))

    if archives.is_pkg(header):
        members = archives.pkg_members(fileobj, budget.consume, MAX_ARCHIVE_MEMBERS)
    elif archives.is_dmg(fileobj):
        members = archives.dmg_members(fileobj, wanted, budget.consume, MAX_ARCHIVE_MEMBERS,
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
            continue
        with open_member() as member_fileobj, _spool(member_fileobj, budget) as spooled:
            if not nested and _is_nested(path, size):
                for inner_path, info in _find_binaries(spooled, path, pattern, budget, nested=True):
                    yield f"{path}/{inner_path}", info
                continue
            info = _inspect_candidate(spooled, path, pattern)
            if info:
                yield path, info


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


def _bottle_assets(source, name, version, bottles):
    """The assets of the bottles a source takes. bottles: {tag: (url, sha256, headers)}"""
    patterns = _asset_regex(source)
    coverage = bottle_coverage(bottles)
    return [Asset(name=f"{name}-{version}.{tag}.bottle.tar.gz", url=url, sha256=sha256, headers=headers,
                  match=tuple(coverage[tag]))
            for tag, (url, sha256, headers) in sorted(bottles.items())
            if bottle_taken(source, patterns, coverage[tag])]


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
        if not asset_matches(regex, (name,)):
            continue
        digest = asset.get("digest") or ""
        assets.append(Asset(name=name, url=asset["browser_download_url"],
                            sha256=digest.removeprefix("sha256:") if digest.startswith("sha256:") else "",
                            match=(name,)))
    published_at = parse_datetime(data.get("published_at") or "")
    return Release(version=data["tag_name"], assets=assets, published_at=published_at)


def latest_homebrew_formula(session, source, identifier):
    if is_tap_identifier(identifier):
        return latest_homebrew_tap(session, source, identifier, cask=False)
    response = session.get(f"https://formulae.brew.sh/api/formula/{identifier}.json",
                           timeout=settings.RELEASE_HTTP_TIMEOUT)
    response.raise_for_status()
    data = response.json()
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


def homebrew_formula_releases(session, source, identifier, limit):
    """The bottles of the older versions of a formula, newest first, from the Homebrew registry (ghcr.io).

    formulae.brew.sh only knows the current version. Every tag of the registry is a version (a rebuild of the same
    bottle gets "-1", "-2"…); its index lists the bottles per macOS with their SHA-256 and the publication date.
    """
    if is_tap_identifier(identifier):
        # a tap has only its current version
        return [latest_homebrew_tap(session, source, identifier, cask=False)]
    headers = {"Authorization": f"Bearer {HOMEBREW_BOTTLE_TOKEN}"}
    base = f"https://ghcr.io/v2/homebrew/core/{_ghcr_repository(identifier)}"
    response = session.get(f"{base}/tags/list", headers=headers, timeout=settings.RELEASE_HTTP_TIMEOUT)
    response.raise_for_status()
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
        digests = {}
        for manifest in index.get("manifests") or []:
            if (manifest.get("platform") or {}).get("os") != "darwin":
                continue
            bottle = manifest.get("annotations") or {}
            # "0.9.1.arm64_sequoia.1": version, bottle tag, rebuild
            bottle_tag = bottle.get("org.opencontainers.image.ref.name", "").removeprefix(f"{version}.").split(".")[0]
            if digest := bottle.get("sh.brew.bottle.digest"):
                digests[bottle_tag] = digest
        bottles = {bottle_tag: (f"{base}/blobs/sha256:{digest}", digest, headers)
                   for bottle_tag, digest in digests.items()}
        releases[version] = Release(version=version, assets=_bottle_assets(source, identifier, version, bottles),
                                    published_at=published_at)
    return list(releases.values())


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
_TAP_SKIPPED_BLOCKS = ("resource", "patch", "head", "livecheck", "test", "service", "caveats")
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


def is_tap_identifier(identifier):
    parts = identifier.split("/")
    return len(parts) == 3 and all(parts)


def _tap_file(session, identifier, cask):
    owner, tap, name = identifier.split("/")
    base = f"https://raw.githubusercontent.com/{owner}/homebrew-{tap}/HEAD"
    folder = "Casks" if cask else "Formula"
    headers = {"Authorization": f"Bearer {settings.GITHUB_TOKEN}"} if settings.GITHUB_TOKEN else {}
    for path in (f"{folder}/{name}.rb", f"{folder}/{name[0]}/{name}.rb", f"{name}.rb"):
        with session.get(f"{base}/{path}", headers=headers, stream=True,
                         timeout=settings.RELEASE_HTTP_TIMEOUT) as response:
            if response.status_code == 404:
                continue
            response.raise_for_status()
            data = b""
            for chunk in response.iter_content(64 * 1024):
                data += chunk
                if len(data) > TAP_FILE_MAX_BYTES:
                    raise ReleaseError(f"{identifier}: the file in the tap is bigger than {TAP_FILE_MAX_BYTES} bytes")
            return data.decode("utf-8", "replace")
    raise ReleaseError(f"{identifier}: not found in the tap {owner}/{tap}")


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
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        line = _RB_COMMENT.sub("", line)
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
        elif url := _RB_URL.match(line):
            pending = [url[1], loose_sha or "", context]
            loose_sha = None
            found["downloads"].append(pending)
        elif sha := _RB_SHA256.match(line):
            if pending is not None and not pending[1]:
                pending[1] = sha[1] or ""
            else:
                loose_sha = sha[1] or ""
        elif re.match(r"^\s*(?:sha256\s+)?(?:arm|intel):", line):
            # sha256 arm: "…", intel: "…" (a cask with #{arch}), also over two lines
            found["arch_sha256"].update(_RB_SHA256_ARCH.findall(line))
        elif (depends := _RB_DEPENDS.match(line)) and not re.search(r":(?:build|test)\b", depends[2] or "") \
                and "linux" not in context:
            found["dependencies"].append(depends[1])
    found["downloads"] = [tuple(download) for download in found["downloads"]]
    return found


def _tap_assets(found, version):
    """The macOS downloads of a tap file, the variables filled in; the ones left with a variable are named"""
    assets = []
    unreadable = []
    for url, sha256, context in found["downloads"]:
        if "linux" in context or re.search(r"linux|windows|win64|\.exe\b|\.msi\b", url, re.I):
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
    found = parse_tap_file(_tap_file(session, identifier, cask))
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
    return Release(version=package_version, assets=assets, dependencies=found["dependencies"])


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
        if not asset_matches(regex, (platform,)):
            continue
        url = next((f["source"] for f in version.get("files", [])
                    if f["assetType"] == "Microsoft.VisualStudio.Services.VSIXPackage"), None)
        if url:
            release.assets.append(Asset(name=f"{identifier}-{version['version']}@{platform}.vsix", url=url,
                                        match=(platform,)))
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
    if "!darwin" in platforms:
        return False
    # the systems named without "!" are the only ones; only exclusions: every other system
    allowed = [platform for platform in platforms if not platform.startswith("!")]
    return not allowed or "darwin" in allowed


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
        # a name can come in several versions: the limit is on what is downloaded
        if len(found) >= settings.RELEASE_MAX_DEPENDENCIES:
            errors.append(f"More than {settings.RELEASE_MAX_DEPENDENCIES} dependencies, the others are skipped")
            break
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
                    found.append(FoundBinary(asset=asset.name, path=path, info=info, dependency=dependency,
                                             asset_match=asset.match))
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


def release_file(asset, match, path, sha256):
    """An entry of Rule.release_files"""
    return {"asset": asset, "match": list(match), "path": path, "sha256": sha256}


def create_release_rules(source, release_version, binaries, enabled=None):
    """enabled: None = as the source approves new versions"""
    if enabled is None:
        enabled = source.auto_approve and not release_version.auto_enable_pending
    rules = []
    # one rule for the binaries sharing an identifier (a Team ID), it keeps all their files
    binaries_of = {}
    for binary in binaries:
        for key in rule_identifiers(source.rule_type, binary.info):
            binaries_of.setdefault(key, []).append(binary)
    for (rule_type, identifier), same in binaries_of.items():
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
                 .only("pk", "rule_type", "identifier", "release_dependency", "release_files",
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


def stale_versions(source, now=None, check_filters=False):
    """The versions to delete: of identifiers removed from the source, and outside the range to keep.

    check_filters: also the versions the version pattern no longer matches, and those whose files the asset or
    binary pattern no longer covers (the "clean up" of the console; the sync leaves them, the patterns only decide
    about new versions there).
    """
    identifiers = set(source.identifiers)
    check_versions = check_filters and source.kind != ReleaseSource.Kind.URL
    outside_files = pattern_stale_versions(source) if check_filters else set()
    cutoff = source.keep_cutoff(now)
    stale = []
    by_identifier = {}
    for version in source.versions.all():
        if (version.identifier not in identifiers or (check_versions and not version_matches(source, version.version))
                or version.pk in outside_files):
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
    """What the clean up of a package rule deletes: (versions, number of their rules, number of the dependency rules
    left over when the dependencies are no longer allowed)"""
    versions = sorted(stale_versions(source, check_filters=True), key=_version_date, reverse=True)
    rule_count = sum(Rule.objects.filter(release_version__in=chunk).count()
                     for chunk in chunked([version.pk for version in versions]))
    return versions, rule_count, len(_left_dependency_rules(source, versions))


def _left_dependency_rules(source, versions):
    """The pks of the rules of dependencies no longer allowed, outside the versions deleted anyway (their rules are
    counted there). Compared in Python: an IN list could exceed the 2100 parameters of SQL Server."""
    if uses_dependencies(source):
        return []
    deleted = {version.pk for version in versions}
    return [rule.pk for rule in source.rules.exclude(release_dependency="").only("pk", "release_version")
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
        for rule in version.rules.only("pk", "rule_type", "identifier", "release_files"):
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


def _record_release(session, source, identifier, release):
    """Download a release and create its version and rules. Return (the new ReleaseVersion or None, errors)."""
    versions = source.versions.filter(identifier=identifier)
    if not release.assets:
        raise ReleaseError(f"No matching asset in release {release.version or '-'}")
    if release.version and versions.filter(version=release.version).exists():
        return None, []
    if release.version and not version_matches(source, release.version):
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
            added = len({b.info.sha256 for b in binaries})
            version.binary_count += added
            version.dependency_binary_count += added
            notes = [f"{b.dependency} – {b.asset}: {b.path} {b.info.sha256}" for b in binaries]
            if included:
                notes.append(f"Dependencies: {', '.join(included)}")
            version.notes = "\n".join(filter(None, [version.notes, *notes, *warnings]))[:10000]
            version.save(update_fields=["dependencies", "dependencies_resolved", "binary_count",
                                        "dependency_binary_count", "notes"])
        errors += [f"{version.version}: {warning}" for warning in warnings]
        logger.info("Release source %s: %s version %s, %s dependencies added",
                    source.name, identifier, version.version, len(included))


def kept_releases(source, releases, now=None):
    """The releases inside the range the source keeps, newest first"""
    releases = [release for release in releases if version_matches(source, release.version)]
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
    files_of = _recorded_files(source) if source.pk else {}
    results = []
    progress.add_total(len(source.identifiers))
    for identifier in source.identifiers:
        progress.start(identifier)
        result = {"identifier": identifier, "error": "", "releases": [],
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
                 .select_related("release_version").only("release_files", "release_version__identifier")):
        for file in rule.release_files:
            if file.get("path"):
                paths.setdefault(rule.release_version.identifier, set()).add(file["path"])
    return {identifier: [{"path": path, "ok": binary_path_matches(path, source.binary_pattern)}
                         for path in sorted(found, key=lambda path: (binary_path_matches(path, source.binary_pattern),
                                                                     path))]
            for identifier, found in paths.items()}


def sync_release_source(source, session=None, progress=None):
    """Check every identifier of the source for a new release. Return the new ReleaseVersions.

    A failing identifier does not stop the others. ReleaseError is raised at the end if one failed,
    the new versions of the other identifiers are kept and in its new_versions.
    """
    session = session or http_session()
    progress = progress or Progress()
    several = source.has_several_identifiers
    new_versions = []
    errors = []
    failed = []
    progress.add_total(len(source.identifiers))
    for identifier in source.identifiers:
        progress.start(identifier)
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
