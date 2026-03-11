"""
Data models for adviser_allocation CloudSQL tables.
Uses dataclasses (not SQLAlchemy ORM) for consistency with gcs-data-lake patterns.
"""

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Dict, List, Optional


@dataclass
class Employee:
    """Employment Hero employee record."""

    employee_id: str
    name: str
    company_email: str
    account_email: Optional[str] = None
    client_limit_monthly: int = 6
    pod_type_effective: Optional[str] = None
    hubspot_owner_id: Optional[str] = None
    is_active: bool = True
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    last_synced: Optional[datetime] = None


@dataclass
class LeaveRequest:
    """Employee leave request from Employment Hero."""

    leave_request_id: str
    employee_id: str
    start_date: date
    end_date: date
    leave_type: Optional[str] = None
    status: str = "approved"
    id: Optional[int] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    last_synced: Optional[datetime] = None


@dataclass
class OfficeClosure:
    """Global office closure period."""

    start_date: date
    end_date: date
    description: Optional[str] = None
    tags: List[str] = field(default_factory=list)
    closure_id: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    created_by: Optional[str] = None
    google_event_id: Optional[str] = None
    last_synced: Optional[datetime] = None


@dataclass
class CapacityOverride:
    """Temporary adviser capacity override."""

    adviser_email: str
    effective_date: date
    client_limit_monthly: int
    effective_start: Optional[date] = None
    effective_week: Optional[int] = None
    pod_type: Optional[str] = None
    notes: Optional[str] = None
    override_id: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    created_by: Optional[str] = None


@dataclass
class AllocationRequest:
    """Allocation audit log entry."""

    request_data: Dict[str, Any]
    timestamp: Optional[datetime] = None
    client_email: Optional[str] = None
    deal_id: Optional[str] = None
    service_package: Optional[str] = None
    service_package_raw: Optional[str] = None
    household_type: Optional[str] = None
    household_type_raw: Optional[str] = None
    agreement_start_date: Optional[datetime] = None
    agreement_start_raw: Optional[str] = None
    adviser_email: Optional[str] = None
    adviser_name: Optional[str] = None
    adviser_hubspot_id: Optional[str] = None
    adviser_service_packages: List[str] = field(default_factory=list)
    adviser_household_types: List[str] = field(default_factory=list)
    allocation_result: Optional[str] = None
    earliest_week: Optional[int] = None
    earliest_week_label: Optional[str] = None
    status: str = "received"
    error_message: Optional[str] = None
    source: Optional[str] = None
    ip_address: Optional[str] = None
    user_agent: Optional[str] = None
    candidates_summary: Optional[Dict[str, Any]] = None
    request_id: Optional[str] = None
    created_at: Optional[datetime] = None
