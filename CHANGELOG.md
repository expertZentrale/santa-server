# Changelog – santa-server

> 🇬🇧 English version · [Deutsche Version](CHANGELOG.de.md)
>
> Every change is recorded in **both** files (EN authoritative — GitHub release notes are generated from this file).

## [Unreleased]

### Added

- **Full clean sync.** Besides the clean sync, a Mac can get a full clean sync: it also drops the
  transitive rules it created itself (Santa 2024.1 or newer; older versions get a clean sync). In the
  Mac's detail, as a bulk action and in the admin.
- **Actions for several Macs.** Select Macs in the list (as for the rules) and request a clean sync, a
  full clean sync, or change their mode (Monitor, Lockdown or the group's, for good or for a time).
- **Check one package.** In a package rule with several packages, *Check* at a package checks only
  that one.

### Changed

- **Default view per list.** The *Views* menu of a list has a built-in *Standard* view, and the star makes any
  view the default. The list opens with the default view from the menu, and *Reset all* goes back to it; the
  console no longer reopens the filter used last.

## [0.12.0] - 2026-10-22

### Added

- **Download rows for the download list.** The downloads of a "Download list (JSON)" package rule are
  now a table in the package rule form, one row per download. A row can be limited to the entries with
  *field = value*, and then either takes the downloads the entry lists (path, URL field, optionally only
  the downloads with *field = value*) or builds the URL from fields of the entry (an https URL with
  `{field}`, dots for nested fields). This replaces the text fields for download paths, URL field and
  `field=regex` filters; existing package rules and older export files are converted automatically. A
  filter that is not a single value (`^value$`), or a third one, can't become a condition: the package
  rule is then disabled with the filter named in its error (import: a warning), to be checked instead of
  taking other downloads.
- **One version for the whole list.** Optionally, every state of a download list is one version with
  the downloads of all its entries, named after a name field and the version field. One package rule
  can then cover, for example, all browsers a test framework's browser manifest pins; *Keep* counts
  these states.
- **A more helpful download list preview.** The preview shows the first entries of the list with their
  fields, names the rows that give no download and why (with the values the list has instead), checks
  every file without downloading it (HEAD request, https only) and shows where each file comes from.

### Changed

- **"Enabled" moved up.** The *Enabled* switch of a package rule now sits right under its name.

### Fixed

- **Clean up after changed download rows.** *Clean up* now finds the rules of a download row that was
  removed or changed: files remember their row, older ones are matched by file name and the row's
  condition.
- **Changed downloads are recorded again.** A version of a download list remembers its download URLs;
  when the same version name comes with other downloads (a row changed, the list moved its files), the
  check records it again and keeps its approval. Versions recorded before are recorded once more at the
  next check.
- **Package rule preview.** With saved, unchanged download rows the preview no longer reports "Add at
  least one download", and a preview draft restored from before the table no longer breaks the form
  ("ManagementForm data is missing").
- **Version mark in the preview.** The ✓ for the version only shows when the package rule has a version
  pattern; on phones it stood alone on a line.

## [0.11.0] - 2026-10-22

### Added

- **Package rule catalog "Download list (JSON)".** For software that publishes its versions only as a
  JSON list, with entries per version and downloads per product and platform. Settings: the path to the
  versions (`*` for objects), the version field, the downloads per entry (one path per line), the URL
  field and `field=regex` filters. Versions are compared as numbers, so *Keep*, *Also allow the older
  kept versions*, the overview and *Clean up* go by version. When the list yields nothing, the preview
  and the check name the step that fails and what is there instead. The list and its files are only
  fetched over https, also after redirects; the list is limited by `RELEASE_MAX_LIST_BYTES` (default
  20 MB), the files by the existing download limits. Included in export / import and the Django admin.
- **Notes for reviewers on requests.** A new optional "Note for reviewers" stays in the console. The
  message to the requester is only sent when its switch is on (off by default); both are optional, also
  when denying.
- **Comments of ignored apps are visible.** The comment shows in the event drawer, with who and when,
  and below the badge in *Blocked apps*.
- **Database metrics per view.** `/metrics` now has `santa_db_connect_seconds` and, per view,
  `santa_view_db_queries`, `santa_view_db_seconds` and `santa_view_db_connect_seconds` (labels `view`,
  `method`, `rows`).

### Changed

- **Longer-lived database connections.** `DB_CONN_MAX_AGE` sets how long a database connection is
  reused (default 600 s, previously a fixed 60 s); opening a new connection often cost more than all
  queries of a request.
- **Package rule form and overview.** The form only shows the help of the chosen catalog. The overview
  shows file names with a link to their host instead of the URL twice, and the files per version. Long
  identifiers and preview headings wrap on phones.
- **Placeholder rows fit the list.** While loading, a list shows as many grey rows as it had last time
  (remembered per list in the session, 10 for a new one) instead of up to 50.

### Fixed

- **"Create rules" follows the selection.** Changing the selection in the list no longer reloads the
  whole drawer: deselected binaries leave without a request, new ones are added after a short pause, and
  what was entered stays. Before, the aborted request was taken for a lost connection, and with slow
  answers the drawer could get stuck.

## [0.10.0] - 2026-10-20

### Added

- **Mode per Mac.** A Mac can be set to Monitor or Lockdown independently of its group, for good or for
  a time (1 hour to 30 days, or until a date). The preflight sends it instead of the group's mode and
  goes back after the end; the profile stays the same. *Macs* → Mac → *Mode* → *Change*.
- **Expiry date for rules.** Rules can expire (default: never): in the rule form, as a bulk action and
  when approving a request (e.g. "Allowed: for 7 days"). Expired rules are removed from the Macs at
  their next sync, and the new hourly command `cleanup_expired_rules` deletes them. Export / import
  include the expiry date.
- **Ignore blocked apps.** Hide an app from the console lists, also with later events: by signing ID
  (default for signed apps), Team ID, SHA-256 or path prefix, for a chosen time and with a comment.
  Available from the bulk bar, the row menu and the event drawer. The status filter *Ignored* offers
  *Stop ignoring*, and *Administration* → *Ignored apps* lists them. Rules, Macs and the users' requests
  are not affected. Included in export / import; expired entries are removed by `cleanup_expired_rules`.
- **Blocked apps in detail.** The counts of blocks, Macs and users in *Blocked apps* open them in the
  drawer with the filters of the list, with tabs between the three (newest 100). A new column *Last at*
  shows the user and Mac of the newest event.
- **Older versions of Homebrew tap formulae** are read from the Git history of the formula file (one
  GitHub API request per check). `GITHUB_TOKEN` is recommended.
- **Roles by hand for sign-in users.** Per user, the roles and console access can be inherited from the
  sign-in groups or set by hand; the admin role from the token still applies.

### Changed

- **Progress of package checks** names the version and the dependency being checked.
- **Bulk bar of the events**: the action is chosen from a list and applied with *Apply*, as for the
  rules.
- **Wide tables**: with selected rows the horizontal scrollbar stays above the bulk actions, the page
  grows with a table wider than the window, and the top bar reaches the right edge again.

### Fixed

- **Homebrew formula rules now match the installed binaries.** Homebrew rewrites the load commands of
  binaries when it installs them and re-signs them on Apple Silicon; the server now does the same with
  the checked bottle, so the rules carry the hash Santa sees. What can't be predicted keeps the bottle's
  hash, and the version names the file and the reason. After upgrading, run
  `python manage.py sync_release_sources --rerecord-homebrew` once to record the existing versions again
  (versions waiting for manual approval must be approved again). Archives inside a bottle are no longer
  unpacked.
- **Homebrew taps**: formulae with constants and local variables, an architecture switch or the program
  in a platform resource are read correctly; formulae that re-sign their program when installed are
  named in the version.
- **Oversized executables** above `RELEASE_MAX_FILE_BYTES` are reported in the version's error instead
  of silently missing.

### Dependencies

- Python packages: hiredis 3.5.0, ruff 0.17.1.

## [0.9.0] - 2026-10-16

### Added

- **Package rules from events.** *Create rules…* has an *As package* tab that suggests a package per
  binary (a known release file by SHA-256, the bundle ID or app name in the Homebrew casks, the install
  path), changeable before saving, then checks and verifies each binary. The event detail shows a hint,
  *Blocked apps* a package chip.
- **Several patterns and a preview in package rules.** Version, asset and binary patterns take several
  values (as chips). The form previews the newest releases of each package with ✓ / ✗ for the version
  and the files.
- **Disk images and installer packages.** `.dmg` and `.pkg` files can be uploaded and used in package
  rules (7-Zip in the image, time limit `RELEASE_UNPACK_TIMEOUT`).
- **Notifications in the console** under a bell (toast, panel, full page);
  `cleanup_events --notification-days` removes old ones.
- **Mac and user filters** for events and execution rules: several values by search, "is / is not".
  For rules they show the rules that apply on the Mac, or on the Macs of the user. Old `?machine=` and
  `?user=` links still work.
- **Homebrew**: taps (`owner/tap/name`), the minimum macOS version and the nearest matching bottle.
- **Comments on tags**, included in the export.
- **Event columns**: path, group and Team ID can be shown as columns of their own.
- **Team ID rules with signing ID prefixes** are evaluated: events and requests no longer show "rule
  exists" wrongly, the signing ID can be added to the existing rule (from the event, the multi-selection
  and the approval of a request), and only the events the rule actually allows are resolved.
