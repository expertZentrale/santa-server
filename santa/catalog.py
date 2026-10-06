"""Search the package catalogs for the identifier field of the release sources, with the icons to show.

Only for the admin and the request form, the release sync does not use it.
"""
import hashlib
import json
import logging
import re
from dataclasses import asdict, dataclass

import requests
from django.conf import settings
from django.core.cache import cache
from django.utils.translation import gettext

from .models import ReleaseSource
from .releases import VSCODE_GALLERY_URL, http_session

logger = logging.getLogger(__name__)

CACHE_TIMEOUT = 3600
# part of the cache keys: bump it when the queries or the results change
CACHE_VERSION = 3
MAX_RESULTS = 10
SEARCH_TIMEOUT = 10
# Homebrew has no search API: the whole list of formulae (~30 MB) or casks (~20 MB) is loaded once a day
HOMEBREW_API = "https://formulae.brew.sh/api"
HOMEBREW_INDEX_TIMEOUT = 86400
HOMEBREW_INDEX_MAX_BYTES = 100 * 1024 * 1024
GITHUB_REPOSITORY_RE = re.compile(r"github\.com[/:]([^/]+)/")

Kind = ReleaseSource.Kind
SEARCHABLE_KINDS = (Kind.GITHUB_RELEASE, Kind.HOMEBREW_FORMULA, Kind.HOMEBREW_CASK, Kind.NPM_PACKAGE,
                    Kind.VSCODE_EXTENSION, Kind.JETBRAINS_PLUGIN)


class CatalogError(Exception):
    pass


@dataclass
class Suggestion:
    identifier: str
    name: str = ""
    description: str = ""
    icon_url: str = ""
    verified: bool = False


def _github_headers():
    headers = {"Accept": "application/vnd.github+json"}
    if settings.GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {settings.GITHUB_TOKEN}"
    return headers


def _github_avatar(url):
    match = GITHUB_REPOSITORY_RE.search(url or "")
    return f"https://github.com/{match.group(1)}.png?size=64" if match else ""


def github_query(query):
    """The search API only matches whole words, except in the repository names with in:name.

    "owner/part" searches the repositories of the owner whose name contains "part".
    """
    owner, slash, name = query.partition("/")
    if slash and owner and " " not in owner:
        return f"{name.strip()} in:name user:{owner}".strip() if name.strip() else f"user:{owner}"
    return f"{query} in:name"


def _search_github(session, query):
    response = session.get("https://api.github.com/search/repositories",
                           params={"q": github_query(query), "per_page": MAX_RESULTS}, headers=_github_headers(),
                           timeout=SEARCH_TIMEOUT)
    if response.status_code in (403, 429):
        raise CatalogError(gettext("GitHub search rate limit reached, try again in a minute (or set github_token)."))
    response.raise_for_status()
    return [Suggestion(identifier=item["full_name"], name=item["name"], description=item.get("description") or "",
                       icon_url=(item.get("owner") or {}).get("avatar_url") or "")
            for item in response.json().get("items", [])]


def _npm_suggestion(package):
    return Suggestion(identifier=package["name"], name=package["name"],
                      description=package.get("description") or "",
                      icon_url=_github_avatar((package.get("links") or {}).get("repository")))


def _search_npm(session, query):
    response = session.get("https://registry.npmjs.org/-/v1/search", params={"text": query, "size": MAX_RESULTS},
                           timeout=SEARCH_TIMEOUT)
    response.raise_for_status()
    return [_npm_suggestion(item["package"]) for item in response.json().get("objects", [])]


