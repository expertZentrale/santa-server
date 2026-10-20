import logging

from .models import POLICY_PRECEDENCE, Rule, payload_hash, unexpired

logger = logging.getLogger(__name__)

GLOBAL, PARENT, GROUP, MACHINE = 0, 1, 2, 3


# The rules of a Mac are read as tuples of these fields, the first four always these: the package rules are tens of
# thousands of rows, a model each took most of the time of a sync (and of the rules of a Mac in the console)
KEY_FIELDS = ("pk", "rule_type", "identifier", "policy")
# what the sync sends (Rule.santa_payload)
SYNC_FIELDS = (*KEY_FIELDS, "custom_msg", "custom_url", "cel_expr")
# what the console sorts and searches the rules of a Mac by
LIST_FIELDS = (*KEY_FIELDS, "description", "created_at")


def _scoped_querysets(machine):
    # an expired rule is no longer sent before the clean up deletes it
    enabled = Rule.objects.filter(unexpired(), is_enabled=True)
    group = machine.group
    scoped_querysets = [
        (GLOBAL, enabled.filter(is_global=True)),
        (GROUP, enabled.filter(groups=group.pk)),
        (MACHINE, enabled.filter(machines=machine)),
    ]
    if group.parent_id and group.inherit_rules:
        scoped_querysets.insert(1, (PARENT, enabled.filter(groups=group.parent_id)))
    return scoped_querysets


def effective_rule_values(machine, fields=SYNC_FIELDS):
    """Return {santa_key: (scope level, row)} for the rules that apply to the machine; row: the tuple of the fields
    (starting with KEY_FIELDS).

    One Santa rule per identifier: the most specific scope wins (Mac, group, parent group, global), then the
    strictest policy. A group based on another one can so allow what its parent blocks.
    """
    chosen = {}
    for level, qs in _scoped_querysets(machine):
        for row in qs.values_list(*fields):
            rank = (level, POLICY_PRECEDENCE[row[3]], row[0])
            key = f"{row[1]}:{row[2]}"
            if key not in chosen or rank > chosen[key][0]:
                chosen[key] = (rank, row)
    return {key: (rank[0], row) for key, (rank, row) in chosen.items()}


def effective_rule_objects(machine):
    """Return {santa_key: (scope level, rule)}: effective_rule_values() with the rules as models"""
    values = effective_rule_values(machine, KEY_FIELDS)
    rules = Rule.objects.in_bulk([row[0] for _level, row in values.values()])
    return {key: (level, rules[row[0]]) for key, (level, row) in values.items()}


def effective_rules(machine):
    """Return {santa_key: santa rule payload} for the rules that apply to the machine."""
    return {key: Rule.santa_payload(*row[1:]) for key, (_level, row) in effective_rule_values(machine).items()}


def build_sync_session(machine):
    """Compute the rules to send, and the ledger to commit once the client confirms them."""
    desired = effective_rules(machine)
    desired_hashes = {key: payload_hash(payload) for key, payload in desired.items()}
    if machine.sync_in_clean_mode:
        to_send = [desired[key] for key in sorted(desired)]
    else:
        synced = machine.synced_rules or {}
        to_send = [desired[key] for key in sorted(desired) if synced.get(key) != desired_hashes[key]]
        for key in sorted(set(synced) - set(desired)):
            rule_type, identifier = key.split(":", 1)
            to_send.append({"identifier": identifier, "rule_type": rule_type, "policy": "REMOVE"})
    return {"rules": to_send, "ledger": desired_hashes}


def get_rule_batch(machine, cursor):
    """Return (rules, next cursor) and persist the session when a new download starts."""
    session = machine.pending_sync
    offset = 0
    if cursor is not None and session is not None:
        try:
            offset = int(cursor)
        except (TypeError, ValueError):
            logger.warning("Machine %s: invalid rule download cursor %r", machine.machine_id, cursor)
            session = None
    else:
        session = None
    if session is None:
        offset = 0
        session = build_sync_session(machine)
        machine.pending_sync = session
        machine.save(update_fields=["pending_sync"])
    batch_size = machine.group.effective().batch_size
    rules = session["rules"][offset:offset + batch_size]
    next_offset = offset + batch_size
    next_cursor = str(next_offset) if next_offset < len(session["rules"]) else None
    return rules, next_cursor


def commit_sync_session(machine):
    session = machine.pending_sync
    if session is None:
        return
    if machine.sync_in_clean_mode and not session["rules"]:
        # Santa skips the clean up when it receives no rule at all: the client still has its old rules.
        # Keep the old ledger, the next normal sync sends the removals.
        pass
    else:
        machine.synced_rules = session["ledger"]
    machine.pending_sync = None
    machine.sync_in_clean_mode = False
