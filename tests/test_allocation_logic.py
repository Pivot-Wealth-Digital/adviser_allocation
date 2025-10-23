import os
import unittest
from datetime import date
from unittest.mock import patch

os.environ.setdefault("USE_FIRESTORE", "false")

import allocate  # noqa: E402


class AllocationLogicTests(unittest.TestCase):
    @patch("allocate.sydney_today")
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

    @patch("allocate.sydney_today")
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

    def test_household_matching_rules(self):
        self.assertTrue(allocate._matches_household("Series A", "Single"))
        self.assertTrue(allocate._matches_household("Series A", "Single;Couple"))
        self.assertFalse(allocate._matches_household("Series A", "Family"))
        self.assertTrue(allocate._matches_household("Seed", "Couple"))
        self.assertTrue(allocate._matches_household("Series B", "Family"))  # no rule


if __name__ == "__main__":
    unittest.main()
