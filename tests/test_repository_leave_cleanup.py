"""Tests for the stale-leave cleanup safety guards in delete_stale_future_leave.

The delete query itself is Postgres-specific (``!= ALL(:array)``), so these tests
drive the Python guard logic with a mocked engine/connection rather than a live DB.
The guards exist because a single incomplete Employment Hero fetch previously wiped
all active/future leave (see fix/eh-leave-sync-stale-delete).
"""

from datetime import date
from unittest.mock import MagicMock, patch

from adviser_allocation.db.repository import STALE_DELETE_MAX_RATIO, AdviserAllocationDB

CUTOFF = date(2026, 6, 2)


def _db_with_counts(tracked_count, stale_count, deleted_count=0):
    """Build an AdviserAllocationDB whose mocked engine returns the given counts.

    execute() is called in order: COUNT(tracked), COUNT(stale), then DELETE.
    """
    tracked_result = MagicMock()
    tracked_result.scalar.return_value = tracked_count
    stale_result = MagicMock()
    stale_result.scalar.return_value = stale_count
    delete_result = MagicMock()
    delete_result.rowcount = deleted_count

    conn = MagicMock()
    conn.execute.side_effect = [tracked_result, stale_result, delete_result]

    engine = MagicMock()
    engine.begin.return_value.__enter__.return_value = conn
    engine.begin.return_value.__exit__.return_value = False
    return AdviserAllocationDB(engine), engine, conn


def test_empty_synced_ids_deletes_nothing_and_skips_db():
    """A zero-result fetch must never touch the DB — it's an upstream problem."""
    db = AdviserAllocationDB(MagicMock())
    deleted = db.delete_stale_future_leave([], CUTOFF)
    assert deleted == 0
    db.engine.begin.assert_not_called()


def test_no_stale_records_returns_zero():
    db, engine, conn = _db_with_counts(tracked_count=10, stale_count=0)
    deleted = db.delete_stale_future_leave(["a", "b", "c"], CUTOFF)
    assert deleted == 0
    # tracked + stale counted, but no DELETE issued (only 2 execute calls).
    assert conn.execute.call_count == 2


def test_normal_cleanup_below_threshold_deletes():
    db, engine, conn = _db_with_counts(tracked_count=10, stale_count=2, deleted_count=2)
    deleted = db.delete_stale_future_leave(["x"], CUTOFF)
    assert deleted == 2
    # tracked + stale counts, then the DELETE.
    assert conn.execute.call_count == 3


@patch("adviser_allocation.db.repository._alert_stale_delete_guard")
def test_guard_trips_above_threshold_skips_and_alerts(mock_alert):
    """The 2026-05-20 'Deleted 24' scenario: most of the window would vanish."""
    db, engine, conn = _db_with_counts(tracked_count=24, stale_count=20)
    deleted = db.delete_stale_future_leave(["x"], CUTOFF)
    assert deleted == 0
    mock_alert.assert_called_once_with(20, 24)
    # No DELETE executed — only the two COUNT queries ran.
    assert conn.execute.call_count == 2


@patch("adviser_allocation.db.repository._alert_stale_delete_guard")
def test_exactly_at_threshold_still_deletes(mock_alert):
    """Guard trips only when strictly above the ratio, not at it."""
    half = int(10 * STALE_DELETE_MAX_RATIO)
    db, engine, conn = _db_with_counts(tracked_count=10, stale_count=half, deleted_count=half)
    deleted = db.delete_stale_future_leave(["x"], CUTOFF)
    assert deleted == half
    mock_alert.assert_not_called()
    assert conn.execute.call_count == 3
