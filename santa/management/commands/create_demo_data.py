import random
import uuid
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from santa.events import build_event
from santa.models import AccessRequest, ClientMode, Event, Group, Machine

# --delete only removes what carries all the marks of this command, not everything with a similar name:
# groups "Demo: …" with this description, Macs "DEMO-…" with a serial "DEMO…" and a demo owner, users of the
# reserved domain .invalid (RFC 2606, no real account has it) that are no superusers
GROUP_PREFIX = "Demo: "
DEMO_NOTE = "Demo data (create_demo_data)"
MACHINE_PREFIX = "DEMO-"
SERIAL_PREFIX = "DEMO"
USER_DOMAIN = "@demo.invalid"

GROUPS = [
    ("Engineering", ClientMode.MONITOR),
    ("Office", ClientMode.LOCKDOWN),
    ("Kiosk", ClientMode.LOCKDOWN),
]
USERS = ["anna", "ben", "clara", "david", "emma", "felix", "greta", "hannes"]
MODELS = ["Mac15,3", "Mac15,6", "Mac14,2", "Mac14,7", "Mac16,1", "Mac16,10", "Mac13,1"]
OS_VERSIONS = [("15.7.1", "24G231"), ("26.0.1", "25A362"), ("26.1", "25B78"), ("14.8.1", "23J30")]
SANTA_VERSIONS = ["2025.12", "2026.8", "2026.9", "2026.10"]

# name, bundle ID, Team ID, signing ID, path, version; Team ID "": unsigned
APPS = [
    ("Visual Studio Code", "com.microsoft.VSCode", "UBF8T346G9", "UBF8T346G9:com.microsoft.VSCode",
     "/Applications/Visual Studio Code.app/Contents/MacOS/Electron", "1.105.1"),
    ("Slack", "com.tinyspeck.slackmacgap", "BQR82RBBHL", "BQR82RBBHL:com.tinyspeck.slackmacgap",
     "/Applications/Slack.app/Contents/MacOS/Slack", "4.46.101"),
    ("Google Chrome", "com.google.Chrome", "EQHXZ8M8AV", "EQHXZ8M8AV:com.google.Chrome",
     "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", "141.0.7390.123"),
    ("Firefox", "org.mozilla.firefox", "43AQ936H96", "43AQ936H96:org.mozilla.firefox",
     "/Applications/Firefox.app/Contents/MacOS/firefox", "144.0.2"),
    ("Docker Desktop", "com.docker.docker", "9BNSXJN65R", "9BNSXJN65R:com.docker.docker",
     "/Applications/Docker.app/Contents/MacOS/Docker Desktop", "4.49.0"),
    ("Zoom", "us.zoom.xos", "BJ4HAAB9B3", "BJ4HAAB9B3:us.zoom.xos",
     "/Applications/zoom.us.app/Contents/MacOS/zoom.us", "6.6.6"),
    ("Spotify", "com.spotify.client", "2FNC3A47ZF", "2FNC3A47ZF:com.spotify.client",
     "/Applications/Spotify.app/Contents/MacOS/Spotify", "1.2.74"),
    ("Microsoft Teams", "com.microsoft.teams2", "UBF8T346G9", "UBF8T346G9:com.microsoft.teams2",
     "/Applications/Microsoft Teams.app/Contents/MacOS/MSTeams", "25275.2601"),
    ("1Password", "com.1password.1password", "2BUA8C4S2C", "2BUA8C4S2C:com.1password.1password",
     "/Applications/1Password.app/Contents/MacOS/1Password", "8.11.16"),
    ("Postman", "com.postmanlabs.mac", "H7H8Q7M5CK", "H7H8Q7M5CK:com.postmanlabs.mac",
     "/Applications/Postman.app/Contents/MacOS/Postman", "11.70.3"),
    ("", "", "", "", "/usr/local/bin/jq", ""),
    ("", "", "", "", "/Users/{user}/Downloads/install-helper", ""),
    ("", "", "", "", "/Users/{user}/go/bin/golangci-lint", ""),
    ("", "", "", "", "/opt/homebrew/Cellar/ripgrep/15.1.0/bin/rg", ""),
]
DECISIONS = ["BLOCK_UNKNOWN"] * 5 + ["ALLOW_UNKNOWN"] * 3 + ["BLOCK_BINARY", "BLOCK_TEAMID", "ALLOW_BINARY",
                                                             "ALLOW_TEAMID", "ALLOW_SIGNINGID"]
PARENTS = ["launchd", "zsh", "bash", "Finder", "Terminal"]
JUSTIFICATIONS = [
    "I need it for the project we start next week.",
    "Our customer sends the files in this format.",
    "The team uses it for the code reviews.",
    "Needed for the training on Thursday.",
]


