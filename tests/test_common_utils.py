"""Tests for common utility functions."""

import os
import unittest
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

os.environ.setdefault("USE_FIRESTORE", "false")

from adviser_allocation.utils.common import (
    sydney_now,
    sydney_today,
    sydney_datetime_from_date,
    SYDNEY_TZ,
)


class SydneyTimeTests(unittest.TestCase):
    """Tests for Sydney timezone utilities."""

    def test_sydney_timezone_constant_defined(self):
        """Test that SYDNEY_TZ is defined correctly."""
        self.assertEqual(str(SYDNEY_TZ), "Australia/Sydney")

    def test_sydney_now_returns_datetime(self):
        """Test that sydney_now returns a datetime object."""
        result = sydney_now()
        self.assertIsInstance(result, datetime)

    def test_sydney_now_uses_sydney_timezone(self):
        """Test that sydney_now uses Sydney timezone."""
        result = sydney_now()
        # Check that result has timezone info
        self.assertIsNotNone(result.tzinfo)

    def test_sydney_today_returns_date(self):
        """Test that sydney_today returns a date object."""
        result = sydney_today()
        self.assertIsInstance(result, date)

    def test_sydney_today_matches_sydney_now_date(self):
        """Test that sydney_today matches the date from sydney_now."""
        today = sydney_today()
        now = sydney_now()
        self.assertEqual(today, now.date())

    def test_sydney_datetime_from_date_midnight(self):
        """Test conversion of date to datetime at midnight."""
        test_date = date(2025, 1, 15)
        result = sydney_datetime_from_date(test_date)

        # Check date matches
        self.assertEqual(result.date(), test_date)

        # Check time is midnight
        self.assertEqual(result.hour, 0)
        self.assertEqual(result.minute, 0)
        self.assertEqual(result.second, 0)

    def test_sydney_datetime_from_date_timezone(self):
        """Test that converted datetime has Sydney timezone."""
        test_date = date(2025, 1, 15)
        result = sydney_datetime_from_date(test_date)

        # Check timezone is Sydney
        self.assertEqual(str(result.tzinfo), "Australia/Sydney")

    def test_sydney_datetime_from_date_past_date(self):
        """Test conversion of past date."""
        past_date = date(2024, 1, 1)
        result = sydney_datetime_from_date(past_date)

        self.assertEqual(result.date(), past_date)
        self.assertEqual(result.hour, 0)

    def test_sydney_datetime_from_date_future_date(self):
        """Test conversion of future date."""
        future_date = date(2030, 12, 31)
        result = sydney_datetime_from_date(future_date)

        self.assertEqual(result.date(), future_date)
        self.assertEqual(result.hour, 0)

    def test_multiple_calls_consistency(self):
        """Test that multiple calls are consistent."""
        date1 = sydney_today()
        date2 = sydney_today()

        # Within same execution, should be same
        self.assertEqual(date1, date2)


class TimezoneConsistencyTests(unittest.TestCase):
    """Tests for timezone consistency across utilities."""

    def test_sydney_now_and_today_consistency(self):
        """Test that sydney_now and sydney_today are consistent."""
        now = sydney_now()
        today = sydney_today()

        # Date from now should match today
        self.assertEqual(now.date(), today)

    def test_datetime_from_today_matches_now(self):
        """Test that datetime created from today matches time of now."""
        today = sydney_today()
        now = sydney_now()
        datetime_from_today = sydney_datetime_from_date(today)

        # Date should match
        self.assertEqual(datetime_from_today.date(), now.date())

        # Both should be in Sydney timezone
        self.assertEqual(
            str(datetime_from_today.tzinfo),
            str(now.tzinfo),
            "Both should use Sydney timezone",
        )


class EdgeCaseTests(unittest.TestCase):
    """Tests for edge cases in timezone utilities."""

    def test_leap_year_date(self):
        """Test handling of February 29 in leap year."""
        leap_date = date(2024, 2, 29)
        result = sydney_datetime_from_date(leap_date)

        self.assertEqual(result.date(), leap_date)
        self.assertEqual(result.month, 2)
        self.assertEqual(result.day, 29)

    def test_year_boundary_date(self):
        """Test handling of December 31 to January 1."""
        dec_31 = date(2024, 12, 31)
        result = sydney_datetime_from_date(dec_31)

        self.assertEqual(result.date(), dec_31)
        self.assertEqual(result.month, 12)
        self.assertEqual(result.day, 31)

        jan_1 = date(2025, 1, 1)
        result2 = sydney_datetime_from_date(jan_1)

        self.assertEqual(result2.date(), jan_1)
        self.assertEqual(result2.month, 1)
        self.assertEqual(result2.day, 1)

    def test_datetime_from_date_has_correct_epoch(self):
        """Test that created datetime represents midnight at start of day."""
        test_date = date(2025, 6, 15)
        result = sydney_datetime_from_date(test_date)

        # Should be exactly at midnight
        self.assertEqual(result.hour, 0)
        self.assertEqual(result.minute, 0)
        self.assertEqual(result.second, 0)
        self.assertEqual(result.microsecond, 0)


if __name__ == "__main__":
    unittest.main()
