# Changelog – santa-server

> 🇩🇪 Deutsche Version · [English version](CHANGELOG.md)
>
> Die englische CHANGELOG.md ist maßgeblich und wird synchron gehalten.

## [Unreleased]

### Hinzugefügt

- **Download-Zeilen für die Download-Liste.** Die Downloads einer Paketregel „Download-Liste (JSON)“
  sind jetzt eine Tabelle im Paketregel-Formular, eine Zeile je Download. Eine Zeile lässt sich auf die
  Einträge mit *Feld = Wert* beschränken und nimmt dann entweder die Downloads, die der Eintrag
  aufführt (Pfad, URL-Feld, optional nur die Downloads mit *Feld = Wert*), oder baut die URL aus Feldern
  des Eintrags (eine https-URL mit `{Feld}`, Punkte für verschachtelte Felder). Das ersetzt die
  Textfelder für Download-Pfade, URL-Feld und `feld=regex`-Filter; bestehende Paketregeln und ältere
  Exportdateien werden automatisch umgewandelt.
- **Eine Version für die ganze Liste.** Auf Wunsch ist jeder Stand einer Download-Liste eine Version mit
  den Downloads aller ihrer Einträge, benannt nach einem Namensfeld und dem Versionsfeld. Eine
  Paketregel kann so zum Beispiel alle Browser abdecken, die das Browser-Manifest eines Test-Frameworks
  festlegt; *Behalten* zählt diese Stände.
- **Eine hilfreichere Vorschau der Download-Liste.** Die Vorschau zeigt die ersten Einträge der Liste
  mit ihren Feldern, nennt Zeilen ohne Download samt Grund (mit den Werten, die die Liste stattdessen
  hat), fragt jede Datei ab, ohne sie herunterzuladen (HEAD-Anfrage, nur https), und zeigt, woher jede
  Datei stammt.

### Geändert

- **„Aktiviert“ weiter oben.** Der Schalter *Aktiviert* einer Paketregel steht jetzt direkt unter ihrem
  Namen.

### Behoben

- **Aufräumen nach geänderten Download-Zeilen.** *Aufräumen* findet jetzt die Regeln einer entfernten
  oder geänderten Download-Zeile: Dateien merken sich ihre Zeile, ältere werden über den Dateinamen und
  die Bedingung der Zeile zugeordnet.
- **Vorschau der Paketregel.** Mit gespeicherten, unveränderten Download-Zeilen meldet die Vorschau
  nicht mehr „Fügen Sie mindestens einen Download hinzu“, und ein wiederhergestellter Vorschau-Entwurf
  von vor der Tabelle bricht das Formular nicht mehr („ManagementForm data is missing“).
- **Versionsmarke in der Vorschau.** Das ✓ für die Version erscheint nur noch, wenn die Paketregel ein
  Versionsmuster hat; auf dem Handy stand es allein in einer Zeile.

## [0.11.0] - 2026-10-22

### Hinzugefügt

- **Paketregel-Katalog „Download-Liste (JSON)“.** Für Software, die ihre Versionen nur als JSON-Liste
  veröffentlicht, mit Einträgen je Version und Downloads je Produkt und Plattform. Einstellungen: Pfad
  zu den Versionen (`*` für Objekte), Versionsfeld, Downloads je Eintrag (ein Pfad pro Zeile), URL-Feld
  und `feld=regex`-Filter. Versionen werden als Zahlen verglichen, *Behalten*, *Auch die älteren
  behaltenen Versionen erlauben*, die Übersicht und *Aufräumen* richten sich also nach der Version.
  Findet die Liste nichts, nennen Vorschau und Prüfung den Schritt, der scheitert, und was dort
  stattdessen steht. Liste und Dateien werden nur über https geladen, auch nach Weiterleitungen; die
  Liste ist durch `RELEASE_MAX_LIST_BYTES` (Standard 20 MB) begrenzt, die Dateien durch die bestehenden
  Download-Limits. Enthalten in Export / Import und im Django-Admin.
- **Notizen für Prüfer bei Anfragen.** Eine neue, optionale „Notiz für Prüfer“ bleibt in der Konsole.
  Die Nachricht an die anfragende Person wird nur noch mit eingeschaltetem Schalter verschickt
  (Standard aus); beides ist optional, auch beim Ablehnen.
- **Kommentare ignorierter Apps sind sichtbar.** Der Kommentar steht im Ereignis-Drawer, mit wer und
  wann, und unter dem Badge in *Blockierte Apps*.
- **Datenbank-Metriken pro View.** `/metrics` enthält jetzt `santa_db_connect_seconds` und pro View
  `santa_view_db_queries`, `santa_view_db_seconds` und `santa_view_db_connect_seconds` (Labels `view`,
  `method`, `rows`).

