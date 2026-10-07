"""Santa sync protocol: https://northpole.dev/development/sync-protocol/

Santa POSTs to <SyncBaseURL>/<stage>/<machine id>, SyncBaseURL being the group sync URL.
"""
import json
import logging
import re
import zlib

from django.conf import settings
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .events import store_events
from .models import RULE_COUNT_PREFIXES, ClientMode, Group, Machine, RemovableMediaAction, split_flags
from .rules import commit_sync_session, get_rule_batch

logger = logging.getLogger(__name__)

MACHINE_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
MAX_INT = 2147483647
# Santa overwrites its regexes with the synced ones, an empty value has to be a regex matching nothing
NON_MATCHING_REGEX = "(?!)"


class BadRequest(Exception):
    pass


def decompress(body, wbits):
    """Decompress with a size limit: a small body must not expand to gigabytes (decompression bomb)"""
    limit = settings.SYNC_MAX_DECOMPRESSED_BYTES
    decompressor = zlib.decompressobj(wbits)
    data = decompressor.decompress(body, limit + 1)
    if len(data) > limit or decompressor.unconsumed_tail:
        raise BadRequest(f"The decompressed body is bigger than {limit} bytes")
    return data


def read_json_body(request):
    body = request.body
    encoding = request.headers.get("Content-Encoding", "").lower()
    try:
        if encoding in ("deflate", "zlib"):
            body = decompress(body, zlib.MAX_WBITS)
        elif encoding == "gzip":
            body = decompress(body, 16 + zlib.MAX_WBITS)
        data = json.loads(body) if body else {}
    except (ValueError, zlib.error, OSError):
        raise BadRequest("Could not read the JSON body")
    if not isinstance(data, dict):
        raise BadRequest("The JSON body is not an object")
    return data


def client_ip(request):
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


def sync_view(func):
    @csrf_exempt
    @require_POST
    def wrapper(request, sync_token, machine_id):
        group = get_object_or_404(Group, sync_token=sync_token)
        if not MACHINE_ID_RE.match(machine_id):
            return JsonResponse({"error": "invalid machine id"}, status=400)
        try:
            data = read_json_body(request)
            with transaction.atomic():
                response = func(request, group, machine_id, data)
        except BadRequest as e:
            # the detail is only for the operators, it can name other groups
            logger.warning("Group %s, machine %s: %s", group.pk, machine_id, e)
            return JsonResponse({"error": "bad request"}, status=400)
        return JsonResponse(response)
    return wrapper


def get_machine(group, machine_id):
    try:
        machine = Machine.objects.select_for_update().select_related("group").get(machine_id=machine_id)
    except Machine.DoesNotExist:
        # every machine starts with a preflight
        raise BadRequest("Unknown machine")
    if machine.group_id != group.pk:
        raise BadRequest(f"Machine belongs to group {machine.group_id}")
    return machine


def clean_rule_count(value):
    if not isinstance(value, int) or value < 0:
        return 0
    return min(value, MAX_INT)


