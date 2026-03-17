"""Database module for adviser_allocation.

Provides CloudSQL connection management and data access layer.
"""

from .connection import CloudSQLConnector, get_db_engine
from .models import (
    AllocationRequest,
    CalendarWatchChannel,
    CapacityOverride,
    Employee,
    LeaveRequest,
    OfficeClosure,
)
from .repository import AdviserAllocationDB

__all__ = [
    "get_db_engine",
    "CloudSQLConnector",
    "Employee",
    "LeaveRequest",
    "OfficeClosure",
    "CapacityOverride",
    "AllocationRequest",
    "CalendarWatchChannel",
    "AdviserAllocationDB",
]
