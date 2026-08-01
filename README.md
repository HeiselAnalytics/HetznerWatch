# Hetzner-Verfügbarkeitsmonitor

Der Monitor prüft alle 60 Sekunden die Verfügbarkeit der konfigurierten
Hetzner-Cloud-Servertypen, speichert jede Prüfung in SQLite und zeigt den
Verlauf in einem lokalen Dashboard an.

## Starten

Konfiguration anlegen:

```bash
cp .env.example .env
```

Danach den Hetzner-Cloud-API-Token als `HCLOUD_TOKEN` in `.env` eintragen und
den Monitor starten:

```bash
docker compose up -d --build
```

Dashboard öffnen:

```text
http://localhost:8080
```

Jede Box im Zeitverlauf entspricht einer Abfrage. Browser-Benachrichtigungen
können oben rechts im Dashboard aktiviert werden. Sie werden bei einem Wechsel
auf „verfügbar“ angezeigt, solange die Seite in einem Browser-Tab geöffnet ist.
Mit „Test senden“ lässt sich die Browser-Berechtigung direkt überprüfen.

Falls keine Meldung erscheint:

1. Das Dashboard direkt unter `http://localhost:8080` in Safari, Chrome oder
   Firefox öffnen, nicht in einer eingebetteten IDE-Vorschau.
2. Die Benachrichtigungsberechtigung für `localhost` in den Website-Einstellungen
   des Browsers auf „Erlauben“ setzen.
3. Unter macOS in **Systemeinstellungen → Mitteilungen** den verwendeten Browser
   beziehungsweise die Website erlauben und einen aktiven Fokus prüfen.

Die Statuszeile unter den Buttons zeigt die erkannte Berechtigung oder den
konkreten Browserfehler an.

## Logs anzeigen

```bash
docker compose logs -f monitor
```

## Stoppen

```bash
docker compose down
```

Das Docker-Volume mit der SQLite-Datenbank bleibt dabei erhalten.

## SQLite-Datenbank prüfen

```bash
docker compose exec monitor python -c "
import sqlite3
conn = sqlite3.connect('/data/hetzner_availability.db')
for row in conn.execute('SELECT * FROM availability_checks ORDER BY id DESC LIMIT 20'):
    print(row)
"
```