### Geändert

- **Langlebigere Datenbankverbindungen.** `DB_CONN_MAX_AGE` legt fest, wie lange eine
  Datenbankverbindung wiederverwendet wird (Standard 600 s, bisher fest 60 s); der Aufbau einer neuen
  Verbindung kostete oft mehr als alle Abfragen einer Anfrage.
- **Paketregel-Formular und -Übersicht.** Das Formular zeigt nur die Hilfe des gewählten Katalogs. Die
  Übersicht zeigt Dateinamen mit einem Link auf ihren Host statt der doppelten URL sowie die Dateien je
  Version. Lange Bezeichner und Vorschau-Überschriften brechen auf dem Handy um.
- **Platzhalterzeilen passen zur Liste.** Beim Laden zeigt eine Liste so viele graue Zeilen wie beim
  letzten Mal (je Liste in der Session gemerkt, 10 für eine neue) statt bis zu 50.

### Behoben

- **„Regeln anlegen“ folgt der Auswahl.** Eine geänderte Auswahl in der Liste lädt nicht mehr den
  ganzen Drawer neu: Abgewählte Binaries verschwinden ohne Anfrage, neue kommen nach einer kurzen Pause
  hinzu, und Eingaben bleiben erhalten. Bisher galt die abgebrochene Anfrage als Verbindungsverlust, und
  bei langsamen Antworten konnte der Drawer hängen bleiben.

## [0.10.0] - 2026-10-20

### Hinzugefügt

- **Modus je Mac.** Ein Mac lässt sich unabhängig von seiner Gruppe auf Monitor oder Lockdown stellen,
  dauerhaft oder für eine Zeit (1 Stunde bis 30 Tage oder bis zu einem Datum). Der Preflight schickt ihn
  statt des Gruppenmodus und kehrt nach dem Ende zurück; das Profil bleibt gleich. *Macs* → Mac →
  *Modus* → *Ändern*.
- **Ablaufdatum für Regeln.** Regeln können ablaufen (Standard: nie): im Regelformular, als Sammelaktion
  und bei der Freigabe einer Anfrage (z. B. „Erlaubt: für 7 Tage“). Abgelaufene Regeln werden bei der
  nächsten Synchronisierung von den Macs entfernt, und der neue stündliche Befehl
  `cleanup_expired_rules` löscht sie. Export / Import enthalten das Ablaufdatum.
- **Blockierte Apps ignorieren.** Eine App verschwindet aus den Listen der Konsole, auch mit späteren
  Ereignissen: nach Signing ID (Standard bei signierten Apps), Team ID, SHA-256 oder Pfad-Präfix, für
  eine gewählte Dauer und mit Kommentar. Aus der Sammelleiste, dem Zeilenmenü und dem Ereignis-Drawer.
  Der Status-Filter *Ignoriert* bietet *Nicht mehr ignorieren*, und *Administration* → *Ignorierte
  Apps* listet sie auf. Regeln, Macs und die Anfragen der Benutzer bleiben unberührt. Enthalten in
  Export / Import; abgelaufene Einträge entfernt `cleanup_expired_rules`.
- **Blockierte Apps im Detail.** Die Zahlen der Blockierungen, Macs und Benutzer in *Blockierte Apps*
  öffnen diese im Drawer mit den Filtern der Liste, mit Reitern zwischen den dreien (die neuesten 100).
  Eine neue Spalte *Zuletzt bei* zeigt Benutzer und Mac des neuesten Ereignisses.
- **Ältere Versionen von Homebrew-Tap-Formeln** werden aus der Git-Historie der Formeldatei gelesen
  (eine GitHub-API-Anfrage je Prüfung). `GITHUB_TOKEN` wird empfohlen.
- **Rollen von Hand für Anmelde-Benutzer.** Je Benutzer lassen sich Rollen und Konsolenzugang von den
  Anmeldegruppen erben oder von Hand setzen; die Admin-Rolle aus dem Token gilt weiterhin.

### Geändert

- **Fortschritt der Paketprüfungen** nennt die geprüfte Version und Abhängigkeit.
- **Sammelleiste der Ereignisse**: Die Aktion wird aus einer Liste gewählt und mit *Anwenden*
  ausgeführt, wie bei den Regeln.
- **Breite Tabellen**: Mit ausgewählten Zeilen bleibt die waagerechte Scrollleiste über den
  Sammelaktionen, die Seite wächst mit einer Tabelle, die breiter als das Fenster ist, und die obere
  Leiste reicht wieder bis zum rechten Rand.

