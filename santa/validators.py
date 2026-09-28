import re

from django.core.exceptions import ValidationError
from django.utils.translation import gettext

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
CDHASH_RE = re.compile(r"^[0-9a-f]{40}$")
TEAM_ID_RE = re.compile(r"^[0-9A-Z]{10}$")
SIGNING_ID_RE = re.compile(r"^(?:[0-9A-Z]{10}|platform):[^\s:][^\s]*$")


def validate_identifier(rule_type, identifier):
    """Return the normalized identifier, or raise a ValidationError"""
    if rule_type in ("BINARY", "CERTIFICATE"):
        identifier = identifier.lower()
        if not SHA256_RE.match(identifier):
            raise ValidationError(gettext("Must be a SHA-256 hex digest (64 characters)."))
    elif rule_type == "CDHASH":
        identifier = identifier.lower()
        if not CDHASH_RE.match(identifier):
            raise ValidationError(gettext("Must be a CDHash (40 hex characters)."))
    elif rule_type == "TEAMID":
        identifier = identifier.upper()
        if not TEAM_ID_RE.match(identifier):
            raise ValidationError(gettext("Must be a 10 character Team ID, e.g. EQHXZ8M8AV."))
    elif rule_type == "SIGNINGID":
        if not SIGNING_ID_RE.match(identifier):
            raise ValidationError(gettext("Must be TEAMID:signing.id or platform:signing.id, "
                                          "e.g. EQHXZ8M8AV:com.google.Chrome."))
    else:
        raise ValidationError(gettext("Unknown rule type %(rule_type)s.") % {"rule_type": rule_type})
    return identifier


def split_path_regexes(value):
    return [line.strip() for line in (value or "").splitlines() if line.strip()]


def validate_path_regexes(value):
    """Return the error messages for the regexes, one per line.

    Santa uses NSRegularExpression (ICU syntax). Python's re is close enough to catch the typos.
    """
    errors = []
    for number, pattern in enumerate(split_path_regexes(value), start=1):
        try:
            re.compile(pattern)
        except re.error as e:
            errors.append(gettext("Line %(number)s (%(pattern)s): %(error)s") % {
                "number": number, "pattern": pattern, "error": e})
    return errors


def combine_path_regexes(value):
    """Santa only takes one regex per setting: combine the lines into one alternation, or return None."""
    patterns = split_path_regexes(value)
    if not patterns:
        return None
    if len(patterns) == 1:
        return patterns[0]
    return "|".join(f"(?:{pattern})" for pattern in patterns)
