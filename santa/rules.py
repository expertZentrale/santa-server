import logging

from .models import POLICY_PRECEDENCE, Rule, payload_hash

logger = logging.getLogger(__name__)

GLOBAL, GROUP, MACHINE = 0, 1, 2


def effective_rule_objects(machine):
    """Return {santa_key: (scope level, rule)} for the rules that apply to the machine.

    One Santa rule per identifier: the most specific scope wins, then the strictest policy.
    """
    enabled = Rule.objects.filter(is_enabled=True)
    scoped_querysets = (
        (GLOBAL, enabled.filter(is_global=True)),
        (GROUP, enabled.filter(groups=machine.group_id)),
        (MACHINE, enabled.filter(machines=machine)),
    )
    chosen = {}
    for level, qs in scoped_querysets:
        for rule in qs:
            rank = (level, POLICY_PRECEDENCE[rule.policy], rule.pk)
            key = rule.santa_key
            if key not in chosen or rank > chosen[key][0]:
                chosen[key] = (rank, rule)
    return {key: (rank[0], rule) for key, (rank, rule) in chosen.items()}


def effective_rules(machine):
    """Return {santa_key: santa rule payload} for the rules that apply to the machine."""
    return {key: rule.to_santa() for key, (_, rule) in effective_rule_objects(machine).items()}


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
    batch_size = machine.group.batch_size
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
