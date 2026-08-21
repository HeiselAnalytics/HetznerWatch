import json
import logging
import os
import re
import secrets
import signal
import sqlite3
import string
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
from flask import Flask, jsonify, request, send_file
from werkzeug.serving import make_server


API_URL = "https://api.hetzner.cloud/v1/server_types"
PRICING_URL = "https://api.hetzner.cloud/v1/pricing"
DEFAULT_MONITORED_TARGETS = [
    {"server_type": "cx23", "location": "fsn1"},
    {"server_type": "cx33", "location": "fsn1"},
    {"server_type": "cx33", "location": "nbg1"},
]
DEFAULT_NTFY_MESSAGE_TEMPLATE = (
    "{server_type} is available in {location}. "
    "Status: {status}. Checked: {checked_at}. Recommended: {recommended}."
)
ALLOWED_TEMPLATE_FIELDS = {
    "server_type",
    "location",
    "status",
    "checked_at",
    "recommended",
}
TARGET_VALUE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
TOPIC_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
DEFAULT_NTFY_TOPIC_PREFIX = "hetznerwatch_"
DEFAULT_NTFY_TOPIC_WORD_COUNT = 4
DEFAULT_NTFY_TOPIC_SEPARATOR_DIGITS = 3
NTFY_TOPIC_WORDS = (
    "acorn",
    "almond",
    "amber",
    "apple",
    "arrow",
    "atlas",
    "aurora",
    "bamboo",
    "basil",
    "beach",
    "berry",
    "birch",
    "bloom",
    "breeze",
    "brook",
    "cedar",
    "cherry",
    "cloud",
    "clover",
    "comet",
    "coral",
    "cosmos",
    "cove",
    "crane",
    "creek",
    "dawn",
    "delta",
    "dream",
    "drift",
    "dune",
    "eagle",
    "echo",
    "elm",
    "ember",
    "fern",
    "field",
    "finch",
    "fjord",
    "flame",
    "flora",
    "forest",
    "fox",
    "frost",
    "galaxy",
    "garden",
    "glade",
    "globe",
    "glow",
    "grass",
    "grove",
    "harbor",
    "hazel",
    "heron",
    "hill",
    "honey",
    "iris",
    "island",
    "ivory",
    "jade",
    "juniper",
    "lake",
    "lark",
    "leaf",
    "lemon",
    "lilac",
    "lime",
    "lotus",
    "maple",
    "meadow",
    "mint",
    "mist",
    "moon",
    "moss",
    "navy",
    "north",
    "oak",
    "ocean",
    "olive",
    "onyx",
    "opal",
    "orbit",
    "otter",
    "palm",
    "pearl",
    "pebble",
    "pine",
    "plum",
    "pond",
    "poppy",
    "quartz",
    "rain",
    "raven",
    "reed",
    "reef",
    "river",
    "robin",
    "rose",
    "ruby",
    "sage",
    "sand",
    "shell",
    "sky",
    "snow",
    "solar",
    "south",
    "spark",
    "spring",
    "star",
    "stone",
    "storm",
    "sun",
    "swift",
    "teal",
    "terra",
    "thyme",
    "tide",
    "tiger",
    "tulip",
    "valley",
    "violet",
    "wave",
    "west",
    "willow",
    "wind",
    "winter",
    "wolf",
    "wood",
    "zenith",
)
MIN_POLL_INTERVAL_SECONDS = 10
MAX_POLL_INTERVAL_SECONDS = 86_400
PRICING_REFRESH_SECONDS = 86_400
DATA_RETENTION_DAYS = 120
RETENTION_CLEANUP_INTERVAL_SECONDS = 86_400
LONG_TERM_RANGES = {
    "24h": {"hours": 24, "bucket_hours": 1, "label": "24 Stunden"},
    "7d": {"hours": 24 * 7, "bucket_hours": 6, "label": "7 Tage"},
    "30d": {"hours": 24 * 30, "bucket_hours": 24, "label": "30 Tage"},
    "90d": {"hours": 24 * 90, "bucket_hours": 72, "label": "90 Tage"},
}

CREATE_DATABASE_SQL = """
CREATE TABLE IF NOT EXISTS availability_checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    checked_at TEXT NOT NULL,
    server_type TEXT NOT NULL,
    location TEXT NOT NULL,
    available INTEGER,
    recommended INTEGER,
    request_success INTEGER NOT NULL,
    http_status_code INTEGER,
    error_message TEXT
);

CREATE INDEX IF NOT EXISTS idx_availability_checks_checked_at
ON availability_checks (checked_at);

CREATE TABLE IF NOT EXISTS availability_rollups (
    bucket_start TEXT NOT NULL,
    server_type TEXT NOT NULL,
    location TEXT NOT NULL,
    total_checks INTEGER NOT NULL,
    available_checks INTEGER NOT NULL,
    recommended_checks INTEGER NOT NULL,
    PRIMARY KEY (bucket_start, server_type, location)
);

CREATE INDEX IF NOT EXISTS idx_availability_rollups_target_time
ON availability_rollups (server_type, location, bucket_start);

CREATE INDEX IF NOT EXISTS idx_availability_rollups_bucket_start
ON availability_rollups (bucket_start);

CREATE TABLE IF NOT EXISTS monitored_targets (
    server_type TEXT NOT NULL,
    location TEXT NOT NULL,
    PRIMARY KEY (server_type, location)
);

CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

HCLOUD_TOKEN = ""
POLL_INTERVAL_SECONDS = 60.0
REQUEST_TIMEOUT_SECONDS = 15.0
DATABASE_PATH = "/data/hetzner_availability.db"
WEB_HOST = "0.0.0.0"
WEB_PORT = 8080
STOP_EVENT = threading.Event()
WAKE_EVENT = threading.Event()
INITIAL_CHECK_COMPLETED_EVENT = threading.Event()
CHECK_LOCK = threading.Lock()
WEB_APP = Flask(__name__)
INDEX_PATH = Path(__file__).with_name("index.html")


class ApiFetchError(Exception):
    def __init__(
        self,
        message: str,
        http_status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.http_status_code = http_status_code


class SettingsValidationError(Exception):
    pass


class NtfyError(Exception):
    pass


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
        stream=sys.stdout,
        force=True,
    )
    logging.Formatter.converter = time.gmtime


def _positive_float_from_env(name: str, default: float) -> float:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default

    try:
        value = float(raw_value)
    except ValueError:
        logging.warning(
            "%s ist ungültig; Standardwert %g wird verwendet.",
            name,
            default,
        )
        return default

    if value <= 0:
        logging.warning(
            "%s muss größer als 0 sein; Standardwert %g wird verwendet.",
            name,
            default,
        )
        return default

    return value


def _port_from_env(name: str, default: int) -> int:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default

    try:
        value = int(raw_value)
    except ValueError:
        logging.warning(
            "%s ist ungültig; Standardwert %d wird verwendet.",
            name,
            default,
        )
        return default

    if not 1 <= value <= 65535:
        logging.warning(
            "%s muss zwischen 1 und 65535 liegen; Standardwert %d wird verwendet.",
            name,
            default,
        )
        return default

    return value


def load_configuration() -> None:
    global HCLOUD_TOKEN
    global POLL_INTERVAL_SECONDS
    global REQUEST_TIMEOUT_SECONDS
    global DATABASE_PATH
    global WEB_HOST
    global WEB_PORT

    # Environment variables remain available for container/runtime tuning and
    # one-time migration, but a .env file is never required.
    HCLOUD_TOKEN = os.getenv("HCLOUD_TOKEN", "").strip()
    POLL_INTERVAL_SECONDS = _positive_float_from_env(
        "POLL_INTERVAL_SECONDS",
        60.0,
    )
    REQUEST_TIMEOUT_SECONDS = _positive_float_from_env(
        "REQUEST_TIMEOUT_SECONDS",
        15.0,
    )
    DATABASE_PATH = (
        os.getenv("DATABASE_PATH", "/data/hetzner_availability.db").strip()
        or "/data/hetzner_availability.db"
    )
    WEB_HOST = os.getenv("WEB_HOST", "0.0.0.0").strip() or "0.0.0.0"
    WEB_PORT = _port_from_env("WEB_PORT", 8080)


def utc_timestamp() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def generate_default_ntfy_topic() -> str:
    words = [
        secrets.choice(NTFY_TOPIC_WORDS)
        for _ in range(DEFAULT_NTFY_TOPIC_WORD_COUNT)
    ]
    separator_limit = 10**DEFAULT_NTFY_TOPIC_SEPARATOR_DIGITS
    suffix = words[0]
    for word in words[1:]:
        separator = secrets.randbelow(separator_limit)
        suffix += f"{separator:0{DEFAULT_NTFY_TOPIC_SEPARATOR_DIGITS}d}{word}"
    return f"{DEFAULT_NTFY_TOPIC_PREFIX}{suffix}"


def _setting_defaults() -> dict[str, str]:
    default_interval = min(
        MAX_POLL_INTERVAL_SECONDS,
        max(MIN_POLL_INTERVAL_SECONDS, POLL_INTERVAL_SECONDS),
    )
    return {
        "hcloud_token": HCLOUD_TOKEN,
        "language": "en",
        "custom_logo_url": "",
        "monitoring_enabled": "true",
        "poll_interval_seconds": f"{default_interval:g}",
        "ntfy_enabled": "false",
        "ntfy_domain": "https://ntfy.sh",
        "ntfy_topic": generate_default_ntfy_topic(),
        "ntfy_default_topic_initialized": "true",
        "ntfy_auth_mode": "none",
        "ntfy_username": "",
        "ntfy_password": "",
        "ntfy_token": "",
        "ntfy_dashboard_url": f"http://localhost:{WEB_PORT}",
        "ntfy_message_template": DEFAULT_NTFY_MESSAGE_TEMPLATE,
        "server_catalog_json": "[]",
        "pricing_json": "{}",
        "pricing_updated_at": "",
        "retention_cleanup_at": "",
    }


def init_database() -> None:
    database_directory = os.path.dirname(DATABASE_PATH)
    if database_directory:
        os.makedirs(database_directory, exist_ok=True)

    with sqlite3.connect(DATABASE_PATH) as connection:
        connection.executescript(CREATE_DATABASE_SQL)
        topic_default_was_initialized = connection.execute(
            "SELECT 1 FROM app_settings WHERE key = ?",
            ("ntfy_default_topic_initialized",),
        ).fetchone() is not None
        defaults = _setting_defaults()
        connection.executemany(
            "INSERT OR IGNORE INTO app_settings (key, value) VALUES (?, ?)",
            defaults.items(),
        )
        if not topic_default_was_initialized:
            stored_topic = connection.execute(
                "SELECT value FROM app_settings WHERE key = ?",
                ("ntfy_topic",),
            ).fetchone()
            if stored_topic is None or not str(stored_topic[0]).strip():
                _save_setting(connection, "ntfy_topic", defaults["ntfy_topic"])
        target_count = connection.execute(
            "SELECT COUNT(*) FROM monitored_targets"
        ).fetchone()[0]
        if target_count == 0:
            connection.executemany(
                """
                INSERT OR IGNORE INTO monitored_targets (server_type, location)
                VALUES (?, ?)
                """,
                (
                    (target["server_type"], target["location"])
                    for target in DEFAULT_MONITORED_TARGETS
                ),
            )
    cleanup_old_data()


def _load_setting(connection: sqlite3.Connection, key: str) -> str:
    row = connection.execute(
        "SELECT value FROM app_settings WHERE key = ?",
        (key,),
    ).fetchone()
    return "" if row is None else str(row[0])


def _save_setting(
    connection: sqlite3.Connection,
    key: str,
    value: str,
) -> None:
    connection.execute(
        """
        INSERT INTO app_settings (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, value),
    )


