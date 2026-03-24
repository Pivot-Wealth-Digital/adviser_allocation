"""Compute simulated clarifies for all advisers.

This job places deals without Clarify meetings into capacity-respecting weeks
using the same logic as the allocation algorithm. Results are stored in
aa_simulated_clarifies for use by the clarify chart view.

Run via:
  - Cloud Scheduler: POST /jobs/compute-simulated-clarifies
  - Manual: python -m adviser_allocation.jobs.compute_simulated_clarifies
"""

import logging
import uuid
from collections import defaultdict
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple

from adviser_allocation.core.allocation import (
    CLARIFY_COL,
    TARGET_CAPACITY_COL,
    classify_deals_list,
    classify_leave_weeks,
    compute_capacity,
    get_deals_no_clarify,
    get_meeting_count,
    get_merged_schedule,
    get_user_client_limits,
    get_user_ids_adviser,
    get_user_meeting_details,
    week_monday_ordinal,
)
from adviser_allocation.utils.common import get_cloudsql_db, sydney_now, sydney_today

logger = logging.getLogger(__name__)


def get_all_deals_without_clarify() -> Dict[str, List[Dict[str, Any]]]:
    """Fetch all deals without Clarify meetings, grouped by adviser email.

    Returns:
        Dict mapping adviser_email -> list of deal dicts
    """
    users = get_user_ids_adviser()
    deals_by_adviser = defaultdict(list)

    for user in users:
        props = user.get("properties") or {}
        email = (props.get("hs_email") or "").lower().strip()
        if not email:
            continue

        taking_on = props.get("taking_on_clients")
        if taking_on is None or str(taking_on).strip() == "":
            continue

        try:
            deals = get_deals_no_clarify(email)
            for deal in deals:
                deal_props = deal.get("properties") or {}
                deals_by_adviser[email].append(
                    {
                        "deal_id": deal.get("id"),
                        "deal_name": deal_props.get("dealname"),
                        "adviser_email": email,
                        "agreement_start_date": deal_props.get("agreement_start_date"),
                        "client_email": None,  # Could extract from associations if needed
                    }
                )
        except Exception as e:
            logger.warning("Failed to fetch deals for %s: %s", email, e)

    return deals_by_adviser


def parse_agreement_date(value: Any) -> Optional[date]:
    """Parse agreement_start_date from various formats."""
    if value is None:
        return None
    try:
        if isinstance(value, date):
            return value
        # HubSpot returns timestamps as milliseconds
        if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
            ts = int(value) / 1000
            return date.fromtimestamp(ts)
        if isinstance(value, str):
            # ISO format
            return date.fromisoformat(value[:10])
    except Exception:
        pass
    return None