### Behoben

- **Regeln für Homebrew-Formeln passen jetzt zu den installierten Binaries.** Homebrew schreibt beim
  Installieren die Ladebefehle der Binaries um und signiert sie auf Apple Silicon neu; der Server macht
  jetzt dasselbe mit dem geprüften Bottle, die Regeln haben also den Hash, den Santa sieht. Was sich
  nicht vorhersagen lässt, behält den Hash des Bottles, und die Version nennt Datei und Grund. Nach dem
  Update einmal `python manage.py sync_release_sources --rerecord-homebrew` ausführen, um die
  bestehenden Versionen neu zu erfassen (Versionen, die auf manuelle Freigabe warten, müssen erneut
  freigegeben werden). Archive in einem Bottle werden nicht mehr entpackt.
- **Homebrew-Taps**: Formeln mit Konstanten und lokalen Variablen, einer Architektur-Weiche oder dem
  Programm in einer Plattform-Ressource werden korrekt gelesen; Formeln, die ihr Programm beim
  Installieren neu signieren, werden in der Version genannt.
- **Zu große Executables** über `RELEASE_MAX_FILE_BYTES` stehen im Fehler der Version, statt
  stillschweigend zu fehlen.

### Abhängigkeiten

- Python-Pakete: hiredis 3.5.0, ruff 0.17.1.

## [0.9.0] - 2026-10-16

### Hinzugefügt

- **Paketregeln aus Ereignissen.** *Regeln anlegen…* hat einen Reiter *Als Paket*, der je Binary ein
  Paket vorschlägt (eine bekannte Release-Datei per SHA-256, Bundle-ID oder App-Name in den
  Homebrew-Casks, der Installationspfad), vor dem Speichern änderbar, und danach jedes Binary prüft und
  nachprüft. Das Ereignis-Detail zeigt einen Hinweis, *Blockierte Apps* einen Paket-Chip.
- **Mehrere Muster und eine Vorschau in Paketregeln.** Versions-, Asset- und Binary-Muster nehmen
  mehrere Werte (als Chips). Das Formular zeigt eine Vorschau der neuesten Releases je Paket mit ✓ / ✗
  für Version und Dateien.
- **Disk-Images und Installer-Pakete.** `.dmg`- und `.pkg`-Dateien lassen sich hochladen und in
  Paketregeln verwenden (7-Zip im Image, Zeitlimit `RELEASE_UNPACK_TIMEOUT`).
- **Benachrichtigungen in der Konsole** unter einer Glocke (Toast, Panel, ganze Seite);
  `cleanup_events --notification-days` entfernt alte.
- **Filter „Mac“ und „Benutzer“** für Ereignisse und Ausführungsregeln: mehrere Werte per Suche, „ist /
  ist nicht“. Bei Regeln zeigen sie die Regeln, die auf dem Mac bzw. auf den Macs des Benutzers gelten.
  Alte Links mit `?machine=` und `?user=` funktionieren weiter.
- **Homebrew**: Taps (`owner/tap/name`), die minimale macOS-Version und das nächstliegende passende
  Bottle.
- **Kommentare bei Tags**, im Export enthalten.
- **Spalten der Ereignisse**: Pfad, Gruppe und Team ID lassen sich als eigene Spalten einblenden.
- **Team-ID-Regeln mit Signing-ID-Präfixen** werden ausgewertet: Ereignisse und Anfragen zeigen nicht
  mehr fälschlich „Regel vorhanden“, die Signing ID lässt sich zur bestehenden Regel hinzufügen (aus dem
  Ereignis, der Mehrfachauswahl und der Freigabe einer Anfrage), und erledigt werden nur die Ereignisse,
  die die Regel tatsächlich erlaubt.
- **Download-Limits als Umgebungsvariablen**: `RELEASE_MAX_DOWNLOAD_BYTES`, `RELEASE_MAX_FILE_BYTES` und
  `RELEASE_MAX_UNPACKED_BYTES`.

### Geändert

- **„Jetzt prüfen“ läuft im Hintergrund.** Die Prüfung läuft weiter, wenn der Drawer geschlossen wird,
  ist für alle sichtbar und lässt sich abbrechen (sie endet nach dem aktuellen Paket). Je Paketregel
  läuft nur eine Prüfung gleichzeitig, auch mit dem geplanten Job. Neue Pakete werden zuerst geprüft;
  beim Annehmen von Anfragen und bei Ereignis-Paketen wird nur das Hinzugefügte geprüft.
