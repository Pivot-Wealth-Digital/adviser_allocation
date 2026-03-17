#!/usr/bin/env python3
"""Sync office closures from Firestore to CloudSQL (deduplicated).

Reads all closures from the Firestore ``office_closures`` collection and
inserts any that do not already exist in CloudSQL ``aa_office_closures``.
Matches on (description, start_date, end_date) to avoid duplicates.

Usage:
    export CLOUD_SQL_USE_PROXY=true
    export CLOUD_SQL_PASSWORD=xxx

    # Dry run — show what would be synced
    uv run python scripts/sync_firestore_closures.py --dry-run

    # Execute sync
    uv run python scripts/sync_firestore_closures.py --execute
"""

import argparse
import logging
import os
import sys
from datetime import date, datetime
from typing import Any, Optional

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


def parse_date(value: Any) -> Optional[date]:
    """Parse date from Firestore value (date, datetime, or string)."""
    if not value:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
        except ValueError:
            pass
        for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y"):
            try:
                return datetime.strptime(value, fmt).date()
            except ValueError:
                continue
    return None


def main():
    parser = argparse.ArgumentParser(
        description="Sync Firestore office_closures to CloudSQL aa_office_closures"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true", help="Show what would be synced")
    group.add_argument("--execute", action="store_true", help="Perform the sync")
    args = parser.parse_args()

    # --- Connect to Firestore ---
    from google.cloud import firestore

    fs_client = firestore.Client()
    logger.info("Connected to Firestore")

    # --- Connect to CloudSQL ---
    from src.adviser_allocation.utils.common import get_cloudsql_db

    db = get_cloudsql_db()
    logger.info("Connected to CloudSQL")

    # --- Build set of existing closures ---
    existing_closures = db.get_global_closures()
    existing_keys = set()
    for closure in existing_closures:
        key = (
            (closure.get("description") or "").strip().lower(),
            closure.get("start_date", ""),
            closure.get("end_date", ""),
        )
        existing_keys.add(key)
    logger.info("Found %d existing closures in CloudSQL", len(existing_keys))

    # --- Read Firestore closures ---
    fs_closures = fs_client.collection("office_closures").stream()

    added = 0
    skipped = 0
    errors = 0

    for doc in fs_closures:
        data = doc.to_dict() or {}
        start = parse_date(data.get("start_date"))
        end = parse_date(data.get("end_date"))
        description = (data.get("description") or "").strip()

        if not start or not end:
            logger.warning("Skipping doc %s: missing dates (start=%s, end=%s)", doc.id, start, end)
            errors += 1
            continue

        key = (description.lower(), start.isoformat(), end.isoformat())

        if key in existing_keys:
            logger.debug("Skipping (exists): %s %s–%s", description, start, end)
            skipped += 1
            continue

        logger.info("  %s: %s %s — %s", "WOULD ADD" if args.dry_run else "ADDING", description, start, end)

        if args.execute:
            try:
                closure_id = db.insert_office_closure(
                    start_date=start,
                    end_date=end,
                    description=description,
                    tags=data.get("tags", []),
                    created_by="firestore-sync",
                )
                logger.info("    -> closure_id=%s", closure_id)
            except Exception as exc:
                logger.error("    -> FAILED: %s", exc)
                errors += 1
                continue

        added += 1

    logger.info("=" * 50)
    logger.info(
        "Sync %s: added=%d, skipped=%d, errors=%d",
        "dry run" if args.dry_run else "complete",
        added,
        skipped,
        errors,
    )
    if args.dry_run and added > 0:
        logger.info("Run with --execute to perform the sync.")


if __name__ == "__main__":
    main()
