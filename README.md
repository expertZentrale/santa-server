# Santa Server

A sync server for [Santa](https://northpole.dev), the binary authorization system for macOS: it tells the Macs
which programs may run, collects what they block, and lets people ask for software.

- **Groups**: each group of Macs has its own Santa configuration (monitor / lockdown, path regexes, USB, …) and
  its own secret **sync URL**. The server generates the configuration profiles for your MDM.
- **Rules** for binaries, certificates, Team IDs, Signing IDs and CDHashes, scoped globally, per group or per Mac,
  with allow, block and CEL policies.
- **Events**: blocked (and, in monitor mode, would-be-blocked) executions, grouped by app and updated live.
  Select them and create the rules in one step, each binary with its own rule type, policy, scope and tags.
- **Package rules**: watch GitHub releases, Homebrew formulae and casks, npm packages, VS Code extensions,
  JetBrains plugins or a URL. The executables of every new release are downloaded, hashed and turned into rules,
  optionally only after a delay.
- **Access requests**: users sign in with your identity provider (OpenID Connect) and ask for a program blocked on
  their Mac, one or more packages, or anything else. Administrators approve or deny in the console.
- **Upload** a binary or an archive to allow or block it. The hashes and code signature are read from the file.
- English and German, light and dark theme, works on phones.

It is a plain Django 6.0 application: a server-rendered console with htmx (no frontend build) plus the Django admin,
Microsoft SQL Server through mssql-django, Redis for the cache and the sessions, gunicorn with WhiteNoise.

## Quick start

```bash
docker compose up --build                  # the image, SQL Server and Redis (docker-compose.yml)
docker compose exec web python manage.py createsuperuser
```

Open <http://localhost:8000/>. The compose file is an example with fixed passwords, for trying it out only.

## Configuration

The image (`santa_server/settings.py`) is configured with environment variables:

| Variable | Required | Description |
|---|---|---|
| `SECRET_KEY` | yes | Django secret key, long and random |
| `DB_HOST`, `DB_USER`, `DB_PASSWORD` | yes | SQL Server (2019 or later). The database must exist. |
| `DB_PORT`, `DB_NAME` | | default `1433`, `santa` |
| `DB_EXTRA_PARAMS` | | ODBC connection options, e.g. `Encrypt=yes;TrustServerCertificate=no` |
| `REDIS_URL` | yes | e.g. `redis://user:password@redis:6379/0` (cache, sessions, lock of the scheduled job) |
| `CACHE_KEY_PREFIX` | | default `santa`, to share a Redis database |
| `SANTA_PUBLIC_BASE_URL` | yes | the URL the Macs use, e.g. `https://santa.example.com`; part of the profiles |
| `ALLOWED_HOSTS` | | comma separated, default the host of `SANTA_PUBLIC_BASE_URL` |
| `CSRF_TRUSTED_ORIGINS` | | comma separated, default the origin of `SANTA_PUBLIC_BASE_URL` |
| `TRUST_X_FORWARDED_PROTO` | behind a proxy | `1` if a TLS terminating proxy sets `X-Forwarded-Proto` |
| `SECURE_COOKIES` | | default on (off with `DEBUG`) |
| `DEBUG` | | never in production |
| `LANGUAGE_CODE` | | default language, `en` or `de`; the browser and the user profile win over it |
| `TIME_ZONE` | | default `UTC` |
| `GITHUB_TOKEN` | recommended | for package rules and the catalog search; without it GitHub allows 60 requests per hour |
| `SANTA_PROFILE_ORGANIZATION` | | `PayloadOrganization` of the profiles |
| `SANTA_PROFILE_IDENTIFIER_PREFIX` | set once | prefix of the profile identifiers, e.g. `com.example.santa`. The profile UUIDs are derived from it: **don't change it after the profiles are deployed** |
| `SANTA_PROFILE_MACHINE_OWNER` | | `MachineOwner` of the group profiles, see [Configuring the Macs](#configuring-the-macs) |
| `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET` | for SSO | see [Sign-in](#sign-in) |
| `OIDC_AUTHORIZATION_ENDPOINT`, `OIDC_TOKEN_ENDPOINT`, `OIDC_JWKS_ENDPOINT` | for SSO | endpoints of the provider |
| `OIDC_ADMIN_ROLE`, `OIDC_ROLES_CLAIM` | | default `Santa.Admin` in the claim `roles` (dotted path for nested claims) |
| `OIDC_USERNAME_CLAIMS`, `OIDC_SCOPES`, `OIDC_PROVIDER_NAME` | | default `preferred_username,upn,email`, `openid email profile`, button text |
| `WAIT_FOR_URL` | | entrypoint: wait until this URL answers, e.g. a sidecar |
| `RUN_MIGRATIONS` | | entrypoint: `0` to skip `migrate` when the web server starts |

For settings that aren't variables, or values rendered into a file by a secret store: create
`santa_server/settings_local.py` with `from .settings import *` and your overrides, mount it into the container and
set `DJANGO_SETTINGS_MODULE=santa_server.settings_local`.

## Deployment

The image `ghcr.io/expertzentrale/santa-server` (built by GitHub Actions: `edge` from `main` for amd64, `1.2.3`, `1.2`
and `latest` from the release tags for amd64 and arm64) runs on any container platform. It runs as an unprivileged user, and SQL Server and Redis run
separately.

- **Web server**: the default command (gunicorn on port 8000). The entrypoint runs `migrate` first; with several
  replicas set `RUN_MIGRATIONS=0` and run `python manage.py migrate` as a job before the rollout.
- **Static files** are in the image and served by gunicorn (WhiteNoise); no nginx, no bucket.
- **Scheduled jobs**, the same image with another command:
  - hourly: `python manage.py sync_release_sources` (new releases of the package rules, delayed approvals)
  - daily: `python manage.py cleanup_events --days 90`
- **Endpoints**: `/health` (liveness, no database), `/ready` (checks the database), `/metrics` (Prometheus).
  They answer before the host check, for probes. `/sync/…` must be reachable by the Macs over HTTPS; the console
  (`/console/`), the request form (`/request/`), `/login/`, `/oidc/…` and `/admin/` can be limited to your network.
- Put a TLS terminating proxy or ingress in front, and set `TRUST_X_FORWARDED_PROTO=1`.
- **Permissions**: the SyncBaseURL and the group profiles contain the secret sync token, the credential of the sync
  API. Only users who may change groups see and download them; a read-only group permission doesn't.

## Sign-in

Users sign in with any OpenID Connect provider. Members of the role `OIDC_ADMIN_ROLE` become administrators (staff,
AuthGroup "Santa admins" with every Santa permission); everyone else can only use the request form. Local accounts
(`python manage.py createsuperuser`) still work on `/login/`, e.g. for break-glass access.

Register a web application with the redirect URI `https://<host>/oidc/callback/`, then set the endpoints:

- **Microsoft Entra ID**: an app registration (single tenant) with a client secret and an app role `Santa.Admin`
  assigned to the administrators.
  `OIDC_AUTHORIZATION_ENDPOINT=https://login.microsoftonline.com/<tenant>/oauth2/v2.0/authorize`,
  `OIDC_TOKEN_ENDPOINT=https://login.microsoftonline.com/<tenant>/oauth2/v2.0/token`,
  `OIDC_JWKS_ENDPOINT=https://login.microsoftonline.com/<tenant>/discovery/v2.0/keys`.
- **Keycloak**: `…/realms/<realm>/protocol/openid-connect/auth`, `/token` and `/certs`; realm roles are in
  `OIDC_ROLES_CLAIM=realm_access.roles`.
- **Okta, Authentik, …**: the endpoints from `/.well-known/openid-configuration`, and a claim with the roles or
  groups (e.g. `OIDC_ROLES_CLAIM=groups`).

## Configuring the Macs

The server generates the configuration profiles; upload them to your MDM (Intune, Jamf Pro, Kandji, Mosyle, …)
as custom profiles:

1. **Base profile**: *Groups* → *Base profile*. The same for every Mac: allows the Santa system extension (and makes
   it non-removable), grants it full disk access, allows its background item, and enables its notifications.
   Assign it to **all** Macs, ideally before Santa is installed so the users get no prompts. If your MDM already
   allows system extensions with its own payload, add Santa there instead.
2. **Group profile**: create a **group**, then *Download configuration profile*. Assign it to the Macs of the group.
   It contains:
   - `SyncBaseURL`: the secret sync URL of the group.
   - `SyncEnableProtoTransfer = false`: this server only speaks the JSON sync protocol.
   - optionally `UnknownBlockMessage`, `BannedBlockMessage`, `EnableBadSignatureProtection` (section
     *Profile only*), and `MachineOwner` (below).
3. Install the Santa package with your MDM.

**Each Mac must get exactly one group profile**: two Santa profiles on the same Mac conflict. Moving a Mac to another
group means assigning the other group's profile; at its next sync it does a clean sync with the new rules.

Everything the server sends at every sync (client mode, path regexes, USB, sync interval, transitive rules, block
dialog button, …) is deliberately **not** in the profile: change it in the console and the Macs pick it up at their
next sync. Only after a change in *Profile only*, or after *Regenerate the sync URL*, download the group profile again
and replace it in the MDM. The identifiers stay the same, so it updates in place.

**`MachineOwner`** (optional, `SANTA_PROFILE_MACHINE_OWNER`): the MDM variable of the primary user, so that the
request form finds the Macs of the signed-in user. For example `{{userprincipalname}}` (Intune) or `$EMAIL`
(Jamf Pro); check the variables of your MDM. Without it the Macs are matched by the local account name (the part of
the username before the `@`).

Not needed:
- **Client certificates**: the secret token in the sync URL authenticates the group over HTTPS. If a URL leaks,
  *Regenerate the sync URL* and replace the group profile.
- **`ServerAuthRootsFile`**: Santa trusts the system keychain. If the server certificate comes from an internal CA,
  deploy that CA to the Macs.
- **`MachineID`**: the default, the hardware UUID, is what the server expects.

**Path regexes** take one regex per line. Santa only accepts one regex per setting, so the server combines the lines
into `(?:line1)|(?:line2)`. Anchor them with `^`, and never allow user-writable paths.

### Rule scope

A rule applies to every Mac (*global*), to groups, or to individual Macs. If several rules target the same
identifier on a Mac, the most specific scope wins (Mac > group > global), and on the same scope a block wins over an
allow. Disabled rules are removed from the Macs at their next sync.

## Daily use

Everything is in the console (`/console/`). The Django admin (`/admin/`) still has every model for low-level editing
and the history of every change.

**Profile**: the picture in the top right (from Gravatar, for the e-mail address) opens the menu: profile, theme
(system, light, dark), language, sign out. The choices are saved in the profile of the user. On phones the
navigation is behind the menu button.

**Lists**: click a column header to sort, again to reverse. Shift-click selects a range of rows.

**Groups**: the Santa configuration of each group, its secret SyncBaseURL, the download of its configuration
profile, *Regenerate the sync URL*, and the base profile on the list.

**Macs**: filter by group, mode, and Macs that haven't synced for 2 days. The detail page shows the hardware, the last
sync, whether the Mac has all its rules, the rules for only this Mac, its recent events and requests, and
*Clean sync*.

**Events**: *Blocked apps* lists the open blocks grouped by binary (Macs, users, last seen, open requests).
*All events* is the flat list; its first page adds new events every 5 seconds. Click a file name for the details
and *Create rule*, with a preview of the identifier the rule will use.

**Allow blocked software**: select apps or events → *Create rules…*. Every binary gets its own row: rule type,
policy, scope (the groups chosen at the top, only the Macs of its events, or all Macs), tags and description.
*Apply to all rows* sets a column for every row. The rule types:
- *Binary*: only this exact file. A new version is blocked again. For unsigned tools.
- *Signing ID*: every version of this app from this developer. Best for signed apps.
- *Team ID*: everything signed by this developer.

If a manual rule for the identifier already exists, its scope is widened instead of creating a second rule. The events
are marked resolved.

**Upload a binary**: *Execution rules* → *Upload binary*. Accepts a Mach-O file, or a zip / tar archive (every
executable inside, or those matching the glob pattern). The file is not stored.

**Tags**: labels to sort and find rules. They have no effect on the Macs.

**Execution rules**: tabs per rule type, filters for policy, scope, tag, origin (manual or package rule) and state.
Rules created by package rules can only be enabled or disabled; change their package rule instead.

**Package rules**: *Package rules* → *New package rule*. For GitHub, npm, VS Code and JetBrains, type in *Packages* to
search the catalog; `owner/part` searches the repositories of a GitHub owner. Homebrew and URLs are typed and added
with Enter.

| Catalog | Identifier | Asset pattern | Binary pattern |
|---|---|---|---|
| GitHub release | `abiosoft/colima` | `Darwin` | |
| Homebrew formula | `colima` | `^arm64_` (bottle tags) | `*/bin/colima` |
| Homebrew cask | cask name | regex on the download URL | glob inside the zip |
| Direct URL | full URL | | |
| npm package | `@esbuild/darwin-arm64` | | |
| VS Code extension | `rust-lang.rust-analyzer` | `^darwin-arm64$` (target platform) | |
| JetBrains plugin | `com.intellij.plugins.watcher` | `^PHPSTORM$` (compatible product) | |

A package rule can list **several packages**, one per line. They share the settings, but every package is checked
on its own and keeps its own versions. A package rule also chooses what the rules look like:
- **Preferred rule type**: Binary (default) or CDHash for one rule per file and version, or Signing ID, Certificate,
  Team ID for signed tools (one rule for all versions). Unsigned executables fall back to a Binary rule.
- **Policy**: allow, allow compiler, block, block silently, or CEL, plus the block message and URL.
- **Version pattern**: a regex, e.g. `^1\.` to stay on 1.x.
- **Auto approve** and a **delay** (e.g. 2 days after the publication), to hear about a compromised release before it
  runs on the Macs. *Keep versions*: only the last releases stay allowed.

Notes:
- Homebrew bottles are hashed per macOS version. Homebrew can rewrite a binary when it installs it (relocation); if
  the Macs still block it, compare with the blocked event and prefer the GitHub release.
- npm tarballs are checked against the published SHA-512, Homebrew bottles against their SHA-256.
- Releases without executables (most npm packages, VS Code extensions and JetBrains plugins are scripts) are recorded
  with 0 binaries: Santa has nothing to allow in them.
- `.dmg` and `.pkg` files cannot be unpacked; those apps are signed, allow them with a Signing ID or Team ID rule.

**Access requests**: users open `/request/`, sign in and choose *Blocked on my Mac* (the open blocks of the last
30 days on their Macs), *Package* (one or more packages from the catalogs) or *Other*. Administrators see the details
in *Requests* and approve (a rule for the requester's Mac, groups or all Macs; packages into a new or an existing
package rule, each package on its own) or deny with a note.

To link the block dialog to the form, set the group's *block dialog URL* to
`https://<host>/request/new/?sha256=%file_sha%` and the button text to e.g. `Request access`.

## Export and import the configuration

Groups, package rules and manual rules can be exported as JSON and imported on another server, e.g. from a test to a
production server. In the admin: *Groups* → *Export configuration* / *Import configuration*. Or:

```bash
python manage.py export_config -o santa-config.json
python manage.py import_config santa-config.json --dry-run     # show the changes, save nothing
python manage.py import_config - < santa-config.json            # import, "-" reads stdin (e.g. docker exec -i)
```

- Matching: groups and package rules by **name**, manual rules by **rule type + identifier + policy**, Mac scopes by
  **serial number**. The scopes of a rule are replaced by the ones in the file.
- **The sync tokens are never exported**: an existing group keeps its token, so its profile stays valid.
- Not exported: Macs, events, and the rules created by package rules (the target builds them).
- `--delete-missing` deletes the manual rules and package rules that are not in the file. Groups are never deleted.
- Everything or nothing: if a single entry is invalid, nothing is imported and every error is listed.

## Development

Open the folder in VS Code → **Reopen in Container**. The devcontainer starts SQL Server 2022 and Redis, creates the
database and runs the migrations (`.devcontainer/setup.sh`). Then:

```bash
python manage.py createsuperuser
python manage.py runserver 0.0.0.0:8000     # http://localhost:8000/
python manage.py test                        # against SQL Server
ruff check .
```

**GitHub token**: copy `.devcontainer/.env.example` to `.devcontainer/.env` (ignored by git and docker), set
`GITHUB_TOKEN` to a fine-grained token with "Public repositories (read-only)" and no permissions, and rebuild the
container. Without it GitHub allows 60 API requests per hour and 10 searches per minute for your IP.

**Translations**: the texts are English, German is in `santa/locale/de/LC_MESSAGES/django.po`. After changing texts:
`python manage.py makemessages -l de`, translate the new entries, `python manage.py compilemessages`, and commit the
`.po` and the `.mo`.

**Testing with a real Mac**: Santa requires HTTPS for the sync URL, except for `localhost`. Run
`python manage.py runserver 0.0.0.0:8000` and make it `localhost:8000` on the test Mac: nothing to do if the
devcontainer runs on the Mac (on Apple silicon, enable Rosetta in Docker Desktop for SQL Server); otherwise open a
reverse tunnel from the Docker host with `ssh -N -R 8000:localhost:8000 <user>@<test-mac>`. Download the profiles of a
test group (they contain `http://localhost:8000/sync/<token>/`), assign them to the test Mac only, then
`sudo santactl sync` and `santactl status` on the Mac.

See [CONTRIBUTING.md](CONTRIBUTING.md) for pull requests, and [AGENTS.md](AGENTS.md) for the rules of the project
(also for AI coding agents).

## Project layout

```
santa_server/          settings.py (image, env vars), settings_common.py, settings_dev.py, urls.py
santa/
  models.py            Group, Machine, Rule, Event, ReleaseSource (package rule), ReleaseVersion, AccessRequest
  sync_views.py        Santa sync protocol (preflight, eventupload, ruledownload, postflight)
  rules.py             which rules apply to a Mac, and the incremental rule sync
  events.py            storage of the uploaded events
  macho.py             reads hashes and code signature identifiers from Mach-O files
  releases.py          package rules (GitHub, Homebrew, npm, VS Code, JetBrains, URL)
  catalog.py           search the package catalogs (suggestions and icons)
  services.py          allow an identifier, resolve the matching events, the Macs of a user
  auth.py              OpenID Connect sign-in
  profiles.py          the configuration profiles (base profile, one per group)
  config_io.py         export / import of the configuration (JSON)
  console/             the console and the request form (views, forms, urls)
  templates/, static/  templates; htmx, console.js, console.css
  admin.py, forms.py   the Django admin
  management/commands  sync_release_sources, cleanup_events, export_config, import_config
  locale/              German translation
  tests/
scripts/               create_database.py (devcontainer and CI)
entrypoint.sh          entrypoint of the image
.devcontainer/         devcontainer (SQL Server + Redis)
.github/               CI (tests against SQL Server) and the image build
```

## License

Apache License 2.0, see [LICENSE](LICENSE). Santa is a project of [North Pole Security](https://northpole.dev);
this server is not affiliated with it. htmx is included under the 0BSD license.
