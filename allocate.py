import os, time, logging
from datetime import datetime, timedelta, date
import requests

from zoneinfo import ZoneInfo

# Weeks before an adviser's start date when they can begin receiving allocations
PRESTART_WEEKS = int(os.environ.get("PRESTART_WEEKS", "3"))


# Optional: persist tokens in Firestore (recommended on App Engine)
USE_FIRESTORE = os.environ.get("USE_FIRESTORE", "true").lower() == "true"
db = None
if USE_FIRESTORE:
    try:
        from google.cloud import firestore
        db = firestore.Client()  # Uses App Engine default credentials
    except Exception as e:
        logging.warning(f"Firestore client init failed in allocate.py: {e}")
        db = None
        USE_FIRESTORE = False


from utils.secrets import get_secret

HUBSPOT_TOKEN = get_secret("HUBSPOT_TOKEN")
HEADERS = {"Authorization": f"Bearer {HUBSPOT_TOKEN}", "Content-Type": "application/json"}


CLARIFY_COL = 0
KICKOFF_COL = 1
LEAVE_COL = 2
DEALS_NO_CLARIFY_COL = 3
TARGET_CAPACITY_COL = 4
ACTUAL_CAPACITY_COL = 5
DIFFERENCE_COL = 6

# Days in a fortnight (used when stepping weeks by ordinals)
FORTNIGHT_DAYS = 14


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


