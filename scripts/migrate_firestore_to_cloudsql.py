#!/usr/bin/env python3
"""
One-time migration script: Firestore -> CloudSQL for adviser_allocation.

Migrates data from Firestore collections to CloudSQL aa_* tables:
- employees + leave_requests (subcollection)
- office_closures
- adviser_capacity_overrides
- allocation_requests
- eh_tokens (OAuth)

Usage:
    # Set environment variables for CloudSQL connection
    export CLOUD_SQL_USE_PROXY=true
    export CLOUD_SQL_PASSWORD=xxx

    # Dry run (no writes)
    python scripts/migrate_firestore_to_cloudsql.py --dry-run

    # Execute migration
    python scripts/migrate_firestore_to_cloudsql.py --execute

    # Migrate specific collection only
    python scripts/migrate_firestore_to_cloudsql.py --execute --collection employees

    # With token encryption key (for eh_tokens)
    export AA_TOKEN_ENCRYPTION_KEY=your-32-byte-key
    python scripts/migrate_firestore_to_cloudsql.py --execute
"""

import argparse
import json
import logging
import os
import sys
from datetime import date, datetime
from typing import Any, Dict, List, Optional

# Add parent to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


def parse_date(value: Any) -> Optional[date]:
    """Parse date from various formats."""
    if not value:
        return None
    if isinstance(value, date):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str):
        # Try ISO format first
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
        except ValueError:
            pass
        # Try common formats
        for fmt in ["%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y"]:
            try:
                return datetime.strptime(value, fmt).date()
            except ValueError:
                continue
    return None


