import copy
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

import app


SERVER_TYPES_PAYLOAD = {
    "server_types": [
        {
            "name": "cx23",
            "description": "Shared vCPU",
            "category": "shared",
            "architecture": "x86",
            "cores": 2,
            "memory": 4,
            "disk": 40,
            "locations": [
                {
                    "location": {"name": "fsn1"},
                    "available": True,
                    "recommended": True,
                }
            ],
        }
    ]
}

PRICING_PAYLOAD = {
    "currency": "EUR",
    "server_types": [
        {
            "name": "cx23",
            "prices": [
                {
                    "location": "fsn1",
                    "price_hourly": {"net": "0.0050", "gross": "0.0060"},
                    "price_monthly": {"net": "3.2000", "gross": "3.8080"},
                }
            ],
        }
    ],
}

MEMORABLE_TOPIC_PATTERN = r"^hetznerwatch_[a-z]+(?:[0-9]{3}[a-z]+){3}$"


class HetznerWatchTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        app.DATABASE_PATH = f"{self.temporary_directory.name}/test.db"
        app.POLL_INTERVAL_SECONDS = 60
        app.HCLOUD_TOKEN = ""
        app.WAKE_EVENT.clear()
        app.INITIAL_CHECK_COMPLETED_EVENT.clear()
        app.init_database()
        self.client = app.WEB_APP.test_client()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_default_settings_and_catalog_error_are_returned(self) -> None:
        response = self.client.get("/api/settings")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["monitoring_enabled"])
        self.assertEqual(payload["poll_interval_seconds"], 60)
        self.assertEqual(len(payload["monitored_targets"]), 3)
        self.assertEqual(payload["general"]["language"], "en")
        self.assertFalse(payload["general"]["hcloud_token_set"])
        self.assertFalse(payload["ntfy"]["enabled"])
        self.assertEqual(payload["ntfy"]["domain"], "https://ntfy.sh")
        self.assertRegex(
            payload["ntfy"]["topic"],
            MEMORABLE_TOPIC_PATTERN,
        )
        self.assertIn("API token", payload["catalog_error"])
        self.assertIn("checked_at", payload["placeholders"])

    def test_existing_empty_ntfy_topic_gets_one_random_default(self) -> None:
        with sqlite3.connect(app.DATABASE_PATH) as connection:
            connection.execute(
                "DELETE FROM app_settings WHERE key = ?",
                ("ntfy_default_topic_initialized",),
            )
            app._save_setting(connection, "ntfy_topic", "")

        app.init_database()
        generated_topic = app.load_ntfy_config()["topic"]
        self.assertRegex(generated_topic, MEMORABLE_TOPIC_PATTERN)

        with sqlite3.connect(app.DATABASE_PATH) as connection:
            app._save_setting(connection, "ntfy_topic", "")
        app.init_database()

        self.assertEqual(app.load_ntfy_config()["topic"], "")

    def test_memorable_ntfy_topic_endpoint_does_not_replace_saved_topic(self) -> None:
        stored_topic = app.load_ntfy_config()["topic"]

        response = self.client.post("/api/settings/ntfy-topic")

        self.assertEqual(response.status_code, 200)
        self.assertRegex(response.get_json()["topic"], MEMORABLE_TOPIC_PATTERN)
        self.assertEqual(app.load_ntfy_config()["topic"], stored_topic)
        self.assertEqual(len(app.NTFY_TOPIC_WORDS), 128)
        self.assertEqual(len(set(app.NTFY_TOPIC_WORDS)), 128)

    def test_health_and_public_app_config_do_not_expose_token(self) -> None:
        with sqlite3.connect(app.DATABASE_PATH) as connection:
            app._save_setting(connection, "hcloud_token", "super-secret-token")

        health = self.client.get("/api/health")
        response = self.client.get("/api/app-config")

        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.get_json(), {"status": "ok"})
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["setup_complete"])
        self.assertTrue(payload["general"]["hcloud_token_set"])
        self.assertNotIn("hcloud_token", payload["general"])

    def test_favicon_is_linked_and_served(self) -> None:
        with self.client.get("/") as index_response:
            self.assertEqual(index_response.status_code, 200)
            self.assertIn(b'href="/static/favicon.svg?v=2"', index_response.data)

        with self.client.get("/static/favicon.svg") as favicon_response:
            self.assertEqual(favicon_response.status_code, 200)
            self.assertEqual(favicon_response.mimetype, "image/svg+xml")
            self.assertIn(b'#FFAA00', favicon_response.data)
            self.assertIn(b'#2E2E2E', favicon_response.data)

    def test_monitoring_can_be_paused_and_resumed(self) -> None:
        pause_response = self.client.post(
            "/api/monitoring",
            json={"enabled": False},
        )

        self.assertEqual(pause_response.status_code, 200)
        self.assertFalse(pause_response.get_json()["monitoring_enabled"])
        self.assertFalse(app.get_monitoring_enabled())
        history = self.client.get("/api/history?limit=1").get_json()
        self.assertFalse(history["monitoring_enabled"])

        resume_response = self.client.post(
            "/api/monitoring",
            json={"enabled": True},
        )

        self.assertEqual(resume_response.status_code, 200)
        self.assertTrue(app.get_monitoring_enabled())

    def test_saving_a_new_token_can_run_the_first_check_immediately(self) -> None:
        refreshed_catalog = app.server_catalog_from_payload(SERVER_TYPES_PAYLOAD)
        with (
            patch.object(app, "run_check", return_value="") as run_check,
            patch.object(
                app,
                "load_cached_server_catalog",
                return_value=refreshed_catalog,
            ),
        ):
            response = self.client.put(
                "/api/settings",
                json={
                    "general": {
                        "language": "en",
                        "hcloud_token": "new-api-token",
                        "custom_logo_url": "",
                    },
                    "poll_interval_seconds": 60,
                    "monitored_targets": [
                        {"server_type": "cx23", "location": "fsn1"}
                    ],
                    "ntfy": {
                        "enabled": False,
                        "domain": "https://ntfy.sh",
                        "topic": "",
                        "auth_mode": "none",
                        "username": "",
                        "password": "",
                        "token": "",
                        "dashboard_url": "http://localhost:8080",
                        "message_template": app.DEFAULT_NTFY_MESSAGE_TEMPLATE,
                    },
                    "run_initial_check": True,
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["initial_check_completed"])
        self.assertEqual(response.get_json()["initial_check_error"], "")
        self.assertEqual(response.get_json()["server_catalog"], refreshed_catalog)
        self.assertEqual(response.get_json()["catalog_error"], "")
        run_check.assert_called_once_with()
        self.assertTrue(app.WAKE_EVENT.is_set())
        self.assertTrue(app.INITIAL_CHECK_COMPLETED_EVENT.is_set())

    def test_server_catalog_preserves_hetzner_category(self) -> None:
        catalog = app.server_catalog_from_payload(SERVER_TYPES_PAYLOAD)

        self.assertEqual(catalog[0]["category"], "shared")
        self.assertTrue(catalog[0]["locations"][0]["recommended"])

    def test_server_catalog_includes_monthly_gross_price_and_currency(self) -> None:
        catalog = app.server_catalog_from_payload(
            SERVER_TYPES_PAYLOAD,
            PRICING_PAYLOAD,
        )

        location = catalog[0]["locations"][0]
        self.assertEqual(location["price_monthly_gross"], "3.8080")
        self.assertEqual(location["price_currency"], "EUR")

    def test_pricing_is_loaded_from_central_hetzner_endpoint(self) -> None:
        app.HCLOUD_TOKEN = "test-token"
        pricing_response = Mock(status_code=200)
        pricing_response.raise_for_status.return_value = None
        pricing_response.json.return_value = {"pricing": PRICING_PAYLOAD}

        with patch.object(
            app.requests,
            "get",
            return_value=pricing_response,
        ) as get:
            pricing = app.fetch_pricing()

        self.assertEqual(pricing["currency"], "EUR")
        self.assertEqual(get.call_args.args[0], app.PRICING_URL)
        self.assertNotIn("params", get.call_args.kwargs)

    def test_pricing_cache_avoids_reloading_prices_during_each_check(self) -> None:
        with patch.object(
            app,
            "fetch_pricing",
            return_value=PRICING_PAYLOAD,
        ) as fetch_pricing:
            first = app.load_pricing_for_catalog()
            second = app.load_pricing_for_catalog()

        self.assertEqual(first["currency"], "EUR")
        self.assertEqual(second, first)
        fetch_pricing.assert_called_once_with()

    def test_settings_update_persists_targets_interval_and_ntfy(self) -> None:
        response = self.client.put(
            "/api/settings",
            json={
                "general": {
                    "language": "de",
                    "hcloud_token": "new-api-token",
                    "custom_logo_url": "https://example.com/logo.svg",
                },
                "poll_interval_seconds": 45,
                "monitored_targets": [
                    {"server_type": "cax11", "location": "fsn1"}
                ],
                "ntfy": {
                    "enabled": True,
                    "domain": "https://ntfy.example.com",
                    "topic": "hetznerwatch",
                    "auth_mode": "basic",
                    "username": "monitor",
                    "password": "secret",
                    "token": "",
                    "dashboard_url": "https://watch.example.com/dashboard",
                    "message_template": "{server_type} {location} {checked_at}",
                },
            },
        )

        self.assertEqual(response.status_code, 200)
        general = app.load_general_config()
        self.assertEqual(general["language"], "de")
        self.assertEqual(general["hcloud_token"], "new-api-token")
        self.assertEqual(
            general["custom_logo_url"],
            "https://example.com/logo.svg",
        )
        self.assertNotIn("hcloud_token", response.get_json()["general"])
        self.assertEqual(app.get_poll_interval_seconds(), 45)
        self.assertEqual(
            app.load_monitored_targets(),
            [{"server_type": "cax11", "location": "fsn1"}],
        )
        ntfy = app.load_ntfy_config()
        self.assertTrue(ntfy["enabled"])
        self.assertEqual(ntfy["password"], "secret")
        public_ntfy = response.get_json()["ntfy"]
        self.assertTrue(public_ntfy["password_set"])
        self.assertFalse(public_ntfy["token_set"])
        self.assertNotIn("password", public_ntfy)
        self.assertNotIn("token", public_ntfy)
        self.assertEqual(
            ntfy["dashboard_url"],
            "https://watch.example.com/dashboard",
        )

    def test_unknown_message_placeholder_is_rejected(self) -> None:
        with self.assertRaises(app.SettingsValidationError):
            app.validate_message_template("Status: {unknown}")

    def test_ntfy_message_uses_requested_date_time_format(self) -> None:
        message = app.render_ntfy_message(
            {"message_template": "Geprüft: {checked_at}"},
            "cx23",
            "fsn1",
            "2025-03-21T15:00:00Z",
            True,
        )

        self.assertEqual(message, "Geprüft: 15:00 21.03.2025")

    def test_ntfy_test_uses_json_root_endpoint_and_basic_auth(self) -> None:
        ntfy_response = Mock()
        ntfy_response.status_code = 200
        ntfy_response.raise_for_status.return_value = None

        with patch.object(app.requests, "post", return_value=ntfy_response) as post:
            response = self.client.post(
                "/api/settings/ntfy-test",
                json={
                    "ntfy": {
                        "enabled": False,
                        "domain": "https://ntfy.example.com/base",
                        "topic": "hetznerwatch",
                        "auth_mode": "basic",
                        "username": "monitor",
                        "password": "secret",
                        "token": "",
                        "dashboard_url": "https://watch.example.com/",
                        "message_template": "{server_type} ist {status}.",
                    }
                },
            )

        self.assertEqual(response.status_code, 200)
        _, kwargs = post.call_args
        self.assertEqual(post.call_args.args[0], "https://ntfy.example.com/base/")
        self.assertEqual(kwargs["json"]["topic"], "hetznerwatch")
        self.assertEqual(kwargs["json"]["click"], "https://watch.example.com/")
        self.assertEqual(kwargs["auth"], ("monitor", "secret"))

    def test_ntfy_rejects_invalid_dashboard_link(self) -> None:
        config = app.load_ntfy_config()
        config.update(
            {
                "enabled": True,
                "topic": "hetznerwatch",
                "dashboard_url": "localhost:8080",
            }
        )

        with self.assertRaisesRegex(
            app.SettingsValidationError,
            "dashboard link",
        ):
            app.validate_ntfy_config(config, config)

    def test_custom_logo_rejects_non_http_url(self) -> None:
        stored = app.load_general_config()

        with self.assertRaisesRegex(
            app.SettingsValidationError,
            "custom logo URL",
        ):
            app.validate_general_config(
                {
                    "language": "en",
                    "custom_logo_url": "javascript:alert(1)",
                },
                stored,
            )

    def test_transition_to_available_triggers_ntfy_notification(self) -> None:
        app.save_application_settings(
            {
                "poll_interval_seconds": 60,
                "monitored_targets": [
                    {"server_type": "cx23", "location": "fsn1"}
                ],
                "ntfy": {
                    "enabled": False,
                    "domain": "https://ntfy.sh",
                    "topic": "",
                    "auth_mode": "none",
                    "username": "",
                    "password": "",
                    "token": "",
                    "message_template": app.DEFAULT_NTFY_MESSAGE_TEMPLATE,
                },
            }
        )
        app.save_check(
            "2026-08-01T12:40:21Z",
            "cx23",
            "fsn1",
            False,
            False,
            True,
            200,
            None,
        )

        with (
            patch.object(
                app,
                "fetch_server_types",
                return_value=(SERVER_TYPES_PAYLOAD, 200),
            ),
            patch.object(
                app,
                "cache_server_catalog",
                return_value=app.server_catalog_from_payload(SERVER_TYPES_PAYLOAD),
            ),
            patch.object(app, "load_pricing_for_catalog", return_value={}),
            patch.object(app, "notify_available") as notify,
        ):
            app.run_check()

        notify.assert_called_once()
        call = notify.call_args.args
        self.assertEqual(call[0:2], ("cx23", "fsn1"))
        self.assertRegex(call[2], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        self.assertTrue(call[3])

    def test_history_preserves_iso_8601_timestamp(self) -> None:
        timestamp = "2026-08-01T12:41:21Z"
        app.save_check(
            timestamp,
            "cx23",
            "fsn1",
            True,
            False,
            True,
            200,
            None,
        )

        response = self.client.get("/api/history?limit=1")

        self.assertEqual(response.status_code, 200)
        checks = response.get_json()["targets"][0]["checks"]
        self.assertEqual(checks[0]["checked_at"], timestamp)

    def test_server_type_catalog_follows_hetzner_pagination(self) -> None:
        app.HCLOUD_TOKEN = "test-token"
        first_response = Mock(status_code=200)
        first_response.raise_for_status.return_value = None
        first_response.json.return_value = {
            "server_types": [{"name": "cx23"}],
            "meta": {"pagination": {"next_page": 2}},
        }
        second_response = Mock(status_code=200)
        second_response.raise_for_status.return_value = None
        second_response.json.return_value = {
            "server_types": [{"name": "cax11"}],
            "meta": {"pagination": {"next_page": None}},
        }

        with patch.object(
            app.requests,
            "get",
            side_effect=[first_response, second_response],
        ) as get:
            payload, status = app.fetch_server_types()

        self.assertEqual(status, 200)
        self.assertEqual(
            [item["name"] for item in payload["server_types"]],
            ["cx23", "cax11"],
        )
        self.assertEqual(get.call_args_list[0].kwargs["params"]["page"], 1)
        self.assertEqual(get.call_args_list[1].kwargs["params"]["page"], 2)

    def test_hourly_rollups_store_every_catalog_location(self) -> None:
        payload = copy.deepcopy(SERVER_TYPES_PAYLOAD)
        payload["server_types"][0]["locations"].append(
            {
                "location": {"name": "nbg1"},
                "available": False,
                "recommended": False,
            }
        )
        catalog = app.server_catalog_from_payload(payload)

        app.save_availability_rollups("2026-08-01T12:10:00Z", catalog)
        app.save_availability_rollups("2026-08-01T12:59:59Z", catalog)

        with sqlite3.connect(app.DATABASE_PATH) as connection:
            rows = connection.execute(
                """
                SELECT location, total_checks, available_checks
                FROM availability_rollups
                ORDER BY location
                """
            ).fetchall()
        self.assertEqual(rows, [("fsn1", 2, 2), ("nbg1", 2, 0)])

    def test_retention_deletes_checks_and_rollups_older_than_120_days(self) -> None:
        now = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
        old_timestamp = app._iso_timestamp(now - timedelta(days=121))
        boundary_timestamp = app._iso_timestamp(now - timedelta(days=120))
        recent_timestamp = app._iso_timestamp(now - timedelta(days=1))
        for timestamp in (old_timestamp, boundary_timestamp, recent_timestamp):
            app.save_check(
                timestamp,
                "cx23",
                "fsn1",
                True,
                True,
                True,
                200,
                None,
            )

        catalog = app.server_catalog_from_payload(SERVER_TYPES_PAYLOAD)
        for timestamp in (old_timestamp, boundary_timestamp, recent_timestamp):
            app.save_availability_rollups(timestamp, catalog)

        result = app.cleanup_old_data(force=True, now=now)

        self.assertTrue(result["ran"])
        self.assertEqual(result["checks_deleted"], 1)
        self.assertEqual(result["rollups_deleted"], 1)
        with sqlite3.connect(app.DATABASE_PATH) as connection:
            check_timestamps = connection.execute(
                "SELECT checked_at FROM availability_checks ORDER BY checked_at"
            ).fetchall()
            rollup_timestamps = connection.execute(
                "SELECT bucket_start FROM availability_rollups ORDER BY bucket_start"
            ).fetchall()
        self.assertEqual(
            check_timestamps,
            [(boundary_timestamp,), (recent_timestamp,)],
        )
        self.assertEqual(
            rollup_timestamps,
            [(boundary_timestamp,), (recent_timestamp,)],
        )

    def test_long_term_api_groups_catalog_and_calculates_percentage(self) -> None:
        available_catalog = app.cache_server_catalog(
            SERVER_TYPES_PAYLOAD,
            PRICING_PAYLOAD,
        )
        unavailable_catalog = copy.deepcopy(available_catalog)
        unavailable_catalog[0]["locations"][0]["available"] = False
        checked_at = app.utc_timestamp()
        app.save_availability_rollups(checked_at, available_catalog)
        app.save_availability_rollups(checked_at, unavailable_catalog)

        response = self.client.get("/api/long-term?range=24h")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["summary"]["targets"], 1)
        self.assertEqual(payload["summary"]["targets_with_data"], 1)
        self.assertEqual(payload["summary"]["availability_percent"], 50.0)
        self.assertEqual(payload["categories"][0]["category"], "shared")
        location = payload["categories"][0]["server_types"][0]["locations"][0]
        self.assertEqual(location["availability_percent"], 50.0)
        self.assertEqual(location["price_monthly_gross"], "3.8080")
        self.assertEqual(location["price_currency"], "EUR")
        self.assertEqual(len(location["series"]), 24)

    def test_long_term_api_rejects_unknown_range(self) -> None:
        response = self.client.get("/api/long-term?range=forever")

        self.assertEqual(response.status_code, 400)
        self.assertIn("range", response.get_json()["error"])

    def test_api_errors_follow_saved_german_language(self) -> None:
        with sqlite3.connect(app.DATABASE_PATH) as connection:
            app._save_setting(connection, "language", "de")

        response = self.client.get("/api/long-term?range=forever")

        self.assertEqual(response.status_code, 400)
        self.assertIn("Zeitraum", response.get_json()["error"])


if __name__ == "__main__":
    unittest.main()