def _hex(rng, length):
    return "".join(rng.choice("0123456789abcdef") for _ in range(length))


class Command(BaseCommand):
    help = ("Create demo data for development: groups, Macs, events, users and requests, no rules or package rules. "
            "Everything is marked (groups \"Demo: …\", Macs \"DEMO-…\", users \"…@demo.invalid\"); --delete removes "
            "it again")

    def add_arguments(self, parser):
        parser.add_argument("--macs", type=int, default=24, help="Number of Macs (default 24)")
        parser.add_argument("--events", type=int, default=400, help="Number of events (default 400)")
        parser.add_argument("--days", type=int, default=14, help="Spread the events over this many days (default 14)")
        parser.add_argument("--seed", type=int, help="Seed for the random values, for the same data every time")
        parser.add_argument("--delete", action="store_true", help="Delete the demo data instead")
        parser.add_argument("--force", action="store_true", help="Run without DEBUG too (not for production)")

    def handle(self, *args, **options):
        if not settings.DEBUG and not options["force"]:
            raise CommandError("Demo data is for development (DEBUG). Use --force to create it anyway.")
        if options["delete"]:
            self.delete()
            return
        if options["macs"] < 1 or options["events"] < 0 or options["days"] < 1:
            raise CommandError("--macs and --days need at least 1, --events at least 0.")
        rng = random.Random(options["seed"])
        with transaction.atomic():
            groups = self.create_groups()
            users = self.create_users()
            machines = self.create_machines(rng, groups, options["macs"])
            events = self.create_events(rng, machines, options["events"], options["days"])
            requests = self.create_requests(rng, users, events)
        self.stdout.write(f"{len(groups)} group(s), {len(machines)} Mac(s), {len(events)} event(s), "
                          f"{len(users)} user(s), {requests} request(s) created")

    def create_groups(self):
        groups = []
        for name, mode in GROUPS:
            group, _created = Group.objects.get_or_create(
                name=f"{GROUP_PREFIX}{name}",
                defaults={"client_mode": mode, "description": DEMO_NOTE})
            groups.append(group)
        return groups

    def create_users(self):
        User = get_user_model()
        users = []
        for name in USERS:
            user, created = User.objects.get_or_create(
                username=f"{name}{USER_DOMAIN}",
                defaults={"first_name": name.capitalize(), "last_name": "Demo", "email": f"{name}{USER_DOMAIN}"})
            if created:
                # no password: a demo account must not be a way in; set one in the admin to try the request form
                user.set_unusable_password()
                user.save(update_fields=["password"])
            users.append(user)
        return users

    def create_machines(self, rng, groups, count):
        now = timezone.now()
        start = Machine.objects.filter(machine_id__startswith=MACHINE_PREFIX).count()
        machines = []
        for number in range(start + 1, start + count + 1):
            os_version, os_build = rng.choice(OS_VERSIONS)
            user = rng.choice(USERS)
            # some Macs haven't synced for days, to fill the "not synced" filter
            last_sync = now - (timedelta(days=rng.randint(3, 20)) if rng.random() < 0.15
                               else timedelta(minutes=rng.randint(1, 600)))
            machine = Machine(
                machine_id=f"{MACHINE_PREFIX}{uuid.UUID(int=rng.getrandbits(128))}".upper(),
                serial_number=f"{SERIAL_PREFIX}{number:04d}{_hex(rng, 4).upper()}",
                hostname=f"mac-{user}-{number:02d}",
                model_identifier=rng.choice(MODELS),
                os_version=os_version,
                os_build=os_build,
                santa_version=rng.choice(SANTA_VERSIONS),
                primary_user=f"{user}{USER_DOMAIN}",
                group=rng.choice(groups),
                last_ip=f"10.20.{rng.randint(0, 255)}.{rng.randint(1, 254)}",
                last_preflight_at=last_sync,
                last_postflight_at=last_sync + timedelta(seconds=rng.randint(1, 30)),
                binary_rule_count=rng.randint(0, 40),
                teamid_rule_count=rng.randint(0, 15),
                signingid_rule_count=rng.randint(0, 25),
                transitive_rule_count=rng.randint(0, 5),
            )
            if rng.random() < 0.1:
                machine.client_mode_override = rng.choice(ClientMode.values)
                machine.client_mode_override_until = now + timedelta(days=rng.randint(1, 7))
            machines.append(machine)
        Machine.objects.bulk_create(machines)
        # not by their ids: SQL Server takes at most 2100 parameters per query
        return list(Machine.objects.filter(machine_id__startswith=MACHINE_PREFIX, created_at__gte=now)
                    .select_related("group"))

    def create_events(self, rng, machines, count, days):
        now = timezone.now()
        # the same binary has the same hash on every Mac: the lists group the events by it
        hashes = {app[4]: (_hex(rng, 64), _hex(rng, 40), _hex(rng, 64)) for app in APPS}
        events = []
        for _ in range(count):
            machine = rng.choice(machines)
            name, bundle_id, team_id, signing_id, path, version = rng.choice(APPS)
            sha256, cdhash, cert_sha256 = hashes[path]
            user = machine.primary_user.split("@", 1)[0]
            path = path.replace("{user}", user)
            executed = now - timedelta(seconds=rng.randint(0, days * 86400))
            raw = {
                "file_sha256": sha256,
                "file_path": path.rsplit("/", 1)[0],
                "file_name": path.rsplit("/", 1)[1],
                "executing_user": user,
                "execution_time": executed.timestamp(),
                "decision": rng.choice(DECISIONS),
                "parent_name": rng.choice(PARENTS),
                "pid": rng.randint(300, 99999),
                "ppid": rng.randint(1, 3000),
            }
            if team_id:
                raw.update({
                    "file_bundle_id": bundle_id, "file_bundle_name": name, "file_bundle_version_string": version,
                    "file_bundle_path": path.split(".app/", 1)[0] + ".app",
                    "signing_id": signing_id, "team_id": team_id, "cdhash": cdhash,
                    "signing_chain": [
                        {"sha256": cert_sha256, "cn": f"Developer ID Application: {name} ({team_id})",
                         "org": name, "ou": team_id},
                        {"sha256": _hex(rng, 64), "cn": "Developer ID Certification Authority",
                         "org": "Apple Inc.", "ou": "Apple Certification Authority"},
                    ],
                })
            events.append(build_event(machine, raw))
        Event.objects.bulk_create(events, batch_size=100)
        # SQL Server returns no primary keys from bulk_create: the requests need the saved events
        return list(Event.objects.filter(machine__machine_id__startswith=MACHINE_PREFIX, created_at__gte=now)
                    .select_related("machine"))

    def create_requests(self, rng, users, events):
        """Requests for some blocked events, by the users of their Macs, in every status"""
        by_username = {user.get_username(): user for user in users}
        blocked = [event for event in events if event.is_blocked and event.machine.primary_user in by_username]
        rng.shuffle(blocked)
        created = 0
        seen = set()
        for event in blocked:
            if created >= 12:
                break
            key = (event.machine_id, event.file_sha256)
            if key in seen:
                continue
            seen.add(key)
            status = rng.choice([AccessRequest.Status.PENDING] * 3
                                + [AccessRequest.Status.APPROVED, AccessRequest.Status.DENIED])
            AccessRequest.objects.create(
                requester=by_username[event.machine.primary_user],
                kind=AccessRequest.Kind.EVENT,
                event=event,
                machine=event.machine,
                file_sha256=event.file_sha256,
                title=event.bundle_name or event.file_name,
                justification=rng.choice(JUSTIFICATIONS),
                status=status,
                decided_at=None if status == AccessRequest.Status.PENDING else timezone.now(),
            )
            created += 1
        for name, link in [("Figma", "https://www.figma.com/downloads/"),
                           ("Wireshark", "https://www.wireshark.org/download.html")]:
            AccessRequest.objects.create(
                requester=rng.choice(users), kind=AccessRequest.Kind.OTHER, title=name,
                justification=rng.choice(JUSTIFICATIONS), link=link)
            created += 1
        return created

    def delete(self):
        User = get_user_model()
        with transaction.atomic():
            users = User.objects.filter(username__endswith=USER_DOMAIN, is_superuser=False)
            # requests protect their requester; the events go with their Macs
            requests = AccessRequest.objects.filter(requester__in=users).delete()[1].get("santa.AccessRequest", 0)
            machines = Machine.objects.filter(machine_id__startswith=MACHINE_PREFIX,
                                              serial_number__startswith=SERIAL_PREFIX,
                                              primary_user__endswith=USER_DOMAIN)
            events = Event.objects.filter(machine__in=machines).delete()[1].get("santa.Event", 0)
            machine_count = machines.delete()[1].get("santa.Machine", 0)
            user_count = users.delete()[1].get(User._meta.label, 0)
            # a demo group someone gave real Macs or rules stays
            groups = Group.objects.filter(name__startswith=GROUP_PREFIX, description=DEMO_NOTE, machines=None,
                                          rules=None, release_sources=None, file_access_rules=None, children=None)
            group_count = groups.delete()[1].get("santa.Group", 0)
        self.stdout.write(f"{group_count} group(s), {machine_count} Mac(s), {events} event(s), {user_count} user(s), "
                          f"{requests} request(s) deleted")
