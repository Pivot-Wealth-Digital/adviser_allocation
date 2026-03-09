"""Common utility functions used across the application."""

import logging
import os
from datetime import date, datetime
from typing import Optional
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


# CloudSQL database instance (singleton)
_cloudsql_db: Optional["AdviserAllocationDB"] = None  # noqa: F821


def get_cloudsql_db() -> "AdviserAllocationDB":  # noqa: F821
    """Get or initialize the CloudSQL database repository.

    Returns:
        AdviserAllocationDB instance for database operations.

    Raises:
        RuntimeError: If database connection fails.
    """
    global _cloudsql_db
    if _cloudsql_db is None:
        from adviser_allocation.db import AdviserAllocationDB, get_db_engine

        try:
            engine = get_db_engine()
            _cloudsql_db = AdviserAllocationDB(engine)
            logging.info("CloudSQL database initialized")
        except Exception as e:
            logging.error("Failed to initialize CloudSQL: %s", e)
            raise RuntimeError(f"CloudSQL initialization failed: {e}") from e
    return _cloudsql_db


# Legacy compatibility - these are deprecated
USE_FIRESTORE = False  # CloudSQL is now the only backend
db = None  # Legacy Firestore client - no longer used


def init_firestore():
    """Deprecated: Firestore is no longer used. Returns None."""
    logging.warning("init_firestore() is deprecated - use get_cloudsql_db() instead")


def get_firestore_client():
    """Deprecated: Firestore is no longer used. Returns None."""
    logging.warning("get_firestore_client() is deprecated - use get_cloudsql_db() instead")
