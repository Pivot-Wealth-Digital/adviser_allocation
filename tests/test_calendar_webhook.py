"""Tests for the Google Calendar webhook endpoint."""

import os
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("GOOGLE_CALENDAR_ID", "test-cal@group.calendar.google.com")

from adviser_allocation.main import create_app


class TestCalendarWebhook(unittest.TestCase):
    """Tests for POST /webhooks/calendar."""

    def setUp(self):
        self.app = create_app({"TESTING": True})
        self.client = self.app.test_client()
        # Reset debounce state between tests
        import adviser_allocation.api.webhooks as wh

        wh._last_calendar_sync_utc = None

    def _post_webhook(self, headers=None):
        default_headers = {
            "X-Goog-Channel-ID": "test-channel-id",
            "X-Goog-Resource-State": "exists",
            "X-Goog-Channel-Token": "",
            "X-Goog-Resource-ID": "test-resource-id",
        }
        if headers:
            default_headers.update(headers)
        return self.client.post("/webhooks/calendar", headers=default_headers)

    @patch("adviser_allocation.api.webhooks._get_calendar_webhook_token")
    def test_sync_state_acknowledged(self, mock_token):
        mock_token.return_value = None
        response = self._post_webhook(
            headers={"X-Goog-Resource-State": "sync"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("sync acknowledged", response.get_json()["message"])

    @patch("adviser_allocation.api.webhooks._get_calendar_webhook_token")
    def test_invalid_token_returns_403(self, mock_token):
        mock_token.return_value = "correct-token"
        response = self._post_webhook(
            headers={"X-Goog-Channel-Token": "wrong-token"},
        )
        self.assertEqual(response.status_code, 403)

    @patch("adviser_allocation.api.webhooks._get_calendar_webhook_token")
    def test_no_token_configured_allows_request(self, mock_token):
        mock_token.return_value = None
        with (
            patch(
                "adviser_allocation.services.calendar_sync_service.sync_calendar_closures"
            ) as mock_sync,
            patch(
                "adviser_allocation.services.calendar_sync_service.get_calendar_sources"
            ) as mock_sources,
            patch("adviser_allocation.utils.common.get_cloudsql_db") as mock_db,
        ):
            mock_sources.return_value = [("test-cal@group.calendar.google.com", None)]
            mock_sync.return_value = {
                "upserted": 0,
                "deleted": 0,
                "errors": 0,
                "skipped": 0,
            }
            mock_db.return_value = MagicMock()
            response = self._post_webhook()
        self.assertEqual(response.status_code, 200)

    @patch("adviser_allocation.api.webhooks._get_calendar_webhook_token")
    @patch("adviser_allocation.utils.common.get_cloudsql_db")
    @patch(
        "adviser_allocation.services.calendar_sync_service.get_calendar_sources",
    )
    @patch(
        "adviser_allocation.services.calendar_sync_service.sync_calendar_closures",
    )
    def test_exists_state_triggers_sync(
        self, mock_sync, mock_sources, mock_db, mock_token
    ):
        mock_token.return_value = None
        mock_db.return_value = MagicMock()
        mock_sources.return_value = [("test-cal@group.calendar.google.com", None)]
        mock_sync.return_value = {
            "upserted": 3,
            "deleted": 1,
            "errors": 0,
            "skipped": 0,
        }

        response = self._post_webhook()

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["upserted"], 3)
        mock_sync.assert_called_once()

    @patch("adviser_allocation.api.webhooks._get_calendar_webhook_token")
    @patch("adviser_allocation.utils.common.get_cloudsql_db")
    @patch(
        "adviser_allocation.services.calendar_sync_service.get_calendar_sources",
    )
    @patch(
        "adviser_allocation.services.calendar_sync_service.sync_calendar_closures",
    )
    def test_debounce_prevents_rapid_resyncs(
        self, mock_sync, mock_sources, mock_db, mock_token
    ):
        mock_token.return_value = None
        mock_db.return_value = MagicMock()
        mock_sources.return_value = [("test-cal@group.calendar.google.com", None)]
        mock_sync.return_value = {
            "upserted": 0,
            "deleted": 0,
            "errors": 0,
            "skipped": 0,
        }

        # First call triggers sync
        response1 = self._post_webhook()
        self.assertEqual(response1.status_code, 200)
        self.assertEqual(mock_sync.call_count, 1)

        # Second call within debounce window is skipped
        response2 = self._post_webhook()
        self.assertEqual(response2.status_code, 200)
        self.assertIn("debounced", response2.get_json()["message"])
        self.assertEqual(mock_sync.call_count, 1)  # Still 1

    @patch("adviser_allocation.api.webhooks._get_calendar_webhook_token")
    @patch("adviser_allocation.utils.common.get_cloudsql_db")
    @patch(
        "adviser_allocation.services.calendar_sync_service.get_calendar_sources",
    )
    @patch(
        "adviser_allocation.services.calendar_sync_service.sync_calendar_closures",
        side_effect=RuntimeError("DB connection failed"),
    )
    def test_sync_error_returns_200(
        self, mock_sync, mock_sources, mock_db, mock_token
    ):
        """Google requires 200 even on errors to avoid retries."""
        mock_token.return_value = None
        mock_db.return_value = MagicMock()
        mock_sources.return_value = [("test-cal@group.calendar.google.com", None)]

        response = self._post_webhook()

        self.assertEqual(response.status_code, 200)
        self.assertIn("sync error", response.get_json()["message"])


if __name__ == "__main__":
    unittest.main()
