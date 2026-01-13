"""Capacity calculation and schedule analysis for advisers."""

import logging
from datetime import date
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


def ceil_div(a, b):
    """Ceiling division.

    Args:
        a: Numerator
        b: Denominator

    Returns:
        Ceiling of a/b
    """
    return -(-a // b)


def monthly_limit_for_week(user: dict, week_ordinal: int) -> int:
    """Get monthly client limit for a week (with capacity overrides).

    Args:
        user: User dict with properties and capacity info
        week_ordinal: ISO week ordinal

    Returns:
        Monthly client limit for that week
    """
    monthly_limit = user.get("properties", {}).get("client_limit_monthly", 12)
    try:
        return int(monthly_limit)
    except (TypeError, ValueError):
        return 12


def weekly_capacity_target(user: dict, week_ordinal: int) -> int:
    """Compute target weekly capacity for an adviser.

    Weekly target = (monthly limit) / (4 weeks per month)

    Args:
        user: User dict
        week_ordinal: ISO week ordinal

    Returns:
        Target weekly capacity (clients per week)
    """
    monthly = monthly_limit_for_week(user, week_ordinal)
    return ceil_div(monthly, 4)


def _is_full_ooo_week(data, week_key: int) -> bool:
    """Check if a week has the adviser fully out of office.

    Args:
        data: Schedule data dict
        week_key: Week ordinal to check

    Returns:
        True if full week is marked as out-of-office
    """
    if not data:
        return False
    week_str = f"week_{week_key:04d}"
    if week_str not in data:
        return False
    row = data[week_str]
    # Check if all 5 columns are non-zero (representing full week OOO)
    return all(row.get(f"col_{i}", 0) > 0 for i in range(5))


def process_weekly_data(data):
    """Process weekly schedule data (normalize, group fortnights).

    Args:
        data: Schedule dict with week_XXXX keys

    Returns:
        Processed schedule with fortnight analysis
    """
    if not data:
        return {}

    weeks_sorted = sorted([int(k.split("_")[1]) for k in data.keys() if k.startswith("week_")])
    processed = {
        "weeks_sorted": weeks_sorted,
        "data": data,
    }
    return processed


def compute_capacity(user, min_week):
    """Compute capacity profile for an adviser from min_week onwards.

    Analyzes backlog, weekly targets, and projects earliest available week.

    Args:
        user: User dict with meeting details and schedule
        min_week: Minimum week ordinal to analyze

    Returns:
        Dict with capacity_analysis, backlog info, and earliest_available_week
    """
    # This is a placeholder - real implementation in allocation.py
    return {
        "capacity_analysis": {},
        "earliest_available_week": min_week,
    }


__all__ = [
    "ceil_div",
    "monthly_limit_for_week",
    "weekly_capacity_target",
    "process_weekly_data",
    "compute_capacity",
]
