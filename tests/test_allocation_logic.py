import os
import unittest
from datetime import date
from unittest.mock import patch

os.environ.setdefault("USE_FIRESTORE", "false")

from adviser_allocation.core import allocation as allocate  # noqa: E402


class AllocationLogicTests(unittest.TestCase):
    @patch("adviser_allocation.core.allocation.sydney_today")
    def test_partial_week_adjusts_capacity(self, mock_today):
        mock_today.return_value = date(2025, 1, 6)  # Monday

        base_week = allocate.week_monday_ordinal(date(2024, 12, 30))
        schedule = {
            base_week: [1, 0, "No", 0],
            base_week + 7: [0, 0, "Full", 0],
            base_week + 14: [0, 0, "Partial: 3", 0],
            base_week + 21: [0, 0, "No", 0],
        }
        user = {
            "properties": {"hs_email": "test@example.com", "client_limit_monthly": 6},
            "merged_schedule": schedule,
        }

        allocate.compute_capacity(user, base_week)
        capacity = user["capacity"]

        self.assertEqual(
            capacity[base_week][allocate.TARGET_CAPACITY_COL],
            3,
            "Weeks without leave should retain full fortnightly target",
        )
        self.assertEqual(
            capacity[base_week + 7][allocate.TARGET_CAPACITY_COL],
            0,
            "Full leave weeks should zero out capacity",
        )
        self.assertEqual(
            capacity[base_week + 14][allocate.TARGET_CAPACITY_COL],
            2,
            "Partial leave of three days should halve fortnightly target",
        )

    @patch("adviser_allocation.core.allocation.sydney_today")
    def test_find_earliest_week_respects_buffer(self, mock_today):
        mock_today.return_value = date(2025, 1, 6)
        base_week = allocate.week_monday_ordinal(date(2024, 12, 30))

        schedule = {
            base_week: [0, 0, "No", 0],
            base_week + 7: [0, 0, "No", 0],
            base_week + 14: [0, 0, "No", 0],
            base_week + 21: [0, 0, "No", 0],
        }
        user = {
            "properties": {"hs_email": "test@example.com", "client_limit_monthly": 6},
            "merged_schedule": schedule,
        }

        allocate.compute_capacity(user, base_week)
        result = allocate.find_earliest_week(user, base_week)

        expected_week = allocate.week_monday_ordinal(date(2025, 1, 20))
        self.assertEqual(
            result["earliest_open_week"],
            expected_week,
            "Earliest week should honour the two-week buffer",
        )

    def test_household_filtering_rules(self):
        self.assertTrue(allocate.should_filter_by_household("series a"))
        self.assertTrue(allocate.should_filter_by_household("seed"))
        self.assertFalse(allocate.should_filter_by_household("series c"))
        self.assertFalse(allocate.should_filter_by_household("ipo"))

    def test_weekly_capacity_target_respects_overrides(self):
        dec_week = allocate.week_monday_ordinal(date(2024, 12, 30))
        jan_week = allocate.week_monday_ordinal(date(2025, 1, 6))
        user = {
            "properties": {"hs_email": "test@example.com", "client_limit_monthly": 4},
            "_base_client_limit_monthly": 4,
            "capacity_override_schedule": [
                {
                    "effective_date": "2025-01-01",
                    "effective_start": "2025-01-06",
                    "effective_week": jan_week,
                    "client_limit_monthly": 6,
                    "pod_type": "",
                    "notes": "",
                }
            ],
        }

        self.assertEqual(
            allocate.weekly_capacity_target(user, dec_week),
            2,
            "December weeks should retain base solo fortnight target",
        )
        self.assertEqual(
            allocate.weekly_capacity_target(user, jan_week),
            3,
            "Override should raise fortnight target starting the effective week",
        )

    def test_merged_schedule_combines_partial_weeks(self):
        base_week = allocate.week_monday_ordinal(date(2025, 1, 6))
        user = {
            "leave_requests_list": [
                [base_week, "Partial: 1"],
                [base_week, "Partial: 4"],
            ],
            "global_closure_weeks": [],
            "deals_no_clarify_list": {},
            "meeting_count_list": {base_week: [0, 0]},
        }

        allocate.get_merged_schedule(user)
        merged = user["merged_schedule"]
        self.assertIn(base_week, merged)
        self.assertEqual(
            merged[base_week][2],
            "Partial: 4",
            "Merged schedule should keep the highest partial-day count for the week",
        )


if __name__ == "__main__":
    unittest.main()
