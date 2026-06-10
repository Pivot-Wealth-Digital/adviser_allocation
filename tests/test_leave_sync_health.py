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

from adviser_allocation import main
from adviser_allocation.db.repository import AdviserAllocationDB
from adviser_allocation.main import (
    _alert_leave_sync_problem,
    _should_track_leave,
    get_leave_requests,
    leave_sync_result_is_healthy,
)

TODAY = date(2026, 6, 2)


def _leave(leave_id, employee_id, end_date="2026-07-28", status="Approved"):
    return {
        "id": leave_id,
        "employee_id": employee_id,
        "start_date": "2026-07-15",
        "end_date": end_date,
        "status": status,
    }


def _make_eh_response(items, total_pages):
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {
        "data": {"items": items, "total_pages": total_pages, "total_count": None}
    }
    return resp


class TestLeaveSyncPagination:
    """Lock in that the EH leave fetch uses 1-based pagination and dedupes.

    Employment Hero's ``page_index`` is 1-based: requesting ``page_index=0`` returns
    the same rows as page 1. A 0-based loop therefore duplicated the first page and
    never fetched the final page (``page_index == total_pages``), silently dropping a
    whole page of leave — which is how an adviser's approved leave went missing.
    """

    def _run_sync(self, pages_by_index, total_pages):
        """Run get_leave_requests against a fake EH that maps page_index -> items.

        ``page_index=0`` deliberately returns page 1's rows (EH's real clamping
        behaviour), so a regression to a 0-based loop drops the last page and fails.
        """
        requested_indexes = []

        def fake_get(url, headers=None, params=None, timeout=None):
            idx = params["page_index"]
            requested_indexes.append(idx)
            real_page = 1 if idx == 0 else idx
            return _make_eh_response(pages_by_index.get(real_page, []), total_pages)

        db = MagicMock()
        db.delete_stale_future_leave.return_value = 0
        db.count_active_future_leave.return_value = 99

        with (
            patch.object(main, "get_access_token", return_value="tok"),
            patch.object(main, "get_org_id", return_value="org-1"),
            patch.object(main, "get_cloudsql_db", return_value=db),
            patch.object(main, "sydney_today", return_value=TODAY),
            patch.object(main.requests, "get", side_effect=fake_get),
        ):
            leave_requests, _status, _headers = get_leave_requests()

        upserted_ids = [
            call.args[0]["leave_request_id"] for call in db.upsert_leave_request_dict.call_args_list
        ]
        return leave_requests, requested_indexes, upserted_ids

    def test_fetches_final_page_including_its_leave(self):
        # Leave that lives only on the last page must not be dropped.
        pages = {
            1: [_leave("p1", "emp-1")],
            2: [_leave("p2", "emp-2")],
            3: [_leave("p3", "emp-3")],
            4: [_leave("p4", "emp-4")],
            5: [_leave("ian-leave", "emp-ian")],
        }
        _kept, requested_indexes, upserted_ids = self._run_sync(pages, total_pages=5)

        # Iterates page_index 1..5 — never 0, and includes the final page.
        assert requested_indexes == [1, 2, 3, 4, 5]
        assert "ian-leave" in upserted_ids

    def test_dedupes_leave_seen_on_multiple_pages(self):
        # A leave id appearing on more than one page is upserted once.
        pages = {
            1: [_leave("dup", "emp-1"), _leave("p1", "emp-1")],
            2: [_leave("dup", "emp-1"), _leave("p2", "emp-2")],
        }
        kept, _requested_indexes, upserted_ids = self._run_sync(pages, total_pages=2)

        assert upserted_ids.count("dup") == 1
        assert sorted(lr["leave_request_id"] for lr in kept) == ["dup", "p1", "p2"]


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
    def test_sends_by_default(self, mock_send, monkeypatch):
        # Default (flag unset) — leave-sync alerts post so genuine failures are noticed.
        monkeypatch.delenv("LEAVE_SYNC_ALERTS_ENABLED", raising=False)
        _alert_leave_sync_problem("Leave sync failed", ["Error: boom"])
        mock_send.assert_called_once()
        payload = mock_send.call_args.args[0]
        # build_chat_card_payload wraps the summary in the card title.
        assert "Leave sync failed" in payload["cards"][0]["header"]["title"]

    @patch("adviser_allocation.api.webhooks.send_chat_alert")
    def test_suppressed_when_disabled(self, mock_send, monkeypatch):
        # Explicit opt-out (e.g. while a known issue is worked) suppresses the Chat post.
        monkeypatch.setenv("LEAVE_SYNC_ALERTS_ENABLED", "false")
        _alert_leave_sync_problem("Leave sync failed", ["Error: boom"])
        mock_send.assert_not_called()

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