def load_monitored_targets(
    connection: sqlite3.Connection | None = None,
) -> list[dict[str, str]]:
    owns_connection = connection is None
    active_connection = connection or sqlite3.connect(DATABASE_PATH)
    try:
        rows = active_connection.execute(
            """
            SELECT server_type, location
            FROM monitored_targets
            ORDER BY server_type, location
            """
        ).fetchall()
        return [
            {"server_type": str(row[0]), "location": str(row[1])}
            for row in rows
        ]
    finally:
        if owns_connection:
            active_connection.close()


def get_poll_interval_seconds() -> float:
    try:
        with sqlite3.connect(DATABASE_PATH) as connection:
            raw_value = _load_setting(connection, "poll_interval_seconds")
        value = float(raw_value)
        if MIN_POLL_INTERVAL_SECONDS <= value <= MAX_POLL_INTERVAL_SECONDS:
            return value
    except (OSError, sqlite3.Error, ValueError):
        pass
    return min(
        MAX_POLL_INTERVAL_SECONDS,
        max(MIN_POLL_INTERVAL_SECONDS, POLL_INTERVAL_SECONDS),
    )


def get_monitoring_enabled(
    connection: sqlite3.Connection | None = None,
) -> bool:
    owns_connection = connection is None
    active_connection = connection or sqlite3.connect(DATABASE_PATH)
    try:
        raw_value = _load_setting(active_connection, "monitoring_enabled")
        return raw_value.strip().lower() != "false"
    except (OSError, sqlite3.Error):
        return True
    finally:
        if owns_connection:
            active_connection.close()


def get_hcloud_token(
    connection: sqlite3.Connection | None = None,
) -> str:
    owns_connection = connection is None
    active_connection = connection or sqlite3.connect(DATABASE_PATH)
    try:
        return _load_setting(active_connection, "hcloud_token") or HCLOUD_TOKEN
    except (OSError, sqlite3.Error):
        return HCLOUD_TOKEN
    finally:
        if owns_connection:
            active_connection.close()


def get_application_language() -> str:
    try:
        with sqlite3.connect(DATABASE_PATH) as connection:
            language = _load_setting(connection, "language")
        return language if language in {"en", "de"} else "en"
    except (OSError, sqlite3.Error):
        return "en"


def localized_message(english: str, german: str) -> str:
    return german if get_application_language() == "de" else english


