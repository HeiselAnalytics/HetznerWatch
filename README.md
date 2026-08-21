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

<br clear="right">

## Install locally

HetznerWatch runs locally on macOS, Windows and Linux. You do not need Git,
Python or a `.env` file.

### 1. Install and open Docker Desktop

Install Docker Desktop on macOS or Windows and make sure it is running. On
Linux, install Docker Engine. The
[official Docker installation guide](https://docs.docker.com/get-started/get-docker/)
covers all supported systems.

### 2. Start with one command

Open Terminal (macOS/Linux) or PowerShell (Windows), paste this command, and
press Enter:

```bash
docker run -d --name hetznerwatch --restart unless-stopped -p 8080:8080 -v hetznerwatch-data:/data ghcr.io/heiselanalytics/hetznerwatch:latest
```

Docker downloads the ready-to-use image, starts it in the background and keeps
all settings and history in the persistent volume `hetznerwatch-data`.

### 3. Open and configure

Open [http://localhost:8080](http://localhost:8080) and go to **Settings**. Add a
Hetzner Cloud API token with **Read** permission. Settings save automatically;
after the token is accepted, HetznerWatch loads the catalog and starts the first
check.

Create the token in Hetzner Cloud Console under **Project → Security → API
Tokens → Generate API Token**. Copy it immediately because it is only shown
once. See the
[official Hetzner documentation](https://docs.hetzner.com/cloud/api/getting-started/generating-api-token/).

<details>
<summary>Build HetznerWatch from source instead</summary>

```bash
git clone https://github.com/HeiselAnalytics/HetznerWatch.git
cd HetznerWatch
docker compose up -d --build
```

When upgrading from a version that used `.env`, Docker Compose imports an
existing `HCLOUD_TOKEN` into SQLite on the first start. The old file can be
removed after the token is shown as configured in Settings.

</details>

### Stop or update later

Stop or start HetznerWatch without deleting its data:

```bash
docker stop hetznerwatch
docker start hetznerwatch
```

Install the latest published image while keeping the data volume:

```bash
docker pull ghcr.io/heiselanalytics/hetznerwatch:latest
docker rm -f hetznerwatch
docker run -d --name hetznerwatch --restart unless-stopped -p 8080:8080 -v hetznerwatch-data:/data ghcr.io/heiselanalytics/hetznerwatch:latest
```

## Install on a server with Docker Compose

This option is intended for an always-on Linux server, VPS or NAS. Install
[Docker Engine](https://docs.docker.com/engine/install/) and the
[Docker Compose plugin](https://docs.docker.com/compose/install/linux/) first.
No Git checkout, Python installation or `.env` file is required.

### 1. Download the server configuration

Run these commands on the server:

```bash
mkdir -p hetznerwatch
cd hetznerwatch
curl -fsSL https://raw.githubusercontent.com/HeiselAnalytics/HetznerWatch/main/compose.server.yml -o compose.yml
```

### 2. Start HetznerWatch

```bash
docker compose up -d
```

The configuration downloads the published image, restarts HetznerWatch after a
server reboot and stores its database in the persistent Docker volume
`hetznerwatch-data`.

### 3. Open the dashboard safely

The server configuration deliberately listens on `127.0.0.1:8080` only. For
initial setup, create an SSH tunnel from your computer:

```bash
ssh -L 8080:127.0.0.1:8080 your-user@your-server
```

Keep that terminal open and visit
[http://localhost:8080](http://localhost:8080). For permanent remote access,
place HetznerWatch behind an authenticated HTTPS reverse proxy. Do not expose
the dashboard directly to the internet because it controls the service settings
and does not provide its own user login.

When using ntfy, enter the externally reachable HTTPS dashboard address in
**Settings → Dashboard URL** so tapping a notification opens HetznerWatch.

### Update, inspect or stop the server installation

Run the commands inside the `hetznerwatch` directory:

```bash
# Install the latest image
docker compose pull
docker compose up -d

# View logs
docker compose logs -f

# Stop the application; the data volume is retained
docker compose down
```

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
docker logs -f hetznerwatch
```

Stop the application:

```bash
docker stop hetznerwatch
```

The named Docker volume `hetznerwatch-data` keeps the SQLite database when the
container is stopped, removed or replaced.

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
