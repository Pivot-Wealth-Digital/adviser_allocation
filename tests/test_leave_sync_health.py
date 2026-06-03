"""Tests for leave-sync health detection and the corrected tracking window.

These lock in the prevention behaviour added after the silent leave-drain bug:
  * `_should_track_leave` — the keep decision (approved + not yet ended), so the
    `start_date > today` boundary regression cannot return.
  * `leave_sync_result_is_healthy` — the zero-result signal that drives alerting.
  * `_alert_leave_sync_problem` — fires a Google Chat alert and never raises.
  * `count_active_future_leave` — the post-sync count feeding the health check.
"""

from datetime import date
from unittest.mock import MagicMock, patch

from adviser_allocation.db.repository import AdviserAllocationDB
from adviser_allocation.main import (
    _alert_leave_sync_problem,
    _should_track_leave,
    leave_sync_result_is_healthy,
)

TODAY = date(2026, 6, 2)


class TestShouldTrackLeave:
    def test_approved_future_is_kept(self):
        assert _should_track_leave("Approved", date(2026, 7, 28), TODAY) is True

    def test_approved_ongoing_is_kept(self):
        # Started before today, ends after today — the case that used to vanish.
        assert _should_track_leave("Approved", date(2026, 6, 10), TODAY) is True

    def test_approved_ending_today_is_kept(self):
        assert _should_track_leave("Approved", TODAY, TODAY) is True

    def test_already_ended_is_excluded(self):
        assert _should_track_leave("Approved", date(2026, 5, 30), TODAY) is False

    def test_non_approved_is_excluded(self):
        assert _should_track_leave("Pending", date(2026, 7, 28), TODAY) is False

    def test_status_is_case_insensitive(self):
        assert _should_track_leave("APPROVED", date(2026, 7, 28), TODAY) is True

    def test_missing_status_is_excluded(self):
        assert _should_track_leave(None, date(2026, 7, 28), TODAY) is False


class TestLeaveSyncResultIsHealthy:
    def test_all_positive_is_healthy(self):
        assert leave_sync_result_is_healthy(120, 30, 30) is True

    def test_zero_fetched_is_unhealthy(self):
        assert leave_sync_result_is_healthy(0, 0, 0) is False

    def test_zero_kept_is_unhealthy(self):
        assert leave_sync_result_is_healthy(120, 0, 0) is False

    def test_zero_active_future_in_db_is_unhealthy(self):
        # Fetched and kept this run, but the table ended up empty — the symptom we saw.
        assert leave_sync_result_is_healthy(120, 5, 0) is False


class TestAlertLeaveSyncProblem:
    @patch("adviser_allocation.api.webhooks.send_chat_alert")
    def test_suppressed_by_default(self, mock_send, monkeypatch):
        # Default (flag unset) — leave-sync alerts must NOT post to Chat.
        monkeypatch.delenv("LEAVE_SYNC_ALERTS_ENABLED", raising=False)
        _alert_leave_sync_problem("Leave sync failed", ["Error: boom"])
        mock_send.assert_not_called()

    @patch("adviser_allocation.api.webhooks.send_chat_alert")
    def test_sends_alert_when_enabled(self, mock_send, monkeypatch):
        monkeypatch.setenv("LEAVE_SYNC_ALERTS_ENABLED", "true")
        _alert_leave_sync_problem("Leave sync failed", ["Error: boom"])
        mock_send.assert_called_once()
        payload = mock_send.call_args.args[0]
        # build_chat_card_payload wraps the summary in the card title.
        assert "Leave sync failed" in payload["cards"][0]["header"]["title"]

    @patch("adviser_allocation.api.webhooks.send_chat_alert", side_effect=RuntimeError("down"))
    def test_swallows_alert_errors(self, _mock_send, monkeypatch):
        # Alerting must never break the sync, even when enabled.
        monkeypatch.setenv("LEAVE_SYNC_ALERTS_ENABLED", "true")
        _alert_leave_sync_problem("Leave sync failed", ["Error: boom"])


class TestCountActiveFutureLeave:
    def test_returns_scalar_count(self):
        result = MagicMock()
        result.scalar.return_value = 7
        conn = MagicMock()
        conn.execute.return_value = result
        engine = MagicMock()
        engine.connect.return_value.__enter__.return_value = conn
        engine.connect.return_value.__exit__.return_value = False

        db = AdviserAllocationDB(engine)
        assert db.count_active_future_leave(TODAY) == 7

    def test_none_count_coerced_to_zero(self):
        result = MagicMock()
        result.scalar.return_value = None
        conn = MagicMock()
        conn.execute.return_value = result
        engine = MagicMock()
        engine.connect.return_value.__enter__.return_value = conn
        engine.connect.return_value.__exit__.return_value = False

        db = AdviserAllocationDB(engine)
        assert db.count_active_future_leave(TODAY) == 0
