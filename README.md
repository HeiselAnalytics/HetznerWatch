# Hetzner-Verfügbarkeitsmonitor

Der Monitor prüft die Verfügbarkeit ausgewählter Hetzner-Cloud-Servertypen,
speichert jede Prüfung in SQLite und zeigt den Verlauf in einem lokalen
Dashboard an. Die Seite **Langzeit** verdichtet erfolgreiche Gesamtabfragen
stündlich für alle von Hetzner gelieferten Servertypen und Standorte. Ziele,
Abfrageintervall und ntfy-Benachrichtigungen werden direkt über die Seite
**Einstellungen** verwaltet.

Die automatischen Abfragen können im Dashboard jederzeit pausiert und wieder
gestartet werden. Der Zustand bleibt auch nach einem Container-Neustart erhalten.

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

Jede Box im Zeitverlauf entspricht einer Abfrage. Auf der Einstellungsseite
können alle von der Hetzner API gelieferten Servertyp-/Standort-Kombinationen
ausgewählt werden. Das Abfrageintervall ist zwischen 10 und 86400 Sekunden
einstellbar.

Die Langzeitansicht gruppiert alle Servertypen nach Kategorie und zeigt für
24 Stunden, 7 Tage, 30 Tage oder 90 Tage die beobachtete Verfügbarkeitsquote
sowie den monatlichen Bruttopreis je Standort. Servertypen werden zunächst als
kompakte Standortübersicht dargestellt und lassen sich für die vollständigen
Zeitverläufe aufklappen. Der Serverkatalog entsteht aus einer Gesamtabfrage;
Preise werden zentral geladen und höchstens einmal innerhalb von 24 Stunden
aktualisiert. Die Langzeitdaten werden ab dem ersten erfolgreichen Abruf nach
Installation gesammelt und sind eine historische Orientierung, keine Prognose.
Detailabfragen und stündliche Langzeitdaten werden 120 Tage aufbewahrt. Ältere
Einträge werden beim Start und anschließend automatisch einmal täglich aus der
SQLite-Datenbank gelöscht.

Für ntfy werden Domain, Topic und optional Basic Auth oder ein Zugriffstoken
hinterlegt. Die Nachricht unterstützt diese Platzhalter:

- `{server_type}`
- `{location}`
- `{status}`
- `{checked_at}`
- `{recommended}`

Mit **Testnachricht senden** kann die Verbindung geprüft werden. Automatische
Meldungen werden serverseitig verschickt, wenn ein zuvor nicht verfügbares Ziel
wieder verfügbar ist; der Browser muss dafür nicht geöffnet bleiben.

Die Einstellungen einschließlich ntfy-Zugangsdaten werden in der lokalen
SQLite-Datenbank im Docker-Volume gespeichert. `POLL_INTERVAL_SECONDS` aus
`.env` dient beim ersten Start als Standardwert; danach gilt der Wert aus der
Einstellungsseite.

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
