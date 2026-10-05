# Santa Server

**[Deutsch](#deutsch) · [English](#english)**

## Deutsch

Ein Sync-Server für [Santa](https://northpole.dev), das System zur Programmfreigabe unter macOS: Er teilt den Macs
mit, welche Programme laufen dürfen, sammelt, was sie blockieren, und nimmt Anfragen nach Software entgegen.

- **Gruppen**: Jede Gruppe von Macs hat ihre eigene Santa-Konfiguration (Monitor / Lockdown, Pfad-Regexe, USB, …) und
  ihre eigene geheime **Sync-URL**. Der Server erzeugt die Konfigurationsprofile für Ihr MDM.
- **Regeln** für Binaries, Zertifikate, Team IDs, Signing IDs und CDHashes, global, je Gruppe oder je Mac, mit den
  Richtlinien Erlauben, Blockieren und CEL.
- **Ereignisse**: blockierte (und im Monitor-Modus: die blockiert worden wären) Ausführungen, nach App gruppiert und
  live aktualisiert. Auswählen und die Regeln in einem Schritt anlegen, jedes Binary mit eigenem Regeltyp, eigener
  Richtlinie, eigenem Geltungsbereich und eigenen Tags.
- **Paketregeln**: beobachten GitHub-Releases, Homebrew-Formulae und -Casks, npm-Pakete, VS-Code-Erweiterungen,
  JetBrains-Plugins oder eine URL. Die ausführbaren Dateien jedes neuen Releases werden heruntergeladen, gehasht und
  zu Regeln, auf Wunsch erst nach einer Wartezeit.
- **Anfragen**: Benutzer melden sich über Ihren Identitätsanbieter (OpenID Connect) an und fragen ein auf ihrem Mac
  blockiertes Programm, ein oder mehrere Pakete oder etwas anderes an. Administratoren genehmigen oder lehnen in der
  Konsole ab und legen die Regel dabei gleich an.
- **Hochladen** eines Binarys oder Archivs, um es zu erlauben oder zu blockieren. Hashes und Codesignatur werden aus
  der Datei gelesen.
- **Administration** in der Konsole: Benutzer, Rollen, Anmeldegruppen (Gruppen des Identitätsanbieters, die Rollen
  vergeben), Tags, Export und Import.
- Deutsch und Englisch, helles und dunkles Design, funktioniert auf dem Smartphone.

Es ist eine gewöhnliche Django-6.0-Anwendung: eine serverseitig gerenderte Konsole mit htmx (ohne Frontend-Build) und
der Django-Admin, Microsoft SQL Server über mssql-django, Redis für Cache und Sessions, gunicorn mit WhiteNoise.

### Schnellstart

```bash
docker compose up --build                  # das Image, SQL Server und Redis (docker-compose.yml)
docker compose exec web python manage.py createsuperuser
```

Öffnen Sie <http://localhost:8000/>. Die Compose-Datei ist ein Beispiel mit festen Passwörtern, nur zum Ausprobieren.

### Konfiguration

Das Image (`santa_server/settings.py`) wird über Umgebungsvariablen konfiguriert:

| Variable | Pflicht | Beschreibung |
|---|---|---|
| `SECRET_KEY` | ja | Django Secret Key, lang und zufällig |
| `DB_HOST`, `DB_USER`, `DB_PASSWORD` | ja | SQL Server (2019 oder neuer). Die Datenbank muss existieren. |
| `DB_PORT`, `DB_NAME` | | Standard `1433`, `santa` |
| `DB_EXTRA_PARAMS` | | ODBC-Verbindungsoptionen, z. B. `Encrypt=yes;TrustServerCertificate=no` |
| `REDIS_URL` | ja | z. B. `redis://user:password@redis:6379/0` (Cache, Sessions, Sperre des geplanten Jobs) |
| `CACHE_KEY_PREFIX` | | Standard `santa`, um eine Redis-Datenbank zu teilen |
| `SANTA_PUBLIC_BASE_URL` | ja | die URL, die die Macs verwenden, z. B. `https://santa.example.com`; Teil der Profile |
| `ALLOWED_HOSTS` | | kommagetrennt, Standard der Host von `SANTA_PUBLIC_BASE_URL` |
| `CSRF_TRUSTED_ORIGINS` | | kommagetrennt, Standard der Origin von `SANTA_PUBLIC_BASE_URL` |
| `TRUST_X_FORWARDED_PROTO` | hinter einem Proxy | `1`, wenn ein TLS-terminierender Proxy `X-Forwarded-Proto` setzt |
| `SECURE_COOKIES` | | Standard an (aus mit `DEBUG`) |
| `DEBUG` | | nie in Produktion |
| `LANGUAGE_CODE` | | Standardsprache, `en` oder `de`; Browser und Benutzerprofil haben Vorrang |
| `TIME_ZONE` | | Standard `UTC`; die Konsole zeigt die Zeiten in der Zeitzone des Browsers oder des Benutzerprofils |
| `GITHUB_TOKEN` | empfohlen | für Paketregeln und die Katalogsuche; ohne erlaubt GitHub 60 Anfragen pro Stunde |
| `SANTA_PROFILE_ORGANIZATION` | | `PayloadOrganization` der Profile |
| `SANTA_PROFILE_IDENTIFIER_PREFIX` | einmal setzen | Präfix der Profil-Identifier, z. B. `com.example.santa`. Die Profil-UUIDs werden daraus abgeleitet: **nach dem Verteilen der Profile nicht mehr ändern** |
| `SANTA_PROFILE_MACHINE_OWNER` | | `MachineOwner` der Gruppenprofile, siehe [Die Macs konfigurieren](#die-macs-konfigurieren) |
| `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET` | für SSO | siehe [Anmeldung](#anmeldung) |
| `OIDC_AUTHORIZATION_ENDPOINT`, `OIDC_TOKEN_ENDPOINT`, `OIDC_JWKS_ENDPOINT` | für SSO | Endpunkte des Anbieters |
| `OIDC_ADMIN_ROLE`, `OIDC_ROLES_CLAIM` | | Standard `Santa.Admin` im Claim `roles` (Pfad mit Punkten für verschachtelte Claims) |
| `OIDC_GROUPS_CLAIM` | | Standard `groups` (Pfad mit Punkten): der Claim, der mit den [Anmeldegruppen](#anmeldegruppen-und-rollen) verglichen wird |
| `OIDC_USERNAME_CLAIMS`, `OIDC_SCOPES`, `OIDC_PROVIDER_NAME` | | Standard `preferred_username,upn,email`, `openid email profile`, Text der Anmelde-Schaltfläche |
| `WAIT_FOR_URL` | | Entrypoint: vor dem Webserver warten, bis diese URL antwortet, z. B. ein Sidecar; andere Befehle (die geplanten Jobs) starten sofort |
| `WAIT_FOR_URL_JOBS` | | Entrypoint: `1`, um auch vor anderen Befehlen zu warten, wenn die Jobs den Sidecar ebenfalls haben |
| `WAIT_FOR_TIMEOUT` | | Entrypoint: nach so vielen Sekunden Warten mit Fehler beenden, damit die Plattform neu startet; Standard `0` = ohne Grenze |
| `RUN_MIGRATIONS` | | Entrypoint: `0`, um `migrate` beim Start des Webservers zu überspringen |
| `GUNICORN_WORKERS`, `GUNICORN_THREADS` | | Standard `2` Worker-Prozesse mit je `4` Threads, siehe [Größe des Webservers](#größe-des-webservers) |
| `GUNICORN_WORKER_CLASS` | | Standard `gthread`; `sync` nur mit einem Thread je Worker |
| `GUNICORN_TIMEOUT` | | Standard `120` Sekunden ohne Lebenszeichen, bevor ein Worker neu gestartet wird |
| `GUNICORN_MAX_REQUESTS` | | Standard `1000`: ein Worker wird nach 1000 bis 1100 Anfragen ersetzt (zufällig bis +10 %), `0` = nie |

Für Einstellungen, die keine Variablen sind, oder Werte, die ein Secret Store in eine Datei schreibt: Legen Sie
`santa_server/settings_local.py` mit `from .settings import *` und Ihren Änderungen an, binden Sie sie in den
Container ein und setzen Sie `DJANGO_SETTINGS_MODULE=santa_server.settings_local`.

### Betrieb

Das Image `ghcr.io/expertzentrale/santa-server` (gebaut von GitHub Actions: `edge` aus `main` für amd64, `1.2.3`,
`1.2` und `latest` aus den Release-Tags für amd64 und arm64) läuft auf jeder Container-Plattform. Es läuft als
Benutzer ohne Rechte; SQL Server und Redis laufen separat.

- **Webserver**: der Standardbefehl (gunicorn auf Port 8000). Der Entrypoint führt zuerst `migrate` aus; bei mehreren
  Replikas `RUN_MIGRATIONS=0` setzen und `python manage.py migrate` vor dem Rollout als Job ausführen.
- **Statische Dateien** sind im Image und werden von gunicorn ausgeliefert (WhiteNoise); kein nginx, kein Bucket.
- **Geplante Jobs**, dasselbe Image mit einem anderen Befehl (sie warten nicht auf `WAIT_FOR_URL`, außer mit
  `WAIT_FOR_URL_JOBS=1`):
  - stündlich: `python manage.py sync_release_sources` (neue Releases der Paketregeln, verzögerte Freigaben)
  - täglich: `python manage.py cleanup_events --days 90`
- **Endpunkte**: `/health` (Liveness, ohne Datenbank), `/ready` (prüft die Datenbank), `/metrics` (Prometheus).
  Sie antworten vor der Host-Prüfung, für Probes. `/sync/…` muss für die Macs über HTTPS erreichbar sein; die Konsole
  (`/console/`), das Anfrageformular (`/request/`), `/login/`, `/oidc/…` und `/admin/` können auf Ihr Netz
  beschränkt werden.
- Setzen Sie einen TLS-terminierenden Proxy oder Ingress davor und `TRUST_X_FORWARDED_PROTO=1`.
- **Berechtigungen**: Die SyncBaseURL und die Gruppenprofile enthalten das geheime Sync-Token, die Zugangsdaten der
  Sync-API. Nur Benutzer, die Gruppen ändern dürfen, sehen sie und laden sie herunter; eine reine Leseberechtigung für
  Gruppen genügt dafür nicht.

#### Größe des Webservers

gunicorn läuft als Master (PID 1 im Container) mit Worker-Prozessen. Der Master beantwortet keine Anfragen: Er startet
die Worker, beendet sie bei `SIGTERM` sauber und startet einen neuen, wenn einer abstürzt oder vom OOM-Killer beendet
wird (Log: `Worker (pid:…) was sent SIGKILL! Perhaps out of memory?`). Der Container läuft dabei weiter.

- **Standard: 2 Worker × 4 Threads (`gthread`).** Die meisten Anfragen warten auf SQL Server, Redis oder einen
  Katalog; das erledigen die Threads. Der zweite Prozess hält Konsole, Syncs und `/health` erreichbar, während ein
  anderer rechnet (Archiv entpacken und hashen bei „Jetzt prüfen“ oder einem Upload).
- **Speicher:** gemessen etwa 25 MB für den Master und 90 MB je Worker unter Last. Für den Standard sind 512 MiB als
  Limit angemessen, für einen Worker 256 MiB. Liegt `/tmp` im Speicher (`emptyDir` mit `medium: Memory`), zählen auch
  die heruntergeladenen Releases dazu.
- **Skalieren:** lieber mehr Replikas als mehr Worker je Container (höchstens zwei). Bei wenig Speicher
  `GUNICORN_WORKERS=1` und `GUNICORN_THREADS=8`.
- **Lange Anfragen:** Die stündliche Paketprüfung läuft als eigener Job, nicht im Webserver. „Jetzt prüfen“ und das
  Genehmigen einer Paketanfrage laden die Releases in der Anfrage; das belegt nur einen Thread, der Worker bleibt
  erreichbar. Dauert es länger als das Timeout des Ingress, zeigt der Browser einen Fehler, die Prüfung läuft auf dem
  Server zu Ende: die Seite neu laden. `gevent` oder `eventlet` passen nicht, der SQL-Server-Treiber würde ihre
  Ereignisschleife blockieren.

### Anmeldung

Benutzer melden sich bei einem beliebigen OpenID-Connect-Anbieter an. Mitglieder der Rolle `OIDC_ADMIN_ROLE` werden
Administratoren (Staff, Rolle „Santa admins“ mit allen Santa-Berechtigungen, den Benutzern und den Rollen). Alle
anderen erhalten, was ihre [Anmeldegruppen](#anmeldegruppen-und-rollen) ihnen geben, standardmäßig nur das
Anfrageformular. Lokale Konten (`python manage.py createsuperuser` oder *Administration* → *Benutzer* →
*Neues lokales Konto*) funktionieren weiterhin auf `/login/`, z. B. als Notfallzugang.

Registrieren Sie eine Webanwendung mit der Redirect-URI `https://<host>/oidc/callback/` und setzen Sie die Endpunkte:

- **Microsoft Entra ID**: eine App-Registrierung (Single Tenant) mit Client Secret und einer App-Rolle `Santa.Admin`,
  die den Administratoren zugewiesen ist, Schritt für Schritt unter
  [Einrichtung in Microsoft Entra ID](#einrichtung-in-microsoft-entra-id).
- **Keycloak**: `…/realms/<realm>/protocol/openid-connect/auth`, `/token` und `/certs`; Realm-Rollen stehen in
  `OIDC_ROLES_CLAIM=realm_access.roles`.
- **Okta, Authentik, …**: die Endpunkte aus `/.well-known/openid-configuration` und ein Claim mit den Rollen oder
  Gruppen (z. B. `OIDC_ROLES_CLAIM=groups`).

#### Anmeldegruppen und Rollen

In der Konsole enthält *Administration* die Benutzer, die Rollen (Sammlungen von Berechtigungen: Ansehen /
Hinzufügen / Ändern / Löschen je Seite und was Benutzer anfragen dürfen), die Anmeldegruppen, die Tags und den Export
und Import der Konfiguration.

Eine **Anmeldegruppe** ist eine Gruppe Ihres Anbieters, die Rollen vergibt, und die Konsole, wenn Sie *Zugriff auf
die Konsole* ankreuzen. Legen Sie nur die Gruppen an, auf die es ankommt: Der Server vergleicht den Gruppen-Claim des
ID-Tokens (`OIDC_GROUPS_CLAIM`) mit ihnen und ignoriert die anderen, ohne sie zu speichern. Die Rollen gelten ab der
nächsten Anmeldung; eine geänderte oder gelöschte Anmeldegruppe gilt sofort für ihre Mitglieder. Rollen, die auf der
Benutzerseite von Hand vergeben wurden, bleiben erhalten.

Die Anmeldegruppe `*` („Everyone“) umfasst alle, die sich anmelden. Sie vergibt die Rolle „Santa requesters“, die
blockierte Apps, Pakete und andere Software anfragen darf. Entfernen Sie eine Berechtigung aus dieser Rolle und geben
Sie sie anderen Rollen, um einzuschränken, wer was anfragen darf.

- **Microsoft Entra ID**: der Gruppenanspruch mit den Objekt-IDs der Gruppen, siehe
  [Einrichtung in Microsoft Entra ID](#einrichtung-in-microsoft-entra-id), Schritt 4. Tragen Sie die Objekt-ID jeder
  Gruppe als *Wert im Gruppen-Claim* ein.
- **Keycloak**: ein *Group Membership*-Mapper am Client (Claim `groups`, zum ID-Token hinzufügen); die Werte sind die
  Gruppenpfade, z. B. `/santa/it`.

#### Einrichtung in Microsoft Entra ID

Santa Server liest die Claims aus dem ID-Token (nicht vom Userinfo-Endpunkt, der bei Entra ID keine Rollen liefert).
Im Entra Admin Center:

1. **App-Registrierung** (*App-Registrierungen* → *Neue Registrierung*): *Nur Konten in diesem Organisationsverzeichnis*
   (Single Tenant), Plattform *Web*, Umleitungs-URI `https://<host>/oidc/callback/`.
   - *Zertifikate & Geheimnisse* → neuer geheimer Clientschlüssel: der **Wert** (nicht die ID) ist
     `OIDC_CLIENT_SECRET`. Notieren Sie das Ablaufdatum: Danach schlägt die Anmeldung fehl.
   - Die *Anwendungs-ID (Client)* ist `OIDC_CLIENT_ID`; mit der *Verzeichnis-ID (Mandant)*:
     `OIDC_AUTHORIZATION_ENDPOINT=https://login.microsoftonline.com/<tenant>/oauth2/v2.0/authorize`,
     `OIDC_TOKEN_ENDPOINT=https://login.microsoftonline.com/<tenant>/oauth2/v2.0/token`,
     `OIDC_JWKS_ENDPOINT=https://login.microsoftonline.com/<tenant>/discovery/v2.0/keys`.
   - *API-Berechtigungen*: Microsoft Graph, delegiert `openid`, `profile`, `email`, dann
     *Administratorzustimmung erteilen*.
2. **App-Rolle für die Administratoren** (*App-Rollen* → *App-Rolle erstellen*): Anzeigename z. B. „Santa admin“,
   *Zulässige Mitgliedstypen* Benutzer/Gruppen, **Wert `Santa.Admin`** (genau wie `OIDC_ADMIN_ROLE`, mit Groß- und
   Kleinschreibung). Unter *Unternehmensanwendungen* → die App → *Benutzer und Gruppen* weisen Sie die Rolle den
   Administratoren zu (Gruppen zuweisen erfordert Entra ID P1). Entra ID schreibt sie ohne weitere Einstellung in den
   Claim `roles` des ID-Tokens.
3. **Optionale Ansprüche** (*Tokenkonfiguration* → *Optionalen Anspruch hinzufügen* → Tokentyp *ID*): `email` und
   `upn`. `preferred_username` ist im ID-Token enthalten und wird der Benutzername; er muss zum `MachineOwner` der
   Gruppenprofile passen (siehe [Die Macs konfigurieren](#die-macs-konfigurieren)), sonst zeigt das Anfrageformular
   die Ereignisse der eigenen Macs nicht.
4. **Gruppenanspruch** für die Anmeldegruppen (*Tokenkonfiguration* → *Gruppenanspruch hinzufügen*):
   - *Der Anwendung zugewiesene Gruppen*, und die Gruppen unter *Benutzer und Gruppen* der Unternehmensanwendung
     zuweisen. Mit *Alle Gruppen* erhalten Benutzer mit mehr als 200 Gruppen statt der Gruppen einen
     Überschreitungsverweis (Overage); der Server ignoriert dann ihre Gruppen und schreibt eine Warnung ins Log.
   - Unter *ID*: *Gruppen-ID*. Der Claim `groups` enthält dann die Objekt-IDs der zugewiesenen Gruppen.
   - **Nicht** *Gruppen als Rollenanspruch ausgeben* ankreuzen: Dann stehen die Gruppen statt der App-Rollen im Claim
     `roles`, `Santa.Admin` fehlt (niemand wird Administrator) und der Claim `groups` ebenso (keine Anmeldegruppe
     passt). Die Einstellungen für *Zugriff* und *SAML* liest Santa Server nicht.
5. **Wer sich anmelden darf** (*Unternehmensanwendungen* → die App → *Eigenschaften*): *Zuweisung erforderlich?* auf
   *Ja*. Sonst kann sich jeder im Mandanten anmelden und erhält über die Anmeldegruppe `*` die Rolle
   „Santa requesters“.

Das ID-Token eines Administrators enthält dann z. B.:

```json
"preferred_username": "jdoe@example.com",
"roles": ["Santa.Admin"],
"groups": ["8f1c…-…", "2b7e…-…"]
```

Änderungen gelten ab der nächsten Anmeldung. Prüfen Sie unter *Administration* → *Benutzer*: Administratoren haben die
Rolle „Santa admins“, die anderen die Rollen ihrer Anmeldegruppen.

### Die Macs konfigurieren

Der Server erzeugt die Konfigurationsprofile; laden Sie sie als benutzerdefinierte Profile in Ihr MDM (Intune,
Jamf Pro, Kandji, Mosyle, …):

1. **Basisprofil**: *Gruppen* → *Basisprofil*. Für jeden Mac gleich: erlaubt die Santa-Systemerweiterung (und macht
   sie nicht entfernbar), gibt ihr vollen Festplattenzugriff, erlaubt ihr Hintergrundobjekt und aktiviert ihre
   Mitteilungen. Weisen Sie es **allen** Macs zu, am besten bevor Santa installiert wird, damit die Benutzer keine
   Rückfragen sehen. Wenn Ihr MDM Systemerweiterungen schon mit einer eigenen Payload erlaubt, ergänzen Sie Santa
   stattdessen dort.
2. **Gruppenprofil**: Legen Sie eine **Gruppe** an, dann *Konfigurationsprofil herunterladen*. Weisen Sie es den Macs
   der Gruppe zu. Es enthält:
   - `SyncBaseURL`: die geheime Sync-URL der Gruppe.
   - `SyncEnableProtoTransfer = false`: Dieser Server spricht nur das JSON-Sync-Protokoll.
   - optional `UnknownBlockMessage`, `BannedBlockMessage`, `EnableBadSignatureProtection`, `OnStartUSBOptions`
     (Abschnitt *Nur im Profil*), `BrandingCompanyName`, `BrandingCompanyLogo`, `BrandingCompanyLogoDark`
     (Abschnitt *Branding*, ab Santa 2026.1) und `MachineOwner` (siehe unten).
3. Installieren Sie das Santa-Paket mit Ihrem MDM.

**Jeder Mac darf genau ein Gruppenprofil erhalten**: Zwei Santa-Profile auf demselben Mac stehen im Konflikt. Um
einen Mac in eine andere Gruppe zu verschieben, weisen Sie ihm das Profil der anderen Gruppe zu; bei seiner nächsten
Synchronisierung macht er einen Clean Sync mit den neuen Regeln.

Alles, was der Server bei jeder Synchronisierung sendet (Client-Modus, Pfad-Regexe, Wechseldatenträger,
Sync-Intervall, transitive Regeln, Schaltfläche im Blockierdialog, …), steht absichtlich **nicht** im Profil: Ändern
Sie es in der Konsole, und die Macs übernehmen es bei ihrer nächsten Synchronisierung. Nur nach einer Änderung in
*Nur im Profil* oder *Branding* oder nach *Sync-URL neu erzeugen* laden Sie das Gruppenprofil erneut herunter und
ersetzen es im MDM. Die Identifier bleiben gleich, das Profil wird also an Ort und Stelle aktualisiert.

**Wechseldatenträger** (USB-Sticks, externe Festplatten, SD-Karten): *Erlauben*, *Blockieren* oder *Mit Flags neu
einhängen* (z. B. `rdonly,noexec`: nur lesen, nichts ausführbar), für verschlüsselte Datenträger optional anders. Der
Server sendet es bei jeder Synchronisierung (`removable_media_policy`, für ältere Santa-Versionen zusätzlich
`block_usb_mount` / `remount_usb_mode`). Was mit den beim Start von Santa schon eingehängten Datenträgern passiert
(`OnStartUSBOptions`), steht nur im Profil.

**Branding** (ab Santa 2026.1): Firmenname und Logo in den Dialogen von Santa. Ein hochgeladenes Logo (PNG oder JPEG,
höchstens 256 KB) kommt als `data:`-URL ins Profil; alternativ eine `file:///`-URL eines Bildes, das Ihr MDM auf die
Macs verteilt. `https://`-URLs unterstützt Santa nicht.

**`MachineOwner`** (optional, `SANTA_PROFILE_MACHINE_OWNER`): die MDM-Variable des Hauptbenutzers, damit das
Anfrageformular die Macs des angemeldeten Benutzers findet. Zum Beispiel `{{userprincipalname}}` (Intune) oder
`$EMAIL` (Jamf Pro); prüfen Sie die Variablen Ihres MDM. Ohne sie werden die Macs über den lokalen Kontonamen
zugeordnet (den Teil des Benutzernamens vor dem `@`).

Nicht nötig:
- **Client-Zertifikate**: Das geheime Token in der Sync-URL authentifiziert die Gruppe über HTTPS. Wenn eine URL nach
  außen gelangt, *Sync-URL neu erzeugen* und das Gruppenprofil ersetzen.
- **`ServerAuthRootsFile`**: Santa vertraut dem Systemschlüsselbund. Stammt das Serverzertifikat von einer internen
  CA, verteilen Sie diese CA auf die Macs.
- **`MachineID`**: Der Standard, die Hardware-UUID, ist das, was der Server erwartet.

**Pfad-Regexe** nehmen eine Regex pro Zeile. Santa akzeptiert nur eine Regex pro Einstellung, daher fasst der Server
die Zeilen zu `(?:zeile1)|(?:zeile2)` zusammen. Verankern Sie sie mit `^` und erlauben Sie nie Pfade, in die Benutzer
schreiben können.

#### Geltungsbereich der Regeln

Eine Regel gilt für jeden Mac (*global*), für Gruppen oder für einzelne Macs. Zielen mehrere Regeln auf dieselbe
Kennung auf einem Mac, gewinnt der spezifischste Geltungsbereich (Mac > Gruppe > global), und im selben
Geltungsbereich gewinnt Blockieren über Erlauben. Deaktivierte Regeln werden bei der nächsten Synchronisierung von
den Macs entfernt.

#### Dateizugriffsregeln

*Dateizugriff* legt fest, welche Prozesse welche Dateien lesen oder schreiben dürfen (File Access Authorization von
Santa), zum Beispiel: Nur `ssh` darf die SSH-Schlüssel lesen, nur die Browser ihre Cookies. Eine Regel hat Pfade (mit
`*`, `?` und `[ ]`, oder ein Pfad mit allem darunter), einen Regeltyp (*nur die genannten Prozesse dürfen* /
*die genannten Prozesse dürfen nicht* auf die Pfade zugreifen, oder umgekehrt *die Prozesse dürfen nur auf* / *nicht
auf* die Pfade zugreifen) und Prozesse (Signing ID mit Team ID oder *Plattform-Binary*, Team ID, Pfad, CDHash oder
Zertifikat; alle ausgefüllten Felder müssen passen).

- Santa erhält die Regeln **über das Gruppenprofil** (`FileAccessPolicy`), das JSON-Sync-Protokoll kennt sie nicht:
  Laden Sie nach einer Änderung die Profile der betroffenen Gruppen erneut herunter (die Konsole nennt sie).
- Beginnen Sie mit *Nur protokollieren* und prüfen Sie die Ereignisse auf den Macs, bevor eine Regel blockiert.
- *Dateizugriff überschreiben* in der Gruppe (*Nur protokollieren*, *Deaktiviert*) wird bei jeder Synchronisierung
  gesendet und gilt sofort für alle Regeln der Gruppe, ohne neues Profil.

### Im Alltag

Alles ist in der Konsole (`/console/`). Der Django-Admin (`/admin/`) hat weiterhin jedes Modell für Änderungen auf
niedriger Ebene und den Verlauf jeder Änderung.

**Profil**: Das Bild oben rechts (von Gravatar, zur E-Mail-Adresse) öffnet das Menü: Profil, Design (System, hell,
dunkel), Sprache, Abmelden. Im Profil gibt es außerdem die Zeitzone (Standard: die des Browsers). Die Auswahl wird im
Profil des Benutzers gespeichert. Auf dem Smartphone liegt die Navigation hinter der Menü-Schaltfläche.

**Listen**: Klicken Sie auf eine Spaltenüberschrift zum Sortieren, erneut zum Umkehren. Ein Klick auf eine Zeile
wählt sie aus, Strg-Klick fügt eine hinzu oder entfernt sie, Umschalt-Klick wählt einen Bereich. Die Aktionen für die
Auswahl erscheinen unten im Fenster; Esc hebt die Auswahl auf.

**Seitenleiste**: Ereignisse, Regeln, Paketregeln, Dateizugriffsregeln, Anfragen, Macs, Gruppen, alles unter
*Administration* und der Verlauf öffnen sich in einer Leiste rechts, auch zum Anlegen und Bearbeiten. Nach dem
Speichern wird die Liste dahinter neu geladen. Zurück und Vor des Browsers (auch die Maustasten) wechseln zwischen den
Ansichten der Leiste. Esc oder ein Klick daneben schließt sie, *Seite ↗* öffnet dasselbe als eigene Seite. *Verlauf*
zeigt jede Änderung an einer Regel, Paketregel, Dateizugriffsregel, Gruppe, einem Mac, einer Anmeldegruppe, Rolle,
einem Tag oder Benutzer.

**Gruppen**: die Santa-Konfiguration jeder Gruppe, ihre geheime SyncBaseURL, der Download ihres
Konfigurationsprofils, *Sync-URL neu erzeugen* und in der Liste das Basisprofil.

**Macs**: Filter nach Gruppe, Modus und Macs, die seit 2 Tagen nicht synchronisiert haben. Die Detailseite zeigt die
Hardware, die letzte Synchronisierung, ob der Mac alle seine Regeln hat, alle Regeln, die für ihn gelten (und ob sie
über diesen Mac, seine Gruppe oder alle Macs kommen), seine letzten Ereignisse und Anfragen und *Clean Sync*.
Ausgewählte Regeln können den Mac aus ihrem Geltungsbereich entfernen (*Diesen Mac entfernen*); die Gruppen- und
globalen Geltungsbereiche bleiben, eine Regel nur für diesen Mac wird gelöscht.

**Ereignisse**: *Blockierte Apps* listet die offenen Blockierungen nach Binary gruppiert (Macs, Benutzer, zuletzt
gesehen, offene Anfragen). *Alle Ereignisse* ist die flache Liste; ihre erste Seite fügt alle 5 Sekunden neue
Ereignisse hinzu. Klicken Sie auf einen Dateinamen für die Details und *Regel anlegen*, mit einer Vorschau der
Kennung, die die Regel verwenden wird.

**Blockierte Software erlauben**: Apps oder Ereignisse auswählen → *Regeln anlegen…*. Jedes Binary erhält eine eigene
Zeile: Regeltyp, Richtlinie, Geltungsbereich (die oben gewählten Gruppen, nur die Macs seiner Ereignisse oder alle
Macs), Tags und Beschreibung. *Auf alle Zeilen anwenden* setzt eine Spalte für jede Zeile. Die Regeltypen:
- *Binary*: nur genau diese Datei. Eine neue Version wird wieder blockiert. Für unsignierte Tools.
- *Signing ID*: jede Version dieser App von diesem Entwickler. Am besten für signierte Apps.
- *Team ID*: alles, was dieser Entwickler signiert hat.

Gibt es für die Kennung schon eine manuelle Regel, wird deren Geltungsbereich erweitert, statt eine zweite Regel
anzulegen. Die Ereignisse werden als erledigt markiert.

**Ein Binary hochladen**: *Ausführungsregeln* → *Binary hochladen*. Akzeptiert eine Mach-O-Datei oder ein Zip-/Tar-Archiv
(jede ausführbare Datei darin oder die, die zum Glob-Muster passen). Die Datei wird nicht gespeichert.

**Tags**: Markierungen, um Regeln zu sortieren und zu finden. Sie haben keine Wirkung auf die Macs. Umbenennen und
Löschen unter *Administration* → *Tags*.

**Ausführungsregeln**: Reiter je Regeltyp, Filter für Richtlinie, Geltungsbereich, Tag, Herkunft (manuell oder
Paketregel) und Zustand. Regeln, die Paketregeln angelegt haben, können nur aktiviert oder deaktiviert werden; ändern
Sie stattdessen ihre Paketregel.

**Paketregeln**: *Paketregeln* → *Neue Paketregel*. Für GitHub, npm, VS Code und JetBrains tippen Sie unter *Pakete*,
um den Katalog zu durchsuchen; `owner/teil` durchsucht die Repositories eines GitHub-Owners. Homebrew und URLs werden
eingetippt und mit Enter hinzugefügt.

| Katalog | Kennung | Asset-Muster | Binary-Muster |
|---|---|---|---|
| GitHub-Release | `abiosoft/colima` | `Darwin` | |
| Homebrew-Formula | `colima` | `^arm64_` (Bottle-Tags) | `*/bin/colima` |
| Homebrew-Cask | Cask-Name | Regex auf die Download-URL | Glob im Zip |
| Direkte URL | vollständige URL | | |
| npm-Paket | `@esbuild/darwin-arm64` | | |
| VS-Code-Erweiterung | `rust-lang.rust-analyzer` | `^darwin-arm64$` (Zielplattform) | |
| JetBrains-Plugin | `com.intellij.plugins.watcher` | `^PHPSTORM$` (kompatibles Produkt) | |

Eine Paketregel kann **mehrere Pakete** enthalten, eines pro Zeile. Sie teilen die Einstellungen, aber jedes Paket
wird für sich geprüft und behält seine eigenen Versionen. Eine Paketregel legt außerdem fest, wie die Regeln aussehen:
- **Bevorzugter Regeltyp**: Binary (Standard) oder CDHash für eine Regel je Datei und Version, oder Signing ID,
  Zertifikat, Team ID für signierte Tools (eine Regel für alle Versionen). Unsignierte ausführbare Dateien erhalten
  stattdessen eine Binary-Regel.
- **Richtlinie**: Erlauben, Compiler erlauben, Blockieren, still blockieren oder CEL, dazu Blockiermeldung und URL.
- **Versionsmuster**: eine Regex, z. B. `^1\.`, um bei 1.x zu bleiben.
- **Automatisch freigeben** und eine **Verzögerung** (z. B. 2 Tage nach der Veröffentlichung), um von einem
  kompromittierten Release zu erfahren, bevor es auf den Macs läuft. *Versionen behalten*: Nur die letzten Releases
  bleiben erlaubt.

Hinweise:
- Homebrew-Bottles werden je macOS-Version gehasht. Homebrew kann ein Binary beim Installieren umschreiben
  (Relocation); blockieren die Macs es trotzdem, vergleichen Sie mit dem blockierten Ereignis und nehmen Sie lieber
  das GitHub-Release.
- npm-Tarballs werden gegen den veröffentlichten SHA-512 geprüft, Homebrew-Bottles gegen ihren SHA-256.
- Releases ohne ausführbare Dateien (die meisten npm-Pakete, VS-Code-Erweiterungen und JetBrains-Plugins sind
  Skripte) werden mit 0 Binaries erfasst: Santa hat darin nichts zu erlauben.
- `.dmg`- und `.pkg`-Dateien können nicht entpackt werden; diese Apps sind signiert, erlauben Sie sie mit einer
  Signing-ID- oder Team-ID-Regel.

**Anfragen**: Benutzer öffnen `/request/`, melden sich an und wählen *Auf meinem Mac blockiert* (die offenen
Blockierungen der letzten 30 Tage auf ihren Macs), *Paket* (ein oder mehrere Pakete aus den Katalogen) oder
*Sonstiges*. Welche Arten ein Benutzer anfragen darf, legen seine Rollen fest. Administratoren sehen die Details unter
*Anfragen* und genehmigen (eine Regel für den Mac des Anfragenden, Gruppen oder alle Macs; Pakete in eine neue oder
bestehende Paketregel, jedes Paket für sich; bei *Sonstiges* gleich eine neue Ausführungsregel, per Kennung oder
hochgeladener App, oder eine Paketregel) oder lehnen mit einer Notiz ab.

Um den Blockierdialog mit dem Formular zu verbinden, setzen Sie die *URL im Blockierdialog* der Gruppe auf
`https://<host>/request/new/?sha256=%file_sha%` und den Text der Schaltfläche z. B. auf `Zugang anfragen`.

### Konfiguration exportieren und importieren

Gruppen, Paketregeln, manuelle Regeln und Dateizugriffsregeln können als JSON exportiert und auf einem anderen Server
importiert werden, z. B. von einem Test- auf einen Produktivserver. In der Konsole: *Administration* →
*Export / Import*. Oder:

```bash
python manage.py export_config -o santa-config.json
python manage.py import_config santa-config.json --dry-run     # Änderungen anzeigen, nichts speichern
python manage.py import_config - < santa-config.json            # importieren, "-" liest stdin (z. B. docker exec -i)
```

- Zuordnung: Gruppen und Paketregeln über den **Namen**, manuelle Regeln über **Regeltyp + Kennung + Richtlinie**,
  Mac-Geltungsbereiche über die **Seriennummer**. Die Geltungsbereiche einer Regel werden durch die aus der Datei
  ersetzt.
- **Die Sync-Tokens werden nie exportiert**: Eine bestehende Gruppe behält ihr Token, ihr Profil bleibt also gültig.
- Nicht exportiert: Macs, Ereignisse und die Regeln der Paketregeln (das Ziel baut sie selbst).
- Die Dateizugriffsregeln werden über den **Namen** zugeordnet, ihre Gruppen über deren Namen.
- `--delete-missing` löscht die manuellen Regeln, Paketregeln und Dateizugriffsregeln, die nicht in der Datei sind.
  Gruppen werden nie gelöscht.
- Alles oder nichts: Ist ein einziger Eintrag ungültig, wird nichts importiert und jeder Fehler aufgelistet.

### Entwicklung

Öffnen Sie den Ordner in VS Code → **Reopen in Container**. Der Devcontainer startet SQL Server 2022 und Redis, legt
die Datenbank an und führt die Migrationen aus (`.devcontainer/setup.sh`). Die Werkzeuge laufen als Benutzer `dev`
ohne Rechte mit der UID Ihres Host-Benutzers, die Dateien im Workspace bleiben also Ihre. Dann:

```bash
python manage.py createsuperuser
python manage.py runserver 0.0.0.0:8000     # http://localhost:8000/
python manage.py test                        # gegen SQL Server
ruff check .
```

**Persönliche Einstellungen**: Kopieren Sie `.devcontainer/.env.example` nach `.devcontainer/.env` (von Git und
Docker ignoriert) und bauen Sie den Container neu. Darin:
- `GITHUB_TOKEN`: ein Fine-grained Token mit „Public repositories (read-only)“ und ohne Berechtigungen. Ohne ihn
  erlaubt GitHub 60 API-Anfragen pro Stunde und 10 Suchen pro Minute für Ihre IP.
- `TIME_ZONE`: die Standardzeitzone des Servers, z. B. `Europe/Berlin`.

**Übersetzungen**: Die Texte sind englisch, Deutsch steht in `santa/locale/de/LC_MESSAGES/django.po`. Nach dem Ändern
von Texten: `python manage.py makemessages -l de`, die neuen Einträge übersetzen, `python manage.py compilemessages`
und `.po` und `.mo` committen.

**Mit einem echten Mac testen**: Santa verlangt HTTPS für die Sync-URL, außer für `localhost`. Starten Sie
`python manage.py runserver 0.0.0.0:8000` und machen Sie ihn auf dem Test-Mac als `localhost:8000` erreichbar: Nichts
zu tun, wenn der Devcontainer auf dem Mac läuft (auf Apple Silicon Rosetta in Docker Desktop für SQL Server
aktivieren); sonst öffnen Sie vom Docker-Host einen Reverse-Tunnel mit
`ssh -N -R 8000:localhost:8000 <user>@<test-mac>`. Laden Sie die Profile einer Testgruppe herunter (sie enthalten
`http://localhost:8000/sync/<token>/`), weisen Sie sie nur dem Test-Mac zu, dann `sudo santactl sync` und
`santactl status` auf dem Mac.

Siehe [CONTRIBUTING.md](CONTRIBUTING.md) für Pull Requests und [AGENTS.md](AGENTS.md) für die Regeln des Projekts
(auch für KI-Coding-Agenten). Commit-Nachrichten und diese README werden zuerst auf Deutsch, dann auf Englisch
geschrieben.

### Projektstruktur

```
santa_server/          settings.py (Image, Umgebungsvariablen), settings_common.py, settings_dev.py, urls.py
santa/
  models.py            Group, Machine, Rule, Event, ReleaseSource (Paketregel), ReleaseVersion, AccessRequest,
                       SignInGroup (Anmeldegruppe), FileAccessRule (Dateizugriffsregel)
  sync_views.py        Santa-Sync-Protokoll (preflight, eventupload, ruledownload, postflight)
  rules.py             welche Regeln für einen Mac gelten, und der inkrementelle Regel-Sync
  events.py            Speichern der hochgeladenen Ereignisse
  macho.py             liest Hashes und Kennungen der Codesignatur aus Mach-O-Dateien
  releases.py          Paketregeln (GitHub, Homebrew, npm, VS Code, JetBrains, URL)
  catalog.py           Suche in den Paketkatalogen (Vorschläge und Icons)
  services.py          eine Kennung erlauben, passende Ereignisse erledigen, die Macs eines Benutzers
  auth.py              Anmeldung über OpenID Connect, Anmeldegruppen und Rollen
  profiles.py          die Konfigurationsprofile (Basisprofil, eines je Gruppe)
  config_io.py         Export / Import der Konfiguration (JSON)
  console/             die Konsole und das Anfrageformular (Views, Forms, URLs)
  templates/, static/  Templates; htmx, console/*.js und *.css (eine Datei je Aufgabe)
  admin.py, forms.py   der Django-Admin
  management/commands  sync_release_sources, cleanup_events, export_config, import_config
  locale/              deutsche Übersetzung
  tests/
scripts/               create_database.py (Devcontainer und CI)
entrypoint.sh          Entrypoint des Images
.devcontainer/         Devcontainer (SQL Server + Redis)
.github/               CI (Tests gegen SQL Server) und der Image-Build
```

### Lizenz

Apache License 2.0, siehe [LICENSE](LICENSE). Santa ist ein Projekt von [North Pole Security](https://northpole.dev);
dieser Server steht in keiner Verbindung dazu. htmx ist unter der 0BSD-Lizenz enthalten.

---

## English

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
  their Mac, one or more packages, or anything else. Administrators approve or deny in the console, and create the
  rule right there.
- **Upload** a binary or an archive to allow or block it. The hashes and code signature are read from the file.
- **Administration** in the console: users, roles, sign-in groups (groups of the identity provider that give
  roles), tags, export and import.
- English and German, light and dark theme, works on phones.

It is a plain Django 6.0 application: a server-rendered console with htmx (no frontend build) plus the Django admin,
Microsoft SQL Server through mssql-django, Redis for the cache and the sessions, gunicorn with WhiteNoise.

### Quick start

```bash
docker compose up --build                  # the image, SQL Server and Redis (docker-compose.yml)
docker compose exec web python manage.py createsuperuser
```

Open <http://localhost:8000/>. The compose file is an example with fixed passwords, for trying it out only.

### Configuration

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
| `TIME_ZONE` | | default `UTC`; the console shows the times in the time zone of the browser, or of the user profile |
| `GITHUB_TOKEN` | recommended | for package rules and the catalog search; without it GitHub allows 60 requests per hour |
| `SANTA_PROFILE_ORGANIZATION` | | `PayloadOrganization` of the profiles |
| `SANTA_PROFILE_IDENTIFIER_PREFIX` | set once | prefix of the profile identifiers, e.g. `com.example.santa`. The profile UUIDs are derived from it: **don't change it after the profiles are deployed** |
| `SANTA_PROFILE_MACHINE_OWNER` | | `MachineOwner` of the group profiles, see [Configuring the Macs](#configuring-the-macs) |
| `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET` | for SSO | see [Sign-in](#sign-in) |
| `OIDC_AUTHORIZATION_ENDPOINT`, `OIDC_TOKEN_ENDPOINT`, `OIDC_JWKS_ENDPOINT` | for SSO | endpoints of the provider |
| `OIDC_ADMIN_ROLE`, `OIDC_ROLES_CLAIM` | | default `Santa.Admin` in the claim `roles` (dotted path for nested claims) |
| `OIDC_GROUPS_CLAIM` | | default `groups` (dotted path): the claim matched against the [sign-in groups](#sign-in-groups-and-roles) |
| `OIDC_USERNAME_CLAIMS`, `OIDC_SCOPES`, `OIDC_PROVIDER_NAME` | | default `preferred_username,upn,email`, `openid email profile`, button text |
| `WAIT_FOR_URL` | | entrypoint: before the web server, wait until this URL answers, e.g. a sidecar; other commands (the scheduled jobs) start at once |
| `WAIT_FOR_URL_JOBS` | | entrypoint: `1` to wait before other commands too, when the jobs have the sidecar as well |
| `WAIT_FOR_TIMEOUT` | | entrypoint: exit with an error after waiting this many seconds, so the platform restarts; default `0` = no limit |
| `RUN_MIGRATIONS` | | entrypoint: `0` to skip `migrate` when the web server starts |
| `GUNICORN_WORKERS`, `GUNICORN_THREADS` | | default `2` worker processes with `4` threads each, see [Sizing the web server](#sizing-the-web-server) |
| `GUNICORN_WORKER_CLASS` | | default `gthread`; `sync` only with one thread per worker |
| `GUNICORN_TIMEOUT` | | default `120` seconds without a heartbeat before a worker is restarted |
| `GUNICORN_MAX_REQUESTS` | | default `1000`: a worker is replaced after 1000 to 1100 requests (randomly up to +10 %), `0` = never |

For settings that aren't variables, or values rendered into a file by a secret store: create
`santa_server/settings_local.py` with `from .settings import *` and your overrides, mount it into the container and
set `DJANGO_SETTINGS_MODULE=santa_server.settings_local`.

### Deployment

The image `ghcr.io/expertzentrale/santa-server` (built by GitHub Actions: `edge` from `main` for amd64, `1.2.3`, `1.2`
and `latest` from the release tags for amd64 and arm64) runs on any container platform. It runs as an unprivileged user, and SQL Server and Redis run
separately.

- **Web server**: the default command (gunicorn on port 8000). The entrypoint runs `migrate` first; with several
  replicas set `RUN_MIGRATIONS=0` and run `python manage.py migrate` as a job before the rollout.
- **Static files** are in the image and served by gunicorn (WhiteNoise); no nginx, no bucket.
- **Scheduled jobs**, the same image with another command (they don't wait for `WAIT_FOR_URL`, unless
  `WAIT_FOR_URL_JOBS=1`):
  - hourly: `python manage.py sync_release_sources` (new releases of the package rules, delayed approvals)
  - daily: `python manage.py cleanup_events --days 90`
- **Endpoints**: `/health` (liveness, no database), `/ready` (checks the database), `/metrics` (Prometheus).
  They answer before the host check, for probes. `/sync/…` must be reachable by the Macs over HTTPS; the console
  (`/console/`), the request form (`/request/`), `/login/`, `/oidc/…` and `/admin/` can be limited to your network.
- Put a TLS terminating proxy or ingress in front, and set `TRUST_X_FORWARDED_PROTO=1`.
- **Permissions**: the SyncBaseURL and the group profiles contain the secret sync token, the credential of the sync
  API. Only users who may change groups see and download them; a read-only group permission doesn't.

#### Sizing the web server

gunicorn runs as a master (PID 1 in the container) with worker processes. The master answers no requests: it starts
the workers, stops them cleanly on `SIGTERM`, and starts a new one when one crashes or is ended by the OOM killer
(log: `Worker (pid:…) was sent SIGKILL! Perhaps out of memory?`). The container keeps running.

- **Default: 2 workers × 4 threads (`gthread`).** Most requests wait for SQL Server, Redis or a catalog; the threads
  handle that. The second process keeps the console, the syncs and `/health` responsive while another one computes
  (unpacking and hashing an archive for "Check now" or an upload).
- **Memory:** measured about 25 MB for the master and 90 MB per worker under load. For the default a limit of 512 MiB
  is reasonable, for one worker 256 MiB. If `/tmp` is in memory (`emptyDir` with `medium: Memory`), the downloaded
  releases count too.
- **Scaling:** prefer more replicas over more workers per container (two at most). With little memory set
  `GUNICORN_WORKERS=1` and `GUNICORN_THREADS=8`.
- **Long requests:** the hourly package check runs as its own job, not in the web server. "Check now" and approving a
  package request download the releases in the request; that holds one thread, the worker stays responsive. If it
  takes longer than the timeout of the ingress, the browser shows an error while the check finishes on the server:
  reload the page. `gevent` or `eventlet` don't fit, the SQL Server driver would block their event loop.

### Sign-in

Users sign in with any OpenID Connect provider. Members of the role `OIDC_ADMIN_ROLE` become administrators (staff,
role "Santa admins" with every Santa permission, the users and the roles). Everyone else gets what their
[sign-in groups](#sign-in-groups-and-roles) give them, by default only the request form. Local accounts
(`python manage.py createsuperuser`, or *Administration* → *Users* → *New local account*) still work on `/login/`,
e.g. for break-glass access.

Register a web application with the redirect URI `https://<host>/oidc/callback/`, then set the endpoints:

- **Microsoft Entra ID**: an app registration (single tenant) with a client secret and an app role `Santa.Admin`
  assigned to the administrators, step by step in [Setting up Microsoft Entra ID](#setting-up-microsoft-entra-id).
- **Keycloak**: `…/realms/<realm>/protocol/openid-connect/auth`, `/token` and `/certs`; realm roles are in
  `OIDC_ROLES_CLAIM=realm_access.roles`.
- **Okta, Authentik, …**: the endpoints from `/.well-known/openid-configuration`, and a claim with the roles or
  groups (e.g. `OIDC_ROLES_CLAIM=groups`).

#### Sign-in groups and roles

In the console, *Administration* holds the users, the roles (sets of permissions: view / add / change / delete per
page, and what users may request), the sign-in groups, the tags and the configuration export / import.

A **sign-in group** is a group of your provider that gives roles, and the console if you tick *Console access*.
Add only the groups that matter: the server compares the groups claim of the ID token (`OIDC_GROUPS_CLAIM`) with
them and ignores and never stores the others. The roles apply at the next sign-in; a changed or deleted sign-in
group applies to its members right away. Roles assigned by hand on the user page stay.

The sign-in group `*` ("Everyone") is everyone who signs in. It gives the role "Santa requesters", which may request
blocked apps, packages and other software. Remove a permission from that role, and give it to other roles, to limit
who may request what.

- **Microsoft Entra ID**: the groups claim with the object IDs of the groups, see
  [Setting up Microsoft Entra ID](#setting-up-microsoft-entra-id), step 4. Enter the object ID of each group as
  *Value in the groups claim*.
- **Keycloak**: a *Group Membership* mapper on the client (claim `groups`, add to the ID token); the values are the
  group paths, e.g. `/santa/it`.

#### Setting up Microsoft Entra ID

Santa Server reads the claims from the ID token (not from the userinfo endpoint, which has no roles with Entra ID).
In the Entra admin center:

1. **App registration** (*App registrations* → *New registration*): *Accounts in this organizational directory only*
   (single tenant), platform *Web*, redirect URI `https://<host>/oidc/callback/`.
   - *Certificates & secrets* → new client secret: the **value** (not the ID) is `OIDC_CLIENT_SECRET`. Note the
     expiry date: sign-in fails after it.
   - The *Application (client) ID* is `OIDC_CLIENT_ID`; with the *Directory (tenant) ID*:
     `OIDC_AUTHORIZATION_ENDPOINT=https://login.microsoftonline.com/<tenant>/oauth2/v2.0/authorize`,
     `OIDC_TOKEN_ENDPOINT=https://login.microsoftonline.com/<tenant>/oauth2/v2.0/token`,
     `OIDC_JWKS_ENDPOINT=https://login.microsoftonline.com/<tenant>/discovery/v2.0/keys`.
   - *API permissions*: Microsoft Graph, delegated `openid`, `profile`, `email`, then *Grant admin consent*.
2. **App role for the administrators** (*App roles* → *Create app role*): display name e.g. "Santa admin", *Allowed
   member types* Users/Groups, **value `Santa.Admin`** (exactly like `OIDC_ADMIN_ROLE`, case-sensitive). Under
   *Enterprise applications* → the app → *Users and groups*, assign the role to the administrators (assigning groups
   needs Entra ID P1). Entra ID puts it into the `roles` claim of the ID token without further settings.
3. **Optional claims** (*Token configuration* → *Add optional claim* → token type *ID*): `email` and `upn`.
   `preferred_username` is in the ID token and becomes the username; it must match the `MachineOwner` of the group
   profiles (see [Configuring the Macs](#configuring-the-macs)), otherwise the request form doesn't show the events of
   the user's own Macs.
4. **Groups claim** for the sign-in groups (*Token configuration* → *Add groups claim*):
   - *Groups assigned to the application*, and assign the groups under *Users and groups* of the enterprise
     application. With *All groups*, users with more than 200 groups get an overage reference instead of the groups;
     the server then ignores their groups and logs a warning.
   - Under *ID*: *Group ID*. The `groups` claim then holds the object IDs of the assigned groups.
   - Do **not** tick *Emit groups as role claims*: the groups then replace the app roles in the `roles` claim,
     `Santa.Admin` is missing (nobody becomes an administrator) and so is the `groups` claim (no sign-in group
     matches). Santa Server doesn't read the settings for *Access* and *SAML*.
5. **Who may sign in** (*Enterprise applications* → the app → *Properties*): set *Assignment required?* to *Yes*.
   Otherwise anyone in the tenant can sign in and gets the role "Santa requesters" through the sign-in group `*`.

The ID token of an administrator then contains, for example:

```json
"preferred_username": "jdoe@example.com",
"roles": ["Santa.Admin"],
"groups": ["8f1c…-…", "2b7e…-…"]
```

Changes apply at the next sign-in. Check under *Administration* → *Users*: administrators have the role
"Santa admins", the others the roles of their sign-in groups.

### Configuring the Macs

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
   - optionally `UnknownBlockMessage`, `BannedBlockMessage`, `EnableBadSignatureProtection`, `OnStartUSBOptions`
     (section *Profile only*), `BrandingCompanyName`, `BrandingCompanyLogo`, `BrandingCompanyLogoDark` (section
     *Branding*, Santa 2026.1 and newer), and `MachineOwner` (below).
3. Install the Santa package with your MDM.

**Each Mac must get exactly one group profile**: two Santa profiles on the same Mac conflict. Moving a Mac to another
group means assigning the other group's profile; at its next sync it does a clean sync with the new rules.

Everything the server sends at every sync (client mode, path regexes, removable media, sync interval, transitive rules,
block dialog button, …) is deliberately **not** in the profile: change it in the console and the Macs pick it up at
their next sync. Only after a change in *Profile only* or *Branding*, or after *Regenerate the sync URL*, download the
group profile again and replace it in the MDM. The identifiers stay the same, so it updates in place.

**Removable media** (USB sticks, external disks, SD cards): *Allow*, *Block* or *Remount with flags* (e.g.
`rdonly,noexec`: read only, nothing executable), optionally different for encrypted media. The server sends it at every
sync (`removable_media_policy`, plus `block_usb_mount` / `remount_usb_mode` for older Santa versions). What happens to
the media already mounted when Santa starts (`OnStartUSBOptions`) is only in the profile.

**Branding** (Santa 2026.1 and newer): company name and logo in the dialogs of Santa. An uploaded logo (PNG or JPEG,
at most 256 KB) goes into the profile as a `data:` URL; or a `file:///` URL of an image your MDM puts on the Macs.
Santa doesn't support `https://` URLs.

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

#### Rule scope

A rule applies to every Mac (*global*), to groups, or to individual Macs. If several rules target the same
identifier on a Mac, the most specific scope wins (Mac > group > global), and on the same scope a block wins over an
allow. Disabled rules are removed from the Macs at their next sync.

#### File access rules

*File access* decides which processes may read or write which files (File Access Authorization of Santa), for
example: only `ssh` may read the SSH keys, only the browsers their cookies. A rule has paths (with `*`, `?` and `[ ]`,
or a path with everything below it), a rule type (*only the listed processes may* / *the listed processes may not*
access the paths, or the other way round *the processes may only* / *may not* access the paths) and processes
(Signing ID with Team ID or *Platform binary*, Team ID, path, CDHash or certificate; every field filled in must
match).

- Santa gets the rules **through the group profile** (`FileAccessPolicy`), the JSON sync protocol doesn't have them:
  after a change, download the profiles of the groups concerned again (the console names them).
- Start with *Audit only* and check the events on the Macs before a rule blocks.
- *File access override* of the group (*Audit only*, *Disabled*) is sent at every sync and applies to every rule of
  the group right away, without a new profile.

### Daily use

Everything is in the console (`/console/`). The Django admin (`/admin/`) still has every model for low-level editing
and the history of every change.

**Profile**: the picture in the top right (from Gravatar, for the e-mail address) opens the menu: profile, theme
(system, light, dark), language, sign out. The profile also has the time zone (default: the one of the browser).
The choices are saved in the profile of the user. On phones the
navigation is behind the menu button.

**Lists**: click a column header to sort, again to reverse. A click on a row selects it, Ctrl-click adds or removes
one, Shift-click selects a range. The actions for the selection appear at the bottom of the window; Esc clears it.

**Side panel**: events, rules, package rules, file access rules, requests, Macs, groups, everything under
*Administration* and the history open in a panel on the right, also to create and edit them. After saving, the list
behind it reloads. Back and Forward of the browser (also the mouse buttons) step through the views of the panel. Esc
or a click next to it closes it, *Page ↗* opens the same as a page of its own. *History* shows every change of a rule,
package rule, file access rule, group, Mac, sign-in group, role, tag or user.

**Groups**: the Santa configuration of each group, its secret SyncBaseURL, the download of its configuration
profile, *Regenerate the sync URL*, and the base profile on the list.

**Macs**: filter by group, mode, and Macs that haven't synced for 2 days. The detail page shows the hardware, the last
sync, whether the Mac has all its rules, every rule that applies to it (and whether it comes through this Mac, its
group or all Macs), its recent events and requests, and *Clean sync*. Selected rules can drop the Mac from their
scope (*Remove this Mac*); the group and global scopes stay, a rule only for this Mac is deleted.

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

**Tags**: labels to sort and find rules. They have no effect on the Macs. Rename and delete them in
*Administration* → *Tags*.

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
30 days on their Macs), *Package* (one or more packages from the catalogs) or *Other*. Their roles decide which kinds
they may request. Administrators see the details in *Requests* and approve (a rule for the requester's Mac, groups or
all Macs; packages into a new or an existing package rule, each package on its own; for *Other* a new execution rule
right away, by identifier or uploaded app, or a package rule) or deny with a note.

To link the block dialog to the form, set the group's *block dialog URL* to
`https://<host>/request/new/?sha256=%file_sha%` and the button text to e.g. `Request access`.

### Export and import the configuration

Groups, package rules, manual rules and file access rules can be exported as JSON and imported on another server, e.g.
from a test to a production server. In the console: *Administration* → *Export / import*. Or:

```bash
python manage.py export_config -o santa-config.json
python manage.py import_config santa-config.json --dry-run     # show the changes, save nothing
python manage.py import_config - < santa-config.json            # import, "-" reads stdin (e.g. docker exec -i)
```

- Matching: groups and package rules by **name**, manual rules by **rule type + identifier + policy**, Mac scopes by
  **serial number**. The scopes of a rule are replaced by the ones in the file.
- **The sync tokens are never exported**: an existing group keeps its token, so its profile stays valid.
- Not exported: Macs, events, and the rules created by package rules (the target builds them).
- File access rules are matched by **name**, their groups by their names.
- `--delete-missing` deletes the manual rules, package rules and file access rules that are not in the file. Groups
  are never deleted.
- Everything or nothing: if a single entry is invalid, nothing is imported and every error is listed.

### Development

Open the folder in VS Code → **Reopen in Container**. The devcontainer starts SQL Server 2022 and Redis, creates the
database and runs the migrations (`.devcontainer/setup.sh`). The tools run as the unprivileged user `dev` with the
UID of your host user, so the files in the workspace stay yours. Then:

```bash
python manage.py createsuperuser
python manage.py runserver 0.0.0.0:8000     # http://localhost:8000/
python manage.py test                        # against SQL Server
ruff check .
```

**Personal settings**: copy `.devcontainer/.env.example` to `.devcontainer/.env` (ignored by git and docker) and
rebuild the container. In it:
- `GITHUB_TOKEN`: a fine-grained token with "Public repositories (read-only)" and no permissions. Without it GitHub
  allows 60 API requests per hour and 10 searches per minute for your IP.
- `TIME_ZONE`: the default time zone of the server, e.g. `Europe/Berlin`.

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
(also for AI coding agents). Commit messages and this README are written in German first, then in English.

### Project layout

```
santa_server/          settings.py (image, env vars), settings_common.py, settings_dev.py, urls.py
santa/
  models.py            Group, Machine, Rule, Event, ReleaseSource (package rule), ReleaseVersion, AccessRequest,
                       SignInGroup, FileAccessRule
  sync_views.py        Santa sync protocol (preflight, eventupload, ruledownload, postflight)
  rules.py             which rules apply to a Mac, and the incremental rule sync
  events.py            storage of the uploaded events
  macho.py             reads hashes and code signature identifiers from Mach-O files
  releases.py          package rules (GitHub, Homebrew, npm, VS Code, JetBrains, URL)
  catalog.py           search the package catalogs (suggestions and icons)
  services.py          allow an identifier, resolve the matching events, the Macs of a user
  auth.py              OpenID Connect sign-in, sign-in groups and roles
  profiles.py          the configuration profiles (base profile, one per group)
  config_io.py         export / import of the configuration (JSON)
  console/             the console and the request form (views, forms, urls)
  templates/, static/  templates; htmx, console/*.js and *.css (one file per task)
  admin.py, forms.py   the Django admin
  management/commands  sync_release_sources, cleanup_events, export_config, import_config
  locale/              German translation
  tests/
scripts/               create_database.py (devcontainer and CI)
entrypoint.sh          entrypoint of the image
.devcontainer/         devcontainer (SQL Server + Redis)
.github/               CI (tests against SQL Server) and the image build
```

### License

Apache License 2.0, see [LICENSE](LICENSE). Santa is a project of [North Pole Security](https://northpole.dev);
this server is not affiliated with it. htmx is included under the 0BSD license.