- **Download limits as environment variables**: `RELEASE_MAX_DOWNLOAD_BYTES`, `RELEASE_MAX_FILE_BYTES` and
  `RELEASE_MAX_UNPACKED_BYTES`.

### Changed

- **"Check now" runs in the background.** It continues when the drawer is closed, is visible to
  everyone and can be cancelled (it ends after the current package). Only one check per package rule
  runs at a time, also with the scheduled job. New packages are checked first; approving requests and
  event packages only check what was added.
- **Faster package checks**: parallel downloads (four, so up to four times the download limit in
  temporary disk space), no registry queries for known Homebrew versions, retries on connection errors,
  each bottle downloaded once per check, only Mach-O executables unpacked, rules created in batches.
- **Long actions show their progress with a heartbeat**: a crash or restart of the server is noticed and
  shown. On shutdown, checks end cleanly after the current package.
- **Clean up** also removes rules whose files no longer match an asset or binary pattern.
- **Allowed or not is decided per Mac**, as Santa does (scope, policy, rule type); event packages only
  resolve the events of the Macs they cover.
- **A faster console** on large rule sets: the groups tab, the rules CSV export, filtered rule lists, the
  package rule drawer and the Mac view; lists only load the fields they show, and the sync of a Mac
  computes its rules more efficiently. The drawer opens at once with a placeholder.