- **Schnellere Paketprüfungen**: parallele Downloads (vier, also bis zum Vierfachen des Download-Limits
  an temporärem Speicher), keine Registry-Abfragen für bekannte Homebrew-Versionen, Wiederholung bei
  Verbindungsfehlern, jedes Bottle nur einmal je Prüfung geladen, nur Mach-O-Executables entpackt,
  Regeln in Blöcken angelegt.
- **Lange Aktionen zeigen ihren Fortschritt mit Herzschlag**: Ein Absturz oder Neustart des Servers
  wird erkannt und angezeigt. Beim Herunterfahren enden Prüfungen sauber nach dem aktuellen Paket.
- **Aufräumen** entfernt auch Regeln, deren Dateien kein Asset- oder Binary-Muster mehr abdeckt.
- **Ob etwas erlaubt ist, entscheidet sich pro Mac**, wie bei Santa (Geltungsbereich, Richtlinie,
  Regeltyp); Ereignis-Pakete erledigen nur die Ereignisse der abgedeckten Macs.
- **Eine schnellere Konsole** bei vielen Regeln: Gruppen-Reiter, CSV-Export der Regeln, gefilterte
  Regellisten, Paketregel-Drawer und Mac-Ansicht; Listen laden nur die Felder, die sie zeigen, und der
  Sync eines Macs berechnet seine Regeln effizienter. Der Drawer öffnet sofort mit einem Platzhalter.
- **Layout**: klebender Tabellenkopf auch bei breiten Tabellen, klebende Seitennavigation,
  Reiterleisten mit Scroll-Pfeilen, die Lichterkette blendet beim Scrollen aus, und Korrekturen fürs
  Handy (Sammelleiste, Zähler der Glocke, Filtermenüs, lange Chips). Formularfelder haben korrekte
  Beschriftungen und kein automatisches Ausfüllen.

### Behoben

- **Paketprüfungen sind robuster**: Vorübergehende Fehler (Verbindungsfehler, 429, 5xx, volle Platte)
  werden nicht mehr als dauerhafte Fehler gespeichert, ein defektes Archiv oder ein unerwarteter Fehler
  stoppt nicht mehr den ganzen Job, und `Retry-After` wird beachtet (höchstens 60 s).
- **Homebrew**: Daten ohne Uhrzeit, Formeln ohne Executables und Formeln nur für Linux (eine Meldung und
  ein Knopf, um sie aus der Paketregel zu entfernen). Abhängigkeitsversionen kommen nur noch aus den
  Bottles, die die Paketregel nimmt.

### Sicherheit

- **Sync-Tokens werden maskiert** in den Logs von gunicorn und Django. Wurden Tokens bereits
  protokolliert, sollten Sie erwägen, die Sync-URLs neu zu erzeugen.

### Abhängigkeiten

- Python-Pakete: Django 6.0.9, tzdata 2026.5.

## [0.8.3] - 2026-10-12

### Behoben

- **Schließen des Drawers nach „Als Seite öffnen“ und Zurück** lädt nicht mehr die ganze Liste neu.

## [0.8.2] - 2026-10-12

### Behoben

- **Textfelder behalten ihre Höhe** nach „Als Seite öffnen“.
- **Fortschrittsbalken**: Er bleibt nicht mehr stehen und fehlt nicht mehr, wenn beim Laden ein Element
  ersetzt wird.
- **⋯-Menü** öffnet am angeklickten Knopf, ohne zu springen.

## [0.8.1] - 2026-10-09

### Geändert

- **Textfelder wachsen mit ihrem Inhalt** (bis 20rem, danach scrollen sie) und lassen sich nur noch
  senkrecht ziehen.
- **CEL-Vorschläge ohne Tippen**: Ein leeres Feld und jede Stelle, an der ein Wert beginnen kann, zeigen
  alle passenden Vorschläge; nach `target.` alle Felder, nach einem Wert und einem Punkt alle
  Funktionen.

### Behoben

- **Code in Hilfetexten bricht auf dem Handy um**: Die langen CEL-Beispiele machten das Regelformular
  breiter als den Bildschirm.

## [0.8.0] - 2026-10-09

### Hinzugefügt

- **Dekoration im Weihnachtsdesign**: Die Lichterkette hängt in Bögen unter der Menüleiste, und ein
  Untermenü *Dekoration* im Benutzermenü schaltet Schnee und Lichterkette ein oder aus (im Browser
  gespeichert).

### Geändert

- **Dunkles Design** mit helleren Grautönen, auch in hellem Licht lesbar und mit Kontrasten nach
  WCAG 2.2 AA für Text und Rahmen von Bedienelementen.
- **Schnee braucht deutlich weniger Rechenzeit und Speicher**; in Ruhe läuft keine Animation, und bei
  „Bewegung reduzieren“ bewegt sich nichts.