def parse_datetime(value: Any) -> Optional[datetime]:
    """Parse datetime from various formats."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            pass
    # Handle epoch milliseconds (from HubSpot)
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value / 1000)
        except (ValueError, OSError):
            pass
    return None


class FirestoreToCloudSQLMigrator:
    """Handles migration from Firestore to CloudSQL."""

    def __init__(self, dry_run: bool = True, encryption_key: str = None):
        self.dry_run = dry_run
        self.encryption_key = encryption_key
        self.fs_db = None
        self.sql_db = None
        self.stats = {
            "employees": {"read": 0, "written": 0, "errors": 0},
            "leave_requests": {"read": 0, "written": 0, "errors": 0},
            "office_closures": {"read": 0, "written": 0, "errors": 0},
            "capacity_overrides": {"read": 0, "written": 0, "errors": 0},
            "allocation_requests": {"read": 0, "written": 0, "errors": 0},
            "oauth_tokens": {"read": 0, "written": 0, "errors": 0},
        }

    def connect(self):
        """Initialize Firestore and CloudSQL connections."""
        # Firestore
        try:
            from google.cloud import firestore
            self.fs_db = firestore.Client()
            logger.info("Connected to Firestore")
        except Exception as e:
            logger.error("Failed to connect to Firestore: %s", e)
            raise

        # CloudSQL
        try:
            from src.adviser_allocation.db import get_db_engine, AdviserAllocationDB
            engine = get_db_engine()
            self.sql_db = AdviserAllocationDB(engine)
            logger.info("Connected to CloudSQL")
        except Exception as e:
            logger.error("Failed to connect to CloudSQL: %s", e)
            raise

    def migrate_employees(self):
        """Migrate employees collection with leave_requests subcollection."""
        logger.info("Migrating employees...")

        employees_ref = self.fs_db.collection("employees")
        for emp_doc in employees_ref.stream():
            self.stats["employees"]["read"] += 1
            emp_data = emp_doc.to_dict() or {}
            emp_id = emp_doc.id

            try:
                if not self.dry_run:
                    from src.adviser_allocation.db.models import Employee
                    emp = Employee(
                        employee_id=emp_id,
                        name=emp_data.get("name", ""),
                        company_email=emp_data.get("company_email", ""),
                        account_email=emp_data.get("account_email"),
                        client_limit_monthly=emp_data.get("client_limit_monthly", 6),
                        pod_type_effective=emp_data.get("pod_type_effective"),
                        is_active=True,
                        last_synced=datetime.utcnow(),
                    )
                    self.sql_db.upsert_employee(emp)
                self.stats["employees"]["written"] += 1

                # Migrate leave_requests subcollection
                leaves_ref = emp_doc.reference.collection("leave_requests")
                for leave_doc in leaves_ref.stream():
                    self.stats["leave_requests"]["read"] += 1
                    leave_data = leave_doc.to_dict() or {}

                    try:
                        if not self.dry_run:
                            from src.adviser_allocation.db.models import LeaveRequest
                            leave = LeaveRequest(
                                leave_request_id=leave_doc.id,
                                employee_id=emp_id,
                                start_date=parse_date(leave_data.get("start_date")),
                                end_date=parse_date(leave_data.get("end_date")),
                                leave_type=leave_data.get("leave_type"),
                                status=leave_data.get("status", "approved"),
                                last_synced=datetime.utcnow(),
                            )
                            self.sql_db.upsert_leave_request(leave)
                        self.stats["leave_requests"]["written"] += 1
                    except Exception as e:
                        self.stats["leave_requests"]["errors"] += 1
                        logger.error("Error migrating leave %s: %s", leave_doc.id, e)

            except Exception as e:
                self.stats["employees"]["errors"] += 1
                logger.error("Error migrating employee %s: %s", emp_id, e)

        logger.info(
            "Employees: read=%d, written=%d, errors=%d",
            self.stats["employees"]["read"],
            self.stats["employees"]["written"],
            self.stats["employees"]["errors"],
        )
        logger.info(
            "Leave requests: read=%d, written=%d, errors=%d",
            self.stats["leave_requests"]["read"],
            self.stats["leave_requests"]["written"],
            self.stats["leave_requests"]["errors"],
        )

    def migrate_office_closures(self):
        """Migrate office_closures collection."""
        logger.info("Migrating office closures...")

        closures_ref = self.fs_db.collection("office_closures")
        for doc in closures_ref.stream():
            self.stats["office_closures"]["read"] += 1
            data = doc.to_dict() or {}

            try:
                start = parse_date(data.get("start_date"))
                end = parse_date(data.get("end_date"))

                if not start or not end:
                    logger.warning("Skipping closure %s: missing dates", doc.id)
                    continue

                if not self.dry_run:
                    self.sql_db.insert_office_closure(
                        start_date=start,
                        end_date=end,
                        description=data.get("description"),
                        tags=data.get("tags", []),
                    )
                self.stats["office_closures"]["written"] += 1

            except Exception as e:
                self.stats["office_closures"]["errors"] += 1
                logger.error("Error migrating closure %s: %s", doc.id, e)

        logger.info(
            "Office closures: read=%d, written=%d, errors=%d",
            self.stats["office_closures"]["read"],
            self.stats["office_closures"]["written"],
            self.stats["office_closures"]["errors"],
        )

    def migrate_capacity_overrides(self):
        """Migrate adviser_capacity_overrides collection."""
        logger.info("Migrating capacity overrides...")

        overrides_ref = self.fs_db.collection("adviser_capacity_overrides")
        for doc in overrides_ref.stream():
            self.stats["capacity_overrides"]["read"] += 1
            data = doc.to_dict() or {}

            try:
                if not self.dry_run:
                    from src.adviser_allocation.db.models import CapacityOverride
                    override = CapacityOverride(
                        adviser_email=data.get("adviser_email", ""),
                        effective_date=parse_date(data.get("effective_date")),
                        effective_start=parse_date(data.get("effective_start")),
                        effective_week=data.get("effective_week"),
                        client_limit_monthly=data.get("client_limit_monthly", 6),
                        pod_type=data.get("pod_type"),
                        notes=data.get("notes"),
                    )
                    self.sql_db.insert_capacity_override(override)
                self.stats["capacity_overrides"]["written"] += 1

            except Exception as e:
                self.stats["capacity_overrides"]["errors"] += 1
                logger.error("Error migrating override %s: %s", doc.id, e)

        logger.info(
            "Capacity overrides: read=%d, written=%d, errors=%d",
            self.stats["capacity_overrides"]["read"],
            self.stats["capacity_overrides"]["written"],
            self.stats["capacity_overrides"]["errors"],
        )

    def migrate_allocation_requests(self):
        """Migrate allocation_requests collection."""
        logger.info("Migrating allocation requests...")

        requests_ref = self.fs_db.collection("allocation_requests")
        for doc in requests_ref.stream():
            self.stats["allocation_requests"]["read"] += 1
            data = doc.to_dict() or {}

            try:
                if not self.dry_run:
                    record = {
                        "request_data": data.get("request_data", data),
                        "timestamp": parse_datetime(data.get("timestamp")),
                        "client_email": data.get("client_email"),
                        "deal_id": data.get("deal_id"),
                        "service_package": data.get("service_package"),
                        "service_package_raw": data.get("service_package_raw"),
                        "household_type": data.get("household_type"),
                        "household_type_raw": data.get("household_type_raw"),
                        "agreement_start_date": parse_datetime(data.get("agreement_start_date")),
                        "agreement_start_raw": data.get("agreement_start_raw"),
                        "adviser_email": data.get("adviser_email"),
                        "adviser_name": data.get("adviser_name"),
                        "adviser_hubspot_id": data.get("adviser_hubspot_id"),
                        "adviser_service_packages": data.get("adviser_service_packages", []),
                        "adviser_household_types": data.get("adviser_household_types", []),
                        "allocation_result": data.get("allocation_result"),
                        "status": data.get("status", "completed"),
                        "error_message": data.get("error_message"),
                        "source": data.get("source"),
                        "ip_address": data.get("ip_address"),
                        "user_agent": data.get("user_agent"),
                    }
                    self.sql_db.store_allocation_record(record)
                self.stats["allocation_requests"]["written"] += 1

            except Exception as e:
                self.stats["allocation_requests"]["errors"] += 1
                logger.error("Error migrating allocation %s: %s", doc.id, e)

        logger.info(
            "Allocation requests: read=%d, written=%d, errors=%d",
            self.stats["allocation_requests"]["read"],
            self.stats["allocation_requests"]["written"],
            self.stats["allocation_requests"]["errors"],
        )

    def migrate_oauth_tokens(self):
        """Migrate eh_tokens collection."""
        if not self.encryption_key:
            logger.warning(
                "Skipping OAuth tokens migration: AA_TOKEN_ENCRYPTION_KEY not set"
            )
            return

        logger.info("Migrating OAuth tokens...")

        tokens_ref = self.fs_db.collection("eh_tokens")
        for doc in tokens_ref.stream():
            self.stats["oauth_tokens"]["read"] += 1
            data = doc.to_dict() or {}

            try:
                if not self.dry_run:
                    self.sql_db.save_tokens(
                        token_key=doc.id,
                        provider="employment_hero",
                        tokens=data,
                        encryption_key=self.encryption_key,
                    )
                self.stats["oauth_tokens"]["written"] += 1

            except Exception as e:
                self.stats["oauth_tokens"]["errors"] += 1
                logger.error("Error migrating token %s: %s", doc.id, e)

        logger.info(
            "OAuth tokens: read=%d, written=%d, errors=%d",
            self.stats["oauth_tokens"]["read"],
            self.stats["oauth_tokens"]["written"],
            self.stats["oauth_tokens"]["errors"],
        )

    def run(self, collection: str = None):
        """Run the migration."""
        self.connect()

        mode = "DRY RUN" if self.dry_run else "EXECUTE"
        logger.info("=" * 60)
        logger.info("Starting Firestore -> CloudSQL migration (%s)", mode)
        logger.info("=" * 60)

        if collection:
            # Migrate specific collection
            method_map = {
                "employees": self.migrate_employees,
                "office_closures": self.migrate_office_closures,
                "capacity_overrides": self.migrate_capacity_overrides,
                "allocation_requests": self.migrate_allocation_requests,
                "oauth_tokens": self.migrate_oauth_tokens,
            }
            if collection not in method_map:
                logger.error("Unknown collection: %s", collection)
                logger.info("Available: %s", ", ".join(method_map.keys()))
                return
            method_map[collection]()
        else:
            # Migrate all in order (respecting FK dependencies)
            self.migrate_employees()  # Also migrates leave_requests
            self.migrate_office_closures()
            self.migrate_capacity_overrides()
            self.migrate_allocation_requests()
            self.migrate_oauth_tokens()

        # Summary
        logger.info("=" * 60)
        logger.info("Migration %s complete!", "dry run" if self.dry_run else "")
        logger.info("=" * 60)
        total_read = sum(s["read"] for s in self.stats.values())
        total_written = sum(s["written"] for s in self.stats.values())
        total_errors = sum(s["errors"] for s in self.stats.values())
        logger.info("Total: read=%d, written=%d, errors=%d", total_read, total_written, total_errors)

        if self.dry_run:
            logger.info("")
            logger.info("This was a dry run. No data was written to CloudSQL.")
            logger.info("Run with --execute to perform the actual migration.")


def main():
    parser = argparse.ArgumentParser(
        description="Migrate adviser_allocation data from Firestore to CloudSQL"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Read from Firestore but don't write to CloudSQL",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually perform the migration",
    )
    parser.add_argument(
        "--collection",
        type=str,
        choices=["employees", "office_closures", "capacity_overrides", "allocation_requests", "oauth_tokens"],
        help="Migrate only this collection",
    )

    args = parser.parse_args()

    if not args.dry_run and not args.execute:
        print("Error: Specify --dry-run or --execute")
        print("Run with --help for usage information")
        sys.exit(1)

    if args.dry_run and args.execute:
        print("Error: Cannot specify both --dry-run and --execute")
        sys.exit(1)

    encryption_key = os.getenv("AA_TOKEN_ENCRYPTION_KEY")

    migrator = FirestoreToCloudSQLMigrator(
        dry_run=args.dry_run,
        encryption_key=encryption_key,
    )
    migrator.run(collection=args.collection)


if __name__ == "__main__":
    main()