- **Layout**: a sticky table header also for wide tables, sticky pagination, tab bars with scroll arrows,
  the string of lights fades out on scroll, and phone fixes (bulk bar, bell counter, filter menus, long
  chips). Form fields have proper labels and no autofill.

### Fixed

- **Package checks are more robust**: temporary failures (connection errors, 429, 5xx, a full disk) are
  no longer stored as permanent errors, a broken archive or an unexpected error no longer stops the whole
  job, and `Retry-After` is honoured (at most 60 s).
- **Homebrew**: dates without a time, formulae without executables, and Linux-only formulae (a message
  and a button to remove them from the package rule). Dependency versions only come from the bottles the
  package rule takes.

### Security

- **Sync tokens are masked** in the gunicorn and Django logs. If tokens were logged before, consider
  regenerating the sync URLs.

### Dependencies

- Python packages: Django 6.0.9, tzdata 2026.5.

## [0.8.3] - 2026-10-12

### Fixed

- **Closing the drawer after "Open as page" and Back** no longer reloads the whole list.

## [0.8.2] - 2026-10-12

### Fixed

- **Text fields keep their height** after "Open as page".
- **Progress bar**: it no longer gets stuck or goes missing when an element is replaced while loading.
- **⋯ menu** opens at the button that was clicked, without jumping.

## [0.8.1] - 2026-10-09

### Changed

- **Text fields grow with their content** (up to 20rem, then they scroll) and can only be resized
  vertically.
- **CEL suggestions without typing**: an empty field, and every place where a value can start, shows all
  matching suggestions; after `target.` all fields, after a value and a dot all functions.

### Fixed

- **Code in help texts wraps on phones**: the long CEL examples made the rule form wider than the
  screen.

## [0.8.0] - 2026-10-09

### Added

- **Christmas theme decorations**: the string of lights hangs in swags under the top bar, and a
  *Decoration* submenu in the user menu switches snow and lights on or off (kept in the browser).

### Changed

- **Dark theme** with lighter greys, readable in bright light and meeting WCAG 2.2 AA contrast for text
  and control borders.
- **Snow uses much less CPU and memory**; no animation runs at rest, and nothing moves with "reduce
  motion".

## [0.7.0] - 2026-10-08

### Added

- **Approving requests**: a CEL policy (also with signing ID prefixes) in every approval form, a catalog
  search for packages, and the keep, older versions and dependencies options right in the approval.
- **Package rules: more ways to keep versions.** *Keep* takes the last *n* versions or the versions of
  the last *n* weeks or months (the newest always stays). *Also allow the older kept versions* works for
  GitHub, Homebrew formulae, npm, VS Code and JetBrains.
