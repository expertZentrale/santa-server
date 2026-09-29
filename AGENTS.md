# Rules for agents editing this project

Santa Server is a plain Django application: a sync server for [Santa](https://northpole.dev) that manages
binary authorization on macOS, with one Santa configuration and one sync URL per group of Macs.
Read this file before changing anything. If a request conflicts with these rules, say so instead of working around them.

## Scope

- **Santa only.** No osquery, Munki, MDM features, inventory, or event pipeline. Don't add them "for later".
- **No organisation-specific code or texts.** The project is public: no internal hostnames, registries, secret stores
  or deployment tools, and no product names where a generic term fits ("MDM", "OpenID Connect provider").
  Examples for specific products (Intune, Jamf Pro, Entra ID, Keycloak) belong in the README.
- Only copy code from projects with a compatible license (Apache-2.0, MIT, BSD), and say so in the commit.
- Keep the project a **normal Django application** that a Django developer understands without studying it:
  - The day-to-day UI is the console (`santa/console/`, `/console/`): plain Django views, forms and templates,
    with htmx (vendored in `santa/static/santa/htmx.min.js`) and a small `console.js`. No build step.
    The request form for the users is `/request/`. The Django admin (`santa/admin.py`) stays for low-level editing;
    configuration belongs in the console (*Administration*), not only in the admin.
    Both use the same functions in `services.py`; don't duplicate business logic in the views.
  - Don't add a SPA, a frontend build, CDN assets, DRF, Celery or other frameworks without the user agreeing first.
  - Scheduled work is a management command (run by the platform's scheduler, see the README), not a worker process.
  - Prefer plain functions in small modules (`rules.py`, `events.py`, `releases.py`, `macho.py`, `services.py`)
    over class hierarchies and abstractions.

## Stack constraints

- **Django 6.0.x** (`Django>=6.0.8,<6.1`). django-prometheus doesn't support 6.1 yet. Don't upgrade past that
  without checking django-prometheus and mssql-django support.
- **Python 3.14** on Alpine (`python:3.14-alpine3.23`; the base image is a build argument of the `Dockerfile`).
  `Dockerfile` and `.devcontainer/Dockerfile` install the same apk packages, plus the development tools
  `git` and `gettext` in the devcontainer. Keep them in sync.
- The image runs as an unprivileged user, `entrypoint.sh` stays generic (optional wait, migrations, `exec`).
  Platform specifics (sidecars, secret stores, ingress) belong to the deployment, not to this repository.
- **Gunicorn serves everything, static files through WhiteNoise.** No nginx, no static bucket.
- **Redis and SQL Server are external.** The containers in `.devcontainer/docker-compose.yml` are for development only.
  Redis is used for the cache (`django_prometheus` Redis backend), the sessions, and the lock of the scheduled jobs.
- Health: `/health` (liveness, no DB), `/ready` (DB check). Metrics: `/metrics` (django-prometheus).
  All three answer before the ALLOWED_HOSTS check (`HealthCheckMiddleware`), for probes and scrapers that use the
  pod IP. Don't move them.

## Database: Microsoft SQL Server through mssql-django

Every model and query must work on SQL Server (mssql-django 2.x, `mssql_python` driver):

- Never use `django.contrib.postgres` (`ArrayField`, `ArraySubquery`, …), `DISTINCT ON`, or database-specific functions.
- No raw SQL. If it is truly unavoidable, write it in T-SQL, test it on SQL Server, and ask the user first.
- SQL Server allows at most **2100 parameters per query**: chunk every `__in=` list that can grow (see `cleanup_events`).
- Indexed or unique `CharField`s stay ≤ 450 characters (index key size limit).
- `JSONField` is for storing documents, don't filter on keys inside it.
- Avoid conditional `UniqueConstraint`s / filtered indexes and database-level cascades (limited on SQL Server).
- mssql-django cannot rename models or fields that have foreign keys. Once deployed, a rename needs a hand-written
  migration (new field, copy the data, drop the old one). Ask first.
- Admin list filters: no `SimpleListFilter`. Its "Show counts" (facets) aggregate over a subquery, which SQL Server
  refuses. Subclass a field list filter instead (see `ExecutingUserFilter`). `test_admin_pages` loads every list
  with counts on, keep new models in it.
- `select_for_update()` inside `transaction.atomic()` is fine and used by the sync views.
- Every model change comes with a migration: `python manage.py makemigrations`. Never edit an applied migration.

## Settings and secrets

- Shared settings go in `santa_server/settings_common.py`, with neutral defaults.
- The image uses `santa_server/settings.py`: every deployment value is an environment variable, read with its
  `env()` helpers. A new variable goes in the configuration table of the README.
- Deployments can add `santa_server/settings_local.py` (`from .settings import *`, gitignored). Never commit one.
- The devcontainer uses `santa_server/settings_dev.py` with the env vars of `.devcontainer/devcontainer.json`.
- Never hardcode secrets, and never put real credentials in the dev settings or the example `docker-compose.yml`.

## Naming

- A `santa.Group` is a group of Macs sharing one Santa configuration and sync URL. It is **not**
  `django.contrib.auth.models.Group` (admin permissions). Never mix them up; if both are needed in one module,
  import the auth one as `AuthGroup`.

## Santa sync protocol — don't break the Macs

- Sync URL: `/sync/<group sync_token>/<preflight|eventupload|ruledownload|postflight>/<machine id>`.
  The configuration profiles contain these URLs. Changing the URL layout or how the tokens work breaks every Mac.
- Rule payloads and preflight keys follow the Santa protocol (https://northpole.dev/development/sync-protocol/).
  Check the protocol docs before adding or renaming keys.
- Santa takes a single allowed and a single blocked path regex. The admin stores one per line, and
  `combine_path_regexes()` joins them for the preflight. Keep the per-line validation.
- Rule sync (`santa/rules.py`): the server keeps, per machine, the rules the client confirmed (`synced_rules`).
  Every sync sends only the difference (or everything on a clean sync), and the ledger is only updated on postflight.
  Keep it that way: a session without a postflight must be resent in full next time.
- Tags (`Tag`, `Rule.tags`) are only labels for the admin. They must never change what is sent to the Macs.
- Scope precedence: machine > group > global, then block > allow on the same scope. It's tested. Change it
  only on purpose.

## Configuration profiles (`santa/profiles.py`)

- Keys the sync server sends (client mode, regexes, USB, intervals, transitive rules, event detail URL, …) never go
  into the group profile: the server must stay the only source. Only profile-only keys belong there.
- `SyncEnableProtoTransfer` stays `false`: the server only implements the JSON protocol.
- The base profile payloads (team ID `ZMCG7MLDV9`, code requirements) come from https://northpole.dev/deployment/.
  Compare with those pages before changing them.
- Profile identifiers and UUIDs are derived from `SANTA_PROFILE_IDENTIFIER_PREFIX` and the group pk, so a new
  download replaces the old profile in the MDM. Don't make them random.
- The profiles work with any MDM. MDM variables (`MachineOwner`) come from settings, never hardcoded.

## Export / import (`santa/config_io.py`)

- A new field on `Group`, `ReleaseSource` or `Rule` that is configuration (not state) must be added to the field
  lists of `config_io.py`, with a round trip test. Bump `VERSION` only for incompatible format changes.
- Match by natural keys only (names, rule type + identifier + policy, serial numbers), never by pk.
- Never export the sync tokens or other secrets.

## Sign-in

- Any OpenID Connect provider through `mozilla-django-oidc` (`santa/auth.py`). Users are matched by the first of
  `OIDC_USERNAME_CLAIMS`. The role `OIDC_ADMIN_ROLE` in `OIDC_ROLES_CLAIM` makes them staff and members of the
  AuthGroup "Santa admins". Local accounts (`ModelBackend`) stay for break-glass access.
- Sign-in groups (`SignInGroup`) map the values of `OIDC_GROUPS_CLAIM` to roles (AuthGroups) and console access.
  Only mapped groups are stored (`SignInGroup.members`, as of the last sign-in), never every group of the token.
  `apply_sign_in_roles()` sets the roles; roles no sign-in group grants are assigned by hand and stay.
  `*` is everyone, with the role "Santa requesters" by default.
- Requests need `request_event` / `request_package` / `request_other` (`request_kinds_for()` in `services.py`).
- The console's *Administration* tab (`views_admin.py`) manages users, roles, sign-in groups, tags and the
  configuration export / import. Keep it on the console patterns; the Django admin stays for low-level editing.
- The console views check `is_staff` (`staff_required`) **and** the model permissions (`require_perms`).
- `Machine.primary_user` is the `MachineOwner` of the group profile (`SANTA_PROFILE_MACHINE_OWNER`). The request
  form only offers events of the Macs of the signed-in user (`machines_for_user`).

## Security

- Sync tokens are secrets, the credential of the sync API: never log them, and only show the SyncBaseURL or serve the
  group profile to users who may change groups (`can_see_sync_token()` in `services.py`), never to read-only viewers.
- Uploaded binaries are only hashed, never stored.
- Everything the server decompresses has a size limit: sync bodies (`SYNC_MAX_DECOMPRESSED_BYTES`), archives
  (`RELEASE_MAX_FILE_BYTES`, `RELEASE_MAX_UNPACKED_BYTES` through `_UnpackBudget`). Keep new code paths within them.
- `ALLOWED_HOSTS` is never `*` by default: it comes from `SANTA_PUBLIC_BASE_URL`.
- Release downloads are size limited, and checked against the published hash when there is one
  (SHA-256 of Homebrew bottles, SHA-512 of npm tarballs). Never remove that check.
- Admin actions and console views that change rules must check permissions and write an admin log entry
  (`log_addition` / `log_change`, in the console the helpers of `santa/console/utils.py`).
- Catalog icons are shown as `<img>` from the catalogs: only `https://` URLs, with `referrerpolicy="no-referrer"`.
  Text from the catalogs and the users is always escaped (templates, `textContent` in `console.js`).
- Redirects to `?next=` go through `safe_next()`.

## Languages (English, German)

- The source texts are English. Every text the console and the request form show is translatable: `{% translate %}`
  / `{% blocktranslate %}` in the templates, `gettext_lazy` for labels, help texts and choices, `gettext` /
  `ngettext` with named placeholders (`%(name)s`) for messages. No f-strings in translated texts.
- The console model forms set their labels in `Meta.labels`: the model fields keep their English names, no migrations.
- After changing texts: `python manage.py makemessages -l de`,
  translate the new entries in `santa/locale/de/LC_MESSAGES/django.po` (formal "Sie"), then
  `python manage.py compilemessages`. Commit the `.po` and the `.mo`; `test_compiled_translations_are_up_to_date`
  fails when they differ or an entry is untranslated.
- Default language: `LANGUAGE_CODE` (env var, default `en`); the browser language and the profile of the user win
  over it. The tests run in English (`santa/test_runner.py`), test German explicitly.

## Tests, lint, workflow

- Run everything **inside the devcontainer**, against SQL Server:
  ```
  python manage.py test
  ruff check .
  python manage.py makemigrations --check --dry-run
  ```
  SQLite is not a substitute: the whole point is SQL Server compatibility. The GitHub CI runs the same checks
  in the devcontainer setup (`.github/workflows/ci.yml`); changes land through pull requests with a green CI.
- New behaviour comes with tests in `santa/tests/`. Tests never reach the network: mock the HTTP session
  (see `test_releases.py`). The binaries in the tests are synthetic Mach-O files from `santa/tests/utils.py`.
- Style: ruff, line length 119. Match the surrounding code. Comments only explain *why*, not *what*.
- Code, identifiers, comments and admin texts are in English (the German UI texts are in the `.po` file).
- **Commit messages: German first, then English.** The German subject line and body, a line `---`, then the same
  content in English (subject line and body). Trailers such as `Co-Authored-By` come last.
- **README: German first, then English**, in the one `README.md`: the German part, then the English part with the
  same sections. Every change to the README updates both parts, so they always say the same. Links inside the
  README point to the headings of their own language.
- Don't commit, push or deploy unless the user asks.
