"""Configuration profiles (.mobileconfig) to upload to the MDM (Intune, Jamf Pro, Kandji, …) as custom profiles.

- the base profile, the same for every Mac: system extension, full disk access, background item, notifications.
  The payloads are the ones documented by North Pole Security (https://northpole.dev/deployment/).
- one Santa configuration profile per group, with its SyncBaseURL.

The identifiers and UUIDs are derived from the group, so a downloaded profile replaces the previous one in the MDM.
"""
import plistlib
import uuid

from django.conf import settings

SANTA_TEAM_ID = "ZMCG7MLDV9"
PROFILE_NAMESPACE = uuid.UUID("9b4f1f3e-6a57-4a0c-9d59-3b8f1d0c5a21")


def _code_requirement(identifier):
    return (f'identifier "{identifier}" and anchor apple generic and certificate 1[field.1.2.840.113635.100.6.2.6] '
            f"/* exists */ and certificate leaf[field.1.2.840.113635.100.6.1.13] /* exists */ "
            f"and certificate leaf[subject.OU] = {SANTA_TEAM_ID}")


def _uuid(name):
    return str(uuid.uuid5(PROFILE_NAMESPACE, name)).upper()


def _payload(payload_type, name, display_name, content):
    identifier = f"{settings.SANTA_PROFILE_IDENTIFIER_PREFIX}.{name}"
    return {
        "PayloadType": payload_type,
        "PayloadIdentifier": identifier,
        "PayloadUUID": _uuid(identifier),
        "PayloadDisplayName": display_name,
        "PayloadVersion": 1,
        "PayloadEnabled": True,
        **content,
    }


def _profile(name, display_name, description, payloads):
    identifier = f"{settings.SANTA_PROFILE_IDENTIFIER_PREFIX}.{name}"
    profile = {
        "PayloadType": "Configuration",
        "PayloadIdentifier": identifier,
        "PayloadUUID": _uuid(identifier),
        "PayloadDisplayName": display_name,
        "PayloadDescription": description,
        "PayloadOrganization": settings.SANTA_PROFILE_ORGANIZATION,
        "PayloadVersion": 1,
        "PayloadScope": "System",
        "PayloadRemovalDisallowed": True,
        "PayloadContent": payloads,
    }
    return plistlib.dumps(profile, fmt=plistlib.FMT_XML, sort_keys=False)


def base_profile():
    tcc_entries = [
        {
            "Allowed": True,
            "CodeRequirement": _code_requirement(identifier),
            "Comment": "",
            "Identifier": identifier,
            "IdentifierType": "bundleID",
            "StaticCode": False,
        }
        for identifier in ("com.northpolesec.santa.daemon", "com.northpolesec.santa.bundleservice")
    ]
    payloads = [
        _payload("com.apple.system-extension-policy", "base.system-extension", "Santa: system extension", {
            "AllowedSystemExtensions": {SANTA_TEAM_ID: ["com.northpolesec.santa.daemon"]},
            "AllowedSystemExtensionTypes": {SANTA_TEAM_ID: ["EndpointSecurityExtension"]},
            "NonRemovableSystemExtensions": {SANTA_TEAM_ID: ["com.northpolesec.santa.daemon"]},
        }),
        _payload("com.apple.TCC.configuration-profile-policy", "base.tcc", "Santa: full disk access", {
            "Services": {"SystemPolicyAllFiles": tcc_entries},
        }),
        _payload("com.apple.servicemanagement", "base.background", "Santa: background item", {
            "Rules": [{"RuleType": "TeamIdentifier", "RuleValue": SANTA_TEAM_ID}],
        }),
        _payload("com.apple.notificationsettings", "base.notifications", "Santa: notifications", {
            "NotificationSettings": [{
                "AlertType": 1,
                "BadgesEnabled": True,
                "BundleIdentifier": "com.northpolesec.santa",
                "CriticalAlertEnabled": True,
                "NotificationsEnabled": True,
                "ShowInLockScreen": True,
                "ShowInNotificationCenter": True,
                "SoundsEnabled": False,
            }],
        }),
    ]
    return _profile("base", "Santa: base", "Santa system extension, full disk access, background item and "
                                           "notifications. Same for every Mac.", payloads)


def group_configuration(group):
    """The keys of the Santa payload for the group.

    Everything the sync server sets (mode, regexes, USB, …) is left out on purpose: the server is the only source.
    """
    config = {
        "SyncBaseURL": group.sync_base_url,
        # this server only speaks the JSON sync protocol
        "SyncEnableProtoTransfer": False,
    }
    if settings.SANTA_PROFILE_MACHINE_OWNER:
        # a variable the MDM fills in with the primary user: Santa reports it in the preflight, the request form
        # finds the Macs of the signed-in user with it
        config["MachineOwner"] = settings.SANTA_PROFILE_MACHINE_OWNER
    if group.unknown_block_message:
        config["UnknownBlockMessage"] = group.unknown_block_message
    if group.banned_block_message:
        config["BannedBlockMessage"] = group.banned_block_message
    if group.enable_bad_signature_protection:
        config["EnableBadSignatureProtection"] = True
    return config


def group_profile(group):
    name = f"config.group-{group.pk}"
    payload = _payload("com.northpolesec.santa", name + ".santa", f"Santa: {group.name}", group_configuration(group))
    return _profile(name, f"Santa: {group.name}", f"Santa configuration of the group {group.name}. "
                                                  "Contains the secret sync URL of the group.", [payload])
