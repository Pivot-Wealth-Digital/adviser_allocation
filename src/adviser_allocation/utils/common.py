"""Common utility functions used across the application."""

import logging
import os
from datetime import date, datetime
from zoneinfo import ZoneInfo

# Sydney timezone constant
SYDNEY_TZ = ZoneInfo("Australia/Sydney")


def sydney_now() -> datetime:
    """Return current datetime in Sydney timezone."""
    return datetime.now(SYDNEY_TZ)


def sydney_today() -> date:
    """Return current date in Sydney timezone."""
    return sydney_now().date()


def sydney_datetime_from_date(d: date) -> datetime:
    """Convert a date to datetime at midnight in Sydney timezone."""
    return datetime.combine(d, datetime.min.time(), SYDNEY_TZ)


# Optional: persist tokens in Firestore (recommended on Cloud Run)
USE_FIRESTORE = os.environ.get("USE_FIRESTORE", "true").lower() == "true"
db = None


def init_firestore():
    """Initialize Firestore client if enabled."""
    global db
    if USE_FIRESTORE and not db:
        try:
            from google.cloud import firestore

            db = firestore.Client()  # Uses default credentials
        except Exception as e:
            logging.warning(f"Firestore client init failed: {e}")
    return db


def get_firestore_client():
    """Get the Firestore client instance."""
    return db or init_firestore()
