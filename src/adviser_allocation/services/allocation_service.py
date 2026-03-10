"""Allocation persistence helpers."""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from adviser_allocation.utils.common import get_cloudsql_db, sydney_now

logger = logging.getLogger(__name__)


def store_allocation_record(
    db,  # Legacy parameter, ignored - uses CloudSQL
    data: Dict[str, Any],
    source: str = "webhook",
    extra_fields: Optional[Dict[str, Any]] = None,
    raw_request: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """Persist an allocation request record in CloudSQL.

    Args:
        db: Legacy parameter (ignored) - CloudSQL is used automatically.
        data: Incoming allocation payload.
        source: Identifier for the caller (e.g., webhook name).
        extra_fields: Optional additional metadata to include in the record.
        raw_request: Optional raw request data to store.

    Returns:
        Request ID on success, otherwise ``None``.
    """
    record = {
        "timestamp": sydney_now(),
        "request_data": raw_request if raw_request is not None else data,
        "client_email": data.get("client_email", ""),
        "adviser_email": data.get("adviser_email", ""),
        "adviser_name": data.get("adviser_name", ""),
        "adviser_hubspot_id": data.get("adviser_hubspot_id", ""),
        "adviser_service_packages": data.get("adviser_service_packages", []),
        "adviser_household_types": data.get("adviser_household_types", []),
        "deal_id": data.get("deal_id", ""),
        "service_package": data.get("service_package", ""),
        "service_package_raw": data.get("service_package_raw", ""),
        "household_type": data.get("household_type", ""),
        "household_type_raw": data.get("household_type_raw", ""),
        "agreement_start_date": data.get("agreement_start_date", ""),
        "agreement_start_raw": data.get("agreement_start_raw", ""),
        "allocation_result": data.get("allocation_result", ""),
        "earliest_week": data.get("earliest_week"),
        "earliest_week_label": data.get("earliest_week_label", ""),
        "status": data.get("status", "received"),
        "source": source,
        "error_message": data.get("error_message", ""),
    }

    if extra_fields:
        record.update(extra_fields)

    try:
        cloudsql_db = get_cloudsql_db()
        request_id = cloudsql_db.store_allocation_record(record)
        logger.debug("Stored allocation record %s", request_id)
        return request_id
    except Exception as exc:
        logger.error("Failed to store allocation record: %s", exc)
        return None