def ceil_div(a, b):
    return -(-a // b)  # integer ceil without math.ceil


def get_employee_leaves_from_firestore(employee_id):
    """
    Queries Firestore to find all leave requests for a given employee ID.
    
    Args:
        employee_id (str): The ID of the employee to search for.
        
    Returns:
        list: A list of dictionaries, where each dictionary is a leave request.
    """
    list_leaves = []
    
    try:
        if not db:
            return []
        # Get the collection reference and create the stream
        leaves_ref = db.collection('employees').document(employee_id).collection('leave_requests')
        docs = leaves_ref.stream()
        
        # Firestore returns a stream of documents, even if only one is expected
        for doc in docs:
            list_leaves.append(doc.to_dict())
            
    except Exception as e:
        # Log the error for internal debugging
        print(f"Firestore query failed: {e}")
        
    return list_leaves


def get_global_closures_from_firestore():
    """
    Fetch global holidays/office closures from Firestore.

    Expected collection: 'office_closures' with documents containing
    at least 'start_date' and 'end_date' as ISO dates (YYYY-MM-DD).

    Returns a list of dicts compatible with classify_leave_weeks.
    """
    items = []
    try:
        if not db:
            return []
        docs = db.collection('office_closures').stream()
        for doc in docs:
            data = doc.to_dict() or {}
            start_date = data.get('start_date')
            end_date = data.get('end_date') or start_date
            if start_date:
                items.append({"start_date": start_date, "end_date": end_date})
    except Exception as e:
        logging.warning(f"Failed to load office closures: {e}")
    return items


def get_employee_id_from_firestore(search_email):
    """
    Queries Firestore to find an employee ID by their company email.
    
    Args:
        search_email (str): The email address to search for.
        
    Returns:
        str: The employee ID if found, otherwise None.
    """
    try:
        if not db:
            return None
        docs = db.collection('employees').where('company_email', '==', search_email).stream()
        
        # We expect a single result, so we can return the first one found.
        for doc in docs:
            return doc.id
        
    except Exception as e:
        # Log the error for internal debugging
        print(f"Firestore query failed: {e}")
        
    return None
    

def get_first_monday_current_month(input_date=None, tz_name="Australia/Sydney") -> int:
    """
    Return the epoch timestamp (milliseconds) for local midnight on the first Monday
    of the month containing `input_date`. If `input_date` is None, use 'now' in tz.
    Accepts a datetime (aware or naive) or a date.
    """
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
    get monday from n weeks ago of input date or today
    """

    # Use provided date or today
    if input_date:
        if isinstance(input_date, datetime):
            today = input_date.date()
        elif isinstance(input_date, date):
            today = input_date
        else:
            raise TypeError("input_date must be a datetime, date, or None")
    else:
        today = datetime.now().date()

    # Calculate the date for the Monday of the current week
    # weekday() returns 0 for Monday, 1 for Tuesday, etc.
    current_week_start = today - timedelta(days=today.weekday())

    # Calculate the date for the Monday of the week two workweeks ago
    two_workweeks_ago_start = current_week_start - timedelta(weeks=n)

    # Convert the date to a datetime object at midnight (start of the day)
    start_of_day_two_workweeks_ago = datetime.combine(
        two_workweeks_ago_start, datetime.min.time()
    )

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
    result = requests.post(url, headers=HEADERS, json=payload, timeout=30)
    result.raise_for_status()
    user["meetings"] = result.json()
    time.sleep(0.005)

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

    # Print the table
    if display_table:
        for row in table_data:
            if row == table_data[0]:  # Print header
                print(f"{row[0]:<15} {row[1]:<16} {row[2]}")
                print("-" * 50)  # Separator
            else:
                print(f"{row[0]:<15} {row[1]:<16} {row[2]}")

    return {
        w: [weekly_clarify_counts.get(w, 0), weekly_kickoff_counts.get(w, 0)]
        for w in all_weeks
    }


def get_user_client_limits(user, tenure_limit=90):
    date_today = date.today()
    user["properties"]["client_limit_monthly"] = 6  # monthly

    start_date_str = (user.get("properties") or {}).get("adviser_start_date")
    pod_type = (user.get("properties") or {}).get("pod_type")

    try:
        if start_date_str:
            start_date = datetime.fromisoformat(start_date_str).date()
            # Adjust capacity for tenure/pod type
            if ((date_today - start_date).days < tenure_limit) or (pod_type == "Solo Adviser"):
                user["properties"]["client_limit_monthly"] = 4

            # Compute earliest allocation week for future starters
            if start_date > date_today:
                availability_date = start_date - timedelta(weeks=PRESTART_WEEKS)
                user["availability_start_week"] = week_monday_ordinal(availability_date)
            else:
                user["availability_start_week"] = None
    except Exception as e:
        logging.warning(f"Failed to parse adviser_start_date '{start_date_str}': {e}")
        user["availability_start_week"] = None

    return user


# def get_user_leave_requests(user_email):
#     url = f"https://pivot-digital-466902.ts.r.appspot.com/get/leave_requests_by_email?email={user_email}"
#     response = requests.get(url)

#     # pprint(user_email)
#     # pprint(response.json())

#     return response.json()["leave_requests"]


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
    classified_weeks_map = {week_num: classification for week_num, classification in classified_weeks}
    global_weeks_map = {week_num: classification for week_num, classification in global_weeks}

    def classification_days(c: str) -> int:
        if c == "Full":
            return 5
        if not c or c == "No":
            return 0
        try:
            # Expect format 'Partial: N'
            return int(str(c).split(":")[1].strip())
        except Exception:
            return 0

    def combine_classification(a: str, b: str) -> str:
        days = max(classification_days(a), classification_days(b))
        if days >= 5:
            return "Full"
        if days > 0:
            return f"Partial: {days}"
        return "No"

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
    current_week_key = week_monday_ordinal(date.today())

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
    current_timestamp = time.time()
    # now = int(time.time() * 1000) # convert https://currentmillis.com/
    two_weeks_ago = datetime.fromtimestamp(current_timestamp) - timedelta(weeks=4)
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
    response = requests.post(url, headers=HEADERS, json=data, timeout=30)
    response.raise_for_status()
    # print(response)
    # pprint(response.json())
    return response.json()["results"]


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

    limit = int(user["properties"]["client_limit_monthly"] / 2)

    # --- Step 1: Fill in missing week keys and create a complete dictionary ---
    # Keys are Monday ordinals; step by 7 days
    max_week = max(data_dict.keys())
    # Ensure we have a forward projection horizon to avoid premature termination
    # when searching for earliest week (e.g., project 52 weeks ahead of min_week)
    desired_max_week = max(max_week, min_week + 52 * 7)
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
    first_week = sorted_weeks[0]
    complete_data_dict[first_week].append(int(limit))

    for week in sorted_weeks[1:]:
        prev_week = week - 7

        # Pull statuses once
        curr_status = complete_data_dict[week][2]
        prev_status = complete_data_dict[prev_week][2]

        # Precompute common quantities once
        half = ceil_div(limit, 2)  # 2 for limit=3
        third = ceil_div(limit, 3)  # 1 for limit=3

        prev_has_high = any(ch in prev_status for ch in HIGH)
        prev_has_low = any(ch in prev_status for ch in LOW)

        curr_has_high = any(ch in curr_status for ch in HIGH)
        curr_has_low = any(ch in curr_status for ch in LOW)

        # Parse Partial:N days if present
        partial_days = None
        if isinstance(curr_status, str) and curr_status.lower().startswith("partial"):
            try:
                partial_days = int(str(curr_status).split(":")[1].strip())
            except Exception:
                partial_days = None

        if curr_status == "No":
            # Full capacity when no OOO
            current_value_capacity = limit
        elif curr_status == "Full":
            current_value_capacity = 0
        elif partial_days is not None:
            # Special handling per fortnight limit
            if limit == 2:
                # Only reduce to 1 when Partial is 3 or 4 days; otherwise keep 2
                current_value_capacity = 1 if partial_days in (3, 4) else 2
            elif limit == 3:
                # Any Partial 1–4 reduces to 2
                current_value_capacity = 2
            else:
                # Fallback to prior behavior (halve capacity on partials)
                current_value_capacity = half
        else:
            # Fallback for other statuses: keep previous behavior
            current_value_capacity = half if (curr_has_high or curr_has_low) else limit

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


def find_earliest_week(user, min_week):
    """Compute the earliest week an adviser can take a new client.

    Maintains backlog of deals without Clarify and a slack_debt representing
    capacity overuse. Weekly spare (target - clarifies(prev+curr)) services
    slack_debt first, then reduces backlog. Requires two consecutive negative
    differences before confirming the earliest week.
    """
    print(f"{user['properties']['hs_email']}")
    now_week = week_monday_ordinal(date.today())
    min_allowed_week = now_week + FORTNIGHT_DAYS  # must be at least 2 weeks out
    # Always start searching at or after the minimum allowed week
    starting_week = max(min_week, min_allowed_week)

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
    print(f"Baseline Week: {week_label_from_ordinal(baseline_week)}, Initial Backlog: {remaining_backlog}")
    # Walk forward in non-overlapping fortnights: accumulate new deals for two weeks,
    # then consume using fortnight spare (target - clarifies(prev+curr)).
    fortnight_target = int((user.get("properties", {}).get("client_limit_monthly") or 0) // 2)
    if fortnight_target <= 0:
        fortnight_target = 1
    pending_deals_block = 0
    prev_slack_debt = 0
    curr_slack_debt = 0

    prev_fortnight_spare = 0
    curr_fortnight_spare = 0

    for idx, wk in enumerate(sorted_weeks[starting_index:]):
        # Add new deals for this week into the pending fortnight block
        new_deals = _get_col(data, wk - deal_no_clarify_delay, DEALS_NO_CLARIFY_COL, 0)
        pending_deals_block += new_deals

        # Only evaluate consumption at the end of each 2-week block
        # if idx % 2 == 1:
        prev_wk = _prev_week(wk)
        clarify_curr = _get_col(data, wk, CLARIFY_COL, 0)
        clarify_prev = _get_col(data, prev_wk, CLARIFY_COL, 0)

        # Use the current week's target as the fortnight target reference (matches how 'difference' is computed)
        block_target = _get_col(data, wk, TARGET_CAPACITY_COL, fortnight_target)

        capacity_next_week = _get_col(data, wk + 7, DIFFERENCE_COL, 0)

        # consider the capacity next week as a ceiling on spare capacity this fortnight
        prev_fortnight_spare = curr_fortnight_spare
        curr_fortnight_spare = max(min(-capacity_next_week, block_target - (clarify_prev + clarify_curr + curr_slack_debt)), 0)

        # First add the pending deals for this block to the backlog, then consume
        remaining_backlog += pending_deals_block
        pending_deals_block = 0

        # slack debt is used to track any used up spare capacity that cannot be applied to backlog
        prev_slack_debt = curr_slack_debt
        curr_slack_debt = min(remaining_backlog, curr_fortnight_spare)
        remaining_backlog = max(0, remaining_backlog - curr_fortnight_spare)
        print(f"Week: {week_label_from_ordinal(wk)}, New Deals: {new_deals}, Clarify Prev: {clarify_prev}, Clarify Curr: {clarify_curr}, Block Target: {block_target}, NWD: {capacity_next_week}, Fortnight Spare: {curr_fortnight_spare}, Remaining Backlog: {remaining_backlog}, Slack Debt: {curr_slack_debt}")
        if remaining_backlog == 0:
            # Enforce condition: two consecutive negative differences (prev and curr)
            diff_curr = _get_col(data, wk, DIFFERENCE_COL, 0) + curr_slack_debt
            diff_prev = _get_col(data, prev_wk, DIFFERENCE_COL, 0) + prev_slack_debt + curr_slack_debt

            print(f"Week: {week_label_from_ordinal(wk)}, Diff Prev: {diff_prev}, Diff Curr: {diff_curr}")
            if diff_prev < 0 and diff_curr < 0:
                if prev_fortnight_spare - prev_slack_debt > 0:
                    earliest_week = prev_wk
                else:
                    earliest_week = wk  # no spare, so only curr week is valid

                candidate = max(earliest_week, min_allowed_week)
                user["earliest_open_week"] = candidate
                print(f"Week: {week_label_from_ordinal(candidate)}")
                return user
            # Otherwise continue scanning subsequent blocks until the condition is met

            prev_slack_debt = 0  # reset slack debt after use
    
    # If backlog still remains after projected weeks, include any pending block deals
    remaining_backlog += pending_deals_block
    last_week = sorted_weeks[-1]
    fortnights_needed = int(ceil_div(max(remaining_backlog, 0), fortnight_target))
    final_week = max(last_week + FORTNIGHT_DAYS * fortnights_needed, min_allowed_week)
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
    user["earliest_open_week"] = chosen if chosen else final_week
    print(f"Week: {week_label_from_ordinal(user['earliest_open_week'])}")
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

    # Print the header row with dynamic spacing
    header_row = " | ".join(
        header.ljust(width) for header, width in zip(headers, column_widths)
    )
    print(header_row)

    # Print the separator line
    separator = "-|-".join("-" * width for width in column_widths)
    print(separator)

    # Print each data row with dynamic spacing
    for week in sorted_weeks:
        row_data = [week_label_from_ordinal(week)] + [str(item) for item in data[week]]
        data_row = " | ".join(
            item.ljust(width) for item, width in zip(row_data, column_widths)
        )
        print(data_row)


def get_user_ids_adviser(service_package):
    url = "https://api.hubapi.com/crm/v3/objects/users?properties=taking_on_clients,hs_email,hubspot_owner_id,adviser_start_date,pod_type,client_types&limit=100"  # include start date and ooo status
    if not HUBSPOT_TOKEN:
        raise RuntimeError("HUBSPOT_TOKEN is not configured")
    response = requests.get(url, headers=HEADERS, timeout=30)
    response.raise_for_status()
    users = response.json().get("results", [])
    users_list = []
    for user in users:
        props = user.get("properties") or {}
        if props.get("taking_on_clients") == "True":
            client_types = props.get("client_types") or ""
            if service_package in client_types:
                users_list.append(user)

    return users_list



def get_adviser(service_package):
    # get users with taking_on_clients on
    print("Getting USER IDs taking on client")
    users_list = get_user_ids_adviser(service_package)

    # Load global closures once
    global_closures = classify_leave_weeks(get_global_closures_from_firestore())

    for i, user in enumerate(users_list):
        print(f"user {i}")
        # get user approved leave requests from EH
        user_email = user["properties"]["hs_email"]
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
            datetime.fromtimestamp((timestamp_milliseconds / 1000)).date()
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
        current_week = week_monday_ordinal(date.today())
        # Respect future start: allow allocation starting PRESTART_WEEKS before start date
        availability_week = user.get("availability_start_week")
        effective_min_week = max(min_week, availability_week) if availability_week else min_week
        user = compute_capacity(user, effective_min_week)

        display_data(user["capacity"])
        print("\n")
        user = find_earliest_week(user, effective_min_week)

        users_list[i] = user
        # Show current and minimum week in YYYY-Www format for clarity
        print(
            week_label_from_ordinal(current_week),
            week_label_from_ordinal(min_week),
        )

    if not users_list:
        raise RuntimeError("No eligible advisers found for the requested service package")
    final_agent = min(users_list, key=lambda user: user.get("earliest_open_week", float('inf')))
    for user in users_list:
        wk = user.get('earliest_open_week')
        wk_label = week_label_from_ordinal(wk) if isinstance(wk, int) else str(wk)
        print(f"{user['properties']['hs_email']} \t Week: {wk_label}")
    print("\n")
    print(f"Earliest open agent: {final_agent['properties']['hs_email']}")

    return final_agent


def get_users_taking_on_clients():
    """Return all HubSpot users with taking_on_clients == True.

    Includes properties: hs_email, hubspot_owner_id, adviser_start_date, pod_type, client_types.
    """
    url = "https://api.hubapi.com/crm/v3/objects/users?properties=taking_on_clients,hs_email,hubspot_owner_id,adviser_start_date,pod_type,client_types&limit=100"
    if not HUBSPOT_TOKEN:
        raise RuntimeError("HUBSPOT_TOKEN is not configured")
    response = requests.get(url, headers=HEADERS, timeout=30)
    response.raise_for_status()
    users = response.json().get("results", [])
    users_list = []
    for user in users:
        props = user.get("properties") or {}
        if props.get("taking_on_clients") == "True":
            users_list.append(user)
    return users_list


def get_users_earliest_availability():
    """
    Compute earliest available week for all advisers taking on clients.

    Returns a list of concise dicts per user suitable for API output.
    """
    users_list = get_users_taking_on_clients()
    results = []

    # Establish baseline week (1 week ago Monday) similar to allocation logic
    timestamp_milliseconds = get_monday_from_weeks_ago(n=1)
    min_week = week_monday_ordinal(
        datetime.fromtimestamp((timestamp_milliseconds / 1000)).date()
    )

    # Load global closures once for this computation
    global_closures = classify_leave_weeks(get_global_closures_from_firestore())

    for user in users_list:
        try:
            # Pull EH leave (from Firestore cache if available)
            user_email = user["properties"].get("hs_email")
            employee_id = get_employee_id_from_firestore(user_email)
            employee_leaves = get_employee_leaves_from_firestore(employee_id) if employee_id else []
            user["leave_requests"] = employee_leaves

            # Classify leave weeks
            user["leave_requests_list"] = classify_leave_weeks(user["leave_requests"])

            # Limits and availability window (pre-start weeks)
            user = get_user_client_limits(user)

            # Meetings since baseline
            user = get_user_meeting_details(user, timestamp_milliseconds)
            user_meetings = (user.get("meetings") or {}).get("results", [])
            user["meeting_count_list"] = get_meeting_count(user_meetings)

            # Deals without Clarify
            user["deals_no_clarify"] = get_deals_no_clarify(user_email)
            user["deals_no_clarify_list"] = classify_deals_list(user["deals_no_clarify"])

            # Merge into schedule (include global closures) and compute capacity
            user["global_closure_weeks"] = global_closures
            user = get_merged_schedule(user)

            availability_week = user.get("availability_start_week")
            effective_min_week = max(min_week, availability_week) if availability_week else min_week
            user = compute_capacity(user, effective_min_week)
            user = find_earliest_week(user, effective_min_week)

            earliest_wk = user.get("earliest_open_week")
            results.append({
                "email": user["properties"].get("hs_email"),
                "pod_type": user["properties"].get("pod_type"),
                "service_packages": (user["properties"].get("client_types") or ""),
                "hubspot_owner_id": user["properties"].get("hubspot_owner_id"),
                "client_limit_monthly": user["properties"].get("client_limit_monthly"),
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
                "error": str(e),
            })

    return results


def get_user_by_email(user_email: str):
    """Return HubSpot user object for the given email among those taking on clients."""
    users = get_users_taking_on_clients()
    for u in users:
        if (u.get("properties") or {}).get("hs_email") == user_email:
            return u
    return None


def compute_user_schedule_by_email(user_email: str):
    """Build and return an adviser's weekly capacity table and earliest week.

    Returns a dict with keys: 'capacity' (dict keyed by Monday ordinal),
    'earliest_open_week' (int), and 'min_week' (int baseline used).
    """
    user = get_user_by_email(user_email)
    if not user:
        raise ValueError("User not found or not taking on clients")

    # Load EH leave
    employee_id = get_employee_id_from_firestore(user_email)
    employee_leaves = get_employee_leaves_from_firestore(employee_id) if employee_id else []
    user["leave_requests"] = employee_leaves

    # Classify leave weeks
    user["leave_requests_list"] = classify_leave_weeks(user["leave_requests"])

    # Limits and availability window
    user = get_user_client_limits(user)

    # Meetings since baseline (1 week ago Monday)
    timestamp_milliseconds = get_monday_from_weeks_ago(n=1)
    min_week = week_monday_ordinal(
        datetime.fromtimestamp((timestamp_milliseconds / 1000)).date()
    )
    user = get_user_meeting_details(user, timestamp_milliseconds)
    user_meetings = (user.get("meetings") or {}).get("results", [])
    user["meeting_count_list"] = get_meeting_count(user_meetings)

    # Deals without Clarify
    user["deals_no_clarify"] = get_deals_no_clarify(user_email)
    user["deals_no_clarify_list"] = classify_deals_list(user["deals_no_clarify"])

    # Global closures
    user["global_closure_weeks"] = classify_leave_weeks(get_global_closures_from_firestore())

    # Merge + compute capacity
    user = get_merged_schedule(user)

    availability_week = user.get("availability_start_week")
    effective_min_week = max(min_week, availability_week) if availability_week else min_week
    user = compute_capacity(user, effective_min_week)
    user = find_earliest_week(user, effective_min_week)

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