## [0.7.0] - 2026-10-08

### Hinzugefügt

- **Anfragen genehmigen**: eine CEL-Richtlinie (auch mit Signing-ID-Präfixen) in allen
  Genehmigungsformularen, eine Katalogsuche für Pakete und die Optionen Behalten, ältere Versionen und
  Abhängigkeiten direkt bei der Genehmigung.
- **Paketregeln: mehr Möglichkeiten, Versionen zu behalten.** *Behalten* nimmt die letzten *n*
  Versionen oder die Versionen der letzten *n* Wochen oder Monate (die neueste bleibt immer). *Auch die
  älteren behaltenen Versionen erlauben* funktioniert für GitHub, Homebrew-Formeln, npm, VS Code und
  JetBrains.
- **Paketregeln: Abhängigkeiten erlauben** für npm-Pakete (auch optionale für macOS) und
  Homebrew-Formeln, auch indirekte, begrenzt durch `RELEASE_MAX_DEPENDENCIES`.
- **Paketregeln: Aufräumen** zeigt und löscht die Versionen, die die Paketregel nicht mehr abdeckt.
- **Konfigurationsimport mit bearbeitbarer Vorschau**: Einträge überspringen, umbenennen oder
  deaktivieren und Löschungen behalten, ohne die Datei erneut hochzuladen.
- **Weihnachtsdesign**: `SANTA_CHRISTMAS_THEME` im Dezember, `SANTA_CHRISTMAS_THEME_FORCE` ganzjährig
  (Standard aus), mit Farben, Lichterkette, Mütze, Schnee und festlichen E-Mails.
- **Verbindungsabbrüche werden erkannt**: Ist der Server weg, sagt die Konsole das, versucht es erneut
  und lädt die angeklickte Seite selbst, sobald er antwortet. Hängende Anfragen werden erkannt und
  wiederholt; ein POST während des Ausfalls wird nicht erneut gesendet, und der Hinweis sagt, dass er
  nicht gespeichert wurde.

### Geändert

- **Die Konsole wechselt Seiten ohne vollständiges Neuladen** (htmx), auch mit Zurück / Vor; Design und
  Sprache wechseln ohne Neuladen, ebenso die Anmeldung mit lokalem Konto.
- **Listen laden ihre Zeilen nach der Seite**, mit grauen Platzhalterzeilen in der echten Tabelle und
  einem Fortschrittsbalken für Anfragen, Uploads und Downloads. *Binary hochladen* öffnet im Drawer, und
  die gewählte Datei bleibt nach einem Formularfehler erhalten.
- **Handy**: Aktionszeilen, Auswahlleiste, Paketsuche, Abstände, Mehrfachauswahl unter iOS und
  Spaltenbreiten verbessert. Karten, Tabellen und Drawer haben keine Schatten mehr.

## [0.6.2] - 2026-10-07

### Geändert

- **Gespeicherte Ansichten**: Umbenennen und Löschen nutzen dieselben Symbole und Knöpfe wie der Rest
  der Konsole; auf dem Handy bricht das Menü um.

## [0.6.1] - 2026-10-07

### Geändert

- **Branding**: Die Hilfetexte sagen, dass Santa den Firmennamen nur ohne Firmenlogo zeigt, und das
  Gruppenformular weist darauf hin, wenn eine Gruppe beides hat.
- **Drawer**: „Regel anlegen“ (Ereignis) sowie „Freigeben“ / „Ablehnen“ (Anfrage) bleiben wie in den
  anderen Formularen unten im Drawer sichtbar.

### Behoben

- **Suche mit Enter** funktioniert wieder in den Listen der Regeln und Anfragen.

## [0.6.0] - 2026-10-07

### Hinzugefügt

- **Gruppen auf Basis einer anderen Gruppe.** Eine Gruppe kann alle Einstellungen einer Basisgruppe
  übernehmen (eine Ebene), außer denen mit *Eigener Wert*. Mit *Regeln erben* gelten die Regeln der
  Basis, mit dem Vorrang Mac > Gruppe > Basisgruppe > global, dann Blockieren > Erlauben; mit
  *Dateizugriffsregeln erben* kommen diese in das Profil der Gruppe. Die Konsole nennt abhängige
  Gruppen, wenn sich das Profil der Basis ändert, eine Basis lässt sich erst löschen, wenn keine Gruppe
  mehr auf ihr basiert, und die Mac-Ansicht kennzeichnet geerbte Regeln. Enthalten in Export / Import.
