"""npm version ranges (https://github.com/npm/node-semver#ranges), enough to resolve the dependencies of a package.

Supported: exact versions, x-ranges (1.x, 1.2, *), ~, ^, the comparators < <= > >= =, hyphen ranges (1.2 - 2.3)
and || between them. Tags, URLs, git and file dependencies are not ranges: they resolve to nothing.
"""
import re

VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$")
PARTIAL_RE = re.compile(r"^v?(\d+|[xX*])(?:\.(\d+|[xX*])(?:\.(\d+|[xX*])(?:-([0-9A-Za-z.-]+))?)?)?"
                        r"(?:\+[0-9A-Za-z.-]+)?$")
COMPARATOR_RE = re.compile(r"^(<=|>=|<|>|=|~>|~|\^)?\s*(.*)$")
HYPHEN_RE = re.compile(r"^(\S+)\s+-\s+(\S+)$")


def _pre_key(pre):
    # a release sorts after its prereleases; numeric parts before alphanumeric ones
    if not pre:
        return (1,)
    return (0, tuple((0, int(part), "") if part.isdigit() else (1, 0, part) for part in pre.split(".")))


def parse(version):
    """A sortable key of a full version, or None"""
    match = VERSION_RE.match((version or "").strip())
    if not match:
        return None
    major, minor, patch, pre = match.groups()
    return int(major), int(minor), int(patch), _pre_key(pre)


def is_prerelease(version):
    key = parse(version)
    return bool(key) and key[3] != (1,)


def _key(major, minor, patch, pre=""):
    return major, minor, patch, _pre_key(pre)


def _floor(major, minor, patch):
    # the lowest version of major.minor.patch, its first prerelease (npm's "-0")
    return _key(major, minor, patch, "0")


def _partial(text):
    match = PARTIAL_RE.match(text)
    if not match:
        raise ValueError(text)
    parts = [None if part is None or part in "xX*" else int(part) for part in match.groups()[:3]]
    # 1.x.3 means 1.x
    for index in range(1, 3):
        if parts[index - 1] is None:
            parts[index] = None
    return parts[0], parts[1], parts[2], match.group(4) or ""


def _comparators(op, text):
    """[(op, key)] of one comparator; an empty list matches everything"""
    if text in ("", "*", "x", "X", "latest"):
        return []
    major, minor, patch, pre = _partial(text)
    if major is None:
        return []
    if op in ("", "="):
        if minor is None:
            return [(">=", _key(major, 0, 0)), ("<", _floor(major + 1, 0, 0))]
        if patch is None:
            return [(">=", _key(major, minor, 0)), ("<", _floor(major, minor + 1, 0))]
        return [("=", _key(major, minor, patch, pre))]
    if op in ("~", "~>"):
        low = _key(major, minor or 0, patch or 0, pre)
        if minor is None:
            return [(">=", low), ("<", _floor(major + 1, 0, 0))]
        return [(">=", low), ("<", _floor(major, minor + 1, 0))]
    if op == "^":
        low = _key(major, minor or 0, patch or 0, pre)
        if major > 0 or minor is None:
            return [(">=", low), ("<", _floor(major + 1, 0, 0))]
        if minor > 0 or patch is None:
            return [(">=", low), ("<", _floor(0, minor + 1, 0))]
        return [(">=", low), ("<", _floor(0, 0, patch + 1))]
    if op == ">":
        if minor is None:
            return [(">=", _key(major + 1, 0, 0))]
        if patch is None:
            return [(">=", _key(major, minor + 1, 0))]
        return [(">", _key(major, minor, patch, pre))]
    if op == ">=":
        return [(">=", _key(major, minor or 0, patch or 0, pre))]
    if op == "<":
        return [("<", _key(major, minor or 0, patch or 0, pre) if patch is not None
                 else _floor(major, minor or 0, 0))]
    # <=
    if minor is None:
        return [("<", _floor(major + 1, 0, 0))]
    if patch is None:
        return [("<", _floor(major, minor + 1, 0))]
    return [("<=", _key(major, minor, patch, pre))]


def _parse_set(text):
    text = text.strip()
    hyphen = HYPHEN_RE.match(text)
    if hyphen:
        return _comparators(">=", hyphen.group(1)) + _comparators("<=", hyphen.group(2))
    # "> 1.2" is one comparator
    text = re.sub(r"(<=|>=|<|>|=|~>|~|\^)\s+", r"\1", text)
    comparators = []
    for part in text.split():
        op, version = COMPARATOR_RE.match(part).groups()
        comparators += _comparators(op or "", version)
    return comparators


def parse_range(text):
    """The comparator sets of a range, or None if it is no version range (a tag, URL, git or file dependency)"""
    try:
        return [_parse_set(part) for part in (text or "").split("||")]
    except ValueError:
        return None


def _test(op, key, bound):
    return {"=": key == bound, "<": key < bound, "<=": key <= bound, ">": key > bound, ">=": key >= bound}[op]


def _satisfies_set(key, comparators):
    if not all(_test(op, key, bound) for op, bound in comparators):
        return False
    if key[3] == (1,):
        return True
    # a prerelease only matches a range that names a prerelease of the same version
    return any(bound[:3] == key[:3] and bound[3] != (1,) and bound[3] != _pre_key("0")
               for _op, bound in comparators)


def satisfies(version, text):
    key = parse(version)
    sets = parse_range(text)
    return bool(key and sets is not None and any(_satisfies_set(key, comparators) for comparators in sets))


def max_satisfying(versions, text):
    """The highest of the versions in the range, or None"""
    sets = parse_range(text)
    if sets is None:
        return None
    best = None
    for version in versions:
        key = parse(version)
        if key and any(_satisfies_set(key, comparators) for comparators in sets) and (best is None or key > best[0]):
            best = (key, version)
    return best[1] if best else None