def compute_simulated_placements_for_adviser(
    user: Dict[str, Any],
    deals: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Compute projected weeks for deals without Clarify for a single adviser.

    Uses the capacity algorithm to place deals in weeks respecting limits.

    Args:
        user: HubSpot user dict with properties
        deals: List of deals without Clarify for this adviser

    Returns:
        List of dicts with deal_id, projected_week, etc.
    """
    if not deals:
        return []

    props = user.get("properties") or {}
    email = (props.get("hs_email") or "").lower().strip()

    # Get user's schedule data (meetings, leave, closures)
    try:
        db = get_cloudsql_db()

        # Leave requests from CloudSQL
        employee_id = db.get_employee_id_by_email(email)
        employee_leaves = db.get_employee_leaves_as_dicts(employee_id) if employee_id else []
        user["leave_requests"] = employee_leaves
        user["leave_requests_list"] = classify_leave_weeks(employee_leaves)

        # Office closures
        user["global_closure_weeks"] = classify_leave_weeks(db.get_global_closures())

        # Client limits
        user = get_user_client_limits(user)

        # Meetings from HubSpot
        four_weeks_ago = sydney_now() - timedelta(weeks=4)
        timestamp_ms = int(four_weeks_ago.timestamp() * 1000)
        user = get_user_meeting_details(user, timestamp_ms)
        user_meetings = (user.get("meetings") or {}).get("results", [])
        user["meeting_count_list"] = get_meeting_count(user_meetings)

        # Deals without clarify (raw HubSpot format for classify_deals_list)
        raw_deals = get_deals_no_clarify(email)
        user["deals_no_clarify"] = raw_deals
        user["deals_no_clarify_list"] = classify_deals_list(raw_deals)

        # Merge into schedule and compute capacity
        user = get_merged_schedule(user)
    except Exception as e:
        logger.warning("Failed to get schedule for %s: %s", email, e)
        # Fallback: place deals naively at agreement_start + 14 days
        return _naive_placements(deals)

    # Compute capacity
    today = sydney_today()
    min_week = week_monday_ordinal(today)
    try:
        user = compute_capacity(user, min_week)
    except Exception as e:
        logger.warning("Failed to compute capacity for %s: %s", email, e)
        return _naive_placements(deals)

    capacity = user.get("capacity", {})
    if not capacity:
        return _naive_placements(deals)

    # Sort deals by agreement_start_date (FIFO queue)
    sorted_deals = sorted(
        deals, key=lambda d: parse_agreement_date(d.get("agreement_start_date")) or date.max
    )

    # Walk weeks and assign deals respecting capacity
    assignments = []
    deal_queue = list(sorted_deals)
    sorted_weeks = sorted(capacity.keys())

    # Track how many we've assigned to each week
    assigned_per_week = defaultdict(int)

    for week_ordinal in sorted_weeks:
        if not deal_queue:
            break

        week_data = capacity[week_ordinal]
        leave_status = week_data[2] if len(week_data) > 2 else "No"

        # Skip full OOO weeks
        if str(leave_status).lower() == "full":
            continue

        # Get target capacity and already booked clarifies
        target = week_data[TARGET_CAPACITY_COL] if len(week_data) > TARGET_CAPACITY_COL else 3
        booked = week_data[CLARIFY_COL] if len(week_data) > CLARIFY_COL else 0
        already_assigned = assigned_per_week[week_ordinal]

        # Available slots = target - booked - already assigned simulated
        available = max(0, target - booked - already_assigned)

        # Assign deals to this week
        while available > 0 and deal_queue:
            deal = deal_queue.pop(0)
            monday = date.fromordinal(week_ordinal)
            assignments.append(
                {
                    "deal_id": deal["deal_id"],
                    "adviser_email": email,
                    "projected_week": monday,
                    "agreement_start_date": parse_agreement_date(deal.get("agreement_start_date")),
                    "deal_name": deal.get("deal_name"),
                    "client_email": deal.get("client_email"),
                }
            )
            assigned_per_week[week_ordinal] += 1
            available -= 1

    # Any remaining deals go to the last week + extensions
    if deal_queue and sorted_weeks:
        last_week = sorted_weeks[-1]
        weekly_target = 3  # Default fallback

        while deal_queue:
            last_week += 7  # Move to next week
            monday = date.fromordinal(last_week)

            for _ in range(min(weekly_target, len(deal_queue))):
                if not deal_queue:
                    break
                deal = deal_queue.pop(0)
                assignments.append(
                    {
                        "deal_id": deal["deal_id"],
                        "adviser_email": email,
                        "projected_week": monday,
                        "agreement_start_date": parse_agreement_date(
                            deal.get("agreement_start_date")
                        ),
                        "deal_name": deal.get("deal_name"),
                        "client_email": deal.get("client_email"),
                    }
                )

    return assignments


def _naive_placements(deals: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Fallback: place deals at agreement_start + 14 days."""
    assignments = []
    for deal in deals:
        agreement_date = parse_agreement_date(deal.get("agreement_start_date"))
        if agreement_date:
            projected = agreement_date + timedelta(days=14)
            # Round to Monday
            days_since_monday = projected.weekday()
            monday = projected - timedelta(days=days_since_monday)
        else:
            monday = sydney_today()

        assignments.append(
            {
                "deal_id": deal["deal_id"],
                "adviser_email": deal.get("adviser_email"),
                "projected_week": monday,
                "agreement_start_date": agreement_date,
                "deal_name": deal.get("deal_name"),
                "client_email": deal.get("client_email"),
            }
        )
    return assignments


def run_computation() -> Tuple[int, int]:
    """Run the full computation for all advisers.

    Returns:
        Tuple of (advisers_processed, deals_assigned)
    """
    batch_id = uuid.uuid4()
    logger.info("Starting simulated clarifies computation (batch %s)", batch_id)

    # Get all deals grouped by adviser
    deals_by_adviser = get_all_deals_without_clarify()
    logger.info("Found deals for %d advisers", len(deals_by_adviser))

    # Get all adviser users for capacity computation
    users = get_user_ids_adviser()
    user_by_email = {}
    for user in users:
        props = user.get("properties") or {}
        email = (props.get("hs_email") or "").lower().strip()
        if email:
            user_by_email[email] = user

    # Compute placements for each adviser
    all_assignments = []
    advisers_processed = 0

    for email, deals in deals_by_adviser.items():
        user = user_by_email.get(email)
        if not user:
            logger.warning("No user data for %s, using naive placement", email)
            assignments = _naive_placements(deals)
        else:
            assignments = compute_simulated_placements_for_adviser(user, deals)

        # Add batch ID to all assignments
        for a in assignments:
            a["computation_batch_id"] = batch_id

        all_assignments.extend(assignments)
        advisers_processed += 1
        logger.debug("Computed %d placements for %s", len(assignments), email)

    # Store in database
    if all_assignments:
        db = get_cloudsql_db()
        db.replace_simulated_clarifies(all_assignments)
        logger.info(
            "Stored %d simulated clarify placements for %d advisers",
            len(all_assignments),
            advisers_processed,
        )

    return advisers_processed, len(all_assignments)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    advisers, deals = run_computation()
    logger.info("Processed %d advisers, assigned %d deals", advisers, deals)