- **E-Mail-Standards pro Anmeldegruppe**: womit ihre Mitglieder je Benachrichtigung beginnen. Benutzer
  können *Standard (…)* wählen, und ihre eigene Wahl hat immer Vorrang; bei mehreren Gruppen gilt die mit
  den meisten E-Mails, und *Keine E-Mails* blockiert immer.
- **Unbekannte Programme im Monitor-Modus** (`ALLOW_UNKNOWN`) stehen unter *Blockierte Apps* und
  zählen zu den offenen Blockierungen von Macs und Gruppen, mit dem Badge *Monitor: im Lockdown
  blockiert*. Das Anfrageformular der Benutzer bietet weiterhin nur echte Blockierungen an.

### Geändert

- **Die Konsole auf dem Handy**: Ein Tipp auf eine Karte schaltet nur diese um, ein größeres
  Kästchen, *Alle auswählen*, hervorgehobene ausgewählte Karten, eine Sammelleiste, die die letzte Karte
  nicht mehr verdeckt, ein Drawer, dessen Kopf und Knöpfe sichtbar bleiben, und nichts mehr breiter als
  der Bildschirm.

## [0.5.2] - 2026-10-06

### Behoben

- **E-Mail**: Ein Fehler beim Schließen der Verbindung zum Mailserver wird nur protokolliert und bricht
  die tägliche Zusammenfassung nicht mehr ab.

## [0.5.1] - 2026-10-06

### Hinzugefügt

- **Konten ohne Postfach**: Eine Anmeldegruppe lässt sich auf *Keine E-Mails* stellen, und ein Benutzer
  als Ausnahme auf *Keine E-Mails* oder *E-Mails erlaubt* (unter *Administration* → *Benutzer*).

### Geändert

- **Status-Filter der Ereignisse** ist eine Mehrfachauswahl (*Offen* / *Erledigt*) wie der Status der
  Anfragen.

### Behoben

- **Eine abgelehnte E-Mail-Adresse hält die anderen nicht mehr auf**: Jede E-Mail geht einzeln über
  dieselbe Verbindung, und eine abgelehnte Adresse wird als Warnung protokolliert.

## [0.5.0] - 2026-10-06

### Hinzugefügt

- **E-Mail-Benachrichtigungen**: entschiedene Anfrage (an die anfragende Person), neue Anfragen,
  Paketversionen zur Freigabe, automatisch freigegebene Versionen und Fehler von Paketregeln, jeweils
  nur mit den passenden Rechten angeboten. Je Benutzer *Aus*, *Sofort* oder *Tägliche Zusammenfassung*;
  die Zusammenfassung verschickt der neue Befehl `send_notification_digest`. Einstellungen:
  `EMAIL_HOST` (leer = aus), `EMAIL_PORT`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`, `EMAIL_USE_TLS` /
  `EMAIL_USE_SSL`, `DEFAULT_FROM_EMAIL`; `EMAIL_NOTIFICATIONS_ENABLED=false` schaltet sie ab. Ein Fehler
  beim Versand bricht die Aktion nie ab.
- **Homebrew-Suche**: Formeln und Casks lassen sich im Katalog durchsuchen (die Listen werden einmal am
  Tag geladen).
- **Benutzer löschen** unter *Administration* (nicht das eigene Konto; Benutzer mit Anfragen oder
  Einträgen im Verlauf werden stattdessen deaktiviert).
- **Anfragen: bestehende Ausführungsregel per Suche wählen** bei der Freigabe sonstiger Software;
  verknüpft wird genau die gewählte Regel.
- **Prometheus-Metriken für SQL Server**: `/metrics` enthält jetzt die `django_db_*`-Metriken zu
  Abfragen, Dauer, Fehlern und Verbindungen. `METRICS_ENABLED=false` schaltet die Metriken ganz ab.
- **Servername und Favicon**: ein Favicon mit Weihnachtsmütze, `SANTA_SERVER_NAME` für den Namen in
  Titeln und Kopfzeile, `SANTA_FAVICON_URL` für ein eigenes Favicon.
- **Logo auf der Anmeldeschaltfläche**: `OIDC_PROVIDER_ICON=microsoft` für die eingebaute Schaltfläche
  oder eine URL / ein Pfad für das Logo eines anderen Anbieters.

### Geändert

- **Ablageflächen für Uploads** (Konfigurationsimport, Binary hochladen, Hash bei der Freigabe,
  Gruppenlogo).
- **Gruppenliste** mit Suche, Modus-Filter, gespeicherten Ansichten, Spalten und Seiten.
- **Ja/Nein-Felder sind Schalter**, Checkboxen im Stil der Konsole, Benutzer der Anmeldung zeigen eine
  Rollenliste (aus der Anmeldung und von Hand), und Listen im Drawer scrollen die Seite dahinter nicht
  mehr mit.
- **Regelbeschreibungen** stehen einzeilig mit „…“ und dem vollen Text im Tooltip; das erneute Öffnen
  der Ansicht, die der Drawer schon zeigt, legt keinen neuen Verlaufseintrag mehr an.

## [0.4.0] - 2026-10-05

### Hinzugefügt

- **Wechseldatenträger**: erlauben, blockieren oder mit Optionen neu einhängen, auf Wunsch anders für
  verschlüsselte Datenträger (ältere Santa-Versionen erhalten die USB-Einstellungen). Bestehende
  USB-Einstellungen werden übernommen, und alte Exportdateien werden verstanden.
- **Branding und USB-Optionen im Gruppenprofil**: `OnStartUSBOptions`, Firmenname und Logos (hell und
  dunkel, hochgeladen oder als `file:///`-URL, mit Vorschau).
