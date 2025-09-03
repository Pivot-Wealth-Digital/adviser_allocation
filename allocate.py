import os, time
from datetime import datetime, timedelta, date
import requests

from zoneinfo import ZoneInfo


# Optional: persist tokens in Firestore (recommended on App Engine)
USE_FIRESTORE = os.environ.get("USE_FIRESTORE", "true").lower() == "true"
db = None
if USE_FIRESTORE:
    from google.cloud import firestore
    db = firestore.Client()  # Uses App Engine default credentials


HUBSPOT_TOKEN = os.environ.get("HUBSPOT_TOKEN")
HEADERS = {"Authorization": f"Bearer {HUBSPOT_TOKEN}", "Content-Type": "application/json"}


CLARIFY_COL = 0
KICKOFF_COL = 1
LEAVE_COL = 2
DEALS_NO_CLARIFY_COL = 3
TARGET_CAPACITY_COL = 4
ACTUAL_CAPACITY_COL = 5


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


def get_employee_id_from_firestore(search_email):
    """
    Queries Firestore to find an employee ID by their company email.
    
    Args:
        search_email (str): The email address to search for.
        
    Returns:
        str: The employee ID if found, otherwise None.
    """
    try:
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
    elif isinstance(input_date, datetime.datetime):
        dt = (
            input_date.astimezone(tz)
            if input_date.tzinfo
            else input_date.replace(tzinfo=tz)
        )
    elif isinstance(input_date, datetime.date):
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

    # Define a workweek as 5 days
    WORKWEEK_DAYS = 5

    if not input_date:
        # Get the current date
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
    result = requests.post(url, headers=HEADERS, json=payload)
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
        week_number = meeting_datetime.isocalendar()[1]

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

    table_data = [["Week Number", "Clarify Meetings", "Kick Off Meetings"]]
    for week in all_weeks:
        total_count = weekly_clarify_counts.get(week, 0)
        kickoff_count = weekly_kickoff_counts.get(week, 0)
        table_data.append([week, total_count, kickoff_count])

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

    start_date_str = user.get("properties").get("adviser_start_date")
    start_date = datetime.fromisoformat(start_date_str).date()
    pod_type = user.get("pod_type")

    if ((date_today - start_date).days < 90) or (pod_type == "Solo Adviser"):
        user["properties"]["client_limit_monthly"] = 4

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
                week_number = current_date.isocalendar()[1]
                weeks_activity[week_number] = weeks_activity.get(week_number, 0) + 1

            current_date += timedelta(days=1)

        # Classify each week and append to the main list
        for week_num, days_count in weeks_activity.items():
            classification = "Full" if days_count == 5 else f"Partial: {days_count}"
            all_classified_weeks.append([week_num, classification])

    return all_classified_weeks