def fetch_server_types() -> tuple[dict[str, Any], int]:
    token = get_hcloud_token()
    if not token:
        raise ApiFetchError(
            localized_message(
                "The Hetzner Cloud API token is not configured.",
                "Der Hetzner-Cloud-API-Token ist nicht eingerichtet.",
            )
        )

    server_types: list[Any] = []
    page = 1
    http_status_code = 200

    while True:
        try:
            response = requests.get(
                API_URL,
                headers={"Authorization": f"Bearer {token}"},
                params={"per_page": 50, "page": page},
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
        except requests.Timeout as error:
            raise ApiFetchError(
                localized_message(
                    f"API timeout after {REQUEST_TIMEOUT_SECONDS:g} seconds.",
                    f"API-Timeout nach {REQUEST_TIMEOUT_SECONDS:g} Sekunden.",
                )
            ) from error
        except requests.RequestException as error:
            raise ApiFetchError(
                localized_message(
                    f"API unreachable ({type(error).__name__}).",
                    f"API nicht erreichbar ({type(error).__name__}).",
                )
            ) from error

        http_status_code = response.status_code
        try:
            response.raise_for_status()
        except requests.HTTPError as error:
            raise ApiFetchError(
                localized_message(
                    f"HTTP error {http_status_code} from the Hetzner API.",
                    f"HTTP-Fehler {http_status_code} von der Hetzner API.",
                ),
                http_status_code,
            ) from error

        try:
            payload = response.json()
        except (requests.exceptions.JSONDecodeError, ValueError) as error:
            raise ApiFetchError(
                localized_message(
                    "The Hetzner API returned invalid JSON.",
                    "Die Hetzner API hat ungültiges JSON zurückgegeben.",
                ),
                http_status_code,
            ) from error

        if not isinstance(payload, dict):
            raise ApiFetchError(
                localized_message(
                    "The Hetzner API did not return a JSON object.",
                    "Die Hetzner API hat kein JSON-Objekt zurückgegeben.",
                ),
                http_status_code,
            )
        page_server_types = payload.get("server_types")
        if not isinstance(page_server_types, list):
            raise ApiFetchError(
                localized_message(
                    "Field 'server_types' is missing or invalid.",
                    "Feld 'server_types' fehlt oder ist ungültig.",
                )
            )
        server_types.extend(page_server_types)

        meta = payload.get("meta")
        pagination = meta.get("pagination", {}) if isinstance(meta, dict) else {}
        if not isinstance(pagination, dict):
            pagination = {}
        next_page = pagination.get("next_page")
        if not isinstance(next_page, int) or next_page <= page:
            break
        page = next_page

    return {"server_types": server_types}, http_status_code


def fetch_pricing() -> dict[str, Any]:
    token = get_hcloud_token()
    if not token:
        raise ApiFetchError(
            localized_message(
                "The Hetzner Cloud API token is not configured.",
                "Der Hetzner-Cloud-API-Token ist nicht eingerichtet.",
            )
        )
    try:
        response = requests.get(
            PRICING_URL,
            headers={"Authorization": f"Bearer {token}"},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except requests.Timeout as error:
        raise ApiFetchError(
            localized_message(
                f"Pricing API timeout after {REQUEST_TIMEOUT_SECONDS:g} seconds.",
                f"Preis-API-Timeout nach {REQUEST_TIMEOUT_SECONDS:g} Sekunden.",
            )
        ) from error
    except requests.RequestException as error:
        raise ApiFetchError(
            localized_message(
                f"Pricing API unreachable ({type(error).__name__}).",
                f"Preis-API nicht erreichbar ({type(error).__name__}).",
            )
        ) from error

    try:
        response.raise_for_status()
    except requests.HTTPError as error:
        raise ApiFetchError(
            localized_message(
                f"HTTP error {response.status_code} from the Hetzner pricing API.",
                f"HTTP-Fehler {response.status_code} von der Hetzner Preis-API.",
            ),
            response.status_code,
        ) from error
    try:
        payload = response.json()
    except (requests.exceptions.JSONDecodeError, ValueError) as error:
        raise ApiFetchError(
            localized_message(
                "The Hetzner pricing API returned invalid JSON.",
                "Die Hetzner Preis-API hat ungültiges JSON zurückgegeben.",
            ),
            response.status_code,
        ) from error
    pricing = payload.get("pricing") if isinstance(payload, dict) else None
    if not isinstance(pricing, dict) or not isinstance(
        pricing.get("server_types"), list
    ):
        raise ApiFetchError(
            localized_message(
                "The Hetzner API pricing data is invalid.",
                "Die Preisdaten der Hetzner API sind ungültig.",
            )
        )
    return pricing


def load_cached_pricing() -> dict[str, Any]:
    with sqlite3.connect(DATABASE_PATH) as connection:
        raw_value = _load_setting(connection, "pricing_json")
    try:
        pricing = json.loads(raw_value or "{}")
    except json.JSONDecodeError:
        return {}
    return pricing if isinstance(pricing, dict) else {}


def load_pricing_for_catalog() -> dict[str, Any]:
    with sqlite3.connect(DATABASE_PATH) as connection:
        raw_pricing = _load_setting(connection, "pricing_json")
        updated_at = _load_setting(connection, "pricing_updated_at")
    try:
        cached_pricing = json.loads(raw_pricing or "{}")
        if not isinstance(cached_pricing, dict):
            cached_pricing = {}
    except json.JSONDecodeError:
        cached_pricing = {}

    fresh = False
    if updated_at:
        try:
            age = datetime.now(timezone.utc) - _parse_iso_timestamp(updated_at)
            fresh = age.total_seconds() < PRICING_REFRESH_SECONDS
        except ValueError:
            pass
    if fresh:
        return cached_pricing

    try:
        pricing = fetch_pricing()
    except ApiFetchError as error:
        logging.warning("Preise konnten nicht aktualisiert werden: %s", error)
        return cached_pricing

    with sqlite3.connect(DATABASE_PATH) as connection:
        _save_setting(
            connection,
            "pricing_json",
            json.dumps(pricing, ensure_ascii=False, separators=(",", ":")),
        )
        _save_setting(connection, "pricing_updated_at", utc_timestamp())
    return pricing


def _location_name(location: dict[str, Any]) -> str:
    value = location.get("location")
    if isinstance(value, dict):
        name = value.get("name")
    elif isinstance(value, str):
        name = value
    else:
        name = location.get("name")
    return name if isinstance(name, str) else ""


def server_catalog_from_payload(
    payload: dict[str, Any],
    pricing: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    raw_server_types = payload.get("server_types")
    if not isinstance(raw_server_types, list):
        raise ApiFetchError(
            localized_message(
                "Field 'server_types' is missing or invalid.",
                "Feld 'server_types' fehlt oder ist ungültig.",
            )
        )

    centralized_prices = {
        item.get("name"): item.get("prices", [])
        for item in (pricing or {}).get("server_types", [])
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }
    currency = (pricing or {}).get("currency")
    price_currency = currency if isinstance(currency, str) else ""

    catalog: list[dict[str, Any]] = []
    for item in raw_server_types:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            continue
        price_entries = centralized_prices.get(item["name"], item.get("prices", []))
        prices_by_location: dict[str, str] = {}
        if isinstance(price_entries, list):
            for price in price_entries:
                if not isinstance(price, dict):
                    continue
                price_location = _location_name(price)
                price_monthly = price.get("price_monthly")
                gross = (
                    price_monthly.get("gross")
                    if isinstance(price_monthly, dict)
                    else None
                )
                if price_location and isinstance(gross, str):
                    prices_by_location[price_location] = gross
        locations: list[dict[str, Any]] = []
        for location in item.get("locations", []):
            if not isinstance(location, dict):
                continue
            name = _location_name(location)
            if not name:
                continue
            locations.append(
                {
                    "name": name,
                    "available": location.get("available"),
                    "recommended": location.get("recommended"),
                    "price_monthly_gross": prices_by_location.get(name),
                    "price_currency": price_currency,
                }
            )
        if not locations:
            continue
        catalog.append(
            {
                "name": item["name"],
                "description": item.get("description") or item["name"],
                "category": item.get("category") or "",
                "architecture": item.get("architecture") or "",
                "cores": item.get("cores"),
                "memory": item.get("memory"),
                "disk": item.get("disk"),
                "locations": sorted(locations, key=lambda value: value["name"]),
            }
        )
    return sorted(catalog, key=lambda value: value["name"])


def cache_server_catalog(
    payload: dict[str, Any],
    pricing: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    catalog = server_catalog_from_payload(payload, pricing)
    with sqlite3.connect(DATABASE_PATH) as connection:
        _save_setting(
            connection,
            "server_catalog_json",
            json.dumps(catalog, ensure_ascii=False, separators=(",", ":")),
        )
    return catalog


def load_cached_server_catalog() -> list[dict[str, Any]]:
    with sqlite3.connect(DATABASE_PATH) as connection:
        raw_value = _load_setting(connection, "server_catalog_json")
    try:
        catalog = json.loads(raw_value or "[]")
    except json.JSONDecodeError:
        return []
    return catalog if isinstance(catalog, list) else []


def _parse_iso_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(
        timezone.utc
    )


def _iso_timestamp(value: datetime) -> str:
    return (
        value.astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def cleanup_old_data(
    force: bool = False,
    now: datetime | None = None,
) -> dict[str, int | bool | str]:
    current_time = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    cutoff = current_time - timedelta(days=DATA_RETENTION_DAYS)
    with sqlite3.connect(DATABASE_PATH) as connection:
        last_cleanup_at = _load_setting(connection, "retention_cleanup_at")
        if not force and last_cleanup_at:
            try:
                cleanup_age = current_time - _parse_iso_timestamp(last_cleanup_at)
                if 0 <= cleanup_age.total_seconds() < RETENTION_CLEANUP_INTERVAL_SECONDS:
                    return {
                        "ran": False,
                        "cutoff": _iso_timestamp(cutoff),
                        "checks_deleted": 0,
                        "rollups_deleted": 0,
                    }
            except ValueError:
                pass

        checks_deleted = connection.execute(
            "DELETE FROM availability_checks WHERE checked_at < ?",
            (_iso_timestamp(cutoff),),
        ).rowcount
        rollups_deleted = connection.execute(
            "DELETE FROM availability_rollups WHERE bucket_start < ?",
            (_iso_timestamp(cutoff),),
        ).rowcount
        _save_setting(connection, "retention_cleanup_at", _iso_timestamp(current_time))

    if checks_deleted or rollups_deleted:
        logging.info(
            "Aufbewahrung bereinigt: %d Detailabfragen und %d Langzeit-Rollups gelöscht.",
            checks_deleted,
            rollups_deleted,
        )
    return {
        "ran": True,
        "cutoff": _iso_timestamp(cutoff),
        "checks_deleted": checks_deleted,
        "rollups_deleted": rollups_deleted,
    }


def _floor_datetime(value: datetime, hours: int) -> datetime:
    bucket_seconds = hours * 3_600
    floored_timestamp = int(value.timestamp()) // bucket_seconds * bucket_seconds
    return datetime.fromtimestamp(floored_timestamp, timezone.utc)


def save_availability_rollups(
    checked_at: str,
    catalog: list[dict[str, Any]],
) -> None:
    bucket_start = _iso_timestamp(
        _parse_iso_timestamp(checked_at).replace(
            minute=0,
            second=0,
            microsecond=0,
        )
    )
    rows: list[tuple[str, str, str, int, int, int]] = []
    for server_type in catalog:
        name = server_type.get("name")
        if not isinstance(name, str):
            continue
        for location in server_type.get("locations", []):
            if not isinstance(location, dict):
                continue
            location_name = location.get("name")
            available = location.get("available")
            recommended = location.get("recommended")
            if (
                not isinstance(location_name, str)
                or not isinstance(available, bool)
                or not isinstance(recommended, bool)
            ):
                continue
            rows.append(
                (
                    bucket_start,
                    name,
                    location_name,
                    1,
                    int(available),
                    int(recommended),
                )
            )

    if not rows:
        return
    with sqlite3.connect(DATABASE_PATH) as connection:
        connection.executemany(
            """
            INSERT INTO availability_rollups (
                bucket_start,
                server_type,
                location,
                total_checks,
                available_checks,
                recommended_checks
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(bucket_start, server_type, location) DO UPDATE SET
                total_checks = total_checks + excluded.total_checks,
                available_checks = available_checks + excluded.available_checks,
                recommended_checks = recommended_checks + excluded.recommended_checks
            """,
            rows,
        )


def build_long_term_statistics(
    range_key: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    range_config = LONG_TERM_RANGES[range_key]
    bucket_hours = int(range_config["bucket_hours"])
    slot_count = int(range_config["hours"]) // bucket_hours
    current_time = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    end_bucket = _floor_datetime(current_time, bucket_hours)
    start_bucket = end_bucket - timedelta(hours=bucket_hours * (slot_count - 1))
    slot_starts = [
        start_bucket + timedelta(hours=bucket_hours * index)
        for index in range(slot_count)
    ]

    catalog = load_cached_server_catalog()
    targets: dict[tuple[str, str], dict[str, Any]] = {}
    server_metadata: dict[str, dict[str, Any]] = {}
    for server_type in catalog:
        name = server_type.get("name")
        if not isinstance(name, str):
            continue
        server_metadata[name] = {
            "name": name,
            "description": server_type.get("description") or name,
            "category": server_type.get("category") or "",
            "architecture": server_type.get("architecture") or "",
            "cores": server_type.get("cores"),
            "memory": server_type.get("memory"),
            "disk": server_type.get("disk"),
        }
        for location in server_type.get("locations", []):
            if not isinstance(location, dict) or not isinstance(
                location.get("name"), str
            ):
                continue
            key = (name, location["name"])
            targets[key] = {
                "current_available": (
                    location.get("available")
                    if isinstance(location.get("available"), bool)
                    else None
                ),
                "current_recommended": (
                    location.get("recommended")
                    if isinstance(location.get("recommended"), bool)
                    else None
                ),
                "price_monthly_gross": location.get("price_monthly_gross"),
                "price_currency": location.get("price_currency") or "",
                "total_checks": 0,
                "available_checks": 0,
                "recommended_checks": 0,
                "last_available_at": None,
                "series_totals": [0] * slot_count,
                "series_available": [0] * slot_count,
            }

    with sqlite3.connect(DATABASE_PATH) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT
                bucket_start,
                server_type,
                location,
                total_checks,
                available_checks,
                recommended_checks
            FROM availability_rollups
            WHERE bucket_start >= ?
            ORDER BY bucket_start
            """,
            (_iso_timestamp(start_bucket),),
        ).fetchall()

    for row in rows:
        try:
            row_start = _parse_iso_timestamp(str(row["bucket_start"]))
        except ValueError:
            continue
        slot_index = int(
            (row_start - start_bucket).total_seconds() // (bucket_hours * 3_600)
        )
        if not 0 <= slot_index < slot_count:
            continue
        key = (str(row["server_type"]), str(row["location"]))
        if key not in targets:
            targets[key] = {
                "current_available": None,
                "current_recommended": None,
                "price_monthly_gross": None,
                "price_currency": "",
                "total_checks": 0,
                "available_checks": 0,
                "recommended_checks": 0,
                "last_available_at": None,
                "series_totals": [0] * slot_count,
                "series_available": [0] * slot_count,
            }
            server_metadata.setdefault(
                key[0],
                {
                    "name": key[0],
                    "description": key[0],
                    "category": "",
                    "architecture": "",
                    "cores": None,
                    "memory": None,
                    "disk": None,
                },
            )
        target = targets[key]
        total_checks = int(row["total_checks"])
        available_checks = int(row["available_checks"])
        recommended_checks = int(row["recommended_checks"])
        target["total_checks"] += total_checks
        target["available_checks"] += available_checks
        target["recommended_checks"] += recommended_checks
        target["series_totals"][slot_index] += total_checks
        target["series_available"][slot_index] += available_checks
        if available_checks > 0:
            target["last_available_at"] = _iso_timestamp(row_start)

    servers: dict[str, dict[str, Any]] = {
        name: {**metadata, "locations": []}
        for name, metadata in server_metadata.items()
    }
    total_checks_all = 0
    available_checks_all = 0
    targets_with_data = 0
    currently_available = 0
    for (server_type, location), target in sorted(targets.items()):
        total_checks = int(target["total_checks"])
        available_checks = int(target["available_checks"])
        total_checks_all += total_checks
        available_checks_all += available_checks
        targets_with_data += int(total_checks > 0)
        currently_available += int(target["current_available"] is True)
        series = []
        for index, slot_start in enumerate(slot_starts):
            slot_total = target["series_totals"][index]
            slot_available = target["series_available"][index]
            series.append(
                {
                    "bucket_start": _iso_timestamp(slot_start),
                    "total_checks": slot_total,
                    "availability_percent": (
                        round(slot_available / slot_total * 100, 1)
                        if slot_total
                        else None
                    ),
                }
            )
        servers[server_type]["locations"].append(
            {
                "name": location,
                "current_available": target["current_available"],
                "current_recommended": target["current_recommended"],
                "price_monthly_gross": target["price_monthly_gross"],
                "price_currency": target["price_currency"],
                "total_checks": total_checks,
                "availability_percent": (
                    round(available_checks / total_checks * 100, 1)
                    if total_checks
                    else None
                ),
                "recommended_percent": (
                    round(target["recommended_checks"] / total_checks * 100, 1)
                    if total_checks
                    else None
                ),
                "last_available_at": target["last_available_at"],
                "series": series,
            }
        )

    categories: dict[str, list[dict[str, Any]]] = {}
    for server in sorted(servers.values(), key=lambda item: item["name"]):
        category = str(server["category"] or "")
        categories.setdefault(category, []).append(server)

    return {
        "generated_at": _iso_timestamp(current_time),
        "range": {
            "key": range_key,
            "label": range_config["label"],
            "bucket_hours": bucket_hours,
            "start": _iso_timestamp(start_bucket),
            "end": _iso_timestamp(end_bucket + timedelta(hours=bucket_hours)),
        },
        "summary": {
            "targets": len(targets),
            "targets_with_data": targets_with_data,
            "currently_available": currently_available,
            "total_checks": total_checks_all,
            "availability_percent": (
                round(available_checks_all / total_checks_all * 100, 1)
                if total_checks_all
                else None
            ),
        },
        "categories": [
            {"category": category, "server_types": server_types}
            for category, server_types in sorted(categories.items())
        ],
    }


def extract_status(
    response: dict[str, Any],
    server_type: str,
    location: str,
) -> tuple[bool | None, bool | None, str | None]:
    server_types = response.get("server_types")
    if not isinstance(server_types, list):
        return None, None, "Feld 'server_types' fehlt oder ist ungültig."

    matching_server_type = next(
        (
            item
            for item in server_types
            if isinstance(item, dict) and item.get("name") == server_type
        ),
        None,
    )
    if matching_server_type is None:
        return None, None, f"Servertyp '{server_type}' nicht gefunden."

    locations = matching_server_type.get("locations")
    if not isinstance(locations, list):
        return (
            None,
            None,
            f"Standorte für Servertyp '{server_type}' fehlen oder sind ungültig.",
        )

    matching_location = next(
        (
            item
            for item in locations
            if isinstance(item, dict) and _location_name(item) == location
        ),
        None,
    )
    if matching_location is None:
        return (
            None,
            None,
            f"Standort '{location}' für Servertyp '{server_type}' nicht gefunden.",
        )

    available = matching_location.get("available")
    recommended = matching_location.get("recommended")
    if not isinstance(available, bool) or not isinstance(recommended, bool):
        return (
            None,
            None,
            f"Verfügbarkeitsdaten für '{server_type}' in '{location}' sind ungültig.",
        )

    return available, recommended, None


def save_check(
    checked_at: str,
    server_type: str,
    location: str,
    available: bool | None,
    recommended: bool | None,
    request_success: bool,
    http_status_code: int | None,
    error_message: str | None,
) -> None:
    with sqlite3.connect(DATABASE_PATH) as connection:
        connection.execute(
            """
            INSERT INTO availability_checks (
                checked_at,
                server_type,
                location,
                available,
                recommended,
                request_success,
                http_status_code,
                error_message
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                checked_at,
                server_type,
                location,
                None if available is None else int(available),
                None if recommended is None else int(recommended),
                int(request_success),
                http_status_code,
                error_message,
            ),
        )


def last_successful_availability(
    server_type: str,
    location: str,
) -> bool | None:
    with sqlite3.connect(DATABASE_PATH) as connection:
        row = connection.execute(
            """
            SELECT available
            FROM availability_checks
            WHERE server_type = ?
              AND location = ?
              AND request_success = 1
              AND available IS NOT NULL
            ORDER BY id DESC
            LIMIT 1
            """,
            (server_type, location),
        ).fetchone()
    return None if row is None else bool(row[0])


def load_history(limit: int) -> list[dict[str, Any]]:
    history: list[dict[str, Any]] = []

    with sqlite3.connect(DATABASE_PATH) as connection:
        connection.row_factory = sqlite3.Row
        targets = load_monitored_targets(connection)

        for target in targets:
            rows = connection.execute(
                """
                SELECT
                    id,
                    checked_at,
                    available,
                    recommended,
                    request_success,
                    http_status_code,
                    error_message
                FROM availability_checks
                WHERE server_type = ? AND location = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (
                    target["server_type"],
                    target["location"],
                    limit,
                ),
            ).fetchall()

            checks = [
                {
                    "id": row["id"],
                    "checked_at": row["checked_at"],
                    "available": (
                        None
                        if row["available"] is None
                        else bool(row["available"])
                    ),
                    "recommended": (
                        None
                        if row["recommended"] is None
                        else bool(row["recommended"])
                    ),
                    "request_success": bool(row["request_success"]),
                    "http_status_code": row["http_status_code"],
                    "error_message": row["error_message"],
                }
                for row in reversed(rows)
            ]

            history.append(
                {
                    "server_type": target["server_type"],
                    "location": target["location"],
                    "checks": checks,
                }
            )

    return history


def _parse_boolean(value: str) -> bool:
    return value.strip().lower() == "true"


def load_general_config(
    connection: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    owns_connection = connection is None
    active_connection = connection or sqlite3.connect(DATABASE_PATH)
    try:
        return {
            "language": _load_setting(active_connection, "language") or "en",
            "custom_logo_url": _load_setting(
                active_connection,
                "custom_logo_url",
            ),
            "hcloud_token": _load_setting(active_connection, "hcloud_token"),
        }
    finally:
        if owns_connection:
            active_connection.close()


def public_general_config(config: dict[str, Any]) -> dict[str, Any]:
    return {
        "language": config["language"],
        "custom_logo_url": config["custom_logo_url"],
        "hcloud_token_set": bool(config["hcloud_token"]),
    }


def _validate_http_url(
    value: str,
    field_name: str,
    german_field_name: str,
) -> str:
    parsed_url = urlparse(value)
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
        raise SettingsValidationError(
            localized_message(
                f"{field_name} must be a complete HTTP or HTTPS URL.",
                f"{german_field_name} muss eine vollständige HTTP- oder HTTPS-URL sein.",
            )
        )
    if parsed_url.username or parsed_url.password:
        raise SettingsValidationError(
            localized_message(
                f"{field_name} must not contain credentials.",
                f"{german_field_name} darf keine Zugangsdaten enthalten.",
            )
        )
    if len(value) > 2_048:
        raise SettingsValidationError(
            localized_message(
                f"{field_name} must not exceed 2048 characters.",
                f"{german_field_name} darf höchstens 2048 Zeichen enthalten.",
            )
        )
    return value


def validate_general_config(
    raw_config: Any,
    stored_config: dict[str, Any],
) -> dict[str, Any]:
    if raw_config is None:
        raw_config = {}
    if not isinstance(raw_config, dict):
        raise SettingsValidationError(
            localized_message(
                "The general settings are invalid.",
                "Die allgemeinen Einstellungen sind ungültig.",
            )
        )

    language = _clean_text(raw_config.get("language")) or stored_config["language"]
    if language not in {"en", "de"}:
        raise SettingsValidationError(
            localized_message(
                "The selected language is invalid.",
                "Die gewählte Sprache ist ungültig.",
            )
        )

    custom_logo_url = (
        _clean_text(raw_config.get("custom_logo_url"))
        if "custom_logo_url" in raw_config
        else stored_config["custom_logo_url"]
    )
    if custom_logo_url:
        _validate_http_url(
            custom_logo_url,
            "The custom logo URL",
            "Die eigene Logo-URL",
        )

    hcloud_token = (
        _clean_text(raw_config.get("hcloud_token"))
        or stored_config["hcloud_token"]
    )
    if len(hcloud_token) > 512 or any(
        character.isspace() for character in hcloud_token
    ):
        raise SettingsValidationError(
            localized_message(
                "The Hetzner Cloud API token is invalid.",
                "Der Hetzner-Cloud-API-Token ist ungültig.",
            )
        )

    return {
        "language": language,
        "custom_logo_url": custom_logo_url,
        "hcloud_token": hcloud_token,
    }


def load_ntfy_config(
    connection: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    owns_connection = connection is None
    active_connection = connection or sqlite3.connect(DATABASE_PATH)
    try:
        return {
            "enabled": _parse_boolean(
                _load_setting(active_connection, "ntfy_enabled")
            ),
            "domain": _load_setting(active_connection, "ntfy_domain"),
            "topic": _load_setting(active_connection, "ntfy_topic"),
            "auth_mode": (
                _load_setting(active_connection, "ntfy_auth_mode") or "none"
            ),
            "username": _load_setting(active_connection, "ntfy_username"),
            "password": _load_setting(active_connection, "ntfy_password"),
            "token": _load_setting(active_connection, "ntfy_token"),
            "dashboard_url": _load_setting(
                active_connection,
                "ntfy_dashboard_url",
            ),
            "message_template": (
                _load_setting(active_connection, "ntfy_message_template")
                or DEFAULT_NTFY_MESSAGE_TEMPLATE
            ),
        }
    finally:
        if owns_connection:
            active_connection.close()


def public_ntfy_config(config: dict[str, Any]) -> dict[str, Any]:
    return {
        "enabled": config["enabled"],
        "domain": config["domain"],
        "topic": config["topic"],
        "auth_mode": config["auth_mode"],
        "username": config["username"],
        "password_set": bool(config["password"]),
        "token_set": bool(config["token"]),
        "dashboard_url": config["dashboard_url"],
        "message_template": config["message_template"],
    }


def _clean_text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def validate_message_template(template: str) -> None:
    if not template:
        raise SettingsValidationError(
            localized_message(
                "The ntfy message must not be empty.",
                "Die ntfy-Nachricht darf nicht leer sein.",
            )
        )
    if len(template) > 1_000:
        raise SettingsValidationError(
            localized_message(
                "The ntfy message must not exceed 1000 characters.",
                "Die ntfy-Nachricht darf höchstens 1000 Zeichen enthalten.",
            )
        )

    try:
        parsed_parts = list(string.Formatter().parse(template))
    except ValueError as error:
        raise SettingsValidationError(
            localized_message(
                "The ntfy message contains invalid braces.",
                "Die ntfy-Nachricht enthält ungültige geschweifte Klammern.",
            )
        ) from error

    parsed_fields = [
        field_name
        for _, field_name, _, _ in parsed_parts
        if field_name is not None
    ]
    if any(
        format_spec or conversion
        for _, field_name, format_spec, conversion in parsed_parts
        if field_name is not None
    ):
        raise SettingsValidationError(
            localized_message(
                "Format specifications and conversions are not allowed in placeholders.",
                "Formatangaben und Konvertierungen sind in Platzhaltern nicht erlaubt.",
            )
        )
    unknown_fields = sorted(set(parsed_fields) - ALLOWED_TEMPLATE_FIELDS)
    if unknown_fields:
        raise SettingsValidationError(
            localized_message(
                "Unknown placeholders: ",
                "Unbekannte Platzhalter: ",
            ) + ", ".join(unknown_fields)
        )


def validate_ntfy_config(
    raw_config: Any,
    stored_config: dict[str, Any],
    require_enabled: bool = True,
) -> dict[str, Any]:
    if not isinstance(raw_config, dict):
        raise SettingsValidationError(
            localized_message(
                "The ntfy settings are invalid.",
                "Die ntfy-Einstellungen sind ungültig.",
            )
        )

    enabled = bool(raw_config.get("enabled"))
    domain = _clean_text(raw_config.get("domain"))
    topic = _clean_text(raw_config.get("topic"))
    auth_mode = _clean_text(raw_config.get("auth_mode")) or "none"
    username = _clean_text(raw_config.get("username"))
    password = _clean_text(raw_config.get("password")) or stored_config["password"]
    token = _clean_text(raw_config.get("token")) or stored_config["token"]
    dashboard_url = (
        _clean_text(raw_config.get("dashboard_url"))
        if "dashboard_url" in raw_config
        else _clean_text(stored_config.get("dashboard_url"))
    )
    message_template = _clean_text(raw_config.get("message_template"))

    if auth_mode not in {"none", "basic", "token"}:
        raise SettingsValidationError(
            localized_message(
                "The ntfy authentication mode is invalid.",
                "Die ntfy-Authentifizierung ist ungültig.",
            )
        )
    validate_message_template(message_template)

    must_validate_connection = enabled if require_enabled else True
    if must_validate_connection:
        parsed_domain = urlparse(domain)
        if parsed_domain.scheme not in {"http", "https"} or not parsed_domain.netloc:
            raise SettingsValidationError(
                localized_message(
                    "The ntfy server must be a complete HTTP or HTTPS URL.",
                    "Die ntfy-Domain muss eine vollständige HTTP- oder HTTPS-URL sein.",
                )
            )
        if parsed_domain.query or parsed_domain.fragment:
            raise SettingsValidationError(
                localized_message(
                    "The ntfy server URL must not contain a query or fragment.",
                    "Die ntfy-Domain darf keine Query-Parameter oder Fragmente enthalten.",
                )
            )
        if not TOPIC_PATTERN.fullmatch(topic):
            raise SettingsValidationError(
                localized_message(
                    "The ntfy topic may only contain letters, numbers, _ and -.",
                    "Das ntfy-Topic darf nur Buchstaben, Zahlen, _ und - enthalten.",
                )
            )
        _validate_http_url(
            dashboard_url,
            "The dashboard link",
            "Der Dashboard-Link",
        )
        if auth_mode == "basic" and (not username or not password):
            raise SettingsValidationError(
                localized_message(
                    "Basic authentication requires a username and password.",
                    "Für Basic Auth werden Benutzername und Passwort benötigt.",
                )
            )
        if auth_mode == "token" and not token:
            raise SettingsValidationError(
                localized_message(
                    "Token authentication requires an access token.",
                    "Für Token-Authentifizierung wird ein Zugriffstoken benötigt.",
                )
            )

    return {
        "enabled": enabled,
        "domain": domain.rstrip("/"),
        "topic": topic,
        "auth_mode": auth_mode,
        "username": username,
        "password": password,
        "token": token,
        "dashboard_url": dashboard_url,
        "message_template": message_template,
    }


def validate_targets(raw_targets: Any) -> list[dict[str, str]]:
    if not isinstance(raw_targets, list) or not raw_targets:
        raise SettingsValidationError(
            localized_message(
                "Select at least one server type and location.",
                "Wähle mindestens einen Servertyp und Standort aus.",
            )
        )
    if len(raw_targets) > 500:
        raise SettingsValidationError(
            localized_message(
                "At most 500 targets can be monitored.",
                "Es können höchstens 500 Ziele überwacht werden.",
            )
        )

    unique_targets: set[tuple[str, str]] = set()
    for raw_target in raw_targets:
        if not isinstance(raw_target, dict):
            raise SettingsValidationError(
                localized_message(
                    "A monitoring target is invalid.",
                    "Ein Überwachungsziel ist ungültig.",
                )
            )
        server_type = _clean_text(raw_target.get("server_type")).lower()
        location = _clean_text(raw_target.get("location")).lower()
        if not TARGET_VALUE_PATTERN.fullmatch(server_type):
            raise SettingsValidationError(
                localized_message(
                    "A server type is invalid.",
                    "Ein Servertyp ist ungültig.",
                )
            )
        if not TARGET_VALUE_PATTERN.fullmatch(location):
            raise SettingsValidationError(
                localized_message(
                    "A location is invalid.",
                    "Ein Standort ist ungültig.",
                )
            )
        unique_targets.add((server_type, location))

    return [
        {"server_type": server_type, "location": location}
        for server_type, location in sorted(unique_targets)
    ]


def validate_poll_interval(value: Any) -> float:
    try:
        interval = float(value)
    except (TypeError, ValueError) as error:
        raise SettingsValidationError(
            localized_message(
                "The check interval must be a number.",
                "Das Abfrageintervall muss eine Zahl sein.",
            )
        ) from error
    if not MIN_POLL_INTERVAL_SECONDS <= interval <= MAX_POLL_INTERVAL_SECONDS:
        raise SettingsValidationError(
            localized_message(
                "The check interval must be between 10 and 86400 seconds.",
                "Das Abfrageintervall muss zwischen 10 und 86400 Sekunden liegen.",
            )
        )
    return interval


def save_application_settings(
    payload: Any,
    wake_monitor: bool = True,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise SettingsValidationError(
            localized_message(
                "The settings are invalid.",
                "Die Einstellungen sind ungültig.",
            )
        )

    interval = validate_poll_interval(payload.get("poll_interval_seconds"))
    targets = validate_targets(payload.get("monitored_targets"))

    with sqlite3.connect(DATABASE_PATH) as connection:
        stored_general = load_general_config(connection)
        general = validate_general_config(payload.get("general"), stored_general)
        stored_ntfy = load_ntfy_config(connection)
        ntfy = validate_ntfy_config(payload.get("ntfy"), stored_ntfy)

        for key, value in general.items():
            _save_setting(connection, key, str(value))
        _save_setting(connection, "poll_interval_seconds", f"{interval:g}")
        for key in (
            "enabled",
            "domain",
            "topic",
            "auth_mode",
            "username",
            "password",
            "token",
            "dashboard_url",
            "message_template",
        ):
            value = ntfy[key]
            serialized_value = (
                "true" if value is True else "false" if value is False else str(value)
            )
            _save_setting(connection, f"ntfy_{key}", serialized_value)

        connection.execute("DELETE FROM monitored_targets")
        connection.executemany(
            """
            INSERT INTO monitored_targets (server_type, location)
            VALUES (?, ?)
            """,
            (
                (target["server_type"], target["location"])
                for target in targets
            ),
        )

    if wake_monitor:
        WAKE_EVENT.set()
    return {
        "general": public_general_config(general),
        "poll_interval_seconds": interval,
        "monitored_targets": targets,
        "ntfy": public_ntfy_config(ntfy),
    }


def render_ntfy_message(
    config: dict[str, Any],
    server_type: str,
    location: str,
    checked_at: str,
    recommended: bool,
) -> str:
    try:
        parsed_checked_at = datetime.fromisoformat(
            checked_at.replace("Z", "+00:00")
        )
        displayed_checked_at = parsed_checked_at.strftime("%H:%M %d.%m.%Y")
    except ValueError:
        displayed_checked_at = checked_at

    language = str(config.get("language") or get_application_language())
    values = {
        "server_type": server_type.upper(),
        "location": location,
        "status": "verfügbar" if language == "de" else "available",
        "checked_at": displayed_checked_at,
        "recommended": (
            "ja" if recommended else "nein"
        ) if language == "de" else (
            "yes" if recommended else "no"
        ),
    }
    return str(config["message_template"]).format_map(values)


def publish_ntfy(
    config: dict[str, Any],
    title: str,
    message: str,
) -> None:
    headers: dict[str, str] = {}
    auth: tuple[str, str] | None = None
    if config["auth_mode"] == "basic":
        auth = (config["username"], config["password"])
    elif config["auth_mode"] == "token":
        headers["Authorization"] = f"Bearer {config['token']}"

    try:
        response = requests.post(
            f"{config['domain'].rstrip('/')}/",
            json={
                "topic": config["topic"],
                "title": title,
                "message": message,
                "tags": ["white_check_mark"],
                "click": config["dashboard_url"],
            },
            headers=headers,
            auth=auth,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except requests.Timeout as error:
        raise NtfyError(
            localized_message(
                f"ntfy timeout after {REQUEST_TIMEOUT_SECONDS:g} seconds.",
                f"ntfy-Timeout nach {REQUEST_TIMEOUT_SECONDS:g} Sekunden.",
            )
        ) from error
    except requests.RequestException as error:
        raise NtfyError(
            localized_message(
                f"ntfy is unreachable ({type(error).__name__}).",
                f"ntfy ist nicht erreichbar ({type(error).__name__}).",
            )
        ) from error

    try:
        response.raise_for_status()
    except requests.HTTPError as error:
        raise NtfyError(
            localized_message(
                f"ntfy responded with HTTP {response.status_code}.",
                f"ntfy antwortet mit HTTP {response.status_code}.",
            )
        ) from error


def notify_available(
    server_type: str,
    location: str,
    checked_at: str,
    recommended: bool,
) -> None:
    config = load_ntfy_config()
    if not config["enabled"]:
        return
    try:
        validated_config = validate_ntfy_config(config, config)
        message = render_ntfy_message(
            validated_config,
            server_type,
            location,
            checked_at,
            recommended,
        )
        publish_ntfy(
            validated_config,
            (
                f"HetznerWatch: {server_type.upper()} verfügbar"
                if get_application_language() == "de"
                else f"HetznerWatch: {server_type.upper()} available"
            ),
            message,
        )
        logging.info("ntfy-Nachricht für %s/%s gesendet.", server_type, location)
    except (SettingsValidationError, NtfyError, OSError, sqlite3.Error) as error:
        logging.error("ntfy-Nachricht konnte nicht gesendet werden: %s", error)


@WEB_APP.after_request
def add_response_headers(response: Any) -> Any:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "connect-src 'self'; "
        "img-src 'self' data: http: https:; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'"
    )
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    return response


@WEB_APP.get("/")
def dashboard() -> Any:
    return send_file(INDEX_PATH)


@WEB_APP.get("/api/health")
def health_api() -> Any:
    return jsonify({"status": "ok"})


@WEB_APP.get("/api/app-config")
def app_config_api() -> Any:
    try:
        general = load_general_config()
    except (OSError, sqlite3.Error) as error:
        logging.error("App configuration could not be loaded: %s", error)
        return jsonify({"error": "The app configuration is unavailable."}), 503
    return jsonify(
        {
            "general": public_general_config(general),
            "setup_complete": bool(general["hcloud_token"]),
        }
    )


@WEB_APP.get("/api/history")
def history_api() -> Any:
    raw_limit = request.args.get("limit", "120")
    try:
        limit = int(raw_limit)
    except ValueError:
        return jsonify(
            {
                "error": localized_message(
                    "The 'limit' parameter must be a number.",
                    "Der Parameter 'limit' muss eine Zahl sein.",
                )
            }
        ), 400

    limit = max(1, min(limit, 500))

    try:
        targets = load_history(limit)
        interval = get_poll_interval_seconds()
        monitoring_enabled = get_monitoring_enabled()
    except (OSError, sqlite3.Error) as error:
        logging.error("SQLite-Fehler beim Laden des Verlaufs: %s", error)
        return jsonify(
            {
                "error": localized_message(
                    "History is currently unavailable.",
                    "Der Verlauf ist derzeit nicht verfügbar.",
                )
            }
        ), 503

    return jsonify(
        {
            "generated_at": utc_timestamp(),
            "monitoring_enabled": monitoring_enabled,
            "poll_interval_seconds": interval,
            "targets": targets,
        }
    )


@WEB_APP.get("/api/long-term")
def long_term_api() -> Any:
    range_key = request.args.get("range", "7d")
    if range_key not in LONG_TERM_RANGES:
        return jsonify(
            {
                "error": localized_message(
                    "The selected range is invalid.",
                    "Der gewählte Zeitraum ist ungültig.",
                )
            }
        ), 400

    try:
        statistics = build_long_term_statistics(range_key)
    except (OSError, sqlite3.Error, ValueError) as error:
        logging.error("Langzeitstatistik konnte nicht geladen werden: %s", error)
        return jsonify(
            {
                "error": localized_message(
                    "Long-term statistics are currently unavailable.",
                    "Die Langzeitstatistik ist derzeit nicht verfügbar.",
                )
            }
        ), 503
    return jsonify(statistics)


@WEB_APP.get("/api/settings")
def settings_api() -> Any:
    try:
        with sqlite3.connect(DATABASE_PATH) as connection:
            general = load_general_config(connection)
            monitoring_enabled = get_monitoring_enabled(connection)
            interval = float(_load_setting(connection, "poll_interval_seconds"))
            targets = load_monitored_targets(connection)
            ntfy = load_ntfy_config(connection)
        catalog = load_cached_server_catalog()
    except (OSError, sqlite3.Error, ValueError) as error:
        logging.error("Einstellungen konnten nicht geladen werden: %s", error)
        return jsonify(
            {
                "error": localized_message(
                    "Settings are currently unavailable.",
                    "Die Einstellungen sind derzeit nicht verfügbar.",
                )
            }
        ), 503

    catalog_error = ""
    try:
        payload, _ = fetch_server_types()
        catalog = cache_server_catalog(payload, load_pricing_for_catalog())
    except (ApiFetchError, OSError, sqlite3.Error) as error:
        catalog_error = str(error)

    return jsonify(
        {
            "general": public_general_config(general),
            "monitoring_enabled": monitoring_enabled,
            "poll_interval_seconds": interval,
            "monitored_targets": targets,
            "ntfy": public_ntfy_config(ntfy),
            "server_catalog": catalog,
            "catalog_error": catalog_error,
            "limits": {
                "min_poll_interval_seconds": MIN_POLL_INTERVAL_SECONDS,
                "max_poll_interval_seconds": MAX_POLL_INTERVAL_SECONDS,
            },
            "placeholders": sorted(ALLOWED_TEMPLATE_FIELDS),
        }
    )


@WEB_APP.put("/api/settings")
def update_settings_api() -> Any:
    payload = request.get_json(silent=True)
    raw_general = payload.get("general") if isinstance(payload, dict) else None
    run_initial_check = bool(
        isinstance(payload, dict)
        and payload.get("run_initial_check") is True
        and isinstance(raw_general, dict)
        and _clean_text(raw_general.get("hcloud_token"))
    )
    try:
        saved_settings = save_application_settings(
            payload,
            wake_monitor=not run_initial_check,
        )
    except SettingsValidationError as error:
        return jsonify({"error": str(error)}), 400
    except (OSError, sqlite3.Error) as error:
        logging.error("Einstellungen konnten nicht gespeichert werden: %s", error)
        return jsonify(
            {
                "error": localized_message(
                    "Settings could not be saved.",
                    "Die Einstellungen konnten nicht gespeichert werden.",
                )
            }
        ), 503
    initial_check_error = ""
    initial_check_attempted = False
    refreshed_catalog: list[dict[str, Any]] | None = None
    if run_initial_check:
        try:
            check_error = run_check()
            initial_check_attempted = True
            if isinstance(check_error, str):
                initial_check_error = check_error
            refreshed_catalog = load_cached_server_catalog()
        except (OSError, sqlite3.Error, ValueError) as error:
            logging.error("Erste Abfrage konnte nicht ausgeführt werden: %s", error)
            initial_check_error = localized_message(
                "The settings were saved, but the first check could not be completed.",
                "Die Einstellungen wurden gespeichert, aber die erste Abfrage konnte nicht abgeschlossen werden.",
            )
        finally:
            if initial_check_attempted:
                INITIAL_CHECK_COMPLETED_EVENT.set()
            WAKE_EVENT.set()

    response_payload = {
        "saved_at": utc_timestamp(),
        "initial_check_completed": run_initial_check and not initial_check_error,
        "initial_check_error": initial_check_error,
        **saved_settings,
    }
    if refreshed_catalog is not None:
        response_payload["server_catalog"] = refreshed_catalog
        response_payload["catalog_error"] = initial_check_error
    return jsonify(response_payload)


@WEB_APP.post("/api/monitoring")
def update_monitoring_api() -> Any:
    payload = request.get_json(silent=True)
    enabled = payload.get("enabled") if isinstance(payload, dict) else None
    if not isinstance(enabled, bool):
        return jsonify(
            {
                "error": localized_message(
                    "The monitoring state is invalid.",
                    "Der Monitoring-Status ist ungültig.",
                )
            }
        ), 400

    try:
        with sqlite3.connect(DATABASE_PATH) as connection:
            _save_setting(
                connection,
                "monitoring_enabled",
                "true" if enabled else "false",
            )
    except (OSError, sqlite3.Error) as error:
        logging.error("Monitoring-Status konnte nicht gespeichert werden: %s", error)
        return jsonify(
            {
                "error": localized_message(
                    "The monitoring state could not be saved.",
                    "Der Monitoring-Status konnte nicht gespeichert werden.",
                )
            }
        ), 503

    logging.info("Automatische Abfragen %s.", "aktiviert" if enabled else "pausiert")
    WAKE_EVENT.set()
    return jsonify(
        {
            "monitoring_enabled": enabled,
            "updated_at": utc_timestamp(),
        }
    )


@WEB_APP.post("/api/settings/ntfy-topic")
def generate_ntfy_topic_api() -> Any:
    return jsonify({"topic": generate_default_ntfy_topic()})


@WEB_APP.post("/api/settings/ntfy-test")
def ntfy_test_api() -> Any:
    payload = request.get_json(silent=True)
    raw_config = payload.get("ntfy") if isinstance(payload, dict) else None
    try:
        stored_config = load_ntfy_config()
        config = validate_ntfy_config(
            raw_config,
            stored_config,
            require_enabled=False,
        )
        checked_at = utc_timestamp()
        message = render_ntfy_message(
            config,
            "cx23",
            "fsn1",
            checked_at,
            True,
        )
        publish_ntfy(
            config,
            (
                "HetznerWatch: Testnachricht"
                if get_application_language() == "de"
                else "HetznerWatch: test message"
            ),
            message,
        )
    except SettingsValidationError as error:
        return jsonify({"error": str(error)}), 400
    except NtfyError as error:
        return jsonify({"error": str(error)}), 502
    except (OSError, sqlite3.Error) as error:
        logging.error("ntfy-Test konnte nicht vorbereitet werden: %s", error)
        return jsonify(
            {
                "error": localized_message(
                    "The ntfy test could not be prepared.",
                    "Der ntfy-Test konnte nicht vorbereitet werden.",
                )
            }
        ), 503
    return jsonify(
        {
            "sent_at": checked_at,
            "message": (
                "ntfy-Testnachricht gesendet."
                if get_application_language() == "de"
                else "ntfy test message sent."
            ),
        }
    )


def _save_check_safely(
    checked_at: str,
    server_type: str,
    location: str,
    available: bool | None,
    recommended: bool | None,
    request_success: bool,
    http_status_code: int | None,
    error_message: str | None,
) -> bool:
    try:
        save_check(
            checked_at,
            server_type,
            location,
            available,
            recommended,
            request_success,
            http_status_code,
            error_message,
        )
        return True
    except (OSError, sqlite3.Error) as error:
        logging.error(
            "SQLite-Fehler beim Speichern von %s/%s: %s",
            server_type,
            location,
            error,
        )
        return False


def run_check() -> str:
    with CHECK_LOCK:
        return _run_check_unlocked()


def _run_check_unlocked() -> str:
    checked_at = utc_timestamp()
    targets = load_monitored_targets()
    if not targets:
        logging.warning("Keine Überwachungsziele konfiguriert.")
        return localized_message(
            "No monitoring targets are configured.",
            "Keine Überwachungsziele konfiguriert.",
        )

    try:
        response, http_status_code = fetch_server_types()
        catalog = cache_server_catalog(response, load_pricing_for_catalog())
        save_availability_rollups(checked_at, catalog)
    except ApiFetchError as error:
        error_message = str(error)
        logging.error("API-Abfrage fehlgeschlagen: %s", error_message)
        for target in targets:
            _save_check_safely(
                checked_at,
                target["server_type"],
                target["location"],
                None,
                None,
                False,
                error.http_status_code,
                error_message,
            )
        return error_message
    except (OSError, sqlite3.Error, ValueError) as error:
        logging.error("Langzeitdaten konnten nicht gespeichert werden: %s", error)
        catalog = []

    for target in targets:
        server_type = target["server_type"]
        location = target["location"]
        previous_availability = last_successful_availability(
            server_type,
            location,
        )
        available, recommended, error_message = extract_status(
            response,
            server_type,
            location,
        )
        request_success = error_message is None

        if error_message:
            logging.error("Auswertung fehlgeschlagen: %s", error_message)

        saved = _save_check_safely(
            checked_at,
            server_type,
            location,
            available,
            recommended,
            request_success,
            http_status_code,
            error_message,
        )

        if request_success and saved:
            logging.info(
                "Prüfung gespeichert: %s/%s available=%s recommended=%s",
                server_type,
                location,
                available,
                recommended,
            )
            if available is True and previous_availability is False:
                notify_available(
                    server_type,
                    location,
                    checked_at,
                    bool(recommended),
                )

    return ""


def handle_shutdown(signum: int, _frame: Any) -> None:
    logging.info("Signal %s empfangen; Monitor wird beendet.", signum)
    STOP_EVENT.set()
    WAKE_EVENT.set()


def monitor_loop() -> None:
    database_ready = False
    while not STOP_EVENT.is_set():
        if not database_ready:
            try:
                init_database()
                database_ready = True
                logging.info("SQLite-Datenbank initialisiert: %s", DATABASE_PATH)
            except (OSError, sqlite3.Error) as error:
                logging.error("SQLite-Initialisierung fehlgeschlagen: %s", error)
                STOP_EVENT.wait(POLL_INTERVAL_SECONDS)
                continue

        try:
            cleanup_old_data()
        except (OSError, sqlite3.Error, ValueError) as error:
            logging.error("Aufbewahrungsbereinigung fehlgeschlagen: %s", error)

        if not get_monitoring_enabled():
            WAKE_EVENT.wait(min(3_600, RETENTION_CLEANUP_INTERVAL_SECONDS))
            WAKE_EVENT.clear()
            continue

        if not get_hcloud_token():
            WAKE_EVENT.wait(min(3_600, RETENTION_CLEANUP_INTERVAL_SECONDS))
            WAKE_EVENT.clear()
            continue

        if INITIAL_CHECK_COMPLETED_EVENT.is_set():
            INITIAL_CHECK_COMPLETED_EVENT.clear()
            WAKE_EVENT.wait(get_poll_interval_seconds())
            WAKE_EVENT.clear()
            continue

        try:
            run_check()
        except Exception as error:
            logging.error(
                "Unerwarteter Fehler während der Prüfung (%s).",
                type(error).__name__,
            )

        WAKE_EVENT.wait(get_poll_interval_seconds())
        WAKE_EVENT.clear()


def main() -> None:
    configure_logging()
    load_configuration()
    STOP_EVENT.clear()
    WAKE_EVENT.clear()
    INITIAL_CHECK_COMPLETED_EVENT.clear()

    signal.signal(signal.SIGTERM, handle_shutdown)
    signal.signal(signal.SIGINT, handle_shutdown)

    try:
        init_database()
    except (OSError, sqlite3.Error) as error:
        logging.error("SQLite-Initialisierung fehlgeschlagen: %s", error)

    try:
        web_server = make_server(
            WEB_HOST,
            WEB_PORT,
            WEB_APP,
            threaded=True,
        )
    except OSError as error:
        logging.error(
            "Lokaler Webserver konnte nicht auf Port %d starten: %s",
            WEB_PORT,
            error,
        )
        return

    web_server.timeout = 0.5
    monitor_thread = threading.Thread(
        target=monitor_loop,
        name="availability-monitor",
        daemon=True,
    )

    logging.info(
        "Hetzner-Verfügbarkeitsmonitor startet mit Intervall %g Sekunden.",
        get_poll_interval_seconds(),
    )
    logging.info("Lokales Dashboard: http://localhost:%d", WEB_PORT)
    monitor_thread.start()

    try:
        while not STOP_EVENT.is_set():
            web_server.handle_request()
    finally:
        STOP_EVENT.set()
        WAKE_EVENT.set()
        web_server.server_close()
        monitor_thread.join(timeout=REQUEST_TIMEOUT_SECONDS + 2)

    logging.info("Hetzner-Verfügbarkeitsmonitor wurde beendet.")


if __name__ == "__main__":
    main()