- **Dateizugriffsregeln**: eine Seite *Dateizugriff* für Regeln der File Access Authorization mit ihren
  Prozessen. Sie erreichen die Macs über das Gruppenprofil (`FileAccessPolicy`); nach einer Änderung
  nennt die Konsole die Gruppen, deren Profil neu verteilt werden muss. Enthalten in Export / Import.
- **Mehr im Drawer**: Benutzer, Rollen, Anmeldegruppen, Tags, Export / Import und neue Gruppen;
  Ausführungsregeln lassen sich dort löschen.

### Geändert

- **Drawer-Verlauf**: Jede Ansicht des Drawers ist ein Eintrag im Browserverlauf, Zurück / Vor gehen
  also durch die Ansichten, und nach einem Neuladen öffnet die Ansicht wieder. Text markieren und
  außerhalb loslassen schließt den Drawer nicht mehr.

### Abhängigkeiten

- GitHub Actions: docker/setup-qemu-action 4.
- Python-Pakete: cryptography 50.0.2, ruff 0.16.10.

## [0.3.5] - 2026-10-05

### Hinzugefügt

- **Filterleiste auf allen Listen**: Suche, „+ Filter“ und die aktiven Filter als Chips; ein Filter
  kann mehrere Werte haben, Zeitfilter nehmen einen Datumsbereich, und Regeln lassen sich nach dem
  Erstellungsdatum filtern. Der letzte Filter jeder Liste bleibt im Profil gespeichert, und Filter
  lassen sich als benannte **Ansichten** speichern.
- **Regeln aus Ereignissen mit CEL**: eine CEL-Richtlinie mit Ausdruck und „Nur Signing IDs, die
  beginnen mit“, je Binary aus dessen eigener Team ID geschrieben.

### Geändert

- **Tabellen und Layout**: Ein Doppelklick auf den Spaltenrand passt die Breite an den Inhalt an, die
  Ereignisliste zeigt den ganzen Pfad und verlinkt vorhandene Regeln, und das Layout bleibt beim
  Seitenwechsel ruhig.

## [0.3.0] - 2026-10-02

### Hinzugefügt

- **Die Anfrageseite zeigt, was es schon gibt**: Pakete, die auf den Macs des Benutzers bereits
  erlaubt sind, sind ausgegraut und werden abgelehnt, frühere Anfragen stehen als Badge mit Datum und
  Status, bereits angefragte Programme werden nicht mehr angeboten, und *Meine Anfragen* hat einen
  Zeitraumfilter.

### Geändert

- **`WAIT_FOR_URL` gilt nur noch vor dem Webserver.** Andere Befehle (geplante Jobs) starten sofort;
  setzen Sie `WAIT_FOR_URL_JOBS=1` für Jobs, die ebenfalls warten sollen. `WAIT_FOR_TIMEOUT` gibt nach
  so vielen Sekunden auf (Standard `0` = ohne Grenze).

## [0.2.0] - 2026-10-01

### Hinzugefügt

- **Regeltabelle**: ungekürzte Kennungen, verstellbare und optionale Spalten, ein klebender Kopf,
  Tastaturbedienung und ein CSV-Export der gefilterten Regeln.
- **Präfix-Regeln über CEL**: Für eine Team-ID-Regel schreibt „Nur Signing IDs, die beginnen mit“ einen
  CEL-Ausdruck, der nur diese Signing IDs erlaubt. CEL-Felder bekommen Vorschläge beim Tippen.
- **Regeln aus Ereignissen im Drawer**: ein Formular für alle ausgewählten Binaries mit einer Vorschau
  je Binary, auch zum Blockieren und stillen Blockieren; vorhandene Regeln stehen in jedem Formular und
  als Badge.