def get_merged_schedule(user):
    """
    Merges classified leave weeks and classified deals into a single data dictionary,
    filling in missing week numbers with default values.
    """
    classified_weeks = user["leave_requests_list"]
    classified_deals = user["deals_no_clarify_list"]
    data_dict = user["meeting_count_list"]

    # Create a map for fast lookup of classified weeks
    classified_weeks_map = {
        week_num: classification for week_num, classification in classified_weeks
    }

    # Step 1: Merge classified leave weeks into data_dict
    for week_num, values in data_dict.items():
        classification = classified_weeks_map.get(week_num, "No")
        data_dict[week_num].append(classification)

    # Step 2: Add new entries for weeks that are in classified_weeks but not in data_dict
    for week_num, classification in classified_weeks_map.items():
        if week_num not in data_dict:
            # We add a 'No' for the deals classification as it's not available here
            data_dict[week_num] = [0, 0, classification]
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
    Calculates the sum of all values for keys less than the current week,
    adds this sum to the dictionary with a key of (current_week - 1),
    and then removes the old keys.
    """
    sum_of_values = 0
    keys_to_remove = []
    current_week = date.today().isocalendar()[1]

    # Iterate through the dictionary to find keys to remove and sum their values
    for key, value_list in data.items():
        if key < current_week:
            sum_of_values += sum(value_list)
            keys_to_remove.append(key)

    # Add the new key-value pair to the dictionary
    data[current_week - 1] = [sum_of_values]

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

    response = requests.post(url, headers=HEADERS, json=data)
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
            week_number = date_obj.isocalendar()[1]

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

    # --- Step 1: Fill in missing week numbers and create a complete dictionary ---
    # min_week = min(data_dict.keys())
    max_week = max(data_dict.keys())
    complete_data_dict = {}

    for week_num in range(min_week, max_week + 1):
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
        prev_week = week - 1

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

        if curr_status == "No":
            # Start from full limit, then subtract based on previous week
            current_value_capacity = limit
        elif curr_status == "Full":
            current_value_capacity = 0
        elif curr_has_high or curr_has_low:
            current_value_capacity = half

        complete_data_dict[week].append(int(current_value_capacity))

    # --- Step 3: Add one column (cumulative sum of 1st column and the week before it) ---
    cumulative_sum_count = 0
    complete_data_dict[sorted_weeks[0]].append(complete_data_dict[sorted_weeks[0]][0])

    for week_num in sorted_weeks[1:]:
        if complete_data_dict[week_num][2] == "Full":
            complete_data_dict[week_num].append(0)
        else:
            value_from_first_column = complete_data_dict[week_num][0]
            prev_week = week_num - 1

            while complete_data_dict[prev_week][2] == "Full":
                prev_week -= 1

            cumulative_sum_count = (
                value_from_first_column + complete_data_dict[prev_week][0]
            )

            complete_data_dict[week_num].append(cumulative_sum_count)

    # --- Step 4: Add the last column (subtracting actual capacity from target capacity) ---
    for values in complete_data_dict.values():
        result = int(values[ACTUAL_CAPACITY_COL] - values[TARGET_CAPACITY_COL])
        values.append(result)

    user["capacity"] = complete_data_dict

    return user


def find_earliest_week(user, min_week):
    print(f"{user['properties']['hs_email']}")
    now_week = date.today().isocalendar()[1]
    if min_week == now_week:
        starting_week = min_week + 2
    else:
        starting_week = min_week
    found_first_negative_week = None

    data = user["capacity"]

    # Get a sorted list of week numbers
    sorted_weeks = sorted(data.keys())
    starting_index = (
        sorted_weeks.index(starting_week) if starting_week in sorted_weeks else 0
    )

    # Find the index of min_week to start the search
    # start_index = sorted_weeks.index(min_week) if starting_week in sorted_weeks else 0
    if (len(sorted_weeks[starting_index:]) < 2) or (starting_index == 0):
        user["earliest_open_week"] = min_week
        return user

    # Iterate from the starting week to check for consecutive negative values
    for i, week in enumerate(sorted_weeks[starting_index:]):
        current_week = week
        previous_week = week - 1

        # The 'Difference' value is the last element in the list
        current_diff = data[current_week][-1]
        previous_diff = data[previous_week][-1]
        # Check if both values are negative
        if current_diff < 0 and previous_diff < 0:
            found_first_negative_week = previous_week
            break

    if not found_first_negative_week:
        found_first_negative_week = current_week

    # allocate deal_no_clarify
    clarify_count = sum(
        v[DEALS_NO_CLARIFY_COL]
        for k, v in data.items()
        if k <= (found_first_negative_week - 2)
    )
    final_week = found_first_negative_week
    print(
        f"first_open_week {final_week}, clar_count_upto_date {found_first_negative_week - 2} clarify count {clarify_count}"
    )
    while clarify_count > 0:
        print(final_week, clarify_count, data[final_week][-1])
        clarify_count += data[final_week][-1]  # get diff column and subtract
        final_week += 1
        if final_week not in sorted_weeks:
            print(f"oops {final_week} no in {sorted_weeks}")
            final_week += int(ceil_div(clarify_count, 1.5))
            print(
                f"clarify count of {clarify_count} adds {ceil_div(clarify_count, 1.5)} weeks"
            )
            break
        data[final_week][-1] -= data[final_week - 1][-1]  # update diff next week

    print(f"current_open_week {final_week}")
    clarify_count = sum(
        v[DEALS_NO_CLARIFY_COL]
        for k, v in data.items()
        if (found_first_negative_week - 1) <= k <= (final_week - 2)
    )
    print(
        f"after initial iteration: {found_first_negative_week - 1}, {final_week - 2} {clarify_count}"
    )

    if clarify_count < 1:
        print(f"Week: {final_week}")
        user["earliest_open_week"] = final_week
        return user
    else:
        print(f"recursion: {final_week}")
        min_week = final_week + 1
        return find_earliest_week(user, min_week)


def display_data(data):
    # Define the table headers
    headers = [
        "Week Number",
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
        row_data = [str(week)] + [str(item) for item in data[week]]
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
        row_data = [str(week)] + [str(item) for item in data[week]]
        data_row = " | ".join(
            item.ljust(width) for item, width in zip(row_data, column_widths)
        )
        print(data_row)


def get_user_ids_adviser(service_package):
    url = "https://api.hubapi.com/crm/v3/objects/users?properties=taking_on_clients,hs_email,hubspot_owner_id,adviser_start_date,pod_type,client_types&limit=100"  # include start date and ooo status
    response = requests.get(url, headers=HEADERS)
    response.raise_for_status()
    users = response.json().get("results", [])
    users_list = []
    for user in users:
        if user.get("properties").get("taking_on_clients") == "True":
            if service_package in user["properties"]["client_types"]:
                users_list.append(user)

    return users_list



def get_adviser(service_package):
    # get users with taking_on_clients on
    print("Getting USER IDs taking on client")
    users_list = get_user_ids_adviser(service_package)

    for i, user in enumerate(users_list):
        print(f"user {i}")
        # get user approved leave requests from EH
        user_email = user["properties"]["hs_email"]
        employee_id = get_employee_id_from_firestore(user_email)
        employee_leaves = get_employee_leaves_from_firestore(employee_id)
        user["leave_requests"] = employee_leaves

        # get week number of approved leave requests
        user["leave_requests_list"] = classify_leave_weeks(user["leave_requests"])

        # get user limit, 6 or 4 depending on some details
        user = get_user_client_limits(user)

        # get meeting details
        timestamp_milliseconds = get_monday_from_weeks_ago(n=1)
        min_week = datetime.fromtimestamp(
            (timestamp_milliseconds / 1000)
        ).isocalendar()[1]

        user = get_user_meeting_details(user, timestamp_milliseconds)

        # get clarify meeting counts
        user_meetings = user["meetings"]["results"]
        user["meeting_count_list"] = get_meeting_count(user_meetings)

        # get deals with no clarify for each user
        user["deals_no_clarify"] = get_deals_no_clarify(user_email)

        # classify deals with no clarify (get week numbers)
        user["deals_no_clarify_list"] = classify_deals_list(user["deals_no_clarify"])

        # merge meeting counts and leave requests
        user = get_merged_schedule(user)

        # allocate deal to most suitable adviser
        current_week = date.today().isocalendar()[1]
        user = compute_capacity(user, min_week)

        display_data(user["capacity"])
        print("\n")
        user = find_earliest_week(user, current_week)

        users_list[i] = user
        print(current_week, min_week)

    final_agent = min(users_list, key=lambda user: user["earliest_open_week"])
    for user in users_list:
        print(f"{user['properties']['hs_email']} \t Week: {user['earliest_open_week']}")
    print("\n")
    print(f"Earliest open agent: {final_agent['properties']['hs_email']}")

    return final_agent
