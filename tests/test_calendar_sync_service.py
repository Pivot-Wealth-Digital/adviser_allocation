"""Tests for Google Calendar sync service."""

import os
import unittest
from datetime import date
from unittest.mock import MagicMock, patch

from adviser_allocation.services.calendar_sync_service import (
    CALENDAR_SYNC_TAG,
    _derive_tags,
    _parse_event_dates,
    sync_calendar_closures,
)


class TestDeriveTags(unittest.TestCase):
    """Tests for _derive_tags()."""

    def test_public_holiday_keyword(self):
        tags = _derive_tags("Australia Day (Public Holiday)")
        self.assertIn(CALENDAR_SYNC_TAG, tags)
        self.assertIn("Public Holiday", tags)

    def test_wellness_day_keyword(self):
        tags = _derive_tags("Pivot Wellness Day")
        self.assertIn(CALENDAR_SYNC_TAG, tags)
        self.assertIn("Wellness Day", tags)

    def test_closure_keyword(self):
        tags = _derive_tags("Office Closure - End of Year")
        self.assertIn("Office Closure", tags)

    def test_unrecognised_title_gets_base_tag_only(self):
        tags = _derive_tags("Random Event")
        self.assertEqual(tags, [CALENDAR_SYNC_TAG])

    def test_empty_title(self):
        tags = _derive_tags("")
        self.assertEqual(tags, [CALENDAR_SYNC_TAG])

    def test_none_title(self):
        tags = _derive_tags(None)
        self.assertEqual(tags, [CALENDAR_SYNC_TAG])

    def test_source_tag_added(self):
        tags = _derive_tags("Some Event", source_tag="Public Holiday")
        self.assertIn("Public Holiday", tags)
        self.assertIn(CALENDAR_SYNC_TAG, tags)

    def test_source_tag_not_duplicated_with_keyword(self):
        tags = _derive_tags("Australia Day (Public Holiday)", source_tag="Public Holiday")
        self.assertEqual(tags.count("Public Holiday"), 1)

    def test_case_insensitive_matching(self):
        tags = _derive_tags("WELLNESS day")
        self.assertIn("Wellness Day", tags)


class TestParseEventDates(unittest.TestCase):
    """Tests for _parse_event_dates()."""

    def test_all_day_single_day(self):
        event = {"start": {"date": "2026-01-26"}, "end": {"date": "2026-01-27"}}
        start, end = _parse_event_dates(event)
        self.assertEqual(start, date(2026, 1, 26))
        self.assertEqual(end, date(2026, 1, 26))  # Exclusive end minus 1

    def test_all_day_multi_day(self):
        event = {"start": {"date": "2026-12-24"}, "end": {"date": "2026-12-28"}}
        start, end = _parse_event_dates(event)
        self.assertEqual(start, date(2026, 12, 24))
        self.assertEqual(end, date(2026, 12, 27))  # 4 days: 24, 25, 26, 27

    def test_timed_event_same_day(self):
        event = {
            "start": {"dateTime": "2026-03-10T09:00:00+11:00"},
            "end": {"dateTime": "2026-03-10T17:00:00+11:00"},
        }
        start, end = _parse_event_dates(event)
        self.assertEqual(start, date(2026, 3, 10))
        self.assertEqual(end, date(2026, 3, 10))

    def test_timed_event_utc(self):
        event = {
            "start": {"dateTime": "2026-03-10T00:00:00Z"},
            "end": {"dateTime": "2026-03-10T23:59:00Z"},
        }
        start, end = _parse_event_dates(event)
        self.assertEqual(start, date(2026, 3, 10))
        self.assertEqual(end, date(2026, 3, 10))

    def test_missing_date_returns_none(self):
        self.assertIsNone(_parse_event_dates({"start": {}, "end": {}}))

    def test_invalid_date_returns_none(self):
        event = {"start": {"date": "not-a-date"}, "end": {"date": "2026-01-01"}}
        self.assertIsNone(_parse_event_dates(event))

    def test_empty_event_returns_none(self):
        self.assertIsNone(_parse_event_dates({}))


