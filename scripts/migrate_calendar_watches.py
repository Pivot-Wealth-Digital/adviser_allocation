"""One-time migration: copy calendar_watch_channels from Firestore to CloudSQL.

Usage:
    CLOUD_SQL_USE_PROXY=true uv run python scripts/migrate_calendar_watches.py --dry-run
    CLOUD_SQL_USE_PROXY=true uv run python scripts/migrate_calendar_watches.py --execute

Rollback:
    DELETE FROM aa_calendar_watch_channels;
"""

import argparse
import logging
import sys

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

FIRESTORE_COLLECTION = "calendar_watch_channels"


def main():
    parser = argparse.ArgumentParser(
        description="Migrate calendar watches from Firestore to CloudSQL"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--dry-run", action="store_true", help="Read Firestore and show what would migrate"
    )
    group.add_argument("--execute", action="store_true", help="Actually write to CloudSQL")
    args = parser.parse_args()

    from adviser_allocation.db.models import CalendarWatchChannel
    from adviser_allocation.utils.common import get_cloudsql_db
    from google.cloud import firestore

    logger.info("Connecting to Firestore...")
    fs_client = firestore.Client()
    docs = list(fs_client.collection(FIRESTORE_COLLECTION).stream())
    logger.info("Found %d watch channels in Firestore", len(docs))

    if not docs:
        logger.info("Nothing to migrate")
        return

    db = get_cloudsql_db()
    migrated_count = 0
    skipped_count = 0

    for doc in docs:
        data = doc.to_dict()
        doc_id = doc.id
        calendar_id = data.get("calendar_id", "")
        channel_id = data.get("channel_id", "")
        resource_id = data.get("resource_id", "")
        expiration_ms = int(data.get("expiration_ms", 0))
        webhook_url = data.get("webhook_url", "")

        if not calendar_id or not channel_id:
            logger.warning("Skipping doc %s: missing calendar_id or channel_id", doc_id)
            skipped_count += 1
            continue

        logger.info(
            "  %s: calendar=%s channel=%s expires=%d",
            doc_id,
            calendar_id[:30],
            channel_id[:8],
            expiration_ms,
        )

        if args.execute:
            watch = CalendarWatchChannel(
                doc_id=doc_id,
                calendar_id=calendar_id,
                channel_id=channel_id,
                resource_id=resource_id,
                expiration_ms=expiration_ms,
                webhook_url=webhook_url,
            )
            db.upsert_calendar_watch(watch)
            migrated_count += 1
        else:
            migrated_count += 1

    action = "Migrated" if args.execute else "Would migrate"
    logger.info("%s %d channels, skipped %d", action, migrated_count, skipped_count)

    if args.dry_run:
        logger.info("Dry run complete. Re-run with --execute to apply.")


if __name__ == "__main__":
    main()