- **Package rules: allow the dependencies** of npm packages (also optional ones for macOS) and Homebrew
  formulae, including indirect ones, capped by `RELEASE_MAX_DEPENDENCIES`.
- **Package rules: Clean up** shows and deletes the versions the package rule no longer covers.
- **Configuration import with an editable preview**: skip, rename or disable entries and keep deletions,
  without uploading the file again.
- **Christmas theme**: `SANTA_CHRISTMAS_THEME` in December, `SANTA_CHRISTMAS_THEME_FORCE` all year (off
  by default), with colors, lights, a hat, snow and festive e-mails.
- **Connection loss is detected**: when the server is gone, the console says so, retries and loads the
  clicked page by itself once it answers. Hanging requests are noticed and repeated; a POST during the
  outage is not sent again, and the note says it was not saved.

### Changed

- **The console navigates without full page loads** (htmx), Back / Forward included; theme and language
  change without a reload; local-account sign-in without a reload.
- **Lists load their rows after the page**, with grey placeholder rows in the real table and a progress
  bar for requests, uploads and downloads. *Upload binary* opens in the drawer, and the chosen file stays
  after a form error.
- **Phones**: action rows, selection bar, package search, spacing, multi-select on iOS and column widths
  improved. Cards, tables and the drawer have no shadows.

## [0.6.2] - 2026-10-07

### Changed

- **Saved views**: renaming and deleting use the same icons and buttons as the rest of the console; the
  menu wraps on phones.

## [0.6.1] - 2026-10-07

### Changed

- **Branding**: the help texts say that Santa shows the company name only without a company logo, and
  the group form points it out when a group has both.
- **Drawer**: "Create rule" (event) and "Approve" / "Deny" (request) stay visible at the bottom of the
  drawer like in the other forms.

### Fixed

- **Search with Enter** works again on the rules and requests lists.

## [0.6.0] - 2026-10-07

### Added

- **Groups based on another group.** A group can take every setting of a base group (one level),
  except those marked *Own value*. With *Inherit rules* the base's rules apply, with precedence Mac >
  group > base group > global, then block > allow; with *Inherit file access rules* they go into the
  group's profile. The console names dependent groups when the base's profile changes, a base can only
  be deleted when no group uses it, and the Mac view marks inherited rules. Included in export / import.
- **E-mail defaults per sign-in group**: what its members start with per notification. Users can pick
  *Default (…)* and their own choice always wins; with several groups the one with the most e-mails
  applies, and *No e-mails* always blocks.
- **Unknown programs in monitor mode** (`ALLOW_UNKNOWN`) are listed under *Blocked apps* and counted in
  the open blocks of Macs and groups, with the badge *Monitor: blocked in lockdown*. The users' request
  form still only offers real blocks.

### Changed

- **The console on phones**: tapping a card toggles only that one, a larger checkbox, *Select all*,
  highlighted selected cards, a bulk bar that no longer covers the last card, a drawer whose head and
  buttons stay in view, and nothing wider than the screen.

## [0.5.2] - 2026-10-06

### Fixed

- **E-mail**: an error while closing the connection to the mail server is only logged and no longer
  stops the daily summary.

## [0.5.1] - 2026-10-06

### Added

- **Accounts without a mailbox**: a sign-in group can be set to *No e-mails*, and a user can be set to
  *No e-mails* or *E-mails allowed* as an exception (in *Administration* → *Users*).

### Changed

- **Events status filter** is a multiple choice (*Open* / *Resolved*) like the status of the requests.

### Fixed

- **A refused e-mail address no longer stops the others**: each e-mail is sent on its own over the same
  connection, and a refused address is logged as a warning.

## [0.5.0] - 2026-10-06

### Added

- **E-mail notifications**: request decided (to the requester), new requests, package versions to
  approve, versions approved automatically and package rule errors, each offered only with the
  matching permissions. Per user *Off*, *Immediately* or *Daily summary*; the summary is sent by the new
  command `send_notification_digest`. Settings: `EMAIL_HOST` (empty = off), `EMAIL_PORT`,
  `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`, `EMAIL_USE_TLS` / `EMAIL_USE_SSL`, `DEFAULT_FROM_EMAIL`;
  `EMAIL_NOTIFICATIONS_ENABLED=false` switches them off. A failure to send never breaks the action.
- **Homebrew search**: formulae and casks can be searched in the catalog (the lists are loaded once a
  day).
