"""The CEL expression of "Only signing IDs starting with": a Team ID rule that allows only some binaries of the team
(Santa has no wildcards). Written by the console, read back to show and extend it, and evaluated like Santa does."""
import re

from .models import Policy, RuleType

SIGNING_PREFIX_RE = re.compile(r"^[A-Za-z0-9._-]+$")
PREFIX_EXPRESSION_RE = re.compile(
    r'^\(target\.signing_id\.startsWith\("[A-Z0-9]{10}:[A-Za-z0-9._-]+"\)'
    r'(?: \|\| target\.signing_id\.startsWith\("[A-Z0-9]{10}:[A-Za-z0-9._-]+"\))*\) \? ALLOWLIST : BLOCKLIST$')


def prefix_expression(team_id, prefixes):
    tests = " || ".join(f'target.signing_id.startsWith("{team_id}:{prefix}")' for prefix in prefixes)
    return f"({tests}) ? ALLOWLIST : BLOCKLIST"


def parse_prefix_expression(expression):
    """The prefixes of an expression written by prefix_expression(), else None"""
    if not PREFIX_EXPRESSION_RE.match(expression or ""):
        return None
    return re.findall(r'startsWith\("[A-Z0-9]{10}:([A-Za-z0-9._-]+)"\)', expression)


def rule_prefixes(rule):
    """The prefixes of a Team ID rule with such an expression, else None (another rule, an expression by hand)"""
    if rule.policy != Policy.CEL or rule.rule_type != RuleType.TEAMID:
        return None
    return parse_prefix_expression(rule.cel_expr)


def prefix_decision(rule, signing_id):
    """True: the expression of the rule allows the signing ID, False: it blocks it, None: not known (no prefix rule)"""
    prefixes = rule_prefixes(rule)
    if prefixes is None:
        return None
    return any((signing_id or "").startswith(f"{rule.identifier}:{prefix}") for prefix in prefixes)


def signing_prefix(rule, signing_id):
    """The prefix that adds the signing ID to the rule (its part after "TEAMID:"), None if it can't be added"""
    team, _, prefix = (signing_id or "").partition(":")
    if team != rule.identifier or not SIGNING_PREFIX_RE.match(prefix):
        return None
    return prefix
