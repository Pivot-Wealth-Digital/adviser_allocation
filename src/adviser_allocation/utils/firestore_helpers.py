"""Shared Firestore access helpers with enhanced error handling."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from utils.common import get_firestore_client

logger = logging.getLogger(__name__)


def _client():
    """Return the Firestore client if configured, otherwise ``None``.

    Returns:
        Firestore client or None if not configured
    """
    return get_firestore_client()


def get_employee_leaves(employee_id: str) -> List[Dict[str, Any]]:
    """Return leave records for the supplied employee id.

    Args:
        employee_id: Employee ID in Firestore

    Returns:
        List of leave request dicts, empty list if unavailable or error
    """
    db = _client()
    if not db:
        logger.warning("Firestore unavailable when reading leaves for %s", employee_id)
        return []

    leaves: List[Dict[str, Any]] = []
    try:
        leaves_ref = db.collection("employees").document(employee_id).collection("leave_requests")
        for doc in leaves_ref.stream():
            data = doc.to_dict()
            if data:
                leaves.append(data)
    except Exception as exc:  # pragma: no cover
        logger.error("Failed to read leave requests for %s: %s", employee_id, exc)
    return leaves


def get_employee_id(email: str) -> Optional[str]:
    """Return the Firestore employee id for the provided company email.

    Args:
        email: Company email address

    Returns:
        Employee ID if found, None otherwise
    """
    db = _client()
    if not db:
        logger.warning("Firestore unavailable when looking up employee for %s", email)
        return None

    try:
        docs = db.collection("employees").where("company_email", "==", email).stream()
        for doc in docs:
            return doc.id
    except Exception as exc:  # pragma: no cover
        logger.error("Failed to query employee id for %s: %s", email, exc)
    return None


def get_global_closures() -> List[Dict[str, Any]]:
    """Return office closure documents from Firestore.

    Returns:
        List of closure dicts with start_date and end_date
    """
    db = _client()
    if not db:
        logger.warning("Firestore unavailable when loading office closures")
        return []

    closures: List[Dict[str, Any]] = []
    try:
        for doc in db.collection("office_closures").stream():
            data = doc.to_dict() or {}
            start_date = data.get("start_date")
            end_date = data.get("end_date") or start_date
            if start_date:
                closures.append({"start_date": start_date, "end_date": end_date})
    except Exception as exc:  # pragma: no cover
        logger.error("Failed to list office closures: %s", exc)
    return closures


def get_capacity_overrides() -> List[Dict[str, Any]]:
    """Return adviser capacity override documents from Firestore.

    Returns:
        List of override dicts with adviser_email, client_limit_monthly, effective_date
    """
    db = _client()
    if not db:
        logger.warning("Firestore unavailable when loading capacity overrides")
        return []

    overrides: List[Dict[str, Any]] = []
    try:
        for doc in db.collection("adviser_capacity_overrides").stream():
            data = doc.to_dict() or {}
            data["id"] = doc.id
            overrides.append(data)
    except Exception as exc:  # pragma: no cover
        logger.error("Failed to list adviser capacity overrides: %s", exc)
    return overrides


# Utility functions for write operations

def save_office_closure(start_date: str, end_date: Optional[str] = None) -> Optional[str]:
    """Save an office closure to Firestore.

    Args:
        start_date: Closure start date (ISO format)
        end_date: Optional closure end date (ISO format)

    Returns:
        Document ID if saved, None if error or Firestore unavailable
    """
    db = _client()
    if not db:
        logger.warning("Firestore unavailable when saving office closure")
        return None

    try:
        doc_ref = db.collection("office_closures").document()
        doc_ref.set({
            "start_date": start_date,
            "end_date": end_date or start_date,
        })
        logger.info("Saved office closure: %s to %s", start_date, end_date or start_date)
        return doc_ref.id
    except Exception as exc:
        logger.error("Failed to save office closure: %s", exc)
        return None


def delete_office_closure(closure_id: str) -> bool:
    """Delete an office closure from Firestore.

    Args:
        closure_id: Document ID of closure to delete

    Returns:
        True if deleted, False if error or Firestore unavailable
    """
    db = _client()
    if not db:
        logger.warning("Firestore unavailable when deleting office closure")
        return False

    try:
        db.collection("office_closures").document(closure_id).delete()
        logger.info("Deleted office closure: %s", closure_id)
        return True
    except Exception as exc:
        logger.error("Failed to delete office closure %s: %s", closure_id, exc)
        return False
