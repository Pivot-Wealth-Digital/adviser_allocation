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
from sqlalchemy.exc import IntegrityError

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
        The employee refresh is stubbed out so ``requested_indexes`` records only the
        leave endpoint's pagination.
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
        # Every fixture employee is known, so pagination is what these tests measure.
        db.get_known_employee_ids.return_value = {
            leave["employee_id"] for items in pages_by_index.values() for leave in items
        }

        with (
            patch.object(main, "get_access_token", return_value="tok"),
            patch.object(main, "get_org_id", return_value="org-1"),
            patch.object(main, "get_cloudsql_db", return_value=db),
            patch.object(main, "sydney_today", return_value=TODAY),
            patch.object(main, "get_employees", return_value=([], 200, {})),
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


class TestUnknownEmployeeLeave:
    """Leave for an employee missing from aa_employees must never abort the sync.

    ``aa_leave_requests.employee_id`` is a foreign key. A new starter's approved leave
    reaches Employment Hero days before the (Monday-only) employee sync creates their
    row, so the insert raised mid-loop and abandoned the remaining pages *and* the
    stale-leave prune with it — two consecutive runs wrote a partial table and died
    (2026-09-03 and 2026-09-06, employee 426deae7…, a new starter).
    """

    def _run_sync(self, pages_by_index, known_employee_ids, total_pages=1, upsert_error=None):
        """Run get_leave_requests with an explicit set of known employee ids."""

        def fake_get(url, headers=None, params=None, timeout=None):
            return _make_eh_response(pages_by_index.get(params["page_index"], []), total_pages)

        db = MagicMock()
        db.delete_stale_future_leave.return_value = 0
        db.count_active_future_leave.return_value = 99
        db.get_known_employee_ids.return_value = set(known_employee_ids)
        if upsert_error is not None:
            db.upsert_leave_request_dict.side_effect = upsert_error

        with (
            patch.object(main, "get_access_token", return_value="tok"),
            patch.object(main, "get_org_id", return_value="org-1"),
            patch.object(main, "get_cloudsql_db", return_value=db),
            patch.object(main, "sydney_today", return_value=TODAY),
            patch.object(main, "get_employees", return_value=([], 200, {})) as mock_employees,
            patch.object(main.requests, "get", side_effect=fake_get),
            patch.object(main, "_alert_leave_sync_problem") as mock_alert,
        ):
            kept, _status, _headers = get_leave_requests()

        upserted_ids = [
            call.args[0]["leave_request_id"] for call in db.upsert_leave_request_dict.call_args_list
        ]
        return kept, upserted_ids, db, mock_alert, mock_employees

    def test_employees_are_refreshed_before_leave_is_read(self):
        # Closes the window entirely: a new starter synced now can own leave now.
        _kept, _upserted, db, _alert, mock_employees = self._run_sync(
            {1: [_leave("l1", "emp-1")]}, known_employee_ids={"emp-1"}
        )
        mock_employees.assert_called_once()
        db.get_known_employee_ids.assert_called_once()

    def test_unknown_employee_leave_is_skipped_not_inserted(self):
        # The FK violation never reaches the database.
        kept, upserted_ids, _db, _alert, _emp = self._run_sync(
            {1: [_leave("known", "emp-1"), _leave("orphan", "emp-new")]},
            known_employee_ids={"emp-1"},
        )
        assert upserted_ids == ["known"]
        assert [lr["leave_request_id"] for lr in kept] == ["known"]

    def test_sync_continues_to_later_pages_after_an_unknown_employee(self):
        # The actual regression: the abort cost every page after the bad record.
        kept, upserted_ids, _db, _alert, _emp = self._run_sync(
            {
                1: [_leave("orphan", "emp-new"), _leave("p1", "emp-1")],
                2: [_leave("p2", "emp-2")],
                3: [_leave("p3", "emp-3")],
            },
            known_employee_ids={"emp-1", "emp-2", "emp-3"},
            total_pages=3,
        )
        assert upserted_ids == ["p1", "p2", "p3"]
        assert len(kept) == 3

    def test_skipped_leave_raises_an_alert_naming_the_employee(self):
        # Skipped leave is missing from capacity, so it must be visible, not silent.
        _kept, _upserted, _db, mock_alert, _emp = self._run_sync(
            {1: [_leave("known", "emp-1"), _leave("orphan", "emp-new")]},
            known_employee_ids={"emp-1"},
        )
        summaries = [call.args[0] for call in mock_alert.call_args_list]
        assert "Leave sync skipped records" in summaries
        details = " ".join(mock_alert.call_args_list[0].args[1])
        assert "emp-new" in details

    def test_clean_run_raises_no_skip_alert(self):
        _kept, _upserted, _db, mock_alert, _emp = self._run_sync(
            {1: [_leave("l1", "emp-1")]}, known_employee_ids={"emp-1"}
        )
        mock_alert.assert_not_called()

    def test_skipped_leave_is_absent_from_the_prune_allowlist(self):
        # Skipped ids must not enter synced_ids; the FK means no stored row exists
        # for them, so their absence can never delete anything real.
        _kept, _upserted, db, _alert, _emp = self._run_sync(
            {1: [_leave("known", "emp-1"), _leave("orphan", "emp-new")]},
            known_employee_ids={"emp-1"},
        )
        synced_ids = db.delete_stale_future_leave.call_args.args[0]
        assert synced_ids == ["known"]

    def test_employee_refresh_failure_still_syncs_leave(self):
        # EH being down for /employees must not cost us the whole leave sync.
        db = MagicMock()
        db.delete_stale_future_leave.return_value = 0
        db.count_active_future_leave.return_value = 99
        db.get_known_employee_ids.return_value = {"emp-1"}

        def fake_get(url, headers=None, params=None, timeout=None):
            return _make_eh_response([_leave("l1", "emp-1")], 1)

        with (
            patch.object(main, "get_access_token", return_value="tok"),
            patch.object(main, "get_org_id", return_value="org-1"),
            patch.object(main, "get_cloudsql_db", return_value=db),
            patch.object(main, "sydney_today", return_value=TODAY),
            patch.object(main, "get_employees", side_effect=RuntimeError("EH 503")),
            patch.object(main.requests, "get", side_effect=fake_get),
        ):
            kept, _status, _headers = get_leave_requests()

        assert [lr["leave_request_id"] for lr in kept] == ["l1"]

    def test_database_rejection_skips_only_that_record(self):
        # Last-resort net for any row-level constraint the pre-check cannot foresee.
        def reject_orphan(item):
            if item["leave_request_id"] == "bad":
                raise IntegrityError("INSERT", {}, Exception("constraint"))

        kept, upserted_ids, _db, mock_alert, _emp = self._run_sync(
            {1: [_leave("bad", "emp-1"), _leave("good", "emp-1")]},
            known_employee_ids={"emp-1"},
            upsert_error=reject_orphan,
        )
        assert upserted_ids == ["bad", "good"]  # both attempted
        assert [lr["leave_request_id"] for lr in kept] == ["good"]  # only one persisted
        mock_alert.assert_called_once()


class TestGetKnownEmployeeIds:
    def test_returns_ids_as_a_set(self):
        conn = MagicMock()
        conn.execute.return_value.fetchall.return_value = [("emp-1",), ("emp-2",)]
        engine = MagicMock()
        engine.connect.return_value.__enter__.return_value = conn
        engine.connect.return_value.__exit__.return_value = False

        assert AdviserAllocationDB(engine).get_known_employee_ids() == {"emp-1", "emp-2"}

    def test_drops_null_ids(self):
        conn = MagicMock()
        conn.execute.return_value.fetchall.return_value = [("emp-1",), (None,)]
        engine = MagicMock()
        engine.connect.return_value.__enter__.return_value = conn
        engine.connect.return_value.__exit__.return_value = False

        assert AdviserAllocationDB(engine).get_known_employee_ids() == {"emp-1"}
