"""Google Calendar sync service for office closures.

Reads events from shared Google Calendars and upserts them into the
aa_office_closures table. Only manages records it created (those with a
non-NULL google_event_id). Manual closures are never touched.

Config (env vars):
    GOOGLE_CALENDAR_ID: Pivot office closures calendar ID.
    GOOGLE_HOLIDAYS_CALENDAR_ID: Australian public holidays calendar ID.
        Default: en.australian#holiday@group.v.calendar.google.com
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Title keyword -> tag mapping (case-insensitive, first match wins).
TITLE_TAG_RULES: List[Tuple[str, str]] = [
    ("public holiday", "Public Holiday"),
    ("national holiday", "National Holiday"),
    ("regional holiday", "Regional Holiday"),
    ("wellness", "Wellness Day"),
    ("pivot day", "Pivot Day"),
    ("team day", "Team Day"),
    ("company day", "Company Day"),
    ("maintenance", "Office Maintenance"),
    ("closure", "Office Closure"),
]
CALENDAR_SYNC_TAG = "Calendar Sync"

# Default Australian holidays calendar ID (Google's built-in).
DEFAULT_HOLIDAYS_CALENDAR_ID = "en.australian#holiday@group.v.calendar.google.com"

# How many months ahead to sync (rolling window).
SYNC_MONTHS_AHEAD = 12


def _get_calendar_service():
    """Build an authenticated Google Calendar API service via ADC."""
    import google.auth
    from googleapiclient.discovery import build

    credentials, _ = google.auth.default(
        scopes=["https://www.googleapis.com/auth/calendar.readonly"]
    )
    return build("calendar", "v3", credentials=credentials, cache_discovery=False)


def _derive_tags(title: str, source_tag: str = None) -> List[str]:
    """Derive tags from a Google Calendar event title.

    Always includes CALENDAR_SYNC_TAG. Adds a category tag based on
    title keywords (case-insensitive). Unrecognised events get only
    the base tag(s).

    Parameters
    ----------
    title : str
        Google Calendar event title (summary field).
    source_tag : str, optional
        Extra tag from the calendar source (e.g. "Public Holiday" for
        the AU holidays calendar).
    """
    tags = [CALENDAR_SYNC_TAG]
    if source_tag:
        tags.append(source_tag)

    lower = (title or "").lower()
    for keyword, tag in TITLE_TAG_RULES:
        if keyword in lower and tag not in tags:
            tags.append(tag)
            break
    return tags


def _parse_event_dates(event: Dict[str, Any]) -> Optional[Tuple[date, date]]:
    """Extract start and end dates from a Google Calendar event.

    Handles all-day events (date) and timed events (dateTime).
    Google Calendar end dates for all-day events are exclusive,
    so we subtract one day.
    """
    try:
        start_raw = event.get("start", {})
        end_raw = event.get("end", {})

        if "date" in start_raw:
            start = date.fromisoformat(start_raw["date"])
            # Google end date is exclusive for all-day events
            end = date.fromisoformat(end_raw["date"]) - timedelta(days=1)
        elif "dateTime" in start_raw:
            start = datetime.fromisoformat(start_raw["dateTime"].replace("Z", "+00:00")).date()
            end = datetime.fromisoformat(end_raw["dateTime"].replace("Z", "+00:00")).date()
        else:
            logger.warning("Event %s has no date or dateTime", event.get("id"))
            return None

        if end < start:
            end = start
        return start, end
    except Exception as exc:
        logger.warning("Failed to parse dates for event %s: %s", event.get("id"), exc)
        return None


def fetch_calendar_events(
    calendar_id: str,
    months_ahead: int = SYNC_MONTHS_AHEAD,
) -> List[Dict[str, Any]]:
    """Fetch upcoming events from a Google Calendar.

    Parameters
    ----------
    calendar_id : str
        The Google Calendar ID to fetch from.
    months_ahead : int
        Number of months ahead to fetch (default 12).

    Returns
    -------
    list
        Google Calendar event resource dicts.
    """
    service = _get_calendar_service()

    today = date.today()
    time_min = datetime(today.year, today.month, today.day).isoformat() + "Z"
    future = today + timedelta(days=months_ahead * 30)
    time_max = datetime(future.year, future.month, future.day).isoformat() + "Z"

    events = []
    page_token = None

    while True:
        kwargs = {
            "calendarId": calendar_id,
            "timeMin": time_min,
            "timeMax": time_max,
            "singleEvents": True,
            "orderBy": "startTime",
            "showDeleted": False,
            "maxResults": 250,
        }
        if page_token:
            kwargs["pageToken"] = page_token

        response = service.events().list(**kwargs).execute()
        events.extend(response.get("items", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            break

    logger.info(
        "Fetched %d events from calendar %s (%s to %s)",
        len(events),
        calendar_id,
        time_min[:10],
        time_max[:10],
    )
    return events


def sync_calendar_closures(
    calendar_sources: List[Tuple[str, Optional[str]]],
    db,
) -> Dict[str, int]:
    """Sync Google Calendar events into the aa_office_closures table.

    Parameters
    ----------
    calendar_sources : list of (calendar_id, source_tag) tuples
        Each tuple is a calendar ID and an optional tag to apply to all
        events from that calendar (e.g. ("cal-id", "Public Holiday")).
    db : AdviserAllocationDB
        Database repository instance.

    Returns
    -------
    dict
        Counts: upserted, deleted, errors, skipped.
    """
    counts = {"upserted": 0, "deleted": 0, "errors": 0, "skipped": 0}
    active_event_ids: List[str] = []

    for calendar_id, source_tag in calendar_sources:
        try:
            events = fetch_calendar_events(calendar_id)
        except Exception as exc:
            logger.error(
                "Failed to fetch events from %s: %s",
                calendar_id,
                exc,
                exc_info=True,
            )
            counts["errors"] += 1
            continue

        for event in events:
            event_id = event.get("id")
            if not event_id:
                counts["skipped"] += 1
                continue

            if event.get("status") == "cancelled":
                counts["skipped"] += 1
                continue

            dates = _parse_event_dates(event)
            if dates is None:
                counts["skipped"] += 1
                continue

            start_date, end_date = dates
            title = event.get("summary") or "Office Closure"
            tags = _derive_tags(title, source_tag)

            try:
                db.upsert_office_closure_by_event_id(
                    google_event_id=event_id,
                    start_date=start_date,
                    end_date=end_date,
                    description=title,
                    tags=tags,
                )
                active_event_ids.append(event_id)
                counts["upserted"] += 1
            except Exception as exc:
                logger.error(
                    "Failed to upsert closure for event %s: %s",
                    event_id,
                    exc,
                    exc_info=True,
                )
                counts["errors"] += 1

    # Remove calendar-managed closures no longer in any calendar
    if active_event_ids:
        try:
            deleted = db.delete_stale_calendar_closures(active_event_ids)
            counts["deleted"] = deleted
        except Exception as exc:
            logger.error(
                "Failed to delete stale closures: %s",
                exc,
                exc_info=True,
            )
            counts["errors"] += 1
    else:
        logger.info("No active events found; skipping stale deletion")

    logger.info(
        "Calendar sync complete: upserted=%d deleted=%d errors=%d skipped=%d",
        counts["upserted"],
        counts["deleted"],
        counts["errors"],
        counts["skipped"],
    )
    return counts
