"""Allocation persistence helpers."""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from adviser_allocation.utils.common import sydney_now

logger = logging.getLogger(__name__)


def store_allocation_record(
    db,
    data: Dict[str, Any],
    source: str = "webhook",
    extra_fields: Optional[Dict[str, Any]] = None,
    raw_request: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """Persist an allocation request document in Firestore.

    Args:
        db: Firestore client (may be None).
        data: Incoming allocation payload.
        source: Identifier for the caller (e.g., webhook name).
        extra_fields: Optional additional metadata to include in the record.

    Returns:
        Firestore document id on success, otherwise ``None``.
    """
    if not db:
        logger.error("Firestore not configured; allocation record not stored")
        return None

    record = {
        "timestamp": sydney_now().isoformat(),
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
        "earliest_week": data.get("earliest_week", ""),
        "status": data.get("status", "received"),
        "source": source,
        "error_message": data.get("error_message", ""),
    }

    if extra_fields:
        record.update(extra_fields)

    try:
        doc_ref = db.collection("allocation_requests").document()
        doc_ref.set(record)
        logger.debug("Stored allocation record %s", doc_ref.id)
        return doc_ref.id
    except Exception as exc:  # pragma: no cover
        logger.error("Failed to store allocation record: %s", exc)
        return None