- **Delete users** in *Administration* (not your own account; users with requests or history entries
  are deactivated instead).
- **Requests: pick an existing execution rule by search** when approving other software; exactly the
  picked rule is linked.
- **Prometheus metrics for SQL Server**: `/metrics` now includes the `django_db_*` query, duration,
  error and connection metrics. `METRICS_ENABLED=false` switches metrics off entirely.
- **Server name and favicon**: a Santa hat favicon, `SANTA_SERVER_NAME` for the name in titles and the
  header, `SANTA_FAVICON_URL` for your own favicon.
- **Sign-in button logo**: `OIDC_PROVIDER_ICON=microsoft` for the built-in button, or a
  URL / path for another provider's logo.

### Changed

- **Drop zones for uploads** (configuration import, upload binary, hash on approval, group logo).
- **Groups list** with search, mode filter, saved views, columns and pages.
- **Yes / no fields are switches**, checkboxes are styled like the console, users from the sign-in show
  one list of roles (from the sign-in and by hand), and lists in the drawer no longer scroll the page
  behind.
- **Rule descriptions** are one line with "…" and the full text in the tooltip; reopening the view the
  drawer already shows no longer adds a history entry.

## [0.4.0] - 2026-10-05

### Added

- **Removable media**: allow, block or remount with flags, optionally different for encrypted media
  (older Santa versions get the USB settings). Existing USB settings are migrated, and old export files
  are understood.
- **Branding and USB options in the group profile**: `OnStartUSBOptions`, company name and logos (light
  and dark, uploaded or as a `file:///` URL, with a preview).
- **File access rules**: a *File access* page to manage File Access Authorization rules with their
  processes. They reach the Macs through the group profile (`FileAccessPolicy`); after a change the
  console names the groups whose profile has to be distributed again. Included in export / import.
- **More in the drawer**: users, roles, sign-in groups, tags, export / import and new groups; execution
  rules can be deleted there.

### Changed

- **Drawer history**: every view of the drawer is an entry of the browser history, so Back / Forward
  step through the views, and after a reload the view opens again. Selecting text and releasing outside
  no longer closes the drawer.

### Dependencies

- GitHub Actions: docker/setup-qemu-action 4.
- Python packages: cryptography 50.0.2, ruff 0.16.10.

## [0.3.5] - 2026-10-05

### Added

- **Filter bar on every list**: search, "+ Filter" and the active filters as chips; a filter can have
  several values, time filters take a date range, and rules can be filtered by creation date. The last
  filter of each list is kept in the profile, and filters can be saved as named **views**.
- **Rules from events with CEL**: a CEL policy with an expression and "Only signing IDs starting with",
  written per binary from its own Team ID.

### Changed

- **Tables and layout**: double-clicking a column edge fits the width to the content, the events list
  shows the whole path and links existing rules, and the pages keep a calm layout when switching.

## [0.3.0] - 2026-10-02

### Added

- **Request page shows what exists**: packages already allowed on the user's Macs are greyed out and
  refused, earlier requests show as a badge with date and status, programs already requested are no
  longer offered, and *My requests* has a time span filter.

### Changed

- **`WAIT_FOR_URL` only applies before the web server.** Other commands (scheduled jobs) start at once;
  set `WAIT_FOR_URL_JOBS=1` for jobs that should wait too. `WAIT_FOR_TIMEOUT` gives up after the given
  seconds (default `0` = no limit).

## [0.2.0] - 2026-10-01

### Added

- **Rules table**: full identifiers, resizable and optional columns, a sticky header, keyboard
  navigation, and a CSV export of the filtered rules.
- **Prefix rules via CEL**: for a Team ID rule, "Only signing IDs starting with" writes a CEL expression
  that allows only those signing IDs. CEL fields get suggestions while typing.
- **Rules from events in the drawer**: one form for all selected binaries with a preview per binary,
  also to block or block silently; existing rules show in every form and as a badge.
- **Live events**: new events appear without a reload, with a counter for unseen rows.
- **Web server sizing**: `GUNICORN_WORKERS`, `GUNICORN_THREADS`, `GUNICORN_WORKER_CLASS`,
  `GUNICORN_TIMEOUT` and `GUNICORN_MAX_REQUESTS`; the defaults match the previous behaviour.

### Changed

- **A consistent, accessible console**: relative sizes, visible focus, reduced motion, larger click
  targets, entries per page (25 to 200), styled switches, selects and tags. Rules, Macs and groups open
  in the drawer with Back to the previous view.

### Security

