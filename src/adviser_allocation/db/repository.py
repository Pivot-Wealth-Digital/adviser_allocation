"""
Database repository for adviser_allocation.
Encapsulates all CloudSQL queries using raw SQL with parameterized queries.
"""

import json
import logging
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import text
from sqlalchemy.engine import Engine

from .models import (
    AllocationRequest,
    CapacityOverride,
    Employee,
    LeaveRequest,
    OfficeClosure,
)

logger = logging.getLogger(__name__)


class AdviserAllocationDB:
    """Data access layer for adviser_allocation CloudSQL tables."""

    def __init__(self, engine: Engine):
        self.engine = engine

    # =========================================================================
    # EMPLOYEES
    # =========================================================================

    def get_employee_by_email(self, email: str) -> Optional[Employee]:
        """Get employee by company email."""
        with self.engine.connect() as conn:
            result = conn.execute(
                text("""
                    SELECT employee_id, name, company_email, account_email,
                           client_limit_monthly, pod_type_effective, hubspot_owner_id,
                           is_active, created_at, updated_at, last_synced
                    FROM aa_employees
                    WHERE company_email = :email
                    LIMIT 1
                """),
                {"email": email},
            )
            row = result.fetchone()
            if not row:
                return None
            return Employee(
                employee_id=row.employee_id,
                name=row.name,
                company_email=row.company_email,
                account_email=row.account_email,
                client_limit_monthly=row.client_limit_monthly or 6,
                pod_type_effective=row.pod_type_effective,
                hubspot_owner_id=row.hubspot_owner_id,
                is_active=row.is_active,
                created_at=row.created_at,
                updated_at=row.updated_at,
                last_synced=row.last_synced,
            )

    def get_employee_id_by_email(self, email: str) -> Optional[str]:
        """Get employee ID by company email (for backwards compatibility)."""
        emp = self.get_employee_by_email(email)
        return emp.employee_id if emp else None

    def upsert_employee(self, emp: Employee) -> None:
        """Insert or update employee record."""
        with self.engine.begin() as conn:
            conn.execute(
                text("""
                    INSERT INTO aa_employees (
                        employee_id, name, company_email, account_email,
                        client_limit_monthly, pod_type_effective, hubspot_owner_id,
                        is_active, last_synced
                    ) VALUES (
                        :employee_id, :name, :company_email, :account_email,
                        :client_limit_monthly, :pod_type_effective, :hubspot_owner_id,
                        :is_active, :last_synced
                    )
                    ON CONFLICT (employee_id) DO UPDATE SET
                        name = EXCLUDED.name,
                        company_email = EXCLUDED.company_email,
                        account_email = EXCLUDED.account_email,
                        client_limit_monthly = EXCLUDED.client_limit_monthly,
                        pod_type_effective = EXCLUDED.pod_type_effective,
                        hubspot_owner_id = EXCLUDED.hubspot_owner_id,
                        is_active = EXCLUDED.is_active,
                        last_synced = EXCLUDED.last_synced,
                        updated_at = CURRENT_TIMESTAMP
                """),
                {
                    "employee_id": emp.employee_id,
                    "name": emp.name,
                    "company_email": emp.company_email,
                    "account_email": emp.account_email,
                    "client_limit_monthly": emp.client_limit_monthly,
                    "pod_type_effective": emp.pod_type_effective,
                    "hubspot_owner_id": emp.hubspot_owner_id,
                    "is_active": emp.is_active,
                    "last_synced": emp.last_synced or datetime.utcnow(),
                },
            )

    # =========================================================================
    # LEAVE REQUESTS
    # =========================================================================

    def get_employee_leaves(self, employee_id: str) -> List[LeaveRequest]:
        """Get all leave requests for an employee."""
        with self.engine.connect() as conn:
            result = conn.execute(
                text("""
                    SELECT id, leave_request_id, employee_id, start_date, end_date,
                           leave_type, status, created_at, updated_at, last_synced
                    FROM aa_leave_requests
                    WHERE employee_id = :employee_id
                    ORDER BY start_date DESC
                """),
                {"employee_id": employee_id},
            )
            return [
                LeaveRequest(
                    id=row.id,
                    leave_request_id=row.leave_request_id,
                    employee_id=row.employee_id,
                    start_date=row.start_date,
                    end_date=row.end_date,
                    leave_type=row.leave_type,
                    status=row.status or "approved",
                    created_at=row.created_at,
                    updated_at=row.updated_at,
                    last_synced=row.last_synced,
                )
                for row in result
            ]

    def get_employee_leaves_as_dicts(self, employee_id: str) -> List[Dict[str, Any]]:
        """Get employee leaves as dictionaries (for backwards compatibility)."""
        leaves = self.get_employee_leaves(employee_id)
        return [
            {
                "leave_request_id": lr.leave_request_id,
                "employee_id": lr.employee_id,
                "start_date": lr.start_date.isoformat() if lr.start_date else None,
                "end_date": lr.end_date.isoformat() if lr.end_date else None,
                "leave_type": lr.leave_type,
                "status": lr.status,
            }
            for lr in leaves
        ]

    def upsert_leave_request(self, leave: LeaveRequest) -> None:
        """Insert or update leave request."""
        with self.engine.begin() as conn:
            conn.execute(
                text("""
                    INSERT INTO aa_leave_requests (
                        leave_request_id, employee_id, start_date, end_date,
                        leave_type, status, last_synced
                    ) VALUES (
                        :leave_request_id, :employee_id, :start_date, :end_date,
                        :leave_type, :status, :last_synced
                    )
                    ON CONFLICT (employee_id, leave_request_id) DO UPDATE SET
                        start_date = EXCLUDED.start_date,
                        end_date = EXCLUDED.end_date,
                        leave_type = EXCLUDED.leave_type,
                        status = EXCLUDED.status,
                        last_synced = EXCLUDED.last_synced,
                        updated_at = CURRENT_TIMESTAMP
                """),
                {
                    "leave_request_id": leave.leave_request_id,
                    "employee_id": leave.employee_id,
                    "start_date": leave.start_date,
                    "end_date": leave.end_date,
                    "leave_type": leave.leave_type,
                    "status": leave.status,
                    "last_synced": leave.last_synced or datetime.utcnow(),
                },
            )

    # =========================================================================
    # OFFICE CLOSURES
    # =========================================================================

    def get_global_closures(self) -> List[Dict[str, Any]]:
        """Get all office closures as dictionaries."""
        with self.engine.connect() as conn:
            result = conn.execute(
                text("""
                    SELECT closure_id, start_date, end_date, description, tags,
                           created_at, updated_at, created_by,
                           google_event_id, last_synced
                    FROM aa_office_closures
                    ORDER BY start_date DESC
                """)
            )
            closures = []
            for row in result:
                closures.append(
                    {
                        "id": str(row.closure_id),
                        "start_date": row.start_date.isoformat() if row.start_date else None,
                        "end_date": row.end_date.isoformat() if row.end_date else None,
                        "description": row.description or "",
                        "tags": row.tags or [],
                        "created_at": row.created_at.isoformat() if row.created_at else None,
                        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
                        "google_event_id": row.google_event_id,
                    }
                )
            return closures

    def insert_office_closure(
        self,
        start_date: date,
        end_date: date,
        description: str = None,
        tags: List[str] = None,
        created_by: str = None,
    ) -> str:
        """Insert office closure. Returns closure_id."""
        with self.engine.begin() as conn:
            result = conn.execute(
                text("""
                    INSERT INTO aa_office_closures (
                        start_date, end_date, description, tags, created_by
                    ) VALUES (
                        :start_date, :end_date, :description, :tags, :created_by
                    )
                    RETURNING closure_id
                """),
                {
                    "start_date": start_date,
                    "end_date": end_date,
                    "description": description,
                    "tags": tags or [],
                    "created_by": created_by,
                },
            )
            row = result.fetchone()
            return str(row.closure_id)

    def update_office_closure(
        self,
        closure_id: str,
        start_date: date = None,
        end_date: date = None,
        description: str = None,
        tags: List[str] = None,
    ) -> bool:
        """Update office closure. Returns True if updated."""
        updates = []
        params = {"closure_id": closure_id}

        if start_date is not None:
            updates.append("start_date = :start_date")
            params["start_date"] = start_date
        if end_date is not None:
            updates.append("end_date = :end_date")
            params["end_date"] = end_date
        if description is not None:
            updates.append("description = :description")
            params["description"] = description
        if tags is not None:
            updates.append("tags = :tags")
            params["tags"] = tags

        if not updates:
            return False

        with self.engine.begin() as conn:
            result = conn.execute(
                text(f"""
                    UPDATE aa_office_closures
                    SET {", ".join(updates)}, updated_at = CURRENT_TIMESTAMP
                    WHERE closure_id = :closure_id
                """),
                params,
            )
            return result.rowcount > 0

    def delete_office_closure(self, closure_id: str) -> bool:
        """Delete office closure. Returns True if deleted."""
        with self.engine.begin() as conn:
            result = conn.execute(
                text("DELETE FROM aa_office_closures WHERE closure_id = :closure_id"),
                {"closure_id": closure_id},
            )
            return result.rowcount > 0

    def upsert_office_closure_by_event_id(
        self,
        google_event_id: str,
        start_date: date,
        end_date: date,
        description: str = None,
        tags: List[str] = None,
    ) -> str:
        """Insert or update a calendar-synced closure by Google event ID.

        Uses ON CONFLICT on the google_event_id unique index.
        Manual closures (google_event_id IS NULL) are never touched.

        Returns:
            The closure_id of the upserted record.
        """
        with self.engine.begin() as conn:
            result = conn.execute(
                text("""
                    INSERT INTO aa_office_closures (
                        google_event_id, start_date, end_date, description, tags,
                        created_by, last_synced
                    ) VALUES (
                        :google_event_id, :start_date, :end_date, :description, :tags,
                        'calendar_sync', NOW()
                    )
                    ON CONFLICT (google_event_id)
                    WHERE google_event_id IS NOT NULL
                    DO UPDATE SET
                        start_date  = EXCLUDED.start_date,
                        end_date    = EXCLUDED.end_date,
                        description = EXCLUDED.description,
                        tags        = EXCLUDED.tags,
                        last_synced = NOW(),
                        updated_at  = NOW()
                    RETURNING closure_id
                """),
                {
                    "google_event_id": google_event_id,
                    "start_date": start_date,
                    "end_date": end_date,
                    "description": description or "",
                    "tags": tags or [],
                },
            )
            row = result.fetchone()
            return str(row.closure_id)

    def delete_stale_calendar_closures(self, active_event_ids: List[str]) -> int:
        """Delete calendar-synced closures no longer present in the calendar.

        Only affects rows with a non-NULL google_event_id.
        Manual closures are never deleted by this method.

        Returns:
            Number of rows deleted.
        """
        if not active_event_ids:
            logger.warning(
                "delete_stale_calendar_closures called with empty active_event_ids; "
                "skipping deletion to avoid data loss"
            )
            return 0

        with self.engine.begin() as conn:
            result = conn.execute(
                text("""
                    DELETE FROM aa_office_closures
                    WHERE google_event_id IS NOT NULL
                      AND google_event_id != ALL(:active_ids)
                """),
                {"active_ids": active_event_ids},
            )
            count = result.rowcount
            if count > 0:
                logger.info("Deleted %d stale calendar closures", count)
            return count

    # =========================================================================
    # CAPACITY OVERRIDES
    # =========================================================================

    def get_capacity_overrides(self) -> List[Dict[str, Any]]:
        """Get all capacity overrides as dictionaries."""
        with self.engine.connect() as conn:
            result = conn.execute(
                text("""
                    SELECT override_id, adviser_email, effective_date, effective_start,
                           effective_week, client_limit_monthly, pod_type, notes,
                           created_at, updated_at, created_by
                    FROM aa_capacity_overrides
                    ORDER BY adviser_email, effective_date DESC
                """)
            )
            overrides = []
            for row in result:
                overrides.append(
                    {
                        "id": str(row.override_id),
                        "adviser_email": row.adviser_email,
                        "effective_date": row.effective_date.isoformat()
                        if row.effective_date
                        else None,
                        "effective_start": row.effective_start.isoformat()
                        if row.effective_start
                        else None,
                        "effective_week": row.effective_week,
                        "client_limit_monthly": row.client_limit_monthly,
                        "pod_type": row.pod_type,
                        "notes": row.notes or "",
                        "created_at": row.created_at.isoformat() if row.created_at else None,
                        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
                    }
                )
            return overrides

    def insert_capacity_override(
        self,
        adviser_email: str = None,
        effective_date: date = None,
        client_limit_monthly: int = None,
        pod_type: str = None,
        notes: str = None,
        effective_start: date = None,
        effective_week: int = None,
        created_by: str = None,
        override: CapacityOverride = None,
    ) -> str:
        """Insert capacity override. Returns override_id.

        Can be called with individual params or a CapacityOverride object.
        """
        # Support both object and keyword argument patterns
        if override is not None:
            adviser_email = override.adviser_email
            effective_date = override.effective_date
            effective_start = override.effective_start
            effective_week = override.effective_week
            client_limit_monthly = override.client_limit_monthly
            pod_type = override.pod_type
            notes = override.notes
            created_by = override.created_by

        with self.engine.begin() as conn:
            result = conn.execute(
                text("""
                    INSERT INTO aa_capacity_overrides (
                        adviser_email, effective_date, effective_start, effective_week,
                        client_limit_monthly, pod_type, notes, created_by
                    ) VALUES (
                        :adviser_email, :effective_date, :effective_start, :effective_week,
                        :client_limit_monthly, :pod_type, :notes, :created_by
                    )
                    RETURNING override_id
                """),
                {
                    "adviser_email": adviser_email,
                    "effective_date": effective_date,
                    "effective_start": effective_start,
                    "effective_week": effective_week,
                    "client_limit_monthly": client_limit_monthly,
                    "pod_type": pod_type,
                    "notes": notes,
                    "created_by": created_by,
                },
            )
            row = result.fetchone()
            return str(row.override_id)

    def update_capacity_override(
        self,
        override_id: str,
        adviser_email: str = None,
        effective_date: date = None,
        effective_start: date = None,
        effective_week: int = None,
        client_limit_monthly: int = None,
        pod_type: str = None,
        notes: str = None,
    ) -> bool:
        """Update capacity override. Returns True if updated."""
        updates = []
        params = {"override_id": override_id}

        if adviser_email is not None:
            updates.append("adviser_email = :adviser_email")
            params["adviser_email"] = adviser_email
        if effective_date is not None:
            updates.append("effective_date = :effective_date")
            params["effective_date"] = effective_date
        if effective_start is not None:
            updates.append("effective_start = :effective_start")
            params["effective_start"] = effective_start
        if effective_week is not None:
            updates.append("effective_week = :effective_week")
            params["effective_week"] = effective_week
        if client_limit_monthly is not None:
            updates.append("client_limit_monthly = :client_limit_monthly")
            params["client_limit_monthly"] = client_limit_monthly
        if pod_type is not None:
            updates.append("pod_type = :pod_type")
            params["pod_type"] = pod_type
        if notes is not None:
            updates.append("notes = :notes")
            params["notes"] = notes

        if not updates:
            return False

        with self.engine.begin() as conn:
            result = conn.execute(
                text(f"""
                    UPDATE aa_capacity_overrides
                    SET {", ".join(updates)}, updated_at = CURRENT_TIMESTAMP
                    WHERE override_id = :override_id
                """),
                params,
            )
            return result.rowcount > 0

    def delete_capacity_override(self, override_id: str) -> bool:
        """Delete capacity override. Returns True if deleted."""
        with self.engine.begin() as conn:
            result = conn.execute(
                text("DELETE FROM aa_capacity_overrides WHERE override_id = :override_id"),
                {"override_id": override_id},
            )
            return result.rowcount > 0

    # =========================================================================
    # ALLOCATION REQUESTS (Audit Log)
    # =========================================================================

    def store_allocation_record(self, data: Dict[str, Any]) -> str:
        """Store allocation request record. Returns request_id."""

        # Helper to convert empty strings to None for nullable fields
        def _nullable(value):
            if value == "" or value is None:
                return None
            return value

        with self.engine.begin() as conn:
            result = conn.execute(
                text("""
                    INSERT INTO aa_allocation_requests (
                        request_data, client_email, deal_id, service_package,
                        service_package_raw, household_type, household_type_raw,
                        agreement_start_date, agreement_start_raw,
                        adviser_email, adviser_name, adviser_hubspot_id,
                        adviser_service_packages, adviser_household_types,
                        allocation_result, earliest_week, earliest_week_label,
                        status, error_message, source, ip_address, user_agent,
                        candidates_summary, timestamp
                    ) VALUES (
                        :request_data, :client_email, :deal_id, :service_package,
                        :service_package_raw, :household_type, :household_type_raw,
                        :agreement_start_date, :agreement_start_raw,
                        :adviser_email, :adviser_name, :adviser_hubspot_id,
                        :adviser_service_packages, :adviser_household_types,
                        :allocation_result, :earliest_week, :earliest_week_label,
                        :status, :error_message, :source, :ip_address, :user_agent,
                        :candidates_summary, :timestamp
                    )
                    RETURNING request_id
                """),
                {
                    "request_data": json.dumps(data.get("request_data", data)),
                    "client_email": _nullable(data.get("client_email")),
                    "deal_id": _nullable(data.get("deal_id")),
                    "service_package": _nullable(data.get("service_package")),
                    "service_package_raw": _nullable(data.get("service_package_raw")),
                    "household_type": _nullable(data.get("household_type")),
                    "household_type_raw": _nullable(data.get("household_type_raw")),
                    "agreement_start_date": _nullable(data.get("agreement_start_date")),
                    "agreement_start_raw": _nullable(data.get("agreement_start_raw")),
                    "adviser_email": _nullable(data.get("adviser_email")),
                    "adviser_name": _nullable(data.get("adviser_name")),
                    "adviser_hubspot_id": _nullable(data.get("adviser_hubspot_id")),
                    "adviser_service_packages": data.get("adviser_service_packages") or [],
                    "adviser_household_types": data.get("adviser_household_types") or [],
                    "allocation_result": _nullable(data.get("allocation_result")),
                    "earliest_week": _nullable(data.get("earliest_week")) or None,
                    "earliest_week_label": _nullable(data.get("earliest_week_label")),
                    "status": data.get("status") or "received",
                    "error_message": _nullable(data.get("error_message")),
                    "source": _nullable(data.get("source")),
                    "ip_address": _nullable(data.get("ip_address")),
                    "user_agent": _nullable(data.get("user_agent")),
                    "candidates_summary": json.dumps(data.get("candidates_summary"))
                    if data.get("candidates_summary")
                    else None,
                    "timestamp": data.get("timestamp") or datetime.utcnow(),
                },
            )
            row = result.fetchone()
            return str(row.request_id)

    def get_allocation_history(
        self,
        deal_id: str = None,
        adviser_email: str = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """Get allocation history with optional filters."""
        conditions = []
        params = {"limit": limit, "offset": offset}

        if deal_id:
            conditions.append("deal_id = :deal_id")
            params["deal_id"] = deal_id
        if adviser_email:
            conditions.append("adviser_email = :adviser_email")
            params["adviser_email"] = adviser_email

        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""

        with self.engine.connect() as conn:
            result = conn.execute(
                text(f"""
                    SELECT request_id, timestamp, request_data, client_email, deal_id,
                           service_package, household_type, adviser_email, adviser_name,
                           allocation_result, status, error_message, source
                    FROM aa_allocation_requests
                    {where_clause}
                    ORDER BY timestamp DESC
                    LIMIT :limit OFFSET :offset
                """),
                params,
            )
            records = []
            for row in result:
                records.append(
                    {
                        "doc_id": str(row.request_id),
                        "timestamp": row.timestamp.isoformat() if row.timestamp else None,
                        "request_data": (
                            row.request_data
                            if isinstance(row.request_data, dict)
                            else json.loads(row.request_data)
                            if row.request_data
                            else {}
                        ),
                        "client_email": row.client_email,
                        "deal_id": row.deal_id,
                        "service_package": row.service_package,
                        "household_type": row.household_type,
                        "adviser_email": row.adviser_email,
                        "adviser_name": row.adviser_name,
                        "allocation_result": row.allocation_result,
                        "status": row.status,
                        "error_message": row.error_message,
                        "source": row.source,
                    }
                )
            return records

    # =========================================================================
    # OAUTH TOKENS
    # =========================================================================

    def save_tokens(
        self,
        token_key: str,
        provider: str,
        tokens: Dict[str, Any],
        encryption_key: str,
    ) -> None:
        """Save encrypted OAuth tokens."""
        with self.engine.begin() as conn:
            conn.execute(
                text("""
                    INSERT INTO aa_oauth_tokens (
                        token_key, provider, encrypted_tokens, expires_at, token_type
                    ) VALUES (
                        :token_key, :provider,
                        pgp_sym_encrypt(:tokens_json, :encryption_key),
                        :expires_at, :token_type
                    )
                    ON CONFLICT (token_key) DO UPDATE SET
                        provider = EXCLUDED.provider,
                        encrypted_tokens = pgp_sym_encrypt(:tokens_json, :encryption_key),
                        expires_at = EXCLUDED.expires_at,
                        token_type = EXCLUDED.token_type,
                        updated_at = CURRENT_TIMESTAMP
                """),
                {
                    "token_key": token_key,
                    "provider": provider,
                    "tokens_json": json.dumps(tokens),
                    "encryption_key": encryption_key,
                    "expires_at": tokens.get("_expires_at"),
                    "token_type": tokens.get("token_type", "Bearer"),
                },
            )

    def load_tokens(
        self,
        token_key: str,
        encryption_key: str,
    ) -> Optional[Dict[str, Any]]:
        """Load and decrypt OAuth tokens."""
        with self.engine.connect() as conn:
            result = conn.execute(
                text("""
                    SELECT pgp_sym_decrypt(encrypted_tokens, :encryption_key)::text as tokens_json,
                           provider, expires_at, token_type
                    FROM aa_oauth_tokens
                    WHERE token_key = :token_key
                """),
                {"token_key": token_key, "encryption_key": encryption_key},
            )
            row = result.fetchone()
            if not row:
                return None
            try:
                tokens = json.loads(row.tokens_json)
                tokens["provider"] = row.provider
                tokens["token_type"] = row.token_type
                return tokens
            except (json.JSONDecodeError, TypeError) as e:
                logger.error("Failed to decrypt tokens for %s: %s", token_key, e)
                return None

    # =========================================================================
    # CONVENIENCE / COMPATIBILITY METHODS
    # =========================================================================

    def upsert_employee_dict(self, data: Dict[str, Any]) -> None:
        """Upsert employee from a dictionary (for sync operations)."""
        emp = Employee(
            employee_id=data.get("id") or data.get("employee_id"),
            name=data.get("name", ""),
            company_email=data.get("company_email", ""),
            account_email=data.get("account_email"),
            client_limit_monthly=data.get("client_limit_monthly", 6),
            pod_type_effective=data.get("pod_type_effective"),
            hubspot_owner_id=data.get("hubspot_owner_id"),
            is_active=data.get("is_active", True),
            last_synced=datetime.utcnow(),
        )
        self.upsert_employee(emp)

    def upsert_leave_request_dict(self, data: Dict[str, Any]) -> None:
        """Upsert leave request from a dictionary (for sync operations)."""
        leave = LeaveRequest(
            leave_request_id=data.get("leave_request_id"),
            employee_id=data.get("employee_id"),
            start_date=self._parse_date(data.get("start_date")),
            end_date=self._parse_date(data.get("end_date")),
            leave_type=data.get("leave_type"),
            status=data.get("status", "approved"),
            last_synced=datetime.utcnow(),
        )
        self.upsert_leave_request(leave)

    def _parse_date(self, value) -> Optional[date]:
        """Parse date from various formats."""
        if value is None:
            return None
        if isinstance(value, date):
            return value
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
            except ValueError:
                return None
        return None

    # =========================================================================
    # CLARIFY CHART DATA (from database view)
    # =========================================================================

    def get_clarify_chart_data(
        self,
        weeks: int = 12,
        adviser_email: str = None,
    ) -> List[Dict[str, Any]]:
        """Get clarify chart data from the clarify_chart_data view.

        Args:
            weeks: Number of weeks of data to return (default 12)
            adviser_email: Optional filter for specific adviser

        Returns:
            List of dicts with week_commencing, adviser_email, booked_clarifies,
            simulated_clarifies, total_clarifies
        """
        from datetime import timedelta

        from adviser_allocation.utils.common import sydney_today

        # Calculate cutoff date (weeks ago from today)
        cutoff_date = sydney_today() - timedelta(weeks=weeks)

        conditions = ["week_commencing >= :cutoff_date"]
        params = {"cutoff_date": cutoff_date}

        if adviser_email:
            conditions.append("LOWER(adviser_email) = LOWER(:adviser_email)")
            params["adviser_email"] = adviser_email

        where_clause = " AND ".join(conditions)

        with self.engine.connect() as conn:
            result = conn.execute(
                text(f"""
                    SELECT week_commencing, adviser_email, booked_clarifies,
                           simulated_clarifies, total_clarifies
                    FROM clarify_chart_data
                    WHERE {where_clause}
                    ORDER BY week_commencing, adviser_email
                """),
                params,
            )
            return [
                {
                    "week_commencing": row.week_commencing,
                    "adviser_email": row.adviser_email,
                    "booked_clarifies": row.booked_clarifies,
                    "simulated_clarifies": row.simulated_clarifies,
                    "total_clarifies": row.total_clarifies,
                }
                for row in result
            ]

    # =========================================================================
    # SIMULATED CLARIFIES
    # =========================================================================

    def replace_simulated_clarifies(self, assignments: List[Dict[str, Any]]) -> int:
        """Replace all simulated clarifies with new assignments.

        Deletes existing records and inserts new ones in a transaction.

        Args:
            assignments: List of dicts with deal_id, adviser_email, projected_week, etc.

        Returns:
            Number of records inserted
        """
        if not assignments:
            # Clear all existing
            with self.engine.begin() as conn:
                conn.execute(text("DELETE FROM aa_simulated_clarifies"))
            return 0

        with self.engine.begin() as conn:
            # Delete all existing records
            conn.execute(text("DELETE FROM aa_simulated_clarifies"))

            # Insert new records
            for a in assignments:
                conn.execute(
                    text("""
                        INSERT INTO aa_simulated_clarifies (
                            deal_id, adviser_email, projected_week,
                            agreement_start_date, deal_name, client_email,
                            computed_at, computation_batch_id
                        ) VALUES (
                            :deal_id, :adviser_email, :projected_week,
                            :agreement_start_date, :deal_name, :client_email,
                            :computed_at, :computation_batch_id
                        )
                    """),
                    {
                        "deal_id": a.get("deal_id"),
                        "adviser_email": a.get("adviser_email"),
                        "projected_week": a.get("projected_week"),
                        "agreement_start_date": a.get("agreement_start_date"),
                        "deal_name": a.get("deal_name"),
                        "client_email": a.get("client_email"),
                        "computed_at": a.get("computed_at") or datetime.utcnow(),
                        "computation_batch_id": a.get("computation_batch_id"),
                    },
                )

        return len(assignments)

    def get_simulated_clarifies_by_week(
        self,
        weeks: int = 12,
        adviser_email: str = None,
    ) -> List[Dict[str, Any]]:
        """Get simulated clarifies aggregated by week and adviser.

        Args:
            weeks: Number of weeks of data to return
            adviser_email: Optional filter for specific adviser

        Returns:
            List of dicts with week_commencing, adviser_email, simulated_count
        """
        from datetime import timedelta

        from adviser_allocation.utils.common import sydney_today

        cutoff_date = sydney_today() - timedelta(weeks=weeks)

        conditions = ["projected_week >= :cutoff_date"]
        params = {"cutoff_date": cutoff_date}

        if adviser_email:
            conditions.append("LOWER(adviser_email) = LOWER(:adviser_email)")
            params["adviser_email"] = adviser_email

        where_clause = " AND ".join(conditions)

        with self.engine.connect() as conn:
            result = conn.execute(
                text(f"""
                    SELECT projected_week AS week_commencing,
                           adviser_email,
                           COUNT(*) AS simulated_count
                    FROM aa_simulated_clarifies
                    WHERE {where_clause}
                    GROUP BY projected_week, adviser_email
                    ORDER BY projected_week, adviser_email
                """),
                params,
            )
            return [
                {
                    "week_commencing": row.week_commencing,
                    "adviser_email": row.adviser_email,
                    "simulated_count": row.simulated_count,
                }
                for row in result
            ]

    def get_all_employees(self, active_only: bool = False) -> List[Dict[str, Any]]:
        """Get all employees as dictionaries."""
        with self.engine.connect() as conn:
            query = """
                SELECT employee_id, name, company_email, account_email,
                       client_limit_monthly, pod_type_effective, hubspot_owner_id,
                       is_active, created_at, updated_at, last_synced
                FROM aa_employees
            """
            if active_only:
                query += " WHERE is_active = TRUE"
            query += " ORDER BY name"

            result = conn.execute(text(query))
            return [
                {
                    "employee_id": row.employee_id,
                    "id": row.employee_id,  # Alias for compatibility
                    "doc_id": row.employee_id,  # Alias for UI templates
                    "name": row.name,
                    "company_email": row.company_email,
                    "account_email": row.account_email,
                    "client_limit_monthly": row.client_limit_monthly or 6,
                    "pod_type_effective": row.pod_type_effective,
                    "hubspot_owner_id": row.hubspot_owner_id,
                    "is_active": row.is_active,
                }
                for row in result
            ]
