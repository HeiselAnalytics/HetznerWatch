import logging
import os
import signal
import sqlite3
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_file
from werkzeug.serving import make_server


API_URL = "https://api.hetzner.cloud/v1/server_types"
MONITORED_TARGETS = [
    {"server_type": "cx23", "location": "fsn1"},
    {"server_type": "cx33", "location": "fsn1"},
    {"server_type": "cx33", "location": "nbg1"},
]

CREATE_TABLE_SQL = """
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
"""

HCLOUD_TOKEN = ""
POLL_INTERVAL_SECONDS = 60.0
REQUEST_TIMEOUT_SECONDS = 15.0
DATABASE_PATH = "/data/hetzner_availability.db"
WEB_HOST = "0.0.0.0"
WEB_PORT = 8080
STOP_EVENT = threading.Event()
WEB_APP = Flask(__name__)
INDEX_PATH = Path(__file__).with_name("index.html")
SERVICE_WORKER_PATH = Path(__file__).with_name("service-worker.js")


class ApiFetchError(Exception):
    def __init__(
        self,
        message: str,
        http_status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.http_status_code = http_status_code


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

    load_dotenv()

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


def init_database() -> None:
    database_directory = os.path.dirname(DATABASE_PATH)
    if database_directory:
        os.makedirs(database_directory, exist_ok=True)

    with sqlite3.connect(DATABASE_PATH) as connection:
        connection.execute(CREATE_TABLE_SQL)


def fetch_server_types() -> tuple[dict[str, Any], int]:
    if not HCLOUD_TOKEN:
        raise ApiFetchError("HCLOUD_TOKEN ist nicht gesetzt.")

    try:
        response = requests.get(
            API_URL,
            headers={"Authorization": f"Bearer {HCLOUD_TOKEN}"},
            params={"per_page": 50},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except requests.Timeout as error:
        raise ApiFetchError(
            f"API-Timeout nach {REQUEST_TIMEOUT_SECONDS:g} Sekunden."
        ) from error
    except requests.RequestException as error:
        raise ApiFetchError(
            f"API nicht erreichbar ({type(error).__name__})."
        ) from error

    http_status_code = response.status_code
    try:
        response.raise_for_status()
    except requests.HTTPError as error:
        raise ApiFetchError(
            f"HTTP-Fehler {http_status_code} von der Hetzner API.",
            http_status_code,
        ) from error

    try:
        payload = response.json()
    except (requests.exceptions.JSONDecodeError, ValueError) as error:
        raise ApiFetchError(
            "Die Hetzner API hat ungültiges JSON zurückgegeben.",
            http_status_code,
        ) from error

    if not isinstance(payload, dict):
        raise ApiFetchError(
            "Die Hetzner API hat kein JSON-Objekt zurückgegeben.",
            http_status_code,
        )

    return payload, http_status_code


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

    matching_location: dict[str, Any] | None = None
    for item in locations:
        if not isinstance(item, dict):
            continue

        location_value = item.get("location")
        if isinstance(location_value, dict):
            location_name = location_value.get("name")
        elif isinstance(location_value, str):
            location_name = location_value
        else:
            location_name = item.get("name")

        if location_name == location:
            matching_location = item
            break

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


def load_history(limit: int) -> list[dict[str, Any]]:
    history: list[dict[str, Any]] = []

    with sqlite3.connect(DATABASE_PATH) as connection:
        connection.row_factory = sqlite3.Row

        for target in MONITORED_TARGETS:
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


@WEB_APP.after_request
def add_response_headers(response: Any) -> Any:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "connect-src 'self'; "
        "img-src 'self' data:; "
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


@WEB_APP.get("/service-worker.js")
def service_worker() -> Any:
    response = send_file(
        SERVICE_WORKER_PATH,
        mimetype="application/javascript",
    )
    response.headers["Service-Worker-Allowed"] = "/"
    return response


@WEB_APP.get("/api/history")
def history_api() -> Any:
    raw_limit = request.args.get("limit", "120")
    try:
        limit = int(raw_limit)
    except ValueError:
        return jsonify({"error": "Der Parameter 'limit' muss eine Zahl sein."}), 400

    limit = max(1, min(limit, 500))

    try:
        targets = load_history(limit)
    except (OSError, sqlite3.Error) as error:
        logging.error("SQLite-Fehler beim Laden des Verlaufs: %s", error)
        return jsonify({"error": "Der Verlauf ist derzeit nicht verfügbar."}), 503

    return jsonify(
        {
            "generated_at": utc_timestamp(),
            "poll_interval_seconds": POLL_INTERVAL_SECONDS,
            "targets": targets,
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


def run_check() -> None:
    checked_at = utc_timestamp()

    try:
        response, http_status_code = fetch_server_types()
    except ApiFetchError as error:
        error_message = str(error)
        logging.error("API-Abfrage fehlgeschlagen: %s", error_message)
        for target in MONITORED_TARGETS:
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
        return

    for target in MONITORED_TARGETS:
        server_type = target["server_type"]
        location = target["location"]
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


def handle_shutdown(signum: int, _frame: Any) -> None:
    logging.info("Signal %s empfangen; Monitor wird beendet.", signum)
    STOP_EVENT.set()


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
            run_check()
        except Exception as error:
            logging.error(
                "Unerwarteter Fehler während der Prüfung (%s).",
                type(error).__name__,
            )

        STOP_EVENT.wait(POLL_INTERVAL_SECONDS)


def main() -> None:
    configure_logging()
    load_configuration()
    STOP_EVENT.clear()

    signal.signal(signal.SIGTERM, handle_shutdown)
    signal.signal(signal.SIGINT, handle_shutdown)

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
        POLL_INTERVAL_SECONDS,
    )
    logging.info("Lokales Dashboard: http://localhost:%d", WEB_PORT)
    monitor_thread.start()

    try:
        while not STOP_EVENT.is_set():
            web_server.handle_request()
    finally:
        STOP_EVENT.set()
        web_server.server_close()
        monitor_thread.join(timeout=REQUEST_TIMEOUT_SECONDS + 2)

    logging.info("Hetzner-Verfügbarkeitsmonitor wurde beendet.")


if __name__ == "__main__":
    main()