def _vscode_query(session, criteria, page_size):
    response = session.post(
        VSCODE_GALLERY_URL,
        json={"filters": [{"criteria": [{"filterType": 8, "value": "Microsoft.VisualStudio.Code"},
                                        {"filterType": 12, "value": "4096"}, *criteria],
                           "pageNumber": 1, "pageSize": page_size, "sortBy": 0, "sortOrder": 0}],
              "assetTypes": ["Microsoft.VisualStudio.Services.Icons.Default"],
              # IncludeFiles | IncludeCategoryAndTags | IncludeAssetUri | IncludeStatistics | IncludeLatestVersionOnly
              "flags": 914},
        headers={"Accept": "application/json;api-version=7.2-preview.1"},
        timeout=SEARCH_TIMEOUT,
    )
    response.raise_for_status()
    suggestions = []
    for result in response.json().get("results", []):
        for extension in result.get("extensions", []):
            publisher = extension.get("publisher") or {}
            files = (extension.get("versions") or [{}])[0].get("files", [])
            icon_url = next((f["source"] for f in files
                             if f.get("assetType") == "Microsoft.VisualStudio.Services.Icons.Default"), "")
            suggestions.append(Suggestion(
                identifier=f"{publisher.get('publisherName')}.{extension.get('extensionName')}",
                name=extension.get("displayName") or extension.get("extensionName") or "",
                description=extension.get("shortDescription") or "",
                icon_url=icon_url,
                verified="verified" in (publisher.get("flags") or ""),
            ))
    return suggestions


def _search_vscode(session, query):
    return _vscode_query(session, [{"filterType": 10, "value": query}], MAX_RESULTS)


def _jetbrains_suggestion(plugin):
    icon = plugin.get("icon") or ""
    return Suggestion(identifier=plugin.get("xmlId") or str(plugin["id"]), name=plugin.get("name") or "",
                      description=plugin.get("preview") or "",
                      icon_url=f"https://plugins.jetbrains.com{icon}" if icon.startswith("/") else icon,
                      verified=bool((plugin.get("vendor") or {}).get("isVerified")))


def _search_jetbrains(session, query):
    response = session.get("https://plugins.jetbrains.com/api/searchPlugins",
                           params={"search": query, "max": MAX_RESULTS}, timeout=SEARCH_TIMEOUT)
    response.raise_for_status()
    return [_jetbrains_suggestion(plugin) for plugin in response.json().get("plugins", [])]


def _homebrew_entry(kind, data):
    """identifier, name, other names (aliases, old names), description, homepage"""
    if kind == Kind.HOMEBREW_CASK:
        return [data["token"], (data.get("name") or [data["token"]])[0], data.get("old_tokens") or [],
                data.get("desc") or "", data.get("homepage") or ""]
    return [data["name"], data["name"], [*(data.get("aliases") or []), *(data.get("oldnames") or [])],
            data.get("desc") or "", data.get("homepage") or ""]


def _homebrew_suggestion(entry):
    identifier, name, _others, description, homepage = entry
    return Suggestion(identifier=identifier, name=name, description=description, icon_url=_github_avatar(homepage))


def _load_homebrew_index(session, kind):
    path = "cask" if kind == Kind.HOMEBREW_CASK else "formula"
    content = bytearray()
    with session.get(f"{HOMEBREW_API}/{path}.json", stream=True, timeout=SEARCH_TIMEOUT) as response:
        response.raise_for_status()
        for chunk in response.iter_content(1024 * 1024):
            content += chunk
            if len(content) > HOMEBREW_INDEX_MAX_BYTES:
                raise ValueError(f"the {path} list is bigger than {HOMEBREW_INDEX_MAX_BYTES} bytes")
    return [_homebrew_entry(kind, data) for data in json.loads(content) if not data.get("disabled")]


def _homebrew_rank(entry, query):
    identifier, name, others, description, _homepage = entry
    names = [identifier.lower(), name.lower(), *(other.lower() for other in others)]
    if query in names:
        return 0
    if any(n.startswith(query) for n in names):
        return 1
    if any(query in n for n in names):
        return 2
    if query in description.lower():
        return 3
    return None


def _search_homebrew(kind):
    def search(session, query):
        index = _cached(f"homebrew-index:{kind}", lambda: _load_homebrew_index(session, kind),
                        HOMEBREW_INDEX_TIMEOUT)
        query = query.lower()
        ranked = []
        for entry in index:
            rank = _homebrew_rank(entry, query)
            if rank is not None:
                ranked.append((rank, len(entry[0]), entry[0], entry))
        ranked.sort(key=lambda item: item[:3])
        return [_homebrew_suggestion(entry) for *_key, entry in ranked[:MAX_RESULTS]]
    return search


SEARCHES = {
    Kind.GITHUB_RELEASE: _search_github,
    Kind.HOMEBREW_FORMULA: _search_homebrew(Kind.HOMEBREW_FORMULA),
    Kind.HOMEBREW_CASK: _search_homebrew(Kind.HOMEBREW_CASK),
    Kind.NPM_PACKAGE: _search_npm,
    Kind.VSCODE_EXTENSION: _search_vscode,
    Kind.JETBRAINS_PLUGIN: _search_jetbrains,
}


