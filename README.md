<picture>
  <source media="(prefers-color-scheme: dark)" srcset="static/heisel-analytics-logo-dark.png">
  <source media="(prefers-color-scheme: light)" srcset="static/heisel-analytics-logo-light.png">
  <img src="static/heisel-analytics-logo-light.png" alt="Heisel Analytics" width="240" align="right">
</picture>

# HetznerWatch

**Self-hosted Hetzner Cloud availability monitoring—simple to install, visual
at a glance, and fully controlled from your browser.**

Track server types and locations over time, inspect current and historical
availability, and receive ntfy notifications when capacity returns.

`Docker` · `Python` · `SQLite` · `ntfy` · `English & Deutsch`

[Deutsche Dokumentation](README.de.md)

<br clear="right">

## Install locally

HetznerWatch runs locally on macOS, Windows and Linux with Docker. You do not
need to install Python or create a `.env` file.

### 1. Install Docker

Install Docker Desktop on macOS or Windows. On Linux, install Docker Engine and
the Docker Compose plugin. The
[official Docker installation guide](https://docs.docker.com/get-started/get-docker/)
covers all supported systems.

Open Terminal (macOS/Linux) or PowerShell (Windows) and confirm that Docker is
ready:

```bash
docker compose version
```

### 2. Download HetznerWatch

Run:

```bash
git clone https://github.com/HeiselAnalytics/HetznerWatch.git
cd HetznerWatch
```

If Git is not installed, open the
[HetznerWatch repository](https://github.com/HeiselAnalytics/HetznerWatch),
select **Code → Download ZIP**, extract the archive, and open a terminal in the
extracted folder.

### 3. Start HetznerWatch

```bash
docker compose up -d --build
```

The first build can take a minute. Docker starts HetznerWatch in the background
and keeps its data in a persistent local volume.

### 4. Complete the setup

Open [http://localhost:8080](http://localhost:8080) and go to **Settings**. Add a
Hetzner Cloud API token with **Read** permission. Settings save automatically;
after the token is accepted, HetznerWatch loads the catalog and starts the first
check.

Create the token in Hetzner Cloud Console under **Project → Security → API
Tokens → Generate API Token**. Copy it immediately because it is only shown
once. See the
[official Hetzner documentation](https://docs.hetzner.com/cloud/api/getting-started/generating-api-token/).

### Stop or update later

Stop HetznerWatch without deleting its data:

```bash
docker compose down
```

Install the latest version after it has been published:

```bash
git pull
docker compose up -d --build
```

When upgrading from an older release, an existing `HCLOUD_TOKEN` in `.env` is
imported into SQLite on the first start. After confirming the token in Settings,
the old file can be removed.

## Features

- Visual per-check timeline for selected server type/location pairs
- Long-term availability views for 24 hours, 7, 30 and 90 days
- Current Hetzner catalog and monthly gross prices
- Configurable monitoring interval and pause/resume control
- Optional ntfy notifications with a configurable dashboard click link
- English by default, with German available in Settings
- Heisel Analytics branding by default, replaceable with an image URL
- All application settings managed in the UI and persisted in SQLite
- Automatic 120-day retention cleanup

## ntfy in short

HetznerWatch preconfigures the public server `https://ntfy.sh` and a persistent,
memorable random topic made from words and numbers, for example
`hetznerwatch_amber482falcon073river916cobalt`. Notifications stay disabled
until setup is complete:

1. Install the ntfy app on Android or iPhone.
2. Copy the generated topic from **Settings → ntfy notifications**.
3. In the app, add a subscription on `https://ntfy.sh` with exactly that topic.
4. Enable ntfy in HetznerWatch and choose **Send test message**.

Use **New topic** to generate another value. This never happens automatically,
because every ntfy app must then subscribe to the new topic.

Anyone who knows a public topic can read or publish to it, so keep the generated
value private. The dashboard link must be reachable from the phone; `localhost`
only works on the HetznerWatch host. See the official [smartphone guide](https://docs.ntfy.sh/subscribe/phone/).
The click action is described in the [publish documentation](https://docs.ntfy.sh/publish/#click-action).

## Operations

View logs:

```bash
docker compose logs -f monitor
```

Stop the application:

```bash
docker compose down
```

The named Docker volume `monitor-data` keeps the SQLite database across
container rebuilds and normal `docker compose down` operations.

## Data and security

Settings, history and service credentials are stored in the local SQLite
database in the Docker volume. Secret values are never returned by the settings
API, but they are not encrypted at rest. Protect filesystem and Docker access,
back up the volume, and place HetznerWatch behind an authenticated HTTPS reverse
proxy before exposing it to a network or the internet.

HetznerWatch is not affiliated with Hetzner or ntfy.

## Development and tests

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python -m unittest -v
```

See [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md).

## License

[MIT](LICENSE)
