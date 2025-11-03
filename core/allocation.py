import os, time, logging
from datetime import datetime, timedelta, date
import re
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from zoneinfo import ZoneInfo
from functools import lru_cache
from typing import Optional, Dict, List

from utils.common import sydney_now, sydney_today, sydney_datetime_from_date, SYDNEY_TZ
from utils.secrets import get_secret
from utils.firestore_helpers import (
    get_employee_leaves as get_employee_leaves_from_firestore,
    get_employee_id as get_employee_id_from_firestore,
    get_global_closures as get_global_closures_from_firestore,
    get_capacity_overrides as get_capacity_overrides_from_firestore,
)

logger = logging.getLogger(__name__)

HUBSPOT_TOKEN = get_secret("HUBSPOT_TOKEN")
HEADERS = {"Authorization": f"Bearer {HUBSPOT_TOKEN}", "Content-Type": "application/json"}
_SPECIAL_UPPER = {"ipo"}
PRESTART_WEEKS = int(os.environ.get("PRESTART_WEEKS", "3"))
# Fetch fresh data from HubSpot every time (set to 0 to disable caching)
# Can be overridden with MATRIX_CACHE_TTL environment variable
_MATRIX_CACHE_TTL = int(os.environ.get("MATRIX_CACHE_TTL", "0"))
_MATRIX_CACHE = {
    "timestamp": 0.0,
    "services": [],
    "households": [],
    "matrix": {},
}

_CAPACITY_OVERRIDE_DATE_FMT = "%Y-%m-%d"


def _parse_iso_date(value: str) -> Optional[date]:
    if not value:
        return None
    try:
        return datetime.strptime(value, _CAPACITY_OVERRIDE_DATE_FMT).date()
    except Exception:
        try:
            parsed = datetime.fromisoformat(value)
            if isinstance(parsed, datetime):
                return parsed.date()
            if isinstance(parsed, date):
                return parsed
        except Exception:
            return None
    return None


@lru_cache(maxsize=1)
def _capacity_override_cache() -> Dict[str, List[Dict]]:
    """Cache adviser capacity overrides grouped by email."""
    overrides_map: dict[str, list[dict]] = {}
    raw_items = get_capacity_overrides_from_firestore()
    for raw in raw_items:
        email = (raw.get("adviser_email") or "").strip().lower()
        if not email:
            continue
        limit_val = raw.get("client_limit_monthly")
        try:
            limit_int = int(limit_val)
        except (TypeError, ValueError):
            continue
        if limit_int <= 0:
            continue
        eff_date = _parse_iso_date(raw.get("effective_date"))
        if eff_date is None:
            continue
        # Overrides take effect from the first Monday on/after the supplied date
        days_until_monday = (7 - eff_date.weekday()) % 7
        effective_start = eff_date if days_until_monday == 0 else eff_date + timedelta(days=days_until_monday)
        effective_week = week_monday_ordinal(effective_start)
        overrides_map.setdefault(email, []).append(
            {
                "effective_date": eff_date.isoformat(),
                "effective_start": effective_start.isoformat(),
                "effective_week": effective_week,
                "client_limit_monthly": limit_int,
                "pod_type": (raw.get("pod_type") or "").strip(),
                "notes": raw.get("notes", ""),
            }
        )
    for value in overrides_map.values():
        value.sort(key=lambda item: item["effective_week"])
    return overrides_map


def refresh_capacity_override_cache() -> None:
    """Clear the cached overrides; used after admin updates."""
    _capacity_override_cache.cache_clear()


def _capacity_schedule_for_email(email: str) -> List[Dict]:
    if not email:
        return []
    return list(_capacity_override_cache().get(email.strip().lower(), []))


def _apply_capacity_overrides(user: dict) -> dict:
    """Attach capacity override schedule to the adviser record and adjust current limit."""
    props = user.setdefault("properties", {})
    email = (props.get("hs_email") or "").strip().lower()
    base_limit = int(props.get("client_limit_monthly") or 0)
    user["_base_client_limit_monthly"] = base_limit

    schedule = _capacity_schedule_for_email(email)
    if not schedule:
        user["capacity_override_schedule"] = []
        user["capacity_override_active"] = None
        user["capacity_override_upcoming"] = []
        return user

    today_week = week_monday_ordinal(sydney_today())
    current_limit = base_limit
    active_entry = None
    upcoming_entries: list[dict] = []

    for entry in schedule:
        if entry["effective_week"] <= today_week:
            current_limit = entry["client_limit_monthly"]
            active_entry = entry
        else:
            upcoming_entries.append(entry)

    props["client_limit_monthly"] = current_limit
    if active_entry and active_entry.get("pod_type"):
        props["pod_type_effective"] = active_entry["pod_type"]

    user["capacity_override_schedule"] = schedule
    user["capacity_override_active"] = active_entry
    user["capacity_override_upcoming"] = upcoming_entries
    return user


def monthly_limit_for_week(user: dict, week_ordinal: int) -> int:
    """Return the monthly client limit that applies for the supplied week."""
    base_limit = int(
        user.get("_base_client_limit_monthly")
        or (user.get("properties", {}).get("client_limit_monthly") or 0)
    )
    schedule = user.get("capacity_override_schedule") or []
    limit = base_limit
    for entry in schedule:
        if entry["effective_week"] <= week_ordinal:
            limit = entry["client_limit_monthly"]
        else:
            break
    return int(limit)


def weekly_capacity_target(user: dict, week_ordinal: int) -> int:
    """Return the weekly (fortnight) capacity target for the supplied week."""
    monthly_limit = monthly_limit_for_week(user, week_ordinal)
    if monthly_limit <= 0:
        return 0
    return int(monthly_limit / 2)


def create_requests_session():
    """Create a requests session with retry logic for network issues."""
    session = requests.Session()
    
    # Define retry strategy
    retry_strategy = Retry(
        total=3,  # Total number of retries
        status_forcelist=[429, 500, 502, 503, 504],  # HTTP status codes to retry on
        allowed_methods=["HEAD", "GET", "POST", "PUT", "DELETE", "OPTIONS", "TRACE"],  # Updated parameter name
        backoff_factor=1,  # Wait time between retries (1, 2, 4 seconds)
        raise_on_status=False
    )
    
    # Mount the adapter to both HTTP and HTTPS
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    
    return session


CLARIFY_COL = 0
KICKOFF_COL = 1
LEAVE_COL = 2
DEALS_NO_CLARIFY_COL = 3
TARGET_CAPACITY_COL = 4
ACTUAL_CAPACITY_COL = 5
DIFFERENCE_COL = 6

# Days in a fortnight (used when stepping weeks by ordinals)
FORTNIGHT_DAYS = 14
WEEKLY_HARD_LIMIT = 2 # maximum number of clarifies that can be allocated in a single week

def _prev_week(week_key: int) -> int:
    """Return the Monday ordinal of the previous week for a given week key."""
    return week_key - 7


def _get_col(data, week_key: int, col_index: int, default: int = 0) -> int:
    """Safely fetch an integer column value for a given week.

    Falls back to `default` if the week is missing or the column index
    does not exist for that week.
    """
    row = data.get(week_key, [])
    try:
        return int(row[col_index]) if len(row) > col_index else default
    except Exception:
        return default


def _first_index_at_or_after(weeks_sorted, min_week_key: int):
    """Return the index of the first week >= min_week_key in the sorted list."""
    for idx, wk in enumerate(weeks_sorted):
        if wk >= min_week_key:
            return idx
    return None


def _is_full_ooo_week(data, week_key: int) -> bool:
    """Check if a week has Full OOO status."""
    row = data.get(week_key, [])
    if len(row) > LEAVE_COL:
        return str(row[LEAVE_COL]).strip().lower() == "full"
    return False


def _find_next_non_full_ooo_week(data, sorted_weeks, start_week: int) -> int:
    """Find the next week that doesn't have Full OOO status."""
    start_idx = _first_index_at_or_after(sorted_weeks, start_week)
    if start_idx is None:
        return start_week
    
    for i in range(start_idx, len(sorted_weeks)):
        week = sorted_weeks[i]
        if not _is_full_ooo_week(data, week):
            return week
    
    # If all remaining weeks are Full OOO, return the last week + 7 days
    return sorted_weeks[-1] + 7 if sorted_weeks else start_week


