import plistlib

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from santa.models import Group
from santa.profiles import base_profile, group_profile


class ProfileTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.group = Group.objects.create(name="Entwicklung", unknown_block_message="Ask the IT for access.")

    def test_group_profile(self):
        profile = plistlib.loads(group_profile(self.group))
        self.assertEqual(profile["PayloadType"], "Configuration")
        self.assertEqual(profile["PayloadScope"], "System")
        payload, = profile["PayloadContent"]
        self.assertEqual(payload["PayloadType"], "com.northpolesec.santa")
        self.assertEqual(payload["SyncBaseURL"], self.group.sync_base_url)
        self.assertIs(payload["SyncEnableProtoTransfer"], False)
        # not set by default: every MDM has its own variable for the user
        self.assertNotIn("MachineOwner", payload)
        with self.settings(SANTA_PROFILE_MACHINE_OWNER="{{userprincipalname}}"):
            payload, = plistlib.loads(group_profile(self.group))["PayloadContent"]
        self.assertEqual(payload["MachineOwner"], "{{userprincipalname}}")
        self.assertEqual(payload["UnknownBlockMessage"], "Ask the IT for access.")
        # the server settings are never in the profile
        for key in ("ClientMode", "AllowedPathRegex", "BlockedPathRegex", "FullSyncInterval", "BannedBlockMessage",
                    "EnableBadSignatureProtection"):
            self.assertNotIn(key, payload)

    def test_profile_only_keys(self):
        self.group.on_start_usb_options = "ForceUnmount"
        self.group.branding_company_name = "Example Corp"
        self.group.branding_company_logo = "file:///Library/Example/logo.png"
        self.group.branding_company_logo_dark = "data:image/png;base64,iVBORw0KGgo="
        self.group.removable_media_action = "BLOCK"
        payload, = plistlib.loads(group_profile(self.group))["PayloadContent"]
        self.assertEqual(payload["OnStartUSBOptions"], "ForceUnmount")
        self.assertEqual(payload["BrandingCompanyName"], "Example Corp")
        self.assertEqual(payload["BrandingCompanyLogo"], "file:///Library/Example/logo.png")
        self.assertEqual(payload["BrandingCompanyLogoDark"], "data:image/png;base64,iVBORw0KGgo=")
        # the sync sends the removable media settings
        for key in ("RemovableMediaAction", "BlockUSBMount", "RemountUSBMode"):
            self.assertNotIn(key, payload)

    def test_group_profile_identifiers_are_stable(self):
        first = plistlib.loads(group_profile(self.group))
        self.group.sync_token = "new-token"
        second = plistlib.loads(group_profile(self.group))
        self.assertEqual(first["PayloadUUID"], second["PayloadUUID"])
        self.assertEqual(first["PayloadContent"][0]["PayloadIdentifier"],
                         second["PayloadContent"][0]["PayloadIdentifier"])
        self.assertIn("new-token", second["PayloadContent"][0]["SyncBaseURL"])
        other = plistlib.loads(group_profile(Group.objects.create(name="Vertrieb")))
        self.assertNotEqual(first["PayloadUUID"], other["PayloadUUID"])

    def test_base_profile(self):
        profile = plistlib.loads(base_profile())
        payloads = {p["PayloadType"]: p for p in profile["PayloadContent"]}
        self.assertEqual(set(payloads), {
            "com.apple.system-extension-policy", "com.apple.TCC.configuration-profile-policy",
            "com.apple.servicemanagement", "com.apple.notificationsettings",
        })
        self.assertEqual(payloads["com.apple.system-extension-policy"]["AllowedSystemExtensions"],
                         {"ZMCG7MLDV9": ["com.northpolesec.santa.daemon"]})
        tcc = payloads["com.apple.TCC.configuration-profile-policy"]["Services"]["SystemPolicyAllFiles"]
        self.assertEqual([e["Identifier"] for e in tcc],
                         ["com.northpolesec.santa.daemon", "com.northpolesec.santa.bundleservice"])
        self.assertTrue(tcc[0]["CodeRequirement"].endswith("certificate leaf[subject.OU] = ZMCG7MLDV9"))
        uuids = [profile["PayloadUUID"]] + [p["PayloadUUID"] for p in profile["PayloadContent"]]
        self.assertEqual(len(set(uuids)), len(uuids))

    def test_downloads(self):
        self.client.force_login(User.objects.create_superuser("admin", "admin@example.com", "pw"))
        response = self.client.get(reverse("admin:santa_group_mobileconfig", args=(self.group.pk,)))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/x-apple-aspen-config")
        self.assertIn('filename="santa-entwicklung.mobileconfig"', response["Content-Disposition"])
        payload = plistlib.loads(response.content)["PayloadContent"][0]
        self.assertEqual(payload["SyncBaseURL"], self.group.sync_base_url)
        response = self.client.get(reverse("admin:santa_group_base_mobileconfig"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(self.client.get(reverse("admin:santa_group_changelist")), "Download base profile")

    def test_download_needs_permission(self):
        self.client.force_login(User.objects.create_user("jdoe", password="pw", is_staff=True))
        response = self.client.get(reverse("admin:santa_group_mobileconfig", args=(self.group.pk,)))
        self.assertEqual(response.status_code, 403)
