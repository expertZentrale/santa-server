import ast
import gettext
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from django.conf import settings
from django.test import override_settings
from django.urls import reverse

from santa.models import Event, Rule, RuleType, UserProfile
from santa.users import TIME_ZONE_COOKIE, gravatar_url, profile_for

from .test_console import SHA_A, SHA_B, ConsoleBase

LOCALE = Path(settings.BASE_DIR) / "santa" / "locale" / "de" / "LC_MESSAGES"


class ProfileTestCase(ConsoleBase):
    def test_profile_page_and_preferences(self):
        response = self.client.get(reverse("profile"))
        self.assertContains(response, 'data-theme="auto"')
        self.assertContains(response, "gravatar.com/avatar/")
        response = self.client.post(reverse("profile"), {"theme": "dark", "language": "de"}, follow=True)
        self.assertContains(response, 'data-theme="dark"')
        self.assertContains(response, 'lang="de"')
        self.assertContains(response, "Einstellungen gespeichert.")
        self.assertContains(self.client.get(reverse("console:rules")), "Ausführungsregeln")
        self.assertEqual((profile_for(self.admin).theme, UserProfile.objects.get(user=self.admin).language),
                         ("dark", "de"))

    def test_quick_preferences_from_the_menu(self):
        response = self.client.post(reverse("set_preference"), {"theme": "light", "next": "/console/rules/"})
        self.assertEqual(response.url, "/console/rules/")
        response = self.client.post(reverse("set_preference"), {"language": "en", "next": "https://evil.example/"})
        self.assertEqual(response.url, reverse("home"))
        self.client.post(reverse("set_preference"), {"theme": "neon", "language": "xx"})
        user_profile = UserProfile.objects.get(user=self.admin)
        self.assertEqual((user_profile.theme, user_profile.language), ("light", "en"))

    def test_users_have_a_profile_too(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(reverse("profile")).status_code, 200)
        response = self.client.get(reverse("requests:list"))
        self.assertNotContains(response, "Execution rules")
        self.assertContains(response, "Profile and preferences")

    def test_gravatar(self):
        self.admin.email = " Admin@Example.com "
        digest = hashlib.sha256(b"admin@example.com").hexdigest()
        self.assertIn(f"/avatar/{digest}?s=64&d=mp", gravatar_url(self.admin, 32))


class LanguageTestCase(ConsoleBase):
    def test_language_of_the_browser(self):
        response = self.client.get(reverse("console:events"), headers={"accept-language": "de-DE,de;q=0.9"})
        self.assertContains(response, "Blockierte Apps")
        self.assertContains(response, "Meine Anfragen")

    @override_settings(LANGUAGE_CODE="de")
    def test_default_language_of_the_server(self):
        self.assertContains(self.client.get(reverse("console:machines")), "Seit 2 Tagen nicht synchronisiert")

    def test_profile_wins_over_the_browser(self):
        profile_for(self.admin)
        UserProfile.objects.filter(user=self.admin).update(language="en")
        response = self.client.get(reverse("console:events"), headers={"accept-language": "de"})
        self.assertContains(response, "Blocked apps")

    def test_german_validation_messages(self):
        UserProfile.objects.create(user=self.admin, language="de")
        response = self.client.post(reverse("console:rule_add"), {
            "rule_type": RuleType.BINARY, "identifier": "nope", "policy": "ALLOWLIST", "is_global": "on"})
        self.assertContains(response, "Muss ein SHA-256-Hexwert sein")

    def test_compiled_translations_are_up_to_date(self):
        """The .mo is committed: it must have every translation of the .po (compilemessages after a change)"""
        with open(LOCALE / "django.mo", "rb") as f:
            catalog = gettext.GNUTranslations(f)._catalog
        # plurals: (msgid, 0) is the singular form
        compiled = {key[0] if isinstance(key, tuple) else key: value for key, value in catalog.items()
                    if not isinstance(key, tuple) or key[1] == 0}
        expected = po_translations(LOCALE / "django.po")
        self.assertGreater(len(expected), 400)
        stale = [msgid for msgid, msgstr in expected.items() if compiled.get(msgid) != msgstr and msgstr]
        self.assertEqual(stale, [])
        self.assertEqual([msgid for msgid, msgstr in expected.items() if not msgstr], [], "untranslated")