@sync_view
def preflight(request, group, machine_id, data):
    serial_number = (data.get("serial_num") or data.get("serial_number") or "").strip()
    if not serial_number:
        raise BadRequest("Missing serial number")

    fields = {
        "serial_number": serial_number[:64],
        "hostname": (data.get("hostname") or "")[:255],
        "model_identifier": (data.get("model_identifier") or "")[:64],
        "os_version": (data.get("os_version") or "")[:32],
        "os_build": (data.get("os_build") or "")[:32],
        "santa_version": (data.get("santa_version") or "")[:64],
        "primary_user": (data.get("primary_user") or "").strip()[:255],
        "client_mode": data.get("client_mode") if data.get("client_mode") in ClientMode.values else "",
        "last_ip": client_ip(request),
        "last_preflight_at": timezone.now(),
    }
    # Santa uses the protobuf JSON mapping, a missing count is a zero count
    for prefix in RULE_COUNT_PREFIXES:
        fields[f"{prefix}_rule_count"] = clean_rule_count(data.get(f"{prefix}_rule_count", 0))

    machine = Machine.objects.select_for_update().filter(machine_id=machine_id).first()
    clean_reason = None
    if machine is None:
        machine = Machine(machine_id=machine_id, group=group)
        clean_reason = "new machine"
    elif machine.group_id != group.pk:
        # moved to another group with a new configuration profile, rebuild its rules from scratch
        logger.info("Machine %s moved from group %s to %s", machine_id, machine.group_id, group.pk)
        machine.group = group
        clean_reason = "group change"
    elif data.get("request_clean_sync"):
        clean_reason = "requested by client"
    elif machine.clean_sync_requested:
        clean_reason = "requested by admin"
    elif machine.synced_rules and not any(fields[f"{p}_rule_count"] for p in RULE_COUNT_PREFIXES):
        clean_reason = "client lost its rules"

    for attr, value in fields.items():
        setattr(machine, attr, value)
    machine.pending_sync = None
    machine.sync_in_clean_mode = clean_reason is not None
    if clean_reason:
        machine.clean_sync_requested = False
        logger.info("Machine %s: clean sync (%s)", machine_id, clean_reason)
    machine.save()

    # a group based on another one: the settings of the parent, except the overridden ones
    group = group.effective()
    response = {
        "client_mode": group.client_mode,
        "batch_size": group.batch_size,
        "full_sync_interval": group.full_sync_interval,
        "allowed_path_regex": group.allowed_path_regex_for_santa() or NON_MATCHING_REGEX,
        "blocked_path_regex": group.blocked_path_regex_for_santa() or NON_MATCHING_REGEX,
        "enable_bundles": group.enable_bundles,
        "enable_transitive_rules": group.enable_transitive_rules,
        "enable_all_event_upload": group.enable_all_event_upload,
        "removable_media_policy": group.removable_media_policy(),
        # the same for Santa versions before removable_media_policy (they ignore the keys they don't know)
        "block_usb_mount": group.removable_media_action != RemovableMediaAction.ALLOW,
        "remount_usb_mode": (split_flags(group.removable_media_remount_flags)
                             if group.removable_media_action == RemovableMediaAction.REMOUNT else []),
    }
    if encrypted := group.encrypted_removable_media_policy():
        response["encrypted_removable_media_policy"] = encrypted
    # always sent: a cleared override must reach the Macs too
    response["override_file_access_action"] = group.override_file_access_action
    if group.event_detail_url:
        response["event_detail_url"] = group.event_detail_url
        if group.event_detail_text:
            response["event_detail_text"] = group.event_detail_text
    if santa_version_tuple(machine.santa_version) < (2024, 1):
        response["clean_sync"] = machine.sync_in_clean_mode
    else:
        response["sync_type"] = "CLEAN" if machine.sync_in_clean_mode else "NORMAL"
    return response


@sync_view
def eventupload(request, group, machine_id, data):
    machine = get_machine(group, machine_id)
    events = data.get("events") or []
    if not isinstance(events, list):
        raise BadRequest("events is not a list")
    store_events(machine, events)
    return {}


@sync_view
def ruledownload(request, group, machine_id, data):
    machine = get_machine(group, machine_id)
    rules, cursor = get_rule_batch(machine, data.get("cursor"))
    response = {"rules": rules}
    if cursor:
        response["cursor"] = cursor
    return response


@sync_view
def postflight(request, group, machine_id, data):
    machine = get_machine(group, machine_id)
    received = data.get("rules_received", 0)
    processed = data.get("rules_processed", 0)
    if isinstance(received, int) and isinstance(processed, int) and processed < received:
        logger.warning("Machine %s: received %s rules, processed only %s", machine_id, received, processed)
    commit_sync_session(machine)
    machine.last_postflight_at = timezone.now()
    machine.save(update_fields=["synced_rules", "pending_sync", "sync_in_clean_mode", "last_postflight_at"])
    return {}


def santa_version_tuple(version):
    parts = []
    for part in (version or "").split(".")[:2]:
        try:
            parts.append(int(part))
        except ValueError:
            break
    while len(parts) < 2:
        parts.append(0)
    return tuple(parts)
