<picture>
  <source media="(prefers-color-scheme: dark)" srcset="static/heisel-analytics-logo-dark.png">
  <source media="(prefers-color-scheme: light)" srcset="static/heisel-analytics-logo-light.png">
  <img src="static/heisel-analytics-logo-light.png" alt="Heisel Analytics" width="240" align="right">
</picture>

# HetznerWatch

**Selbst gehostetes Hetzner-Cloud-Verfügbarkeitsmonitoring—einfach installiert,
visuell auf einen Blick und vollständig im Browser konfigurierbar.**

Beobachte Servertypen und Standorte im Zeitverlauf, vergleiche aktuelle und
historische Verfügbarkeit und erhalte ntfy-Benachrichtigungen, sobald wieder
Kapazität vorhanden ist.

`Docker` · `Python` · `SQLite` · `ntfy` · `English & Deutsch`

[English documentation](README.md)

<br clear="right">

## Lokal installieren

HetznerWatch läuft mit Docker lokal unter macOS, Windows und Linux. Du musst
weder Python installieren noch eine `.env`-Datei anlegen.

### 1. Docker installieren

Installiere Docker Desktop unter macOS oder Windows. Unter Linux brauchst du
Docker Engine und das Docker-Compose-Plugin. Die
[offizielle Docker-Installationsanleitung](https://docs.docker.com/get-started/get-docker/)
deckt alle unterstützten Systeme ab.

Öffne das Terminal (macOS/Linux) oder PowerShell (Windows) und prüfe, ob Docker
bereit ist:

```bash
docker compose version
```

### 2. HetznerWatch herunterladen

Führe diese Befehle aus:

```bash
git clone https://github.com/HeiselAnalytics/HetznerWatch.git
cd HetznerWatch
```

Falls Git nicht installiert ist, öffne das
[HetznerWatch-Repository](https://github.com/HeiselAnalytics/HetznerWatch),
wähle **Code → Download ZIP**, entpacke die Datei und öffne ein Terminal im
entpackten Ordner.

### 3. HetznerWatch starten

```bash
docker compose up -d --build
```

Der erste Build kann eine Minute dauern. Docker startet HetznerWatch im
Hintergrund und speichert die Daten dauerhaft in einem lokalen Volume.

### 4. Einrichtung abschließen

Öffne [http://localhost:8080](http://localhost:8080) und gehe zu
**Einstellungen**. Hinterlege einen Hetzner-Cloud-API-Token mit **Leserechten**.
Die Einstellungen werden automatisch gespeichert. Sobald der Token akzeptiert
wurde, lädt HetznerWatch den Katalog und startet die erste Abfrage.

Den Token erstellst du in der Hetzner Cloud Console unter **Projekt → Sicherheit
→ API-Tokens → API-Token generieren**. Kopiere ihn sofort, da er nur einmal
angezeigt wird. Siehe die
[offizielle Hetzner-Dokumentation](https://docs.hetzner.com/de/cloud/api/getting-started/generating-api-token/).

### Später stoppen oder aktualisieren

Stoppe HetznerWatch, ohne die gespeicherten Daten zu löschen:

```bash
docker compose down
```

Installiere eine neue Version nach ihrer Veröffentlichung:

```bash
git pull
docker compose up -d --build
```

Beim Upgrade einer älteren Version wird ein vorhandener `HCLOUD_TOKEN` aus
`.env` beim ersten Start in SQLite übernommen. Nachdem der Token in den
Einstellungen als gespeichert angezeigt wird, kann die alte Datei entfernt
werden.

## ntfy kurz erklärt

HetznerWatch konfiguriert den öffentlichen Server `https://ntfy.sh` und ein
dauerhaft gespeichertes, merkbares Zufalls-Topic aus Wörtern und Zahlen vor, zum
Beispiel `hetznerwatch_amber482falcon073river916cobalt`. Benachrichtigungen
bleiben bis zur Einrichtung deaktiviert:

1. Installiere die ntfy-App auf Android oder iPhone.
2. Kopiere das erzeugte Topic unter **Einstellungen → ntfy-Benachrichtigungen**.
3. Füge in der App ein Abonnement auf `https://ntfy.sh` mit exakt diesem Topic hinzu.
4. Aktiviere ntfy in HetznerWatch und wähle **Testnachricht senden**.

Mit **Neues Topic** kannst du einen anderen Wert erzeugen. Das passiert nie
automatisch, weil anschließend jede ntfy-App das neue Topic abonnieren muss.

Jeder, der ein öffentliches Topic kennt, kann es lesen oder dort veröffentlichen.
Halte den erzeugten Wert daher geheim. Der Dashboard-Link muss vom Smartphone
erreichbar sein; `localhost` funktioniert nur auf dem HetznerWatch-Host. Siehe
die offizielle [Smartphone-Anleitung](https://docs.ntfy.sh/subscribe/phone/) und
die [Dokumentation zur Click Action](https://docs.ntfy.sh/publish/#click-action).

## Betrieb

```bash
docker compose logs -f monitor
```

Einstellungen, Verlauf und Zugangsdaten liegen in der lokalen SQLite-Datenbank
im Docker-Volume. Geheime Werte werden nicht über die Einstellungs-API
zurückgegeben, sind aber nicht verschlüsselt gespeichert. Schütze Datei- und
Docker-Zugriff und verwende vor einer Freigabe im Netzwerk einen authentifizierten
HTTPS-Reverse-Proxy.

HetznerWatch steht in keiner Verbindung zu Hetzner oder ntfy.

Lizenz: [MIT](LICENSE)
