"""Google Calendar push notification (watch) channel management.

Registers watch channels so Google sends real-time POST notifications
to /webhooks/calendar when calendar events change. Channels expire
after ~7 days and must be renewed periodically.

Channel state is persisted in Firestore collection ``calendar_watch_channels``.
"""

from __future__ import annotations

import hashlib
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

WATCH_COLLECTION = "calendar_watch_channels"
RENEWAL_BUFFER_HOURS = 48
CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar"


def _get_calendar_service_rw():
    """Build Calendar API service with full calendar scope (needed for watch)."""
    import google.auth
    from googleapiclient.discovery import build

    credentials, _ = google.auth.default(scopes=[CALENDAR_SCOPE])
    return build("calendar", "v3", credentials=credentials, cache_discovery=False)


def _get_firestore_client():
    """Return a Firestore client."""
    from google.cloud import firestore

    return firestore.Client()


def _sanitize_doc_id(calendar_id: str) -> str:
    """Create a safe Firestore document ID from a calendar ID."""
    return hashlib.sha256(calendar_id.encode()).hexdigest()[:16]


def register_calendar_watch(
    calendar_id: str,
    webhook_url: str,
    channel_token: str,
) -> dict[str, Any]:
    """Create a push notification channel watching a Google Calendar.

    Parameters
    ----------
    calendar_id : str
        Google Calendar ID to watch.
    webhook_url : str
        Public HTTPS URL Google will POST notifications to.
    channel_token : str
        Secret token Google will echo back in X-Goog-Channel-Token header.

    Returns
    -------
    dict
        Channel metadata: channel_id, resource_id, expiration_ms.
    """
    service = _get_calendar_service_rw()
    channel_id = str(uuid.uuid4())

    watch_body = {
        "id": channel_id,
        "type": "web_hook",
        "address": webhook_url,
        "token": channel_token,
    }

    response = (
        service.events()
        .watch(
            calendarId=calendar_id,
            body=watch_body,
        )
        .execute()
    )

    expiration_ms = int(response.get("expiration", 0))
    resource_id = response.get("resourceId", "")

    # Persist to Firestore
    doc_id = _sanitize_doc_id(calendar_id)
    doc_data = {
        "calendar_id": calendar_id,
        "channel_id": channel_id,
        "resource_id": resource_id,
        "expiration_ms": expiration_ms,
        "webhook_url": webhook_url,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
    }

    firestore_client = _get_firestore_client()
    firestore_client.collection(WATCH_COLLECTION).document(doc_id).set(doc_data)

    expiry_utc = datetime.fromtimestamp(expiration_ms / 1000, tz=timezone.utc)
    logger.info(
        "Registered watch for %s (channel=%s, expires=%s)",
        calendar_id[:30],
        channel_id[:8],
        expiry_utc.isoformat(),
    )
    return doc_data


def stop_calendar_watch(channel_id: str, resource_id: str) -> None:
    """Stop an existing watch channel.

    Parameters
    ----------
    channel_id : str
        Channel UUID from registration.
    resource_id : str
        Resource ID returned by Google during registration.
    """
    service = _get_calendar_service_rw()
    service.channels().stop(
        body={
            "id": channel_id,
            "resourceId": resource_id,
        }
    ).execute()
    logger.info("Stopped watch channel %s", channel_id[:8])


def renew_expiring_watches(
    calendar_sources: list[tuple[str, str | None]],
) -> dict[str, int]:
    """Renew watch channels expiring within RENEWAL_BUFFER_HOURS.

    Also registers watches for any calendar not yet being watched.

    Parameters
    ----------
    calendar_sources : list of (calendar_id, source_tag) tuples
        Calendars to watch. source_tag is stored but not used by this function.

    Returns
    -------
    dict
        Counts: renewed, registered, skipped, errors.
    """
    webhook_url = _build_webhook_url()
    channel_token = _load_channel_token()
    if not channel_token:
        logger.error("CALENDAR_WEBHOOK_TOKEN not configured; cannot register watches")
        return {"renewed": 0, "registered": 0, "skipped": 0, "errors": 1}

    firestore_client = _get_firestore_client()
    counts = {"renewed": 0, "registered": 0, "skipped": 0, "errors": 0}

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    buffer_ms = RENEWAL_BUFFER_HOURS * 3600 * 1000
    threshold_ms = now_ms + buffer_ms

    for calendar_id, _source_tag in calendar_sources:
        doc_id = _sanitize_doc_id(calendar_id)
        doc_ref = firestore_client.collection(WATCH_COLLECTION).document(doc_id)
        existing = doc_ref.get()

        try:
            if existing.exists:
                channel_data = existing.to_dict()
                expiration_ms = channel_data.get("expiration_ms", 0)

                if expiration_ms > threshold_ms:
                    counts["skipped"] += 1
                    continue

                # Stop old channel before re-registering
                _stop_watch_safe(
                    channel_data.get("channel_id", ""),
                    channel_data.get("resource_id", ""),
                )
                register_calendar_watch(calendar_id, webhook_url, channel_token)
                counts["renewed"] += 1
            else:
                register_calendar_watch(calendar_id, webhook_url, channel_token)
                counts["registered"] += 1
        except Exception as exc:
            logger.error(
                "Failed to renew/register watch for %s: %s",
                calendar_id[:30],
                exc,
                exc_info=True,
            )
            counts["errors"] += 1

    logger.info(
        "Watch renewal complete: renewed=%d registered=%d skipped=%d errors=%d",
        counts["renewed"],
        counts["registered"],
        counts["skipped"],
        counts["errors"],
    )
    return counts


def get_active_watches() -> list[dict[str, Any]]:
    """List all active watch channels from Firestore."""
    firestore_client = _get_firestore_client()
    docs = firestore_client.collection(WATCH_COLLECTION).stream()
    return [doc.to_dict() for doc in docs]


def _build_webhook_url() -> str:
    """Build the webhook URL from APP_BASE_URL env var."""
    base_url = os.environ.get("APP_BASE_URL")
    if not base_url:
        raise RuntimeError("APP_BASE_URL environment variable is required for calendar webhooks")
    return f"{base_url.rstrip('/')}/webhooks/calendar"


def _load_channel_token() -> str | None:
    """Load the channel verification token from secrets."""
    from adviser_allocation.utils.secrets import get_secret

    return get_secret("CALENDAR_WEBHOOK_TOKEN")


def _stop_watch_safe(channel_id: str, resource_id: str) -> None:
    """Stop a watch channel, ignoring errors (channel may already be expired)."""
    if not channel_id or not resource_id:
        return
    try:
        stop_calendar_watch(channel_id, resource_id)
    except Exception as exc:
        logger.warning("Failed to stop channel %s (may be expired): %s", channel_id[:8], exc)
