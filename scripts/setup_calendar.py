"""Setup script: Create 'Pivot Office Closures' Google Calendar and share with Cloud Run SA.

Usage:
    python scripts/setup_calendar.py

Requires:
    - google-api-python-client
    - google-auth
    - User must be authenticated via `gcloud auth application-default login`
      with a Workspace admin account that can create calendars.

Outputs the calendar ID to set as GOOGLE_CALENDAR_ID env var in Cloud Run.
"""

import sys

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# Scopes needed to create calendars and manage sharing
SCOPES = ["https://www.googleapis.com/auth/calendar"]

# App Engine default service account
APP_ENGINE_SA = "pivot-digital-466902@appspot.gserviceaccount.com"

# Australian public holidays calendar (public, read-only by default)
AU_HOLIDAYS_CALENDAR_ID = "en.australian#holiday@group.v.calendar.google.com"

CALENDAR_NAME = "Pivot Office Closures"
CALENDAR_DESCRIPTION = (
    "Office closures, wellness days, and company events. "
    "Synced automatically to the adviser allocation system."
)


def get_calendar_service():
    """Build Calendar API service using application default credentials.

    Falls back to OAuth installed-app flow if ADC doesn't have calendar scope.
    """
    try:
        import google.auth.default

        credentials, _ = google.auth.default(scopes=SCOPES)
        credentials.refresh(Request())
    except Exception:
        print("ADC not available for Calendar scope. Using OAuth flow...")
        print("Visit the URL below to authorise this script.\n")
        flow = InstalledAppFlow.from_client_config(
            {
                "installed": {
                    "client_id": "YOUR_CLIENT_ID.apps.googleusercontent.com",
                    "client_secret": "YOUR_CLIENT_SECRET",
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "redirect_uris": ["http://localhost"],
                }
            },
            SCOPES,
        )
        credentials = flow.run_local_server(port=0)

    return build("calendar", "v3", credentials=credentials)


def create_calendar(service):
    """Create the Pivot Office Closures calendar.

    Returns:
        Calendar ID string.
    """
    # Check if calendar already exists
    calendar_list = service.calendarList().list().execute()
    for cal in calendar_list.get("items", []):
        if cal.get("summary") == CALENDAR_NAME:
            print(f"Calendar '{CALENDAR_NAME}' already exists: {cal['id']}")
            return cal["id"]

    calendar_body = {
        "summary": CALENDAR_NAME,
        "description": CALENDAR_DESCRIPTION,
        "timeZone": "Australia/Sydney",
    }

    created = service.calendars().insert(body=calendar_body).execute()
    calendar_id = created["id"]
    print(f"Created calendar '{CALENDAR_NAME}': {calendar_id}")
    return calendar_id


def share_calendar_with_sa(service, calendar_id, sa_email, role="reader"):
    """Share a calendar with a service account.

    Args:
        service: Calendar API service.
        calendar_id: Calendar to share.
        sa_email: Service account email.
        role: ACL role (reader, writer, owner).
    """
    acl_body = {
        "role": role,
        "scope": {
            "type": "user",
            "value": sa_email,
        },
    }

    try:
        service.acl().insert(calendarId=calendar_id, body=acl_body).execute()
        print(f"Shared '{calendar_id}' with {sa_email} ({role})")
    except HttpError as exc:
        if exc.resp.status == 409:
            print(f"ACL already exists for {sa_email} on '{calendar_id}'")
        else:
            raise


def main():
    service = get_calendar_service()

    # Step 1: Create the Pivot Office Closures calendar
    print("\n--- Step 1: Create calendar ---")
    calendar_id = create_calendar(service)

    # Step 2: Share with App Engine SA (reader)
    print("\n--- Step 2: Share calendars with App Engine SA ---")
    share_calendar_with_sa(service, calendar_id, APP_ENGINE_SA, role="reader")

    # Step 3: Share AU holidays calendar with App Engine SA
    # Public calendars are readable by default, but explicit ACL ensures access
    try:
        share_calendar_with_sa(service, AU_HOLIDAYS_CALENDAR_ID, APP_ENGINE_SA, role="reader")
    except HttpError as exc:
        print(f"Note: Could not add ACL to public holidays calendar ({exc.resp.status})")
        print("  This is expected — public calendars are readable without explicit ACL.")

    # Output
    print("\n" + "=" * 60)
    print("SETUP COMPLETE")
    print("=" * 60)
    print(f"\nCalendar ID: {calendar_id}")
    print("\nSet as Cloud Run env var:")
    print(f'  GOOGLE_CALENDAR_ID: "{calendar_id}"')

    return calendar_id


if __name__ == "__main__":
    try:
        main()
    except HttpError as exc:
        print(f"\nGoogle API error: {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nAborted.")
        sys.exit(1)