def _cached(key, compute, timeout=CACHE_TIMEOUT):
    cache_key = f"santa:catalog:v{CACHE_VERSION}:" + hashlib.sha256(key.encode()).hexdigest()
    value = cache.get(cache_key)
    if value is None:
        value = compute()
        # None: the catalog could not be reached, try again next time
        if value is not None:
            cache.set(cache_key, value, timeout)
    return value


def search(kind, query, session=None):
    """Suggestions for a search text. Raises CatalogError when the catalog cannot be reached."""
    query = query.strip()
    if kind not in SEARCHES or len(query) < 2:
        return []

    def compute():
        try:
            return [asdict(s) for s in SEARCHES[kind](session or http_session(), query)]
        except (requests.RequestException, KeyError, ValueError) as e:
            logger.warning("Catalog search %s %r: %s", kind, query, e)
            raise CatalogError(gettext("The catalog could not be searched: %(error)s") % {"error": e})

    return [Suggestion(**item) for item in _cached(f"search:{kind}:{query.lower()}", compute)]


def _lookup(session, kind, identifier):
    if kind == Kind.GITHUB_RELEASE:
        response = session.get(f"https://api.github.com/repos/{identifier}", headers=_github_headers(),
                               timeout=SEARCH_TIMEOUT)
        response.raise_for_status()
        data = response.json()
        return Suggestion(identifier=data["full_name"], name=data["name"], description=data.get("description") or "",
                          icon_url=(data.get("owner") or {}).get("avatar_url") or "")
    if kind in (Kind.HOMEBREW_FORMULA, Kind.HOMEBREW_CASK):
        path = "cask" if kind == Kind.HOMEBREW_CASK else "formula"
        response = session.get(f"{HOMEBREW_API}/{path}/{identifier}.json", timeout=SEARCH_TIMEOUT)
        response.raise_for_status()
        return _homebrew_suggestion(_homebrew_entry(kind, response.json()))
    if kind == Kind.NPM_PACKAGE:
        response = session.get(f"https://registry.npmjs.org/{identifier.replace('/', '%2F')}/latest",
                               timeout=SEARCH_TIMEOUT)
        response.raise_for_status()
        data = response.json()
        repository = data.get("repository")
        return Suggestion(identifier=data["name"], name=data["name"], description=data.get("description") or "",
                          icon_url=_github_avatar(repository.get("url") if isinstance(repository, dict)
                                                  else repository))
    if kind == Kind.VSCODE_EXTENSION:
        found = _vscode_query(session, [{"filterType": 7, "value": identifier}], 1)
        return found[0] if found else None
    if kind == Kind.JETBRAINS_PLUGIN:
        path = identifier if identifier.isdigit() else f"intellij/{identifier}"
        response = session.get(f"https://plugins.jetbrains.com/api/plugins/{path}", timeout=SEARCH_TIMEOUT)
        response.raise_for_status()
        return _jetbrains_suggestion(response.json())
    return None


def lookup(kind, identifier, session=None):
    """The catalog entry of an identifier, or None (unknown, not searchable, or the catalog is down)"""
    if kind not in SEARCHES or not identifier:
        return None

    def compute():
        try:
            found = _lookup(session or http_session(), kind, identifier)
        except (requests.RequestException, KeyError, ValueError) as e:
            logger.info("Catalog lookup %s %r: %s", kind, identifier, e)
            return None
        return asdict(found) if found else {}

    value = _cached(f"lookup:{kind}:{identifier.lower()}", compute)
    return Suggestion(**value) if value else None


def update_identifier_icons(source, picked=None, session=None):
    """Fill source.identifier_icons for its identifiers: from the suggestions picked in the form, else the catalog"""
    picked = picked or {}
    icons = {}
    for identifier in source.identifiers:
        if identifier in picked:
            icons[identifier] = picked[identifier]
        elif identifier in source.identifier_icons:
            icons[identifier] = source.identifier_icons[identifier]
        else:
            found = lookup(source.kind, identifier, session)
            if found:
                icons[identifier] = {"name": found.name, "icon_url": found.icon_url}
    source.identifier_icons = icons