- **Sync API errors are generic**: a rejected sync request gets `{"error": "bad request"}`, the reason
  only goes to the log. Before, the message could reveal the ID of another group.

### Documentation

- **Step-by-step OpenID Connect setup** in the README: app registration, admin role, optional claims
  and the groups claim, with an example ID token.

## [0.1.0] - 2026-09-29

### Added

- **Groups of Macs**, each with its own Santa configuration (monitor / lockdown, path regexes, USB, …),
  its own secret sync URL and a generated configuration profile for any MDM, plus a base profile for all
  Macs.
- **Execution rules** for binaries, certificates, Team IDs, signing IDs and CDHashes, scoped globally,
  per group or per Mac, with allow, block and CEL policies. Only the differences are sent to each Mac.
- **Events**: blocked executions grouped by app; select them and create the rules in one step.
- **Package rules** that watch GitHub releases, Homebrew formulae and casks, npm packages, VS Code
  extensions, JetBrains plugins or a direct download URL, and turn the executables of every new release
  into rules, optionally after a delay. Downloads are checked against the published hash where there is
  one.
- **Access requests** at `/request/`: users sign in with an OpenID Connect provider and ask for a
  blocked program, packages or other software; administrators approve or deny in the console.
- **Upload a binary or archive** to allow or block it; files are only hashed, never stored.
- **The console** (`/console/`) with a side drawer for details and forms, a history of every change,
  and an *Administration* tab for users, roles, sign-in groups, tags and the configuration export /
  import (also as `export_config` / `import_config` commands, without sync tokens).
- **Sign-in groups** map the groups of the OpenID Connect provider to roles and console access; local
  accounts stay for break-glass access.
- English and German, light and dark theme, usable on phones. Runs on SQL Server and Redis, with
  `/health`, `/ready` and `/metrics` endpoints.

### Security

- **Sync tokens are only visible to users who may change groups**: read-only viewers see neither the
  sync URL nor the group profiles.
- **`ALLOWED_HOSTS` and `CSRF_TRUSTED_ORIGINS` default to the host of `SANTA_PUBLIC_BASE_URL`**, which
  is required, instead of `*`.
- **Size limits** for decompressed sync bodies and for unpacking archives.

### Dependencies

- GitHub Actions: actions/checkout 7, docker/build-push-action 7, docker/login-action 4,
  docker/setup-buildx-action 4, docker/metadata-action 6.
- Python packages: hiredis 3.4.2, tzdata 2026.4.

[Unreleased]: https://github.com/expertZentrale/santa-server/compare/v0.12.0...HEAD
[0.12.0]: https://github.com/expertZentrale/santa-server/compare/v0.11.0...v0.12.0
[0.11.0]: https://github.com/expertZentrale/santa-server/compare/v0.10.0...v0.11.0
[0.10.0]: https://github.com/expertZentrale/santa-server/compare/v0.9.0...v0.10.0
[0.9.0]: https://github.com/expertZentrale/santa-server/compare/v0.8.3...v0.9.0
[0.8.3]: https://github.com/expertZentrale/santa-server/compare/v0.8.2...v0.8.3
[0.8.2]: https://github.com/expertZentrale/santa-server/compare/v0.8.1...v0.8.2
[0.8.1]: https://github.com/expertZentrale/santa-server/compare/v0.8.0...v0.8.1
[0.8.0]: https://github.com/expertZentrale/santa-server/compare/v0.7.0...v0.8.0
[0.7.0]: https://github.com/expertZentrale/santa-server/compare/v0.6.2...v0.7.0
[0.6.2]: https://github.com/expertZentrale/santa-server/compare/v0.6.1...v0.6.2
[0.6.1]: https://github.com/expertZentrale/santa-server/compare/v0.6.0...v0.6.1
[0.6.0]: https://github.com/expertZentrale/santa-server/compare/v0.5.2...v0.6.0
[0.5.2]: https://github.com/expertZentrale/santa-server/compare/v0.5.1...v0.5.2
[0.5.1]: https://github.com/expertZentrale/santa-server/compare/v0.5.0...v0.5.1
[0.5.0]: https://github.com/expertZentrale/santa-server/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/expertZentrale/santa-server/compare/v0.3.5...v0.4.0
[0.3.5]: https://github.com/expertZentrale/santa-server/compare/v0.3.0...v0.3.5
[0.3.0]: https://github.com/expertZentrale/santa-server/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/expertZentrale/santa-server/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/expertZentrale/santa-server/releases/tag/v0.1.0
