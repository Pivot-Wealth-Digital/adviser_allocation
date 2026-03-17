"""Tests for Google Calendar watch (push notification) service."""

import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from adviser_allocation.db.models import CalendarWatchChannel
from adviser_allocation.services.calendar_watch_service import (
    RENEWAL_BUFFER_HOURS,
    _sanitize_doc_id,
    register_calendar_watch,
    renew_expiring_watches,
    stop_calendar_watch,
)


class TestSanitizeDocId(unittest.TestCase):
    """Tests for _sanitize_doc_id()."""

    def test_returns_16_char_hex(self):
        result = _sanitize_doc_id("test@group.calendar.google.com")
        self.assertEqual(len(result), 16)
        self.assertTrue(all(c in "0123456789abcdef" for c in result))

    def test_deterministic(self):
        cal_id = "abc@group.calendar.google.com"
        self.assertEqual(_sanitize_doc_id(cal_id), _sanitize_doc_id(cal_id))

    def test_different_ids_produce_different_hashes(self):
        self.assertNotEqual(
            _sanitize_doc_id("cal-a@group.calendar.google.com"),
            _sanitize_doc_id("cal-b@group.calendar.google.com"),
        )


class TestRegisterCalendarWatch(unittest.TestCase):
    """Tests for register_calendar_watch()."""

    @patch("adviser_allocation.services.calendar_watch_service._get_db")
    @patch("adviser_allocation.services.calendar_watch_service._get_calendar_service_rw")
    def test_registers_and_stores_in_cloudsql(self, mock_svc, mock_db):
        mock_service = MagicMock()
        mock_svc.return_value = mock_service
        mock_service.events().watch().execute.return_value = {
            "expiration": "1741234567000",
            "resourceId": "resource-abc",
        }

        mock_db_instance = MagicMock()
        mock_db.return_value = mock_db_instance

        result = register_calendar_watch(
            calendar_id="test-cal@group.calendar.google.com",
            webhook_url="https://example.com/webhooks/calendar",
            channel_token="secret-token",
        )

        self.assertEqual(result["calendar_id"], "test-cal@group.calendar.google.com")
        self.assertEqual(result["resource_id"], "resource-abc")
        self.assertEqual(result["expiration_ms"], 1741234567000)
        self.assertIn("channel_id", result)

        mock_db_instance.upsert_calendar_watch.assert_called_once()
        watch_arg = mock_db_instance.upsert_calendar_watch.call_args[0][0]
        self.assertIsInstance(watch_arg, CalendarWatchChannel)
        self.assertEqual(watch_arg.calendar_id, "test-cal@group.calendar.google.com")

    @patch("adviser_allocation.services.calendar_watch_service._get_db")
    @patch("adviser_allocation.services.calendar_watch_service._get_calendar_service_rw")
    def test_watch_body_includes_token(self, mock_svc, mock_db):
        mock_service = MagicMock()
        mock_svc.return_value = mock_service
        mock_service.events().watch().execute.return_value = {
            "expiration": "9999999999999",
            "resourceId": "res-1",
        }
        mock_db.return_value = MagicMock()

        register_calendar_watch("cal-id", "https://url.com/hook", "my-token")

        mock_service.events().watch.assert_called()


class TestStopCalendarWatch(unittest.TestCase):
    """Tests for stop_calendar_watch()."""

    @patch("adviser_allocation.services.calendar_watch_service._get_calendar_service_rw")
    def test_calls_channels_stop(self, mock_svc):
        mock_service = MagicMock()
        mock_svc.return_value = mock_service

        stop_calendar_watch("channel-123", "resource-456")

        mock_service.channels().stop.assert_called_once_with(
            body={"id": "channel-123", "resourceId": "resource-456"},
        )


class TestRenewExpiringWatches(unittest.TestCase):
    """Tests for renew_expiring_watches()."""

    @patch("adviser_allocation.services.calendar_watch_service._build_webhook_url")
    @patch("adviser_allocation.services.calendar_watch_service._load_channel_token")
    def test_no_token_returns_error(self, mock_token, mock_url):
        mock_token.return_value = None
        mock_url.return_value = "https://example.com/webhooks/calendar"
        result = renew_expiring_watches([("cal-id", None)])
        self.assertEqual(result["errors"], 1)

    @patch("adviser_allocation.services.calendar_watch_service.register_calendar_watch")
    @patch("adviser_allocation.services.calendar_watch_service._build_webhook_url")
    @patch("adviser_allocation.services.calendar_watch_service._load_channel_token")
    @patch("adviser_allocation.services.calendar_watch_service._get_db")
    def test_registers_new_calendar(self, mock_db, mock_token, mock_url, mock_register):
        mock_token.return_value = "test-token"
        mock_url.return_value = "https://example.com/webhooks/calendar"

        mock_db_instance = MagicMock()
        mock_db.return_value = mock_db_instance
        mock_db_instance.get_calendar_watch.return_value = None

        mock_register.return_value = {"channel_id": "new-ch"}

        result = renew_expiring_watches([("new-cal@google.com", None)])
        self.assertEqual(result["registered"], 1)
        mock_register.assert_called_once()

    @patch("adviser_allocation.services.calendar_watch_service._stop_watch_safe")
    @patch("adviser_allocation.services.calendar_watch_service.register_calendar_watch")
    @patch("adviser_allocation.services.calendar_watch_service._build_webhook_url")
    @patch("adviser_allocation.services.calendar_watch_service._load_channel_token")
    @patch("adviser_allocation.services.calendar_watch_service._get_db")
    def test_renews_expiring_channel(self, mock_db, mock_token, mock_url, mock_register, mock_stop):
        mock_token.return_value = "test-token"
        mock_url.return_value = "https://example.com/webhooks/calendar"

        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        expiring_ms = now_ms + (1 * 3600 * 1000)  # 1 hour — within 48h buffer

        mock_db_instance = MagicMock()
        mock_db.return_value = mock_db_instance
        mock_db_instance.get_calendar_watch.return_value = CalendarWatchChannel(
            doc_id="abc123",
            calendar_id="expiring-cal@google.com",
            channel_id="old-ch",
            resource_id="old-res",
            expiration_ms=expiring_ms,
            webhook_url="https://example.com/webhooks/calendar",
        )

        mock_register.return_value = {"channel_id": "new-ch"}

        result = renew_expiring_watches([("expiring-cal@google.com", None)])
        self.assertEqual(result["renewed"], 1)
        mock_stop.assert_called_once_with("old-ch", "old-res")

    @patch("adviser_allocation.services.calendar_watch_service._build_webhook_url")
    @patch("adviser_allocation.services.calendar_watch_service._load_channel_token")
    @patch("adviser_allocation.services.calendar_watch_service._get_db")
    def test_skips_non_expiring_channel(self, mock_db, mock_token, mock_url):
        mock_token.return_value = "test-token"
        mock_url.return_value = "https://example.com/webhooks/calendar"

        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        far_future_ms = now_ms + (7 * 24 * 3600 * 1000)

        mock_db_instance = MagicMock()
        mock_db.return_value = mock_db_instance
        mock_db_instance.get_calendar_watch.return_value = CalendarWatchChannel(
            doc_id="abc123",
            calendar_id="valid-cal@google.com",
            channel_id="valid-ch",
            resource_id="valid-res",
            expiration_ms=far_future_ms,
            webhook_url="https://example.com/webhooks/calendar",
        )

        result = renew_expiring_watches([("valid-cal@google.com", None)])
        self.assertEqual(result["skipped"], 1)


if __name__ == "__main__":
    unittest.main()