class TestSyncCalendarClosures(unittest.TestCase):
    """Tests for sync_calendar_closures()."""

    @patch("adviser_allocation.services.calendar_sync_service.fetch_calendar_events")
    def test_upserts_valid_events(self, mock_fetch):
        mock_fetch.return_value = [
            {
                "id": "event_001",
                "summary": "Australia Day (Public Holiday)",
                "status": "confirmed",
                "start": {"date": "2026-01-26"},
                "end": {"date": "2026-01-27"},
            }
        ]
        mock_db = MagicMock()
        result = sync_calendar_closures([("test-cal", None)], mock_db)

        mock_db.upsert_office_closure_by_event_id.assert_called_once_with(
            google_event_id="event_001",
            start_date=date(2026, 1, 26),
            end_date=date(2026, 1, 26),
            description="Australia Day (Public Holiday)",
            tags=[CALENDAR_SYNC_TAG, "Public Holiday"],
        )
        self.assertEqual(result["upserted"], 1)
        self.assertEqual(result["errors"], 0)

    @patch("adviser_allocation.services.calendar_sync_service.fetch_calendar_events")
    def test_skips_cancelled_events(self, mock_fetch):
        mock_fetch.return_value = [
            {
                "id": "evt_x",
                "status": "cancelled",
                "start": {"date": "2026-01-01"},
                "end": {"date": "2026-01-02"},
            }
        ]
        mock_db = MagicMock()
        result = sync_calendar_closures([("test-cal", None)], mock_db)
        mock_db.upsert_office_closure_by_event_id.assert_not_called()
        self.assertEqual(result["skipped"], 1)

    @patch("adviser_allocation.services.calendar_sync_service.fetch_calendar_events")
    def test_skips_events_without_id(self, mock_fetch):
        mock_fetch.return_value = [
            {"summary": "No ID", "start": {"date": "2026-01-01"}, "end": {"date": "2026-01-02"}}
        ]
        mock_db = MagicMock()
        result = sync_calendar_closures([("test-cal", None)], mock_db)
        mock_db.upsert_office_closure_by_event_id.assert_not_called()
        self.assertEqual(result["skipped"], 1)

    @patch("adviser_allocation.services.calendar_sync_service.fetch_calendar_events")
    def test_deletes_stale_closures(self, mock_fetch):
        mock_fetch.return_value = [
            {
                "id": "evt_active",
                "status": "confirmed",
                "summary": "Wellness Day",
                "start": {"date": "2026-04-01"},
                "end": {"date": "2026-04-02"},
            }
        ]
        mock_db = MagicMock()
        mock_db.delete_stale_calendar_closures.return_value = 2
        result = sync_calendar_closures([("test-cal", None)], mock_db)
        mock_db.delete_stale_calendar_closures.assert_called_once_with(["evt_active"])
        self.assertEqual(result["deleted"], 2)

    @patch("adviser_allocation.services.calendar_sync_service.fetch_calendar_events")
    def test_calendar_api_error_counted(self, mock_fetch):
        mock_fetch.side_effect = Exception("API quota exceeded")
        mock_db = MagicMock()
        result = sync_calendar_closures([("test-cal", None)], mock_db)
        self.assertEqual(result["errors"], 1)
        mock_db.upsert_office_closure_by_event_id.assert_not_called()

    @patch("adviser_allocation.services.calendar_sync_service.fetch_calendar_events")
    def test_empty_events_skips_deletion(self, mock_fetch):
        mock_fetch.return_value = []
        mock_db = MagicMock()
        result = sync_calendar_closures([("test-cal", None)], mock_db)
        mock_db.delete_stale_calendar_closures.assert_not_called()

    @patch("adviser_allocation.services.calendar_sync_service.fetch_calendar_events")
    def test_multi_calendar_sources(self, mock_fetch):
        mock_fetch.side_effect = [
            [
                {
                    "id": "pivot_001",
                    "summary": "Wellness Day",
                    "status": "confirmed",
                    "start": {"date": "2026-04-01"},
                    "end": {"date": "2026-04-02"},
                }
            ],
            [
                {
                    "id": "holiday_001",
                    "summary": "Australia Day",
                    "status": "confirmed",
                    "start": {"date": "2026-01-26"},
                    "end": {"date": "2026-01-27"},
                }
            ],
        ]
        mock_db = MagicMock()
        sources = [("pivot-cal", None), ("au-holidays", "Public Holiday")]
        result = sync_calendar_closures(sources, mock_db)

        self.assertEqual(result["upserted"], 2)
        self.assertEqual(mock_db.upsert_office_closure_by_event_id.call_count, 2)

        # Check the holidays event got the source tag
        holiday_call = mock_db.upsert_office_closure_by_event_id.call_args_list[1]
        self.assertIn("Public Holiday", holiday_call.kwargs["tags"])

    @patch("adviser_allocation.services.calendar_sync_service.fetch_calendar_events")
    def test_upsert_error_counted_but_continues(self, mock_fetch):
        mock_fetch.return_value = [
            {
                "id": "evt_1",
                "summary": "Event 1",
                "status": "confirmed",
                "start": {"date": "2026-03-01"},
                "end": {"date": "2026-03-02"},
            },
            {
                "id": "evt_2",
                "summary": "Event 2",
                "status": "confirmed",
                "start": {"date": "2026-03-05"},
                "end": {"date": "2026-03-06"},
            },
        ]
        mock_db = MagicMock()
        mock_db.upsert_office_closure_by_event_id.side_effect = [
            Exception("DB error"),
            "closure-id-2",
        ]
        result = sync_calendar_closures([("test-cal", None)], mock_db)
        self.assertEqual(result["upserted"], 1)
        self.assertEqual(result["errors"], 1)
        # Only the successful event should be in active_ids for stale deletion
        mock_db.delete_stale_calendar_closures.assert_called_once_with(["evt_2"])


if __name__ == "__main__":
    unittest.main()