- **Live-Ereignisse**: Neue Ereignisse erscheinen ohne Neuladen, mit einem Zähler für ungesehene
  Zeilen.
- **Größe des Webservers**: `GUNICORN_WORKERS`, `GUNICORN_THREADS`, `GUNICORN_WORKER_CLASS`,
  `GUNICORN_TIMEOUT` und `GUNICORN_MAX_REQUESTS`; die Standards entsprechen dem bisherigen Verhalten.

### Geändert

- **Eine einheitliche, barrierearme Konsole**: relative Größen, sichtbarer Fokus, reduzierte Bewegung,
  größere Klickflächen, Einträge pro Seite (25 bis 200), gestaltete Schalter, Auswahllisten und Tags.
  Regeln, Macs und Gruppen öffnen im Drawer, mit Zurück zur vorigen Ansicht.

### Sicherheit

- **Allgemeine Fehlermeldungen der Sync-API**: Eine abgelehnte Sync-Anfrage erhält
  `{"error": "bad request"}`, der Grund steht nur im Log. Bisher konnte die Meldung die ID einer anderen
  Gruppe verraten.

### Dokumentation

- **Schritt-für-Schritt-Einrichtung von OpenID Connect** in der README: App-Registrierung, Admin-Rolle,
  optionale Ansprüche und Gruppenanspruch, mit einem Beispiel-ID-Token.

## [0.1.0] - 2026-09-29

### Hinzugefügt

- **Gruppen von Macs**, jede mit eigener Santa-Konfiguration (Monitor / Lockdown, Pfad-Regexe, USB, …),
  eigener geheimer Sync-URL und einem erzeugten Konfigurationsprofil für jedes MDM, dazu ein
  Basisprofil für alle Macs.
- **Ausführungsregeln** für Binaries, Zertifikate, Team IDs, Signing IDs und CDHashes, global, je
  Gruppe oder je Mac, mit Erlauben-, Blockieren- und CEL-Richtlinien. An jeden Mac gehen nur die
  Unterschiede.
- **Ereignisse**: blockierte Ausführungen nach App gruppiert; auswählen und die Regeln in einem Schritt
  anlegen.
- **Paketregeln**, die GitHub-Releases, Homebrew-Formeln und -Casks, npm-Pakete,
  VS-Code-Erweiterungen, JetBrains-Plugins oder eine direkte Download-URL beobachten und die
  Executables jedes neuen Releases in Regeln umwandeln, auf Wunsch erst nach einer Wartezeit. Downloads
  werden gegen den veröffentlichten Hash geprüft, wo es einen gibt.
- **Zugriffsanfragen** unter `/request/`: Benutzer melden sich über einen OpenID-Connect-Anbieter an
  und fragen ein blockiertes Programm, Pakete oder sonstige Software an; Administratoren genehmigen oder
  lehnen in der Konsole ab.
- **Binary oder Archiv hochladen**, um es zu erlauben oder zu blockieren; Dateien werden nur gehasht,
  nie gespeichert.
- **Die Konsole** (`/console/`) mit einem seitlichen Drawer für Details und Formulare, einem Verlauf
  jeder Änderung und einem Reiter *Administration* für Benutzer, Rollen, Anmeldegruppen, Tags und den
  Export / Import der Konfiguration (auch als Befehle `export_config` / `import_config`, ohne
  Sync-Tokens).
- **Anmeldegruppen** ordnen die Gruppen des OpenID-Connect-Anbieters Rollen und Konsolenzugang zu;
  lokale Konten bleiben für den Notfallzugang.
- Englisch und Deutsch, helles und dunkles Design, auf dem Handy nutzbar. Läuft mit SQL Server und
  Redis, mit den Endpunkten `/health`, `/ready` und `/metrics`.

### Sicherheit

- **Sync-Tokens sind nur für Benutzer sichtbar, die Gruppen ändern dürfen**: Reine Leser sehen weder die
  Sync-URL noch die Gruppenprofile.
- **`ALLOWED_HOSTS` und `CSRF_TRUSTED_ORIGINS` folgen standardmäßig dem Host von
  `SANTA_PUBLIC_BASE_URL`**, das Pflicht ist, statt `*`.
- **Größenlimits** für entpackte Sync-Anfragen und beim Entpacken von Archiven.

### Abhängigkeiten

- GitHub Actions: actions/checkout 7, docker/build-push-action 7, docker/login-action 4,
  docker/setup-buildx-action 4, docker/metadata-action 6.
- Python-Pakete: hiredis 3.4.2, tzdata 2026.4.

[Unreleased]: https://github.com/expertZentrale/santa-server/compare/v0.11.0...HEAD
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