def _find_prev_non_full_ooo_week(data, sorted_weeks, start_week: int) -> int:
    """Find the previous week that doesn't have Full OOO status."""
    # Find the index of start_week or the closest week before it
    start_idx = None
    for i, week in enumerate(sorted_weeks):
        if week >= start_week:
            start_idx = i
            break
    
    if start_idx is None:
        start_idx = len(sorted_weeks)
    
    # Look backwards from start_idx
    for i in range(start_idx - 1, -1, -1):
        week = sorted_weeks[i]
        if not _is_full_ooo_week(data, week):
            return week
    
    # If no previous non-Full OOO week found, return the first week - 7 days
    return sorted_weeks[0] - 7 if sorted_weeks else start_week - 7


def ceil_div(a, b):
    return -(-a // b)  # integer ceil without math.ceil


def get_first_monday_current_month(input_date=None, tz_name=None) -> int:
    """
    Return the epoch timestamp (milliseconds) for local midnight on the first Monday
    of the month containing `input_date`. If `input_date` is None, use 'now' in tz.
    Accepts a datetime (aware or naive) or a date.
    """
    # Use Sydney timezone by default
    if tz_name is None:
        tz = SYDNEY_TZ
    else:
        tz = ZoneInfo(tz_name)

    if input_date is None:
        dt = datetime.now(tz)
    elif isinstance(input_date, datetime):
        dt = input_date.astimezone(tz) if input_date.tzinfo else input_date.replace(tzinfo=tz)
    elif isinstance(input_date, date):
        dt = datetime(input_date.year, input_date.month, input_date.day, tzinfo=tz)
    else:
        raise TypeError("input_date must be a datetime, date, or None")

    month_start = datetime(dt.year, dt.month, 1, tzinfo=tz)

    # Calculate days to the next Monday (Monday = 0 in Python's weekday())
    days_to_monday = (7 - month_start.weekday()) % 7
    first_monday = month_start + timedelta(days=days_to_monday)

    # Get the week number and convert the date to epoch milliseconds
    week_number = first_monday.isocalendar()[1]
    epoch_ms = int(
        datetime(
            first_monday.year, first_monday.month, first_monday.day, tzinfo=tz
        ).timestamp()
        * 1000
    )

    return epoch_ms, week_number


def get_monday_from_weeks_ago(input_date=None, n=1):
    """
    get monday from n weeks ago of input date or today (using Sydney timezone)
    """

    # Use provided date or today (Sydney time)
    if input_date:
        if isinstance(input_date, datetime):
            today = input_date.date()
        elif isinstance(input_date, date):
            today = input_date
        else:
            raise TypeError("input_date must be a datetime, date, or None")
    else:
        today = sydney_today()

    # Calculate the date for the Monday of the current week
    # weekday() returns 0 for Monday, 1 for Tuesday, etc.
    current_week_start = today - timedelta(days=today.weekday())

    # Calculate the date for the Monday of the week two workweeks ago
    two_workweeks_ago_start = current_week_start - timedelta(weeks=n)

    # Convert the date to a datetime object at midnight (start of the day) in Sydney timezone
    start_of_day_two_workweeks_ago = sydney_datetime_from_date(two_workweeks_ago_start)

    # Convert the datetime object to a Unix timestamp in milliseconds
    timestamp_milliseconds = int(start_of_day_two_workweeks_ago.timestamp() * 1000)

    return timestamp_milliseconds


def get_user_meeting_details(user, timestamp_milliseconds):
    """
    returns details of user meetings from input timestamp
    """

    url = "https://api.hubapi.com/crm/v3/objects/meetings/search"

    user_id = user["properties"]["hubspot_owner_id"]
    payload = {
        "filterGroups": [
            {
                "filters": [
                    {
                        "propertyName": "hubspot_owner_id",
                        "operator": "EQ",
                        "value": f"{user_id}",
                        # 250884516 - sturman
                    },
                    {
                        "propertyName": "hs_meeting_start_time",
                        "operator": "GTE",
                        "value": f"{timestamp_milliseconds}",
                    },
                    {
                        "propertyName": "hs_activity_type",
                        "operator": "IN",
                        "values": ["Clarify", "Kick Off"],
                    },
                    # {
                    #     "propertyName": "hs_object_id",
                    #     "operator": "EQ",
                    #     "value": "41467563648", # deal id of shane sample
                    # },
                ]
            }
        ],
        "properties": [
            "hs_meeting_title",
            "hs_meeting_start_time",
            # "hs_meeting_end_time",
            # "hs_meeting_location",
            # "hs_meeting_body",
            "hs_meeting_outcome",
            "hubspot_owner_id",
            "hs_activity_type",
        ],
        "sorts": [{"propertyName": "hs_meeting_start_time", "direction": "DESCENDING"}],
        "limit": 100,
    }
    if not HUBSPOT_TOKEN:
        raise RuntimeError("HUBSPOT_TOKEN is not configured")
    
    session = create_requests_session()
    try:
        result = session.post(url, headers=HEADERS, json=payload, timeout=30)
        result.raise_for_status()
        user["meetings"] = result.json()
        time.sleep(0.005)
    except requests.exceptions.RequestException as e:
        logging.warning(f"Failed to fetch meetings for user: {e}")
        user["meetings"] = {"results": []}  # Fallback to empty results
    finally:
        session.close()

    return user


def get_meeting_count(user_meetings, display_table=False):
    # Dictionary to store meeting counts per week
    weekly_clarify_counts = {}
    weekly_kickoff_counts = {}

    # user_meetings = get_user_meeting_details(user_id, input_date)
    # print(user_meetings)

    for meeting in user_meetings:
        # Extract the meeting start time string
        start_time_str = meeting["properties"]["hs_meeting_start_time"]

        # Convert the ISO 8601 string to a datetime object
        # The 'Z' indicates UTC, so .fromisoformat() handles it.
        meeting_datetime = datetime.fromisoformat(start_time_str.replace("Z", "+00:00"))

        # Get the ISO week number (1 to 52 or 53)
        # isocalendar() returns a tuple (year, week, weekday)
        week_number = week_monday_ordinal(meeting_datetime.date())

        # Check if the activity type is 'Kick Off' and increment its specific count
        if meeting["properties"]["hs_activity_type"] == "Kick Off":
            weekly_kickoff_counts[week_number] = (
                weekly_kickoff_counts.get(week_number, 0) + 1
            )

        # Check if the activity type is 'Clarify' and increment its specific count
        if meeting["properties"]["hs_activity_type"] == "Clarify":
            weekly_clarify_counts[week_number] = (
                weekly_clarify_counts.get(week_number, 0) + 1
            )

        # print(week_number, meeting['properties']['hs_meeting_start_time'], meeting['properties']['hs_activity_type'], meeting['properties']['hs_meeting_title'])

    # Prepare data for the table
    # Get all unique week numbers from both dictionaries
    all_weeks = sorted(
        list(set(weekly_clarify_counts.keys()).union(weekly_kickoff_counts.keys()))
    )

    table_data = [["Week", "Clarify Meetings", "Kick Off Meetings"]]
    for week in all_weeks:
        total_count = weekly_clarify_counts.get(week, 0)
        kickoff_count = weekly_kickoff_counts.get(week, 0)
        table_data.append([week_label_from_ordinal(week), total_count, kickoff_count])

    # Optionally log the table for debugging
    if display_table:
        lines = []
        for row in table_data:
            if row == table_data[0]:
                lines.append(f"{row[0]:<15} {row[1]:<16} {row[2]}")
                lines.append("-" * 50)
            else:
                lines.append(f"{row[0]:<15} {row[1]:<16} {row[2]}")
        logger.debug("Meeting count table:\n%s", "\n".join(lines))

    return {
        w: [weekly_clarify_counts.get(w, 0), weekly_kickoff_counts.get(w, 0)]
        for w in all_weeks
    }


def get_user_client_limits(user, tenure_limit=90):
    props = user.setdefault("properties", {})
    date_today = sydney_today()
    props["client_limit_monthly"] = 6  # monthly

    start_date_str = props.get("adviser_start_date")
    pod_type = props.get("pod_type")

    try:
        if start_date_str:
            start_date = datetime.fromisoformat(start_date_str).date()
            # Adjust capacity for tenure/pod type
            if ((date_today - start_date).days < tenure_limit) or (pod_type == "Solo Adviser"):
                props["client_limit_monthly"] = 4

            # Compute earliest allocation week for future starters
            if start_date > date_today:
                availability_date = start_date - timedelta(weeks=PRESTART_WEEKS)
                user["availability_start_week"] = week_monday_ordinal(availability_date)
            else:
                user["availability_start_week"] = None
    except Exception as e:
        logging.warning(f"Failed to parse adviser_start_date '{start_date_str}': {e}")
        user["availability_start_week"] = None

    return _apply_capacity_overrides(user)


def classify_leave_weeks(leave_requests):
    """
    Classifies the weeks for a list of leave requests as 'Full' or 'Partial'.

    Args:
        leave_requests (list): A list of dictionaries, where each dict is a leave request.

    Returns:
        list: A flattened list of lists, with each inner list containing the
              week number and its classification.
    """
    all_classified_weeks = []
    for request in leave_requests:
        start_date_str = request.get("start_date")
        end_date_str = request.get("end_date")

        if not start_date_str or not end_date_str:
            continue

        try:
            start = datetime.fromisoformat(start_date_str).date()
            end = datetime.fromisoformat(end_date_str).date()
        except ValueError:
            continue

        current_date = start
        weeks_activity = {}

        while current_date <= end:
            if 0 <= current_date.weekday() <= 4:
                week_key = week_monday_ordinal(current_date)
                weeks_activity[week_key] = weeks_activity.get(week_key, 0) + 1

            current_date += timedelta(days=1)

        # Classify each week and append to the main list
        for week_key, days_count in weeks_activity.items():
            classification = "Full" if days_count == 5 else f"Partial: {days_count}"
            all_classified_weeks.append([week_key, classification])

    return all_classified_weeks


def get_merged_schedule(user):
    """
    Merges classified leave weeks and classified deals into a single data dictionary,
    filling in missing week numbers with default values.
    """
    classified_weeks = user["leave_requests_list"]
    global_weeks = user.get("global_closure_weeks", [])
    classified_deals = user["deals_no_clarify_list"]
    data_dict = user["meeting_count_list"]

    # Create maps for fast lookup of classified weeks
    def classification_days(c: str) -> int:
        if c == "Full":
            return 5
        if not c or c == "No":
            return 0
        try:
            match = re.search(r"(\d+)", str(c))
            if match:
                return int(match.group(1))
        except Exception:
            return 0
        return 0

    def combine_classification(a: str, b: str) -> str:
        days = max(classification_days(a), classification_days(b))
        if days >= 5:
            return "Full"
        if days > 0:
            return f"Partial: {days}"
        return "No"

    classified_weeks_map: Dict[int, str] = {}
    for week_num, classification in classified_weeks:
        existing = classified_weeks_map.get(week_num, "No")
        classified_weeks_map[week_num] = combine_classification(existing, classification)

    global_weeks_map: Dict[int, str] = {}
    for week_num, classification in global_weeks:
        existing = global_weeks_map.get(week_num, "No")
        global_weeks_map[week_num] = combine_classification(existing, classification)

    # Step 1: Merge classified leave weeks into data_dict
    for week_num, values in data_dict.items():
        base_cls = classified_weeks_map.get(week_num, "No")
        glob_cls = global_weeks_map.get(week_num, "No")
        classification = combine_classification(base_cls, glob_cls)
        data_dict[week_num].append(classification)

    # Step 2: Add new entries for weeks that are in classified_weeks but not in data_dict
    for week_num, classification in classified_weeks_map.items():
        if week_num not in data_dict:
            # We add a 'No' for the deals classification as it's not available here
            glob_cls = global_weeks_map.get(week_num, "No")
            data_dict[week_num] = [0, 0, combine_classification(classification, glob_cls)]
    # Also add entries for global-only weeks
    for week_num, gclassification in global_weeks_map.items():
        if week_num not in data_dict:
            data_dict[week_num] = [0, 0, combine_classification("No", gclassification)]
            # Since classified_deals is now a dictionary, it's simpler to handle the merge separately below.

    # Step 3: Merge classified deals (now a dictionary of counts) into the data_dict
    # First, process the weeks that are already in data_dict
    for week_num, values in data_dict.items():
        # Retrieve the count for the week from the classified_deals dictionary
        # The .get() method is used to return a default of [0] if the key is not found
        deals_count_list = classified_deals.get(week_num, [0])

        # add 2 weeks to agreement start date
        values.append(deals_count_list[0])

    # Step 4: Add new entries for weeks that are in classified_deals but not in data_dict
    for week_num, count_list in classified_deals.items():
        if week_num not in data_dict:
            # Create a new entry with default values for other columns
            data_dict[week_num] = [0, 0, "No", count_list[0]]

    user["merged_schedule"] = data_dict
    return user


def process_weekly_data(data):
    """
    Rolls up values for all weeks prior to the current week into a single
    entry at key (current_week_monday_ordinal - 7), then removes the older keys.
    """
    sum_of_values = 0
    keys_to_remove = []
    current_week_key = week_monday_ordinal(sydney_today())

    # Iterate through the dictionary to find keys to remove and sum their values
    for key, value_list in data.items():
        if key < current_week_key:
            sum_of_values += sum(value_list)
            keys_to_remove.append(key)

    # Add the new key-value pair to the dictionary
    data[current_week_key - 7] = [sum_of_values]

    # Remove the summed keys from the dictionary
    for key in keys_to_remove:
        del data[key]

    return data


def get_deals_no_clarify(user_email):
    # Use Sydney timezone for current time
    current_sydney_time = sydney_now()
    current_timestamp = current_sydney_time.timestamp()
    # now = int(time.time() * 1000) # convert https://currentmillis.com/
    two_weeks_ago = current_sydney_time - timedelta(weeks=4)
    two_weeks_ago_timestamp = int(two_weeks_ago.timestamp() * 1000)

    url = "https://api.hubapi.com/crm/v3/objects/deals/search"
    data = {
        "filterGroups": [
            {
                "filters": [
                    {
                        "propertyName": "pipeline",
                        "operator": "EQ",
                        "value": "152417162",  # client onboarding pipeline
                    },
                    {
                        "propertyName": "dealstage",
                        "operator": "IN",
                        "values": ["257144337", "257144338", "257144339"]
                    },
                    # {
                    #     "propertyName": "agreement_start_date",
                    #     "operator": "GTE",
                    #     "value": f"{two_weeks_ago_timestamp}",
                    # },
                    {
                        "propertyName": "most_recent_clarify_booked_date",
                        "operator": "NOT_HAS_PROPERTY",
                    },
                    {
                        "propertyName": "most_recent_clarify_call_date",
                        "operator": "NOT_HAS_PROPERTY",
                    },
                    {
                        "propertyName": "advisor_email",
                        "operator": "EQ",
                        "value": f"{user_email}",
                    },
                ]
            }
        ],
        "properties": [
            "advisor_email",
            "hubspot_owner_id",
            "agreement_start_date",
            "hs_next_meeting_id",
            "hs_next_meeting_name",
            "hs_next_meeting_start_time",
            "dealname",
            "most_recent_clarify_booked_date",
            "most_recent_clarify_call_date",
        ],
        "limit": 100,
    }

    if not HUBSPOT_TOKEN:
        raise RuntimeError("HUBSPOT_TOKEN is not configured")
    
    session = create_requests_session()
    try:
        response = session.post(url, headers=HEADERS, json=data, timeout=30)
        response.raise_for_status()
        return response.json()["results"]
    except requests.exceptions.RequestException as e:
        logging.warning(f"Failed to fetch deals without clarify for user {user_email}: {e}")
        return []  # Fallback to empty results
    finally:
        session.close()


def classify_deals_list(data):
    # Create an empty dictionary to store the results
    week_counts = {}

    # Iterate through the data to count items by week number
    for item in data:
        # Get the agreement_start_date from the nested dictionary
        date_str = item.get("properties", {}).get("agreement_start_date")

        if date_str:
            # Parse the date string and get the ISO week number
            date_obj = datetime.strptime(date_str, "%Y-%m-%d").date()
            week_number = week_monday_ordinal(date_obj)

            # Increment the count for the corresponding week
            week_counts[week_number] = week_counts.get(week_number, 0) + 1

    # Format the output to match the requested sample result
    week_counts_result = {week: [count] for week, count in week_counts.items()}
    final_counts = process_weekly_data(week_counts_result)

    return final_counts


def compute_capacity(user, min_week):
    """
    change numbers to names ex. leave_col = 2
    """

    data_dict = user["merged_schedule"]

    # --- Step 1: Fill in missing week keys and create a complete dictionary ---
    # Keys are Monday ordinals; step by 7 days
    max_week = max(data_dict.keys())
    # Ensure we have a forward projection horizon to avoid premature termination
    # when searching for earliest week (e.g., project 52 weeks ahead of min_week)
    desired_max_week = max(max_week, min_week + 26 * 7)
    complete_data_dict = {}

    for week_num in range(min_week, desired_max_week + 1, 7):
        if week_num in data_dict:
            complete_data_dict[week_num] = data_dict[week_num].copy()
        else:
            complete_data_dict[week_num] = [0, 0, "No", 0]

    # Sort the keys to ensure calculations are done in chronological order
    sorted_weeks = sorted(complete_data_dict.keys())

    # --- Step 2: Add one more column for target capacity ---
    HIGH = {"3", "4"}
    LOW = {"1", "2"}
    for week in sorted_weeks:
        raw_status = complete_data_dict[week][2]
        if isinstance(raw_status, str):
            status_value = raw_status.strip()
        else:
            status_value = "No"
        if not status_value:
            status_value = "No"

        target_capacity = weekly_capacity_target(user, week)
        half_capacity = ceil_div(target_capacity, 2)

        curr_has_high = any(ch in status_value for ch in HIGH)
        curr_has_low = any(ch in status_value for ch in LOW)

        status_lower = status_value.lower()
        partial_days = None

        if status_lower.startswith("partial"):
            try:
                partial_days = int(str(status_value).split(":")[1].strip())
            except Exception:
                partial_days = None

        if status_lower == "no":
            current_value_capacity = target_capacity
        elif status_lower == "full":
            current_value_capacity = 0
        elif partial_days is not None:
            current_value_capacity = half_capacity if partial_days in (3, 4) else target_capacity
        else:
            current_value_capacity = half_capacity if (curr_has_high or curr_has_low) else target_capacity

        complete_data_dict[week].append(int(current_value_capacity))

    # --- Step 3: Add one column (cumulative sum of 1st column and the week before it) ---
    cumulative_sum_count = 0
    complete_data_dict[sorted_weeks[0]].append(complete_data_dict[sorted_weeks[0]][0])

    for week_num in sorted_weeks[1:]:
        if complete_data_dict[week_num][2] == "Full":
            complete_data_dict[week_num].append(0)
        else:
            value_from_first_column = complete_data_dict[week_num][0]
            prev_week = week_num - 7

            # Walk back until we find a non-Full week or run out
            while prev_week in complete_data_dict and complete_data_dict[prev_week][2] == "Full":
                prev_week -= 7

            if prev_week in complete_data_dict:
                cumulative_sum_count = (
                    value_from_first_column + complete_data_dict[prev_week][0]
                )
            else:
                cumulative_sum_count = value_from_first_column

            complete_data_dict[week_num].append(cumulative_sum_count)

    # --- Step 4: Add the last column (subtracting actual capacity from target capacity) ---
    for values in complete_data_dict.values():
        result = int(values[ACTUAL_CAPACITY_COL] - values[TARGET_CAPACITY_COL])
        values.append(result)

    user["capacity"] = complete_data_dict

    return user


def find_earliest_week(user, min_week, agreement_start_date=None):
    """Compute the earliest week an adviser can take a new client.

    Maintains backlog of deals without Clarify and a slack_debt representing
    capacity overuse. Weekly spare (target - clarifies(prev+curr)) services
    slack_debt first, then reduces backlog. Requires two consecutive negative
    differences before confirming the earliest week.
    
    Args:
        user: User object with capacity data
        min_week: Baseline week for capacity calculations
        agreement_start_date: Optional datetime for minimum agreement start constraint
    """
    user_name = user['properties']['hs_email'].split('@')[0].replace('.', ' ').title()
    logger.info("Finding earliest week for %s", user_name)
    now_week = week_monday_ordinal(sydney_today())
    min_allowed_week = now_week + FORTNIGHT_DAYS  # must be at least 2 weeks out
    
    # Consider agreement start date as additional constraint
    agreement_start_week = None
    agreement_allocation_week = None
    if agreement_start_date:
        if isinstance(agreement_start_date, datetime):
            agreement_start_week = week_monday_ordinal(agreement_start_date.date())
        elif hasattr(agreement_start_date, 'date'):
            agreement_start_week = week_monday_ordinal(agreement_start_date.date())
        if agreement_start_week is not None:
            agreement_allocation_week = agreement_start_week + 7
            allocation_label = week_label_from_ordinal(agreement_allocation_week)
            logger.info(
                "Agreement start constraint for %s: %s (allocations from %s)",
                user_name,
                week_label_from_ordinal(agreement_start_week),
                allocation_label,
            )
    
    # Always start searching at or after the minimum allowed week and agreement start week
    starting_week = max(min_week, min_allowed_week)
    if agreement_allocation_week:
        starting_week = max(starting_week, agreement_allocation_week)

    data = user["capacity"]
    sorted_weeks = sorted(data.keys())

    deal_no_clarify_delay = FORTNIGHT_DAYS  # shift deal start by a fortnight before counting towards backlog

    # Choose the first week >= starting_week to begin evaluation
    starting_index = _first_index_at_or_after(sorted_weeks, starting_week)

    # If there is no capacity data at or after the starting week
    if starting_index is None:
        if not sorted_weeks:
            user["earliest_open_week"] = starting_week
            return user
        # fall back to the last projected week and extend using fortnightly target
        last_week = sorted_weeks[-1]

        # assume at least one fortnight needed
        user["earliest_open_week"] = max(last_week + 14, starting_week)
        return user

    baseline_week = sorted_weeks[starting_index]

    # Backlog before baseline: deals without clarify from weeks before the baseline week
    remaining_backlog = sum(
        v[DEALS_NO_CLARIFY_COL] for k, v in data.items() if k < baseline_week - deal_no_clarify_delay
    )

    # Initialize overflow to 0 baseline - 14 days
    # Overflow is clarify count - target capacity / 2, we remove 1  per capacity until less than 0
    remaining_backlog += max(_get_col(data, baseline_week - 21, CLARIFY_COL, 0) - _get_col(data, baseline_week - 21, TARGET_CAPACITY_COL, 0) / 2, 0)
    # overflow for baseline week - 14 days
    remaining_backlog += _get_col(data, baseline_week - 14, CLARIFY_COL, 0) - _get_col(data, baseline_week - 14, TARGET_CAPACITY_COL, 0) / 2
    # overflow for baseline week - 7 days
    remaining_backlog += _get_col(data, baseline_week - 7, CLARIFY_COL, 0) - _get_col(data, baseline_week - 7, TARGET_CAPACITY_COL, 0) / 2

    logger.debug(
        "Baseline week for %s: %s (initial backlog %.2f)",
        user_name,
        week_label_from_ordinal(baseline_week),
        remaining_backlog,
    )
    # Walk forward in non-overlapping fortnights: accumulate new deals for two weeks,
    # then consume using fortnight spare (target - clarifies(prev+curr)).
    fortnight_target = weekly_capacity_target(user, baseline_week)
    if fortnight_target <= 0:
        fortnight_target = 1

    backlog_assigned_curr = 0
    backlog_assigned_prev = 0

    clarify_accum = sum(
        v[CLARIFY_COL] for k, v in data.items() if k < baseline_week
    )
    target_accum = sum(
        v[TARGET_CAPACITY_COL] for k, v in data.items() if k <  baseline_week
    ) / 2

    logger.debug(
        "Starting accumulators for %s -> clarify %.2f target %.2f",
        user_name,
        clarify_accum,
        target_accum,
    )
    for idx, wk in enumerate(sorted_weeks[starting_index:]):
        # Check if this week has Full OOO status - skip entirely if so
        if _is_full_ooo_week(data, wk):
            logger.debug("Week %s skipped for %s (Full OOO)", week_label_from_ordinal(wk), user_name)
            continue
        
        # starts at baseline_week current + 14 days
        # Add new deals for this week into the pending fortnight block
        new_deals = _get_col(data, wk - deal_no_clarify_delay, DEALS_NO_CLARIFY_COL, 0)
        remaining_backlog += new_deals

        # Only evaluate consumption at the end of each 2-week block
        # if idx % 2 == 1:
        prev_wk = _find_prev_non_full_ooo_week(data, sorted_weeks, wk)
        clarify_curr = _get_col(data, wk, CLARIFY_COL, 0)
        clarify_prev = _get_col(data, prev_wk, CLARIFY_COL, 0)


        # Use the current week's target as the fortnight target reference (matches how 'difference' is computed)
        block_target = _get_col(data, wk, TARGET_CAPACITY_COL, weekly_capacity_target(user, wk))
        capacity_this_week = block_target - clarify_prev - clarify_curr - backlog_assigned_prev
        
        # Find the next non-Full OOO week for capacity calculation
        next_available_week = _find_next_non_full_ooo_week(data, sorted_weeks, wk + 7)
        capacity_next_week = -_get_col(data, next_available_week, DIFFERENCE_COL, 0)

        actual_capacity_this_week = min(capacity_this_week, capacity_next_week)

        week_limit = WEEKLY_HARD_LIMIT - clarify_curr
        diff_curr = _get_col(data, wk, DIFFERENCE_COL, 0)
        diff_next = _get_col(data, next_available_week, DIFFERENCE_COL, 0)
        diff_prev = _get_col(data, prev_wk, DIFFERENCE_COL, 0)
    
        backlog_assigned_curr = max(min(min(max(min(actual_capacity_this_week, week_limit), 0), remaining_backlog), max(-diff_prev, -diff_next)), 0)
        
        remaining_backlog -= backlog_assigned_curr 
        backlog_assigned_prev = backlog_assigned_curr

        final_capacity_curr = actual_capacity_this_week - backlog_assigned_curr

        # overflow computation added to remaining backlog
        remaining_backlog += max(clarify_curr, backlog_assigned_curr) - block_target / 2

        target_accum += block_target / 2
        clarify_accum += clarify_curr + new_deals

        logger.debug(
            "Week %s (%s): new deals=%s clarify_prev=%s clarify_curr=%s target=%s capacity=%.2f assigned=%.2f backlog=%.2f",
            week_label_from_ordinal(wk),
            user_name,
            new_deals,
            clarify_prev,
            clarify_curr,
            block_target,
            final_capacity_curr,
            backlog_assigned_curr,
            remaining_backlog,
        )
        logger.debug(
            "Accumulators for %s -> clarify %.2f target %.2f",
            user_name,
            clarify_accum,
            target_accum,
        )
        if remaining_backlog <= 0 and final_capacity_curr > 0.5 and target_accum > clarify_accum:
            candidate = max(wk, min_allowed_week)
            if agreement_allocation_week:
                candidate = max(candidate, agreement_allocation_week)
            
            user["earliest_open_week"] = candidate
            logger.info(
                "Earliest week found for %s: %s (capacity %.1f)",
                user_name,
                week_label_from_ordinal(candidate),
                final_capacity_curr,
            )
            return user
            

    # If backlog still remains after projected weeks, include any pending block deals
    last_week = sorted_weeks[-1]
    fortnights_needed = int(ceil_div(max(remaining_backlog, 0), fortnight_target))
    final_week = max(last_week + FORTNIGHT_DAYS * fortnights_needed, min_allowed_week)
    if agreement_allocation_week:
        final_week = max(final_week, agreement_allocation_week)
    
    # Try to find the first 2-week pair with negative differences at/after final_week
    sorted_weeks = sorted(data.keys())
    start_idx = _first_index_at_or_after(sorted_weeks, final_week)
    chosen = None
    if start_idx is not None:
        for i in range(start_idx, len(sorted_weeks)):
            wk = sorted_weeks[i]
            prev_wk = _prev_week(wk)
            diff_curr = _get_col(data, wk, DIFFERENCE_COL, 0)
            diff_prev = _get_col(data, prev_wk, DIFFERENCE_COL, 0)
            if diff_prev < 0 and diff_curr < 0:
                chosen = wk
                break
    
    result = chosen if chosen else final_week
    if agreement_allocation_week:
        result = max(result, agreement_allocation_week)
    user["earliest_open_week"] = result
    logger.info(
        "Final earliest week for %s: %s",
        user_name,
        week_label_from_ordinal(user['earliest_open_week']),
    )
    return user


def display_data(data):
    # Define the table headers
    headers = [
        "Week",
        "Clarify Count",
        "Kick Off Count",
        "OOO",
        "Deal No Clarify",
        "Target",
        "Actual",
        "Difference",
    ]

    # Get a sorted list of week numbers to display the data chronologically
    sorted_weeks = sorted(data.keys())

    # Determine column widths for consistent formatting
    column_widths = [len(header) for header in headers]
    for week in sorted_weeks:
        row_data = [week_label_from_ordinal(week)] + [str(item) for item in data[week]]
        for i, item in enumerate(row_data):
            if len(item) > column_widths[i]:
                column_widths[i] = len(item)

    lines = []
    header_row = " | ".join(
        header.ljust(width) for header, width in zip(headers, column_widths)
    )
    lines.append(header_row)

    separator = "-|-".join("-" * width for width in column_widths)
    lines.append(separator)

    for week in sorted_weeks:
        row_data = [week_label_from_ordinal(week)] + [str(item) for item in data[week]]
        data_row = " | ".join(
            item.ljust(width) for item, width in zip(row_data, column_widths)
        )
        lines.append(data_row)

    logger.debug("Capacity table:\n%s", "\n".join(lines))


def get_user_ids_adviser():
    url = (
        "https://api.hubapi.com/crm/v3/objects/users"
        "?properties=taking_on_clients,hs_email,hubspot_owner_id,adviser_start_date,"
        "pod_type,client_types,household_type&limit=100"
    )
    if not HUBSPOT_TOKEN:
        raise RuntimeError("HUBSPOT_TOKEN is not configured")
    
    session = create_requests_session()
    
    try:
        logger.info("Loading HubSpot users")
        response = session.get(url, headers=HEADERS, timeout=30)
        response.raise_for_status()
        users = response.json().get("results", [])
        logger.info("Loaded %d HubSpot users", len(users))
        return users
        
    except requests.exceptions.ConnectionError as e:
        error_msg = f"Failed to connect to HubSpot API. Please check your internet connection and try again. Details: {str(e)}"
        logger.error("HubSpot connection error: %s", error_msg)
        raise RuntimeError(error_msg)
        
    except requests.exceptions.Timeout as e:
        error_msg = f"HubSpot API request timed out. Please try again. Details: {str(e)}"
        logger.error("HubSpot timeout: %s", error_msg)
        raise RuntimeError(error_msg)
        
    except requests.exceptions.HTTPError as e:
        error_msg = f"HubSpot API returned an error: {e.response.status_code} - {e.response.text}"
        logger.error("HubSpot HTTP error: %s", error_msg)
        raise RuntimeError(error_msg)
        
    except requests.exceptions.RequestException as e:
        error_msg = f"An error occurred while connecting to HubSpot API: {str(e)}"
        logger.error("HubSpot request error: %s", error_msg)
        raise RuntimeError(error_msg)
        
    except Exception as e:
        error_msg = f"An unexpected error occurred: {str(e)}"
        logger.error("HubSpot unexpected error: %s", error_msg)
        raise RuntimeError(error_msg)
    
    finally:
        session.close()


def get_adviser(service_package, agreement_start_date=None, household_type=None):
    logger.info("Adviser allocation started for service package %s", service_package)
    if agreement_start_date:
        agreement_date = datetime.fromtimestamp(int(agreement_start_date) / 1000, tz=SYDNEY_TZ).date()
        logger.info("Agreement start date provided: %s", agreement_date.isoformat())
    else:
        logger.info("Agreement start date not provided")

    logger.info("Finding eligible advisers for package %s", service_package)
    service_lower = (service_package or "").strip().lower()
    household_lower = (household_type or "").strip().lower()
    users = get_user_ids_adviser()
    users_list = []

    for user in users:
        props = user.get("properties") or {}
        if props.get("taking_on_clients") != "True":
            continue
        client_types_raw = props.get("client_types") or ""
        client_types = _normalized_set(client_types_raw)
        if service_lower not in client_types:
            continue

        # Filter by household type if the service package requires it
        if should_filter_by_household(service_lower) and household_lower:
            # For service packages that require household filtering (Series A, Seed)
            # the adviser must support the specific household type of this deal
            adviser_household_norms = _normalized_set(props.get("household_type") or "")

            # If adviser has household preferences, the deal's household type must be in them
            if adviser_household_norms and household_lower not in adviser_household_norms:
                # Adviser doesn't support this specific household type for this deal
                continue

        users_list.append(user)

    logger.info("Advisers eligible for %s (household=%s): %d", service_package, household_lower or "<any>", len(users_list))

    # Load global closures once
    global_closures = classify_leave_weeks(get_global_closures_from_firestore())

    # Convert agreement_start_date from milliseconds to week ordinal
    agreement_start_week = None
    agreement_allocation_week = None
    if agreement_start_date:
        agreement_start_week = week_monday_ordinal(
            datetime.fromtimestamp(int(agreement_start_date) / 1000, tz=SYDNEY_TZ).date()
        )
        if agreement_start_week is not None:
            agreement_allocation_week = agreement_start_week + 7
            logger.info(
                "Agreement start week %s (allocations from %s)",
                week_label_from_ordinal(agreement_start_week),
                week_label_from_ordinal(agreement_allocation_week),
            )

    for i, user in enumerate(users_list):
        user_email = user["properties"]["hs_email"]
        user_name = user_email.split('@')[0].replace('.', ' ').title()
        logger.info("Processing adviser %d/%d: %s", i + 1, len(users_list), user_email)

        # get user approved leave requests from EH
        employee_id = get_employee_id_from_firestore(user_email)
        if not employee_id:
            employee_leaves = []
        else:
            employee_leaves = get_employee_leaves_from_firestore(employee_id)
        user["leave_requests"] = employee_leaves

        # get week number of approved leave requests
        user["leave_requests_list"] = classify_leave_weeks(user["leave_requests"])

        # get user limit, 6 or 4 depending on some details
        user = get_user_client_limits(user)

        # get meeting details
        timestamp_milliseconds = get_monday_from_weeks_ago(n=1)
        min_week = week_monday_ordinal(
            datetime.fromtimestamp((timestamp_milliseconds / 1000), tz=SYDNEY_TZ).date()
        )

        user = get_user_meeting_details(user, timestamp_milliseconds)

        # get clarify meeting counts
        user_meetings = (user.get("meetings") or {}).get("results", [])
        user["meeting_count_list"] = get_meeting_count(user_meetings)

        # get deals with no clarify for each user
        user["deals_no_clarify"] = get_deals_no_clarify(user_email)

        # classify deals with no clarify (get week numbers)
        user["deals_no_clarify_list"] = classify_deals_list(user["deals_no_clarify"])

        # merge meeting counts, leave requests, and global closures
        user["global_closure_weeks"] = global_closures
        user = get_merged_schedule(user)

        # allocate deal to most suitable adviser
        current_week = week_monday_ordinal(sydney_today())
        # Respect future start: allow allocation starting PRESTART_WEEKS before start date
        availability_week = user.get("availability_start_week")
        effective_min_week = max(min_week, availability_week) if availability_week else min_week

        # Display adviser-specific parameters
        logger.info(
            "%s monthly client limit: %s",
            user_email,
            user['properties']['client_limit_monthly'],
        )
        if availability_week:
            availability_date = date.fromordinal(availability_week)
            logger.info(
                "%s availability opens %s (week %s)",
                user_email,
                availability_date.isoformat(),
                week_label_from_ordinal(availability_week),
            )
        logger.debug(
            "%s analysis window: %s -> %s",
            user_email,
            week_label_from_ordinal(min_week),
            week_label_from_ordinal(effective_min_week + 26 * 7),
        )

        user = compute_capacity(user, effective_min_week)

        display_data(user["capacity"])
        
        # Convert agreement_start_date to datetime for find_earliest_week
        agreement_start_datetime = None
        if agreement_start_date:
            agreement_start_datetime = datetime.fromtimestamp(int(agreement_start_date) / 1000, tz=SYDNEY_TZ)
        
        user = find_earliest_week(user, effective_min_week, agreement_start_datetime)

        users_list[i] = user
        logger.info("Completed adviser analysis for %s", user_email)

    if not users_list:
        raise RuntimeError("No eligible advisers found for the requested service package")

    logger.info("Evaluating final adviser selection")

    # Show all advisers and their earliest weeks
    logger.debug("All adviser availability:")
    for user in users_list:
        user_name = user["properties"]["hs_email"].split('@')[0].replace('.', ' ').title()
        wk = user.get('earliest_open_week')
        wk_label = week_label_from_ordinal(wk) if isinstance(wk, int) else str(wk)
        earliest_date = date.fromordinal(wk).strftime('%B %d, %Y') if isinstance(wk, int) else "Unknown"
        logger.debug("  %s -> %s (%s)", user_name, wk_label, earliest_date)

    # Filter advisers whose earliest_open_week is later than or equal to the first allocation week
    if agreement_allocation_week:
        logger.info(
            "Filtering advisers available on/after %s",
            week_label_from_ordinal(agreement_allocation_week),
        )
        eligible_users = []
        for user in users_list:
            earliest_week = user.get("earliest_open_week", float('inf'))
            if isinstance(earliest_week, int) and earliest_week >= agreement_allocation_week:
                eligible_users.append(user)

        if not eligible_users:
            raise RuntimeError("No advisers available starting the week after the agreement start date")

        logger.info("%d advisers meet the agreement start window", len(eligible_users))
        users_list = eligible_users

    # Find the earliest week among remaining advisers
    earliest_week = min(user.get("earliest_open_week", float('inf')) for user in users_list)
    earliest_date = date.fromordinal(earliest_week).strftime('%B %d, %Y') if isinstance(earliest_week, int) else "Unknown"

    # Get all advisers tied for the earliest week
    tied_advisers = [user for user in users_list if user.get("earliest_open_week") == earliest_week]

    logger.info(
        "Earliest availability selected: %s (%s) across %d advisers",
        week_label_from_ordinal(earliest_week),
        earliest_date,
        len(tied_advisers),
    )

    if len(tied_advisers) == 1:
        final_agent = tied_advisers[0]
        agent_name = final_agent["properties"]["hs_email"].split('@')[0].replace('.', ' ').title()
        logger.info("Single adviser available: %s", agent_name)
    else:
        logger.info("Applying workload ratio tiebreaker")

        # Tiebreaker: select adviser with lowest ratio of accumulated clarify counts to target
        def calculate_tiebreaker_ratio(user):
            capacity = user.get("capacity", {})
            if not capacity:
                return 0.0

            total_clarify = 0
            total_target = 0

            # Sum clarify counts and targets from all weeks up to earliest_week
            for week_key, week_data in capacity.items():
                if week_key <= earliest_week:
                    total_clarify += week_data[CLARIFY_COL] if len(week_data) > CLARIFY_COL else 0
                    total_target += week_data[TARGET_CAPACITY_COL] / 2 if len(week_data) > TARGET_CAPACITY_COL else 0

            # Avoid division by zero
            return total_clarify / max(total_target, 1)

        # Select adviser with lowest ratio (most capacity available relative to target)
        min_ratio = min(calculate_tiebreaker_ratio(user) for user in tied_advisers)
        ratio_tied_advisers = [user for user in tied_advisers if abs(calculate_tiebreaker_ratio(user) - min_ratio) < 1e-6]

        logger.debug("Workload ratios (clarify/target):")
        for user in tied_advisers:
            user_name = user["properties"]["hs_email"].split('@')[0].replace('.', ' ').title()
            ratio = calculate_tiebreaker_ratio(user)
            status = ""
            if user in ratio_tied_advisers:
                status = " 🎯 (LOWEST)" if len(ratio_tied_advisers) == 1 else " 🎯 (TIED FOR LOWEST)"
            logger.debug("  %s -> %.3f%s", user_name, ratio, status)

        if len(ratio_tied_advisers) == 1:
            final_agent = ratio_tied_advisers[0]
            agent_name = final_agent["properties"]["hs_email"].split('@')[0].replace('.', ' ').title()
            logger.info("Selected by workload ratio: %s", agent_name)
        else:
            # Final tiebreaker: random selection
            import random
            final_agent = random.choice(ratio_tied_advisers)
            agent_name = final_agent["properties"]["hs_email"].split('@')[0].replace('.', ' ').title()
            logger.info(
                "Random selection resolved tie of %d advisers: %s",
                len(ratio_tied_advisers),
                agent_name,
            )

    logger.info("Allocation complete")
    final_agent_name = final_agent["properties"]["hs_email"].split('@')[0].replace('.', ' ').title()
    final_week_date = date.fromordinal(final_agent.get("earliest_open_week")).strftime('%B %d, %Y')
    logger.info(
        "Selected adviser %s (%s) for week %s (%s)",
        final_agent_name,
        final_agent['properties']['hs_email'],
        week_label_from_ordinal(final_agent.get('earliest_open_week')),
        final_week_date,
    )
    logger.debug("Selected adviser HubSpot owner id: %s", final_agent['properties']['hubspot_owner_id'])

    candidates_summary = []
    for user in users_list:
        props = user.get("properties") or {}
        email = props.get("hs_email") or ""
        earliest_week_value = user.get("earliest_open_week")
        earliest_label = (
            week_label_from_ordinal(earliest_week_value)
            if isinstance(earliest_week_value, int)
            else None
        )
        candidates_summary.append(
            {
                "email": email,
                "name": _pretty_email(email),
                "service_packages": props.get("client_types") or "",
                "household_type": props.get("household_type") or "",
                "earliest_open_week": earliest_week_value,
                "earliest_open_week_label": earliest_label,
            }
        )

    candidates_summary.sort(
        key=lambda c: (
            c["earliest_open_week"]
            if isinstance(c["earliest_open_week"], int)
            else float("inf")
        )
    )

    return final_agent, candidates_summary


def get_users_taking_on_clients():
    """Return all HubSpot users with taking_on_clients == True.

    Includes properties: hs_email, hubspot_owner_id, adviser_start_date, pod_type, client_types.
    """
    url = (
        "https://api.hubapi.com/crm/v3/objects/users"
        "?properties=taking_on_clients,hs_email,hubspot_owner_id,adviser_start_date,"
        "pod_type,client_types,household_type&limit=100"
    )
    if not HUBSPOT_TOKEN:
        raise RuntimeError("HUBSPOT_TOKEN is not configured")
    
    session = create_requests_session()
    try:
        response = session.get(url, headers=HEADERS, timeout=30)
        response.raise_for_status()
        users = response.json().get("results", [])
        users_list = []
        for user in users:
            props = user.get("properties") or {}
            if props.get("taking_on_clients") == "True":
                users_list.append(user)
        return users_list
    except requests.exceptions.RequestException as e:
        error_msg = f"Failed to load advisers taking on clients: {str(e)}"
        logger.error("HubSpot adviser load error: %s", error_msg)
        raise RuntimeError(error_msg)
    finally:
        session.close()


def get_users_earliest_availability(agreement_start_date=None, include_no=True):
    """
    Compute earliest available week for all advisers taking on clients.
    
    Args:
        agreement_start_date (datetime, optional): Start date for the agreement.
            If None, defaults to Sydney now time.
        include_no (bool, optional): If True, includes advisers not taking on clients.
            If False, only computes for advisers who are taking on clients.

    Returns a list of concise dicts per user suitable for API output.
    """
    logging.info("Starting earliest availability computation (include_no=%s)", include_no)
    # Use the helper that already filters to advisers taking on clients
    users = get_user_ids_adviser()
    logging.info("Fetched %d HubSpot users for availability check", len(users))
    # Filter advisers based on include_no parameter BEFORE computation
    users_list = []
    for user in users:
        props = user.get("properties") or {}
        taking_on_clients_raw = props.get("taking_on_clients")
        
        # Skip advisers with blank/null taking_on_clients values
        if taking_on_clients_raw is None or str(taking_on_clients_raw).strip() == "":
            continue
            
        taking_on_clients = str(taking_on_clients_raw).lower() == "true"
        
        if include_no:
            # Include all advisers with non-blank taking_on_clients (both True and False)
            users_list.append(user)
        else:
            # Only include advisers who are taking on clients
            if taking_on_clients:
                users_list.append(user)
    logging.info("Advisers after filtering: %d", len(users_list))
    
    results = []

    # Use provided agreement_start_date or default to Sydney now
    if agreement_start_date is None:
        agreement_start_date = sydney_now()
    elif not hasattr(agreement_start_date, 'tzinfo') or agreement_start_date.tzinfo is None:
        # If naive datetime, assume it's in Sydney timezone
        agreement_start_date = agreement_start_date.replace(tzinfo=SYDNEY_TZ)
    elif agreement_start_date.tzinfo != SYDNEY_TZ:
        # If timezone-aware but not Sydney, convert to Sydney
        agreement_start_date = agreement_start_date.astimezone(SYDNEY_TZ)

    # Establish baseline week using current date (1 week ago Monday) - not agreement start date
    # This ensures display weeks are consistent regardless of future agreement dates
    timestamp_milliseconds = get_monday_from_weeks_ago(n=1)
    min_week = week_monday_ordinal(
        datetime.fromtimestamp((timestamp_milliseconds / 1000), tz=SYDNEY_TZ).date()
    )

    # Load global closures once for this computation
    global_closures = classify_leave_weeks(get_global_closures_from_firestore())

    for idx, user in enumerate(users_list, start=1):
        try:
            user_email = user["properties"].get("hs_email")
            logging.info("Processing adviser %d/%d: %s", idx, len(users_list), user_email)
            # Pull EH leave (from Firestore cache if available)
            employee_id = get_employee_id_from_firestore(user_email)
            employee_leaves = get_employee_leaves_from_firestore(employee_id) if employee_id else []
            user["leave_requests"] = employee_leaves
            logging.debug("  Leave records retrieved: %d", len(employee_leaves))

            # Classify leave weeks
            user["leave_requests_list"] = classify_leave_weeks(user["leave_requests"])

            # Limits and availability window (pre-start weeks)
            user = get_user_client_limits(user)

            # Meetings since baseline
            user = get_user_meeting_details(user, timestamp_milliseconds)
            user_meetings = (user.get("meetings") or {}).get("results", [])
            user["meeting_count_list"] = get_meeting_count(user_meetings)
            logging.debug("  Meetings retrieved: %d", len(user_meetings))

            # Deals without Clarify
            user["deals_no_clarify"] = get_deals_no_clarify(user_email)
            user["deals_no_clarify_list"] = classify_deals_list(user["deals_no_clarify"])
            logging.debug("  Deals without clarify: %d", len(user["deals_no_clarify"]))

            # Merge into schedule (include global closures) and compute capacity
            user["global_closure_weeks"] = global_closures
            user = get_merged_schedule(user)

            availability_week = user.get("availability_start_week")
            effective_min_week = max(min_week, availability_week) if availability_week else min_week
            user = compute_capacity(user, effective_min_week)
            user = find_earliest_week(user, effective_min_week, agreement_start_date)

            earliest_wk = user.get("earliest_open_week")
            logging.info("Finished adviser %s: earliest week %s", user_email, week_label_from_ordinal(earliest_wk) if isinstance(earliest_wk, int) else "n/a")
            results.append({
                "email": user["properties"].get("hs_email"),
                "pod_type": user["properties"].get("pod_type"),
                "service_packages": (user["properties"].get("client_types") or ""),
                "hubspot_owner_id": user["properties"].get("hubspot_owner_id"),
                "client_limit_monthly": user["properties"].get("client_limit_monthly"),
                "taking_on_clients": user["properties"].get("taking_on_clients"),
                "household_type": user["properties"].get("household_type"),
                "availability_start_week": user.get("availability_start_week"),
                "earliest_open_week": earliest_wk,
                "earliest_open_week_label": week_label_from_ordinal(earliest_wk) if isinstance(earliest_wk, int) else None,
            })
        except Exception as e:
            # Collect error per user but continue with others
            results.append({
                "email": user.get("properties", {}).get("hs_email"),
                "service_packages": (user.get("properties", {}).get("client_types") or ""),
                "pod_type": user.get("properties", {}).get("pod_type"),
                "hubspot_owner_id": user.get("properties", {}).get("hubspot_owner_id"),
                "household_type": user.get("properties", {}).get("household_type"),
                "error": str(e),
            })

    return results


def get_user_by_email(user_email: str):
    """Return HubSpot user object for the given email among those taking on clients."""
    users = get_user_ids_adviser()
    for u in users:
        if (u.get("properties") or {}).get("hs_email") == user_email:
            return u
    return None


def compute_user_schedule_by_email(user_email: str, agreement_start_date=None):
    """Build and return an adviser's weekly capacity table and earliest week.
    
    Args:
        user_email (str): Email of the user to compute schedule for.
        agreement_start_date (datetime, optional): Start date for the agreement.
            If None, defaults to Sydney now time.

    Returns a dict with keys: 'capacity' (dict keyed by Monday ordinal),
    'earliest_open_week' (int), and 'min_week' (int baseline used).
    """
    logging.info("Computing schedule for %s", user_email)
    user = get_user_by_email(user_email)
    if not user:
        raise ValueError("User not found or not taking on clients")

    # Load EH leave
    employee_id = get_employee_id_from_firestore(user_email)
    employee_leaves = get_employee_leaves_from_firestore(employee_id) if employee_id else []
    user["leave_requests"] = employee_leaves
    logging.debug("  Leave records retrieved: %d", len(employee_leaves))

    # Classify leave weeks
    user["leave_requests_list"] = classify_leave_weeks(user["leave_requests"])

    # Limits and availability window
    user = get_user_client_limits(user)

    # Use provided agreement_start_date or default to Sydney now
    if agreement_start_date is None:
        agreement_start_date = sydney_now()
    elif not hasattr(agreement_start_date, 'tzinfo') or agreement_start_date.tzinfo is None:
        # If naive datetime, assume it's in Sydney timezone
        agreement_start_date = agreement_start_date.replace(tzinfo=SYDNEY_TZ)
    elif agreement_start_date.tzinfo != SYDNEY_TZ:
        # If timezone-aware but not Sydney, convert to Sydney
        agreement_start_date = agreement_start_date.astimezone(SYDNEY_TZ)

    # Establish baseline week using current date (1 week ago Monday) - not agreement start date
    # This ensures display weeks are consistent regardless of future agreement dates
    timestamp_milliseconds = get_monday_from_weeks_ago(n=1)
    min_week = week_monday_ordinal(
        datetime.fromtimestamp((timestamp_milliseconds / 1000), tz=SYDNEY_TZ).date()
    )
    user = get_user_meeting_details(user, timestamp_milliseconds)
    user_meetings = (user.get("meetings") or {}).get("results", [])
    user["meeting_count_list"] = get_meeting_count(user_meetings)
    logging.debug("  Meetings retrieved: %d", len(user_meetings))

    # Deals without Clarify
    user["deals_no_clarify"] = get_deals_no_clarify(user_email)
    user["deals_no_clarify_list"] = classify_deals_list(user["deals_no_clarify"])
    logging.debug("  Deals without clarify: %d", len(user["deals_no_clarify"]))

    # Global closures
    user["global_closure_weeks"] = classify_leave_weeks(get_global_closures_from_firestore())

    # Merge + compute capacity
    user = get_merged_schedule(user)

    availability_week = user.get("availability_start_week")
    effective_min_week = max(min_week, availability_week) if availability_week else min_week
    user = compute_capacity(user, effective_min_week)
    user = find_earliest_week(user, effective_min_week, agreement_start_date)
    logging.info(
        "Computed schedule for %s: earliest week %s",
        user_email,
        week_label_from_ordinal(user.get("earliest_open_week")) if isinstance(user.get("earliest_open_week"), int) else "n/a",
    )
    return {
        "capacity": user.get("capacity", {}),
        "earliest_open_week": user.get("earliest_open_week"),
        "min_week": effective_min_week,
        "email": user_email,
    }

def week_monday_ordinal(d: date) -> int:
    """Return the ordinal of the Monday for the week containing date ``d``.

    Using Monday's ordinal as the week key avoids ISO week rollover issues
    across year boundaries.
    """
    monday = d - timedelta(days=d.weekday())
    return monday.toordinal()


def week_label_from_ordinal(wk: int) -> str:
    """Human-readable label for a week ordinal (YYYY-Www)."""
    monday = date.fromordinal(wk)
    iso_year, iso_week, _ = monday.isocalendar()
    return f"{iso_year}-W{iso_week:02d}"


# Package types that require household type filtering
HOUSEHOLD_RULES = {
    "series a": {"single", "couple"},
    "seed": {"single", "couple"},
    "series c": None,  # Ignore household type
    "ipo": None,  # Ignore household type
}


def should_filter_by_household(package_type: str) -> bool:
    """
    Check if a package type should be filtered by household type.

    Returns True if household type filtering should be applied.
    Returns False if household type should be ignored (e.g., Series C, IPO).

    Args:
        package_type: Normalized (lowercase) package type string

    Returns:
        bool: True if household filtering applies, False if should ignore household type
    """
    if package_type not in HOUSEHOLD_RULES:
        return True  # Default to filtering for unknown types

    rules = HOUSEHOLD_RULES[package_type]
    return rules is not None  # None means ignore household type


def _normalized_set(raw: str) -> set[str]:
    """Split a semi-structured CRM string into normalized lowercase values."""
    return {p.strip().lower() for p in re.split(r"[;,/|]+", raw or "") if p.strip()}


def _format_service_label(token: str) -> str:
    token = (token or "").strip()
    if not token:
        return ""
    lower = token.lower()
    if lower in _SPECIAL_UPPER:
        return token.upper()
    return lower.title()


def _format_household_label(token: str) -> str:
    token = (token or "").strip()
    if not token:
        return ""
    lower = token.lower()
    if lower in _SPECIAL_UPPER:
        return token.upper()
    return lower.title()


def _pretty_email(email: str) -> str:
    local = (email or "").split("@")[0]
    parts = re.split(r"[._-]+", local)
    return " ".join(p.capitalize() for p in parts if p) or (email or "")


def build_service_household_matrix():
    """Return sorted service/household labels with adviser allocations."""
    now = time.time()
    cache = _MATRIX_CACHE
    if cache["timestamp"] and now - cache["timestamp"] < _MATRIX_CACHE_TTL:
        cached_matrix = {
            svc: {hh: list(entries) for hh, entries in hh_map.items()}
            for svc, hh_map in cache["matrix"].items()
        }
        return list(cache["services"]), list(cache["households"]), cached_matrix

    users = get_user_ids_adviser()

    services_map: dict[str, str] = {}
    household_map: dict[str, str] = {}
    all_households_norm: set[str] = set()
    for allowed in HOUSEHOLD_RULES.values():
        if allowed is not None:  # Skip None values (e.g., Series C, IPO)
            all_households_norm.update(allowed)

    adviser_payload = []
    adviser_color_cycle = ["blue", "green", "purple", "orange", "pink", "teal"]
    adviser_color_map: dict[str, str] = {}

    for user in users:
        props = user.get("properties") or {}
        if str(props.get("taking_on_clients")).lower() != "true":
            continue

        email = props.get("hs_email") or ""
        service_norms = _normalized_set(props.get("client_types"))
        if not service_norms:
            continue

        household_norms = _normalized_set(props.get("household_type"))
        all_households_norm.update(household_norms)

        for svc in service_norms:
            services_map.setdefault(svc, _format_service_label(svc))
        for hh in household_norms:
            household_map.setdefault(hh, _format_household_label(hh))

        adviser_payload.append(
            {
                "email": email,
                "name": _pretty_email(email),
                "service_norms": service_norms,
                "household_norms": household_norms,
                "household_raw": props.get("household_type") or "",
                "service_packages": service_norms,  # Store for later filtering
            }
        )

    if not services_map:
        return [], [], {}

    if not household_map and all_households_norm:
        for hh in all_households_norm:
            household_map.setdefault(hh, _format_household_label(hh))

    service_items = sorted(services_map.items(), key=lambda item: item[1].lower())
    household_items = sorted(household_map.items(), key=lambda item: item[1].lower()) if household_map else []

    for hh in sorted(all_households_norm):
        display = household_map.setdefault(hh, _format_household_label(hh))
        if (hh, display) not in household_items:
            household_items.append((hh, display))
    household_items.sort(key=lambda item: item[1].lower())

    matrix = {
        svc_display: {hh_display: [] for _, hh_display in household_items}
        for _, svc_display in service_items
    }

    for adviser in adviser_payload:
        email = adviser["email"]
        name = adviser["name"]
        household_norms = adviser["household_norms"]
        household_raw = adviser["household_raw"]

        for svc_norm, svc_display in service_items:
            if svc_norm not in adviser["service_norms"]:
                continue

            # Check if this service package requires household filtering
            if not should_filter_by_household(svc_norm):
                # No household filtering by service rules, but still respect adviser's household preferences
                # For Series C/IPO: add adviser to columns matching their household preferences
                for hh_norm, hh_display in household_items:
                    # Only add if adviser has no preferences OR their preferences include this household
                    if household_norms and hh_norm not in household_norms:
                        continue

                    bucket = matrix[svc_display][hh_display]
                    colour = adviser_color_map.setdefault(
                        email,
                        adviser_color_cycle[len(adviser_color_map) % len(adviser_color_cycle)],
                    )
                    entry = {"email": email, "name": name, "cls": colour}
                    if entry not in bucket:
                        bucket.append(entry)
            else:
                # Household filtering required - match adviser's household preferences
                allowed_households = HOUSEHOLD_RULES.get(svc_norm)

                for hh_norm, hh_display in household_items:
                    # Check if household type is allowed for this service
                    if allowed_households and hh_norm not in allowed_households:
                        continue
                    # Check if adviser has this household preference
                    # If adviser has no household preferences, include them for all allowed types
                    if household_norms and hh_norm not in household_norms:
                        continue

                    bucket = matrix[svc_display][hh_display]
                    colour = adviser_color_map.setdefault(
                        email,
                        adviser_color_cycle[len(adviser_color_map) % len(adviser_color_cycle)],
                    )
                    entry = {"email": email, "name": name, "cls": colour}
                    if entry not in bucket:
                        bucket.append(entry)

    for svc_display in matrix:
        for hh_display in matrix[svc_display]:
            matrix[svc_display][hh_display].sort(key=lambda item: item["name"].lower())

    services_list = [svc_display for _, svc_display in service_items]
    households_list = [hh_display for _, hh_display in household_items]

    cache["timestamp"] = now
    cache["services"] = list(services_list)
    cache["households"] = list(households_list)
    cache["matrix"] = {
        svc: {hh: list(entries) for hh, entries in hh_map.items()}
        for svc, hh_map in matrix.items()
    }

    return services_list, households_list, matrix
