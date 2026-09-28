import logging
from datetime import datetime, timezone

from .models import Event

logger = logging.getLogger(__name__)


def _text(event, key, max_length):
    value = event.get(key)
    if value is None:
        return ""
    return str(value)[:max_length]


def _execution_time(event):
    try:
        return datetime.fromtimestamp(float(event["execution_time"]), tz=timezone.utc)
    except (KeyError, TypeError, ValueError, OverflowError):
        return datetime.now(tz=timezone.utc)


def build_event(machine, event):
    signing_chain = event.get("signing_chain") or []
    leaf = signing_chain[0] if signing_chain and isinstance(signing_chain[0], dict) else {}
    return Event(
        machine=machine,
        group=machine.group,
        decision=_text(event, "decision", 64) or "UNKNOWN",
        execution_time=_execution_time(event),
        file_sha256=_text(event, "file_sha256", 64).lower(),
        file_path=_text(event, "file_path", 1024),
        file_name=_text(event, "file_name", 255),
        executing_user=_text(event, "executing_user", 255),
        parent_name=_text(event, "parent_name", 255),
        bundle_id=_text(event, "file_bundle_id", 255),
        bundle_name=_text(event, "file_bundle_name", 255),
        bundle_version=_text(event, "file_bundle_version_string", 128) or _text(event, "file_bundle_version", 128),
        signing_id=_text(event, "signing_id", 255),
        team_id=_text(event, "team_id", 32),
        cdhash=_text(event, "cdhash", 64).lower(),
        cert_sha256=str(leaf.get("sha256") or "")[:64].lower(),
        cert_cn=str(leaf.get("cn") or "")[:255],
        raw=event,
    )


def store_events(machine, events):
    """Store the uploaded events. Santa uploads them again if a previous upload failed, skip the known ones."""
    new_events = [build_event(machine, e) for e in events if isinstance(e, dict)]
    if not new_events:
        return []
    known = set(
        Event.objects.filter(
            machine=machine,
            execution_time__gte=min(e.execution_time for e in new_events),
            execution_time__lte=max(e.execution_time for e in new_events),
        ).values_list("file_sha256", "execution_time")
    )
    to_create = []
    for event in new_events:
        key = (event.file_sha256, event.execution_time)
        if key not in known:
            known.add(key)
            to_create.append(event)
    Event.objects.bulk_create(to_create, batch_size=100)
    logger.info("Machine %s: %s event(s) stored, %s duplicate(s)",
                machine.machine_id, len(to_create), len(new_events) - len(to_create))
    return to_create