def po_translations(path):
    """{msgid: msgstr (the singular form for plurals)} of a .po file"""
    entries, current, field = {}, {}, None
    for line in path.read_text(encoding="utf-8").splitlines() + [""]:
        if line.startswith(("msgid ", "msgid_plural ", "msgstr ", "msgstr[0] ", "msgstr[1] ")):
            field, _, value = line.partition(" ")
            current[field] = ast.literal_eval(value)
        elif line.startswith('"') and field:
            current[field] += ast.literal_eval(line)
        elif not line.strip():
            if current.get("msgid"):
                entries[current["msgid"]] = current.get("msgstr", current.get("msgstr[0]", ""))
            current, field = {}, None
    return entries


class TimeZoneTestCase(ConsoleBase):
    def setUp(self):
        super().setUp()
        event = self.make_event(file_name="timed")
        Event.objects.filter(pk=event.pk).update(execution_time=datetime(2026, 9, 29, 11, 4, tzinfo=UTC))

    def events_page(self):
        return self.client.get(reverse("console:events"), {"view": "all", "days": "", "resolved": "all"})

    @override_settings(TIME_ZONE="UTC")
    def test_time_zone_of_the_browser(self):
        self.assertContains(self.events_page(), "29.09.2026 11:04")
        self.client.cookies[TIME_ZONE_COOKIE] = "Europe/Berlin"
        self.assertContains(self.events_page(), "29.09.2026 13:04")
        # an unknown name is ignored
        self.client.cookies[TIME_ZONE_COOKIE] = "Mars/Olympus"
        self.assertContains(self.events_page(), "29.09.2026 11:04")

    def test_profile_wins_over_the_browser(self):
        self.client.cookies[TIME_ZONE_COOKIE] = "Europe/Berlin"
        self.client.post(reverse("profile"), {"theme": "auto", "language": "", "time_zone": "America/New_York"})
        self.assertEqual(profile_for(self.admin).time_zone, "America/New_York")
        self.assertContains(self.events_page(), "29.09.2026 07:04")
        self.assertContains(self.client.get(reverse("profile")), "Times are shown in America/New_York.")


class SortingTestCase(ConsoleBase):
    def test_sort_rules(self):
        Rule.objects.create(rule_type=RuleType.BINARY, identifier=SHA_B, is_global=True)
        Rule.objects.create(rule_type=RuleType.BINARY, identifier=SHA_A, is_global=True)
        response = self.client.get(reverse("console:rules"), {"sort": "identifier"})
        self.assertEqual([rule.identifier for rule in response.context["page"]], [SHA_A, SHA_B])
        self.assertContains(response, 'aria-sort="ascending"')
        response = self.client.get(reverse("console:rules"), {"sort": "-identifier"})
        self.assertEqual([rule.identifier for rule in response.context["page"]], [SHA_B, SHA_A])
        # unknown columns fall back to the default order
        self.assertEqual(self.client.get(reverse("console:rules"), {"sort": "password"}).context["sort"], "-created")

    def test_sort_blocked_apps_and_live_only_in_time_order(self):
        self.make_event()
        self.make_event(machine=self.other_machine)
        self.make_event(sha256=SHA_B, file_name="other")
        response = self.client.get(reverse("console:events"), {"sort": "-blocks"})
        self.assertEqual([row["event_count"] for row in response.context["page"]], [2, 1])
        response = self.client.get(reverse("console:events"), {"view": "all", "sort": "file"})
        self.assertFalse(response.context["live"])
        self.assertTrue(self.client.get(reverse("console:events"), {"view": "all"}).context["live"])
        for name in ("sources", "machines", "groups", "requests"):
            self.assertEqual(self.client.get(reverse(f"console:{name}"), {"sort": "-name"}).status_code, 200)


class RulePreviewTestCase(ConsoleBase):
    def test_the_rule_type_select_knows_the_identifiers(self):
        event = self.make_event(signing_id="ABCDE12345:com.example.tool", team_id="ABCDE12345")
        response = self.client.get(reverse("console:event", args=(event.pk,)), HTTP_HX_REQUEST="true")
        identifiers = json.loads(response.context["form"].fields["rule_type"].widget.attrs["data-identifiers"])
        self.assertEqual(identifiers, {"SIGNINGID": "ABCDE12345:com.example.tool", "BINARY": SHA_A,
                                       "TEAMID": "ABCDE12345"})
        self.assertContains(response, "data-identifiers=")
