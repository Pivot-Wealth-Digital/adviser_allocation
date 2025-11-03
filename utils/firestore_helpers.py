"""Shared Firestore access helpers."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from utils.common import get_firestore_client


def _client():
    """Return the Firestore client if configured, otherwise ``None``."""
    return get_firestore_client()


def get_employee_leaves(employee_id: str) -> List[Dict[str, Any]]:
    """Return leave records for the supplied employee id."""
    db = _client()
    if not db:
        logging.warning("Firestore unavailable when reading leaves for %s", employee_id)
        return []

    leaves: List[Dict[str, Any]] = []
    try:
        leaves_ref = db.collection("employees").document(employee_id).collection("leave_requests")
        for doc in leaves_ref.stream():
            leaves.append(doc.to_dict())
    except Exception as exc:  # pragma: no cover
        logging.error("Failed to read leave requests for %s: %s", employee_id, exc)
    return leaves


def get_employee_id(email: str) -> Optional[str]:
    """Return the Firestore employee id for the provided company email."""
    db = _client()
    if not db:
        logging.warning("Firestore unavailable when looking up employee for %s", email)
        return None

    try:
        docs = db.collection("employees").where("company_email", "==", email).stream()
        for doc in docs:
            return doc.id
    except Exception as exc:  # pragma: no cover
        logging.error("Failed to query employee id for %s: %s", email, exc)
    return None


def get_global_closures() -> List[Dict[str, Any]]:
    """Return office closure documents from Firestore."""
    db = _client()
    if not db:
        logging.warning("Firestore unavailable when loading office closures")
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
        logging.error("Failed to list office closures: %s", exc)
    return closures


def get_capacity_overrides() -> List[Dict[str, Any]]:
    """Return adviser capacity override documents from Firestore."""
    db = _client()
    if not db:
        logging.warning("Firestore unavailable when loading capacity overrides")
        return []

    overrides: List[Dict[str, Any]] = []
    try:
        for doc in db.collection("adviser_capacity_overrides").stream():
            data = doc.to_dict() or {}
            data["id"] = doc.id
            overrides.append(data)
    except Exception as exc:  # pragma: no cover
        logging.error("Failed to list adviser capacity overrides: %s", exc)
    return overrides
