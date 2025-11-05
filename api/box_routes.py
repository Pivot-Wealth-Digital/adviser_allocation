import logging
import os
from datetime import datetime, timezone
from functools import lru_cache
from typing import Optional, Tuple, List, Dict
import hashlib

import requests
from flask import Blueprint, jsonify, redirect, render_template, request, session

from services.box_folder_service import (
    BoxAutomationError,
    provision_box_folder,
    ensure_box_service,
    CLIENT_SHARING_SUBFOLDER,
    CLIENT_SHARING_ROLE,
)
from services import box_folder_service as box_service
from utils.secrets import get_secret
from utils.common import (
    get_firestore_client,
    USE_FIRESTORE,
    sydney_today,
    sydney_now,
    SYDNEY_TZ,
)

logger = logging.getLogger(__name__)

box_bp = Blueprint("box_api", __name__)


@lru_cache(maxsize=1)
def _hubspot_portal_id() -> str:
    value = get_secret("HUBSPOT_PORTAL_ID") or os.environ.get("HUBSPOT_PORTAL_ID") or ""
    return value.strip()


def refresh_hubspot_portal_id_cache() -> None:
    _hubspot_portal_id.cache_clear()  # type: ignore[attr-defined]


def _hubspot_contact_url(contact_id: Optional[str]) -> Optional[str]:
    contact_id = (contact_id or "").strip()
    portal_id = _hubspot_portal_id()
    if not contact_id or not portal_id:
        return None
    return f"https://app.hubspot.com/contacts/{portal_id}/record/0-1/{contact_id}"


def _resolve_deal_id(payload: dict) -> Optional[str]:
    return (
        payload.get("deal_id")
        or payload.get("hs_deal_record_id")
        or payload.get("dealId")
        or payload.get("id")
        or (payload.get("object") or {}).get("id")
        or (payload.get("fields") or {}).get("hs_deal_record_id")
    )


def _stable_bucket(value: str, slots: int) -> int:
    normalized = (value or "").strip()
    if not normalized or slots <= 1:
        return 0
    if normalized.isdigit():
        return int(normalized) % slots
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % slots


def _parse_assignee_names(raw: Optional[str], default_slots: int = 8) -> List[str]:
    if raw:
        names = [name.strip() for name in raw.split(",") if name.strip()]
        if names:
            return names[:64]
    return [f"Slot {idx + 1}" for idx in range(default_slots)]


def _normalize_snapshot_entries(raw: Optional[List[object]]) -> List[Dict[str, object]]:
    normalized: List[Dict[str, object]] = []
    if not raw:
        return normalized
    for item in raw:
        entry: Dict[str, object]
        if isinstance(item, str):
            entry = {"id": item.strip()}
        elif isinstance(item, dict):
            entry = {key: value for key, value in item.items() if value not in (None, "")}
        else:
            continue
        folder_id = str(entry.get("id") or "").strip()
        if not folder_id:
            continue
        entry["id"] = folder_id
        normalized.append(entry)
    return normalized


def _record_metadata_snapshot_tag(folder_id: str) -> None:
    folder_id = (folder_id or "").strip()
    if not folder_id:
        return
    if not USE_FIRESTORE:
        return

    db = get_firestore_client()
    if not db:
        return

    doc_ref = db.collection("box_folder_metadata").document("tagging_status")
    now_iso = datetime.now(timezone.utc).isoformat()

    try:
        snapshot = doc_ref.get()
        payload = snapshot.to_dict() if snapshot.exists else {}
        tagged_entries = _normalize_snapshot_entries(payload.get("tagged"))
        untagged_entries = _normalize_snapshot_entries(payload.get("untagged"))

        tagged_by_id = {entry["id"]: entry for entry in tagged_entries}
        untagged_by_id = {entry["id"]: entry for entry in untagged_entries}
        untagged_by_id.pop(folder_id, None)

        entry = tagged_by_id.get(folder_id) or {"id": folder_id}
        entry["tagged_at"] = now_iso
        tagged_by_id[folder_id] = entry

        new_tagged = sorted(tagged_by_id.values(), key=lambda item: item.get("id"))
        new_untagged = sorted(untagged_by_id.values(), key=lambda item: item.get("id"))

        update_doc: Dict[str, object] = {
            "tagged": new_tagged,
            "untagged": new_untagged,
            "total_tagged": len(new_tagged),
            "total_untagged": len(new_untagged),
            "total_scanned": len(new_tagged) + len(new_untagged),
            "updated_at": now_iso,
        }
        if "issues" in payload:
            update_doc["issues"] = payload.get("issues") or []
            update_doc["issue_count"] = len(update_doc["issues"])

        doc_ref.set(update_doc, merge=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Unable to update metadata snapshot for folder %s after tagging: %s",
            folder_id,
            exc,
        )


def _hubspot_headers() -> dict:
    token = get_secret("HUBSPOT_TOKEN") or os.environ.get("HUBSPOT_TOKEN")
    if not token:
        raise RuntimeError("HUBSPOT_TOKEN is not configured")
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


HUBSPOT_BOX_FOLDER_DEAL_PROPERTY = (
    get_secret("HUBSPOT_BOX_FOLDER_DEAL_PROPERTY")
    or os.environ.get("HUBSPOT_BOX_FOLDER_DEAL_PROPERTY")
    or ""
).strip()

HUBSPOT_BOX_FOLDER_CONTACT_PROPERTY = (
    get_secret("HUBSPOT_BOX_FOLDER_CONTACT_PROPERTY")
    or os.environ.get("HUBSPOT_BOX_FOLDER_CONTACT_PROPERTY")
    or "box_folder"
).strip()


def _ensure_logged_in():
    """Enforce session auth for UI views served by this blueprint."""
    if session.get("is_authenticated"):
        return None
    accepts_html = not request.accept_mimetypes or "text/html" in request.accept_mimetypes
    if accepts_html:
        nxt = request.path
        return redirect(f"/login?next={nxt}")
    return jsonify({"error": "Unauthorized"}), 401


def _extract_payload_value(payload: dict, key: str) -> Optional[str]:
    """
    Retrieve a value from common HubSpot workflow payload locations.

    Order of precedence: top-level, fields, inputFields.
    """
    for container in (
        payload,
        payload.get("fields") or {},
        payload.get("inputFields") or {},
    ):
        value = container.get(key)
        if value not in (None, ""):
            return value
    return None


REQUIRED_METADATA_PAYLOAD_FIELDS = [
    "hs_deal_record_id",
    "hs_contact_id",
    "hs_contact_firstname",
    "hs_contact_lastname",
    "hs_contact_email",
    "deal_salutation",
    "household_type",
]

REQUIRED_METADATA_TEMPLATE_FIELDS = {
    "deal_salutation",
    "household_type",
    "primary_contact_id",
    "primary_contact_link",
}


def _missing_metadata_fields(metadata: Optional[dict]) -> List[str]:
    if not metadata:
        return sorted(list(REQUIRED_METADATA_TEMPLATE_FIELDS))

    missing: List[str] = []
    for key in REQUIRED_METADATA_TEMPLATE_FIELDS:
        value = metadata.get(key)
        if value is None:
            missing.append(key)
            continue
        if isinstance(value, str) and not value.strip():
            missing.append(key)
            continue
    return missing


def _build_metadata_from_payload(payload: dict) -> Tuple[dict, List[dict], Optional[str]]:
    """
    Build metadata and contact hints from a HubSpot workflow event payload.

    Returns:
        metadata dict,
        contacts list shaped like HubSpot API results (for folder naming/sharing),
        primary contact email for share override.
    """
    metadata: Dict[str, object] = {}
    contacts: List[dict] = []

    primary_contact_id = _extract_payload_value(payload, "hs_contact_id")
    primary_email = _extract_payload_value(payload, "hs_contact_email")
    primary_first = _extract_payload_value(payload, "hs_contact_firstname")
    primary_last = _extract_payload_value(payload, "hs_contact_lastname")

    spouse_id = _extract_payload_value(payload, "hs_spouse_id")
    spouse_first = _extract_payload_value(payload, "hs_spouse_firstname")
    spouse_last = _extract_payload_value(payload, "hs_spouse_lastname")
    spouse_email = _extract_payload_value(payload, "hs_spouse_email")

    deal_salutation = _extract_payload_value(payload, "deal_salutation")
    household_type = _extract_payload_value(payload, "household_type")

    if deal_salutation:
        metadata["deal_salutation"] = deal_salutation
    if household_type:
        metadata["household_type"] = household_type

    if primary_contact_id:
        metadata["primary_contact_id"] = primary_contact_id
        metadata["primary_contact_link"] = (
            _hubspot_contact_url(primary_contact_id) or primary_contact_id
        )

    if spouse_id:
        metadata["hs_spouse_id"] = spouse_id
        spouse_link = _hubspot_contact_url(spouse_id)
        if spouse_link:
            metadata["spouse_contact_link"] = spouse_link

    def _make_contact(contact_id, first, last, email) -> dict:
        props = {
            "firstname": first or "",
            "lastname": last or "",
            "email": email or "",
        }
        return {"id": contact_id, "properties": props}

    if any([primary_contact_id, primary_first, primary_last, primary_email]):
        contacts.append(
            _make_contact(primary_contact_id, primary_first, primary_last, primary_email)
        )

    if any([spouse_id, spouse_first, spouse_last, spouse_email]):
        contacts.append(
            _make_contact(spouse_id, spouse_first, spouse_last, spouse_email)
        )

    associated_ids: List[str] = []
    associated_contacts: List[dict] = []
    for idx, contact in enumerate(contacts):
        contact_id = contact.get("id")
        props = contact.get("properties") or {}
        first = props.get("firstname") or ""
        last = props.get("lastname") or ""
        email = props.get("email") or ""

        if contact_id:
            associated_ids.append(str(contact_id))

        display = box_service._format_contact_display(contact, position=idx)
        contact_entry = {
            "id": contact_id,
            "firstname": first,
            "lastname": last,
            "email": email,
            "display_name": display,
            "url": _hubspot_contact_url(contact_id),
        }
        associated_contacts.append(contact_entry)

    if associated_ids:
        metadata["associated_contact_ids"] = associated_ids
    if associated_contacts:
        metadata["associated_contacts"] = associated_contacts

    share_email = primary_email or None

    return metadata, contacts, share_email


def _fetch_deal_metadata(deal_id: str) -> Optional[dict]:
    try:
        url = f"https://api.hubapi.com/crm/v3/objects/deals/{deal_id}"
        params = {
            "properties": [
                "hs_deal_record_id",
                "service_package",
                "agreement_start_date",
                "household_type",
                "hs_spouse_id",
                "hs_contact_id",
                "deal_salutation",
            ]
        }
        resp = requests.get(url, headers=_hubspot_headers(), params=params, timeout=10)
        if resp.status_code == 404:
            logger.warning("HubSpot deal %s not found while fetching metadata", deal_id)
            return None
        resp.raise_for_status()
        props = resp.json().get("properties", {})
        contacts = box_service.get_hubspot_deal_contacts(deal_id)
        associated_contact_ids: list[str] = []
        associated_contacts: list[dict] = []
        for idx, contact in enumerate(contacts):
            contact_id = contact.get("id")
            if contact_id:
                associated_contact_ids.append(contact_id)
            props_contact = contact.get("properties") or {}
            associated_contacts.append(
                {
                    "id": contact_id,
                    "firstname": props_contact.get("firstname"),
                    "lastname": props_contact.get("lastname"),
                    "email": props_contact.get("email"),
                    "display_name": box_service._format_contact_display(contact, position=idx),
                    "url": _hubspot_contact_url(contact_id),
                }
            )

        primary_contact_id = props.get("hs_contact_id") or (
            associated_contact_ids[0] if associated_contact_ids else None
        )
        primary_contact_link = _hubspot_contact_url(primary_contact_id)

        spouse_id = props.get("hs_spouse_id")
        if not spouse_id and len(associated_contact_ids) > 1:
            spouse_id = associated_contact_ids[1]
        spouse_link = _hubspot_contact_url(spouse_id)

        metadata: dict[str, object] = {}
        if props.get("household_type"):
            metadata["household_type"] = props.get("household_type")
        if props.get("deal_salutation"):
            metadata["deal_salutation"] = props.get("deal_salutation")
        if primary_contact_id:
            metadata["primary_contact_id"] = primary_contact_id
            metadata["primary_contact_link"] = primary_contact_link or primary_contact_id
        if spouse_id:
            metadata["hs_spouse_id"] = spouse_id
            if spouse_link:
                metadata["spouse_contact_link"] = spouse_link
        if associated_contact_ids:
            metadata["associated_contact_ids"] = associated_contact_ids
        if associated_contacts:
            metadata["associated_contacts"] = associated_contacts

        logger.info("Fetched HubSpot metadata for deal %s: %s", deal_id, metadata)
        return metadata
    except requests.RequestException as exc:
        logger.error("Failed to fetch HubSpot metadata for deal %s: %s", deal_id, exc)
        return None
    except RuntimeError:
        logger.error("HubSpot configuration missing; metadata upload skipped for deal %s", deal_id)
        return None


def _merge_metadata(base: Optional[dict], override: Optional[dict]) -> Optional[dict]:
    if not override:
        return base
    merged = dict(base or {})
    for key, value in override.items():
        merged[key] = value
    return merged


def _extract_payload_value(payload: dict, key: str) -> Optional[str]:
    """
    Retrieve a value from top-level, fields, or inputFields sections of the payload.
    """
    for container in (
        payload,
        payload.get("fields") or {},
        payload.get("inputFields") or {},
    ):
        value = container.get(key)
        if value not in (None, ""):
            return value
    return None


def _build_metadata_from_payload(payload: dict) -> Tuple[dict, List[dict], Optional[str]]:
    """
    Build metadata and contact hints from a HubSpot workflow event payload.

    Returns:
        metadata dict,
        contacts list shaped like HubSpot API results,
        primary contact email for optional sharing.
    """
    metadata: Dict[str, object] = {}
    contacts: List[dict] = []

    primary_contact_id = _extract_payload_value(payload, "hs_contact_id")
    primary_email = _extract_payload_value(payload, "hs_contact_email")
    primary_first = _extract_payload_value(payload, "hs_contact_firstname")
    primary_last = _extract_payload_value(payload, "hs_contact_lastname")

    spouse_id = _extract_payload_value(payload, "hs_spouse_id")
    spouse_first = _extract_payload_value(payload, "hs_spouse_firstname")
    spouse_last = _extract_payload_value(payload, "hs_spouse_lastname")
    spouse_email = _extract_payload_value(payload, "hs_spouse_email")

    deal_salutation = _extract_payload_value(payload, "deal_salutation")
    household_type = _extract_payload_value(payload, "household_type")

    if deal_salutation:
        metadata["deal_salutation"] = deal_salutation
    if household_type:
        metadata["household_type"] = household_type

    if primary_contact_id:
        metadata["primary_contact_id"] = primary_contact_id
        metadata["primary_contact_link"] = (
            _hubspot_contact_url(primary_contact_id) or primary_contact_id
        )

    if spouse_id:
        metadata["hs_spouse_id"] = spouse_id
        spouse_link = _hubspot_contact_url(spouse_id)
        if spouse_link:
            metadata["spouse_contact_link"] = spouse_link

    def _make_contact(contact_id, first, last, email) -> dict:
        props = {
            "firstname": first or "",
            "lastname": last or "",
            "email": email or "",
        }
        return {"id": contact_id, "properties": props}

    if any([primary_contact_id, primary_first, primary_last, primary_email]):
        contacts.append(
            _make_contact(primary_contact_id, primary_first, primary_last, primary_email)
        )

    if any([spouse_id, spouse_first, spouse_last, spouse_email]):
        contacts.append(
            _make_contact(spouse_id, spouse_first, spouse_last, spouse_email)
        )

    associated_ids: List[str] = []
    associated_contacts: List[dict] = []
    for idx, contact in enumerate(contacts):
        contact_id = contact.get("id")
        props = contact.get("properties") or {}
        first = props.get("firstname") or ""
        last = props.get("lastname") or ""
        email = props.get("email") or ""

        if contact_id:
            associated_ids.append(str(contact_id))

        display = box_service._format_contact_display(contact, position=idx)
        contact_entry = {
            "id": contact_id,
            "firstname": first,
            "lastname": last,
            "email": email,
            "display_name": display,
            "url": _hubspot_contact_url(contact_id),
        }
        associated_contacts.append(contact_entry)

    if associated_ids:
        metadata["associated_contact_ids"] = associated_ids
    if associated_contacts:
        metadata["associated_contacts"] = associated_contacts

    share_email = primary_email or None
    return metadata, contacts, share_email


def _search_hubspot_contact_by_email(email: str) -> Optional[dict]:
    email = (email or "").strip()
    if not email:
        return None
    payload = {
        "filterGroups": [
            {
                "filters": [
                    {
                        "propertyName": "email",
                        "operator": "EQ",
                        "value": email,
                    }
                ]
            }
        ],
        "properties": ["firstname", "lastname", "email", "phone"],
        "limit": 1,
    }

    timeouts = (10, 25)
    last_exc: Optional[requests.RequestException] = None
    for timeout in timeouts:
        try:
            resp = requests.post(
                "https://api.hubapi.com/crm/v3/objects/contacts/search",
                headers=_hubspot_headers(),
                json=payload,
                timeout=timeout,
            )
            resp.raise_for_status()
            results = resp.json().get("results", [])
            return results[0] if results else None
        except requests.Timeout as exc:
            logger.warning(
                "HubSpot contact search timed out for %s after %ss; retrying...",
                email,
                timeout,
            )
            last_exc = exc
            continue
    if last_exc:
        raise last_exc
    return None


def _fetch_contact_associated_deal_ids(contact_id: str) -> List[str]:
    url = f"https://api.hubapi.com/crm/v4/objects/contacts/{contact_id}/associations/deals"
    resp = requests.get(url, headers=_hubspot_headers(), timeout=10)
    resp.raise_for_status()
    return [
        str(entry.get("toObjectId"))
        for entry in resp.json().get("results", [])
        if entry.get("toObjectId")
    ]


def _fetch_hubspot_deal(deal_id: str) -> Optional[dict]:
    try:
        url = f"https://api.hubapi.com/crm/v3/objects/deals/{deal_id}"
        params = {
            "properties": [
                "dealname",
                "dealstage",
                "pipeline",
                "amount",
                "closedate",
                "agreement_start_date",
                "hs_deal_record_id",
            ]
        }
        resp = requests.get(url, headers=_hubspot_headers(), params=params, timeout=10)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        data = resp.json()
        data["url"] = f"https://app.hubspot.com/contacts/{_hubspot_portal_id()}/record/0-3/{deal_id}"
        return data
    except requests.RequestException as exc:
        logger.error("Failed to fetch HubSpot deal %s: %s", deal_id, exc)
        return None


def _update_hubspot_deal_properties(deal_id: str, properties: Dict[str, object]) -> bool:
    deal_id = (deal_id or "").strip()
    if not deal_id or not properties:
        return False
    payload = {"properties": properties}
    try:
        resp = requests.patch(
            f"https://api.hubapi.com/crm/v3/objects/deals/{deal_id}",
            headers=_hubspot_headers(),
            json=payload,
            timeout=10,
        )
        if resp.status_code == 404:
            logger.warning("HubSpot deal %s not found while updating properties %s", deal_id, list(properties.keys()))
            return False
        resp.raise_for_status()
        return True
    except requests.RequestException as exc:
        logger.debug(
            "Failed to update HubSpot deal %s properties %s: %s",
            deal_id,
            list(properties.keys()),
            exc,
        )
        return False


def _update_hubspot_contact_property(contact_id: str, property_name: str, value: str) -> bool:
    contact_id = (contact_id or "").strip()
    property_name = (property_name or "").strip()
    if not contact_id or not property_name:
        return False
    payload = {"properties": {property_name: value}}
    try:
        resp = requests.patch(
            f"https://api.hubapi.com/crm/v3/objects/contacts/{contact_id}",
            headers=_hubspot_headers(),
            json=payload,
            timeout=10,
        )
        if resp.status_code == 404:
            logger.warning("HubSpot contact %s not found when updating %s", contact_id, property_name)
            return False
        resp.raise_for_status()
        return True
    except requests.RequestException as exc:
        logger.debug(
            "Failed to update HubSpot contact %s property %s: %s",
            contact_id,
            property_name,
            exc,
        )
        return False


def _extract_folder_id(payload: dict) -> Optional[str]:
    folder_id = payload.get("folder_id")
    if folder_id:
        return str(folder_id)
    folder = payload.get("folder")
    if isinstance(folder, dict):
        val = folder.get("id")
        if val:
            return str(val)
    return None


@box_bp.route("/box/folder/create", methods=["POST"])
def box_folder_create_only():
    """Create the Box client folder without metadata or sharing."""
    if not request.is_json:
        return jsonify({"message": "Invalid Content-Type"}), 415

    payload = request.get_json() or {}
    deal_id = _resolve_deal_id(payload)
    if not deal_id:
        logger.error("Create folder request missing deal_id; payload keys=%s", list(payload.keys()))
        return jsonify({"message": "deal_id is required"}), 400

    deal_id = str(deal_id)
    folder_name_override = (payload.get("folder_name") or "").strip() or None
    metadata_payload, contacts_hint, share_email = _build_metadata_from_payload(payload)

    primary_contact = contacts_hint[0] if contacts_hint else {}
    primary_props = (primary_contact or {}).get("properties") or {}
    primary_first = (primary_props.get("firstname") or "").strip()
    primary_last = (primary_props.get("lastname") or "").strip()
    if not (primary_first and primary_last):
        logger.error(
            "Create folder request for deal %s missing primary contact names; provided fields=%s",
            deal_id,
            sorted(metadata_payload.keys()),
        )
        return (
            jsonify(
                {
                    "message": "Primary contact first and last name are required",
                    "missing": ["hs_contact_firstname", "hs_contact_lastname"],
                }
            ),
            400,
        )

    salutation = (metadata_payload.get("deal_salutation") or "").strip()
    if not salutation:
        logger.error("Create folder request for deal %s missing deal_salutation", deal_id)
        return (
            jsonify(
                {
                    "message": "deal_salutation is required for folder creation",
                    "missing": ["deal_salutation"],
                }
            ),
            400,
        )

    logger.info(
        "Create-only Box folder request for deal %s (override=%s, metadata_fields=%s)",
        deal_id,
        folder_name_override or "<auto>",
        sorted(metadata_payload.keys()),
    )

    try:
        result = provision_box_folder(
            deal_id,
            contacts_override=contacts_hint,
            folder_name_override=folder_name_override,
        )
    except BoxAutomationError as exc:
        logger.error("Box folder creation failed for deal %s: %s", deal_id, exc)
        return (
            jsonify(
                {
                    "message": "Box folder creation failed",
                    "error": str(exc),
                    "deal_id": deal_id,
                }
            ),
            502,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Unexpected error during Box folder creation for deal %s", deal_id)
        return (
            jsonify(
                {
                    "message": "Unexpected server error during Box folder creation",
                    "error": str(exc),
                    "deal_id": deal_id,
                }
            ),
            500,
        )

    status = result.get("status", "created")
    code = 200 if status == "created" else 202
    response = {
        "deal_id": deal_id,
        "status": status,
        "folder": result.get("folder"),
        "contacts": result.get("contacts"),
        "metadata_fields": sorted(metadata_payload.keys()),
        "share_email_hint": share_email,
    }
    return jsonify(response), code


@box_bp.route("/box/folder/tag", methods=["POST"])
def box_folder_apply_metadata():
    """Apply metadata template to an existing Box folder."""
    if not request.is_json:
        return jsonify({"message": "Invalid Content-Type"}), 415

    payload = request.get_json() or {}
    folder_id = _extract_folder_id(payload)
    if not folder_id:
        return jsonify({"message": "folder_id is required"}), 400

    deal_id = _resolve_deal_id(payload)
    missing_payload_fields = [
        key for key in REQUIRED_METADATA_PAYLOAD_FIELDS if not _extract_payload_value(payload, key)
    ]
    if missing_payload_fields:
        return (
            jsonify(
                {
                    "message": "Missing required payload fields for metadata tagging",
                    "missing": missing_payload_fields,
                }
            ),
            400,
        )

    metadata_payload, _, _ = _build_metadata_from_payload(payload)
    metadata_override = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else None
    metadata = _merge_metadata(metadata_payload, metadata_override) or {}

    missing_metadata = _missing_metadata_fields(metadata)
    if missing_metadata:
        return (
            jsonify(
                {
                    "message": "Missing required metadata fields",
                    "missing": missing_metadata,
                }
            ),
            400,
        )

    service = ensure_box_service()
    if not service:
        return jsonify({"message": "Box automation not configured"}), 503

    try:
        service.apply_metadata_template(folder_id, metadata)
    except BoxAutomationError as exc:
        logger.error("Metadata apply failed for folder %s deal %s: %s", folder_id, deal_id, exc)
        return jsonify({"message": "Box metadata apply failed", "error": str(exc)}), 500

    folder_url = f"https://app.box.com/folder/{folder_id}"
    response = {
        "deal_id": deal_id,
        "folder_id": folder_id,
        "metadata_fields": sorted(metadata.keys()),
        "status": "tagged",
        "box_folder_url": folder_url,
    }
    try:
        _record_metadata_snapshot_tag(folder_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Unable to update metadata snapshot after tagging %s: %s", folder_id, exc)
    if HUBSPOT_BOX_FOLDER_DEAL_PROPERTY:
        if not _update_hubspot_deal_properties(deal_id, {HUBSPOT_BOX_FOLDER_DEAL_PROPERTY: folder_url}):
            logger.warning(
                "Unable to update HubSpot deal %s with box folder url %s",
                deal_id,
                folder_url,
            )
    contact_ids = set()
    primary_contact_id = str(metadata.get("primary_contact_id") or metadata_payload.get("hs_contact_id") or "").strip()
    spouse_contact_id = str(metadata.get("hs_spouse_id") or metadata_payload.get("hs_spouse_id") or "").strip()
    if primary_contact_id:
        contact_ids.add(primary_contact_id)
    if spouse_contact_id:
        contact_ids.add(spouse_contact_id)
    for contact_id in contact_ids:
        if not _update_hubspot_contact_property(contact_id, HUBSPOT_BOX_FOLDER_CONTACT_PROPERTY, folder_url):
            logger.warning(
                "Unable to update HubSpot contact %s with box folder url %s",
                contact_id,
                folder_url,
            )
    return jsonify(response), 200


@box_bp.route("/box/folder/tag/auto", methods=["POST"])
def box_folder_apply_metadata_auto():
    """Apply metadata using minimal payload, fetching missing fields from HubSpot if required."""
    if not request.is_json:
        return jsonify({"message": "Invalid Content-Type"}), 415

    payload = request.get_json() or {}
    folder_id = _extract_folder_id(payload)
    if not folder_id:
        return jsonify({"message": "folder_id is required"}), 400

    deal_id = _resolve_deal_id(payload)
    if not deal_id:
        return jsonify({"message": "deal_id is required"}), 400

    metadata_payload, _, _ = _build_metadata_from_payload(payload)
    metadata_override = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else None
    metadata = _merge_metadata(metadata_payload, metadata_override) or {}
    metadata_source = "payload" if metadata else ""

    missing_metadata = _missing_metadata_fields(metadata)
    if missing_metadata:
        fetched_metadata = _fetch_deal_metadata(str(deal_id)) or {}
        if fetched_metadata:
            metadata = _merge_metadata(metadata, fetched_metadata) or fetched_metadata
            metadata_source = "payload+hubspot" if metadata_source == "payload" else "hubspot"
        missing_metadata = _missing_metadata_fields(metadata)

    if missing_metadata:
        return (
            jsonify(
                {
                    "message": "Unable to build complete metadata",
                    "missing": missing_metadata,
                }
            ),
            400,
        )

    service = ensure_box_service()
    if not service:
        return jsonify({"message": "Box automation not configured"}), 503

    try:
        service.apply_metadata_template(folder_id, metadata)
    except BoxAutomationError as exc:
        logger.error("Auto metadata apply failed for folder %s deal %s: %s", folder_id, deal_id, exc)
        return jsonify({"message": "Box metadata apply failed", "error": str(exc)}), 500

    folder_url = f"https://app.box.com/folder/{folder_id}"
    response = {
        "deal_id": str(deal_id),
        "folder_id": folder_id,
        "metadata_fields": sorted(metadata.keys()),
        "status": "tagged",
        "metadata_source": metadata_source or "hubspot",
        "box_folder_url": folder_url,
    }
    try:
        _record_metadata_snapshot_tag(folder_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Unable to update metadata snapshot after auto-tagging %s: %s", folder_id, exc)
    if HUBSPOT_BOX_FOLDER_DEAL_PROPERTY:
        if not _update_hubspot_deal_properties(str(deal_id), {HUBSPOT_BOX_FOLDER_DEAL_PROPERTY: folder_url}):
            logger.warning(
                "Unable to update HubSpot deal %s with box folder url %s",
                deal_id,
                folder_url,
            )
    contact_ids = set()
    primary_contact_id = str(metadata.get("primary_contact_id") or metadata_payload.get("hs_contact_id") or "").strip()
    spouse_contact_id = str(metadata.get("hs_spouse_id") or metadata_payload.get("hs_spouse_id") or "").strip()
    if primary_contact_id:
        contact_ids.add(primary_contact_id)
    if spouse_contact_id:
        contact_ids.add(spouse_contact_id)
    for contact_id in contact_ids:
        if not _update_hubspot_contact_property(contact_id, HUBSPOT_BOX_FOLDER_CONTACT_PROPERTY, folder_url):
            logger.warning(
                "Unable to update HubSpot contact %s with box folder url %s",
                contact_id,
                folder_url,
            )
    return jsonify(response), 200


@box_bp.route("/box/folder/deal-box-url", methods=["POST"])
def box_folder_update_deal_box_url():
    if not request.is_json:
        return jsonify({"message": "Invalid Content-Type"}), 415

    if not HUBSPOT_BOX_FOLDER_DEAL_PROPERTY:
        return jsonify({"message": "HUBSPOT_BOX_FOLDER_DEAL_PROPERTY is not configured"}), 400

    payload = request.get_json() or {}
    folder_id = _extract_folder_id(payload)
    deal_id = _resolve_deal_id(payload)
    if not folder_id:
        return jsonify({"message": "folder_id is required"}), 400
    if not deal_id:
        return jsonify({"message": "deal_id is required"}), 400

    folder_id = str(folder_id).strip()
    folder_url = f"https://app.box.com/folder/{folder_id}"

    if not _update_hubspot_deal_properties(str(deal_id), {HUBSPOT_BOX_FOLDER_DEAL_PROPERTY: folder_url}):
        return (
            jsonify(
                {
                    "message": "Failed to update HubSpot deal property",
                    "deal_id": str(deal_id),
                    "folder_id": folder_id,
                }
            ),
            502,
        )

    return jsonify(
        {
            "status": "updated",
            "deal_id": str(deal_id),
            "folder_id": folder_id,
            "box_folder_url": folder_url,
        }
    ), 200


@box_bp.route("/box/folder/share", methods=["POST"])
def box_folder_share_client_subfolder():
    """Share the Client Sharing subfolder with specified email addresses."""
    if not request.is_json:
        return jsonify({"message": "Invalid Content-Type"}), 415

    payload = request.get_json() or {}
    folder_id = _extract_folder_id(payload)
    if not folder_id:
        return jsonify({"message": "folder_id is required"}), 400

    raw_emails = payload.get("emails")
    deal_id = _resolve_deal_id(payload)

    required_payload_fields = ["hs_contact_email"]
    missing_payload_fields = [
        key for key in required_payload_fields if not _extract_payload_value(payload, key)
    ]
    if missing_payload_fields:
        return (
            jsonify(
                {
                    "message": "Missing required payload fields for folder sharing",
                    "missing": missing_payload_fields,
                }
            ),
            400,
        )

    if isinstance(raw_emails, str):
        emails = [raw_emails.strip()]
    elif isinstance(raw_emails, list):
        emails = [str(email).strip() for email in raw_emails if str(email).strip()]
    else:
        emails = []

    if not emails:
        emails = [
            value
            for value in (
                _extract_payload_value(payload, "hs_contact_email"),
                _extract_payload_value(payload, "hs_spouse_email"),
            )
            if value
        ]

    if not emails:
        return jsonify({"message": "emails array is required"}), 400

    service = ensure_box_service()
    if not service:
        return jsonify({"message": "Box automation not configured"}), 503

    unique_emails: List[str] = []
    for email in emails:
        if email not in unique_emails:
            unique_emails.append(email)

    results = []
    for email in unique_emails:
        try:
            result = service.share_subfolder_with_email(
                parent_folder_id=folder_id,
                subfolder_name=CLIENT_SHARING_SUBFOLDER,
                email=email,
                role=CLIENT_SHARING_ROLE,
            )
            results.append({"email": email, "status": "shared", "collaboration": result})
        except BoxAutomationError as exc:
            logger.error(
                "Failed to share Client Sharing subfolder for folder %s email=%s: %s",
                folder_id,
                email,
                exc,
            )
            results.append({"email": email, "status": "error", "error": str(exc)})

    response = {
        "folder_id": folder_id,
        "subfolder": CLIENT_SHARING_SUBFOLDER,
        "role": CLIENT_SHARING_ROLE,
        "results": results,
    }
    return jsonify(response), 200


@box_bp.route("/box/folder/collaborators", methods=["GET"])
def box_folder_list_collaborators():
    """Return collaborator emails and highlight non-pivotwealth entries."""
    folder_id = (request.args.get("folder_id") or "").strip()
    if not folder_id:
        return jsonify({"message": "folder_id query parameter is required"}), 400

    service = ensure_box_service()
    if not service:
        return jsonify({"message": "Box automation not configured"}), 503

    subfolder_name = (request.args.get("subfolder") or "").strip()
    subfolder_id = (request.args.get("subfolder_id") or "").strip()
    include_subfolders = request.args.get("include_subfolders") == "1"

    inspected_name = None

    try:
        if subfolder_id:
            collaborators, target_folder_id = service.list_collaborators(subfolder_id)
            inspected_name = subfolder_name or None
        else:
            collaborators, target_folder_id = service.list_collaborators(
                folder_id,
                subfolder_name=subfolder_name or None,
            )
            inspected_name = subfolder_name or None

        subfolders = (
            service.list_subfolders(target_folder_id) if include_subfolders else []
        )
    except BoxAutomationError as exc:
        logger.error("Failed to list collaborators for folder %s: %s", folder_id, exc)
        return jsonify({"message": "Box collaborator list failed", "error": str(exc)}), 500

    pivot_domain = "@pivotwealth.com.au"

    external = [
        collab
        for collab in collaborators
        if collab.get("email") and not collab["email"].endswith(pivot_domain)
    ]

    root_folder_info = None
    try:
        details = service._get_folder_details(folder_id)  # noqa: SLF001
        root_folder_info = {
            "id": folder_id,
            "name": details.get("name"),
            "url": f"https://app.box.com/folder/{folder_id}",
            "path": service._folder_display_path(details),  # noqa: SLF001
        }
    except BoxAutomationError as exc:  # pragma: no cover - best effort only
        logger.warning("Unable to fetch root folder details for %s: %s", folder_id, exc)

    inspected_name = inspected_name or (root_folder_info or {}).get("name")

    return jsonify(
        {
            "folder_id": folder_id,
            "target_folder_id": target_folder_id,
            "inspected": {
                "id": target_folder_id,
                "name": inspected_name,
            },
            "total": len(collaborators),
            "collaborators": collaborators,
            "external": external,
            "subfolders": subfolders if include_subfolders else None,
            "root_folder": root_folder_info,
        }
    ), 200


@box_bp.route("/box/folder/subfolders", methods=["GET"])
def box_folder_list_subfolders():
    """Return immediate subfolders for a given Box folder id."""
    folder_id = (request.args.get("folder_id") or "").strip()
    if not folder_id:
        return jsonify({"message": "folder_id query parameter is required"}), 400

    service = ensure_box_service()
    if not service:
        return jsonify({"message": "Box automation not configured"}), 503

    try:
        subfolders = service.list_subfolders(folder_id)
    except BoxAutomationError as exc:
        logger.error("Failed to list subfolders for %s: %s", folder_id, exc)
        return jsonify({"message": "Box subfolder list failed", "error": str(exc)}), 500

    return jsonify(
        {
            "folder_id": folder_id,
            "count": len(subfolders),
            "subfolders": subfolders,
        }
    ), 200


@box_bp.route("/_public/box/folder/missing-metadata", methods=["GET"])
def box_folder_find_missing_metadata():
    """Return up to five active client folders that currently lack metadata."""
    service = ensure_box_service()
    if not service:
        return jsonify({"message": "Box automation not configured"}), 503

    cursor_param = request.args.get("cursor")
    start_index = 0
    if cursor_param is not None and cursor_param != "":
        try:
            start_index = int(cursor_param)
            if start_index < 0:
                raise ValueError
        except ValueError:
            return jsonify({"message": "cursor must be a non-negative integer"}), 400

    try:
        folders, issues, next_cursor = service.find_folder_missing_metadata(start_index=start_index)
    except BoxAutomationError as exc:
        logger.error("Failed to locate folder missing metadata: %s", exc)
        return jsonify({"message": "Unable to scan folders for metadata", "error": str(exc)}), 500

    issues = issues or []
    folders = folders or []
    if not folders:
        message = (
            "All active client folders already have metadata"
            if not issues
            else "Unable to locate an untagged folder because some folders could not be inspected."
        )
        payload = {"message": message, "folders": []}
        if issues:
            payload["issues"] = issues
        return jsonify(payload), 200

    payload = {"status": "ok", "folders": folders}
    # Backwards compatibility: include first folder as `folder`
    if folders:
        payload["folder"] = folders[0]
    if issues:
        payload["issues"] = issues
    if next_cursor is not None:
        payload["next_cursor"] = next_cursor
    return jsonify(payload), 200


@box_bp.route("/box/folder/metadata/cache", methods=["POST"])
def box_folder_cache_metadata_status():
    """Scan Box folders and persist tagged/untagged ids to Firestore."""
    guard = _ensure_logged_in()
    if guard is not None:
        return guard

    if not USE_FIRESTORE:
        return jsonify({"message": "Firestore is not enabled; cannot persist metadata status"}), 503

    db = get_firestore_client()
    if not db:
        return jsonify({"message": "Firestore client unavailable"}), 503

    service = ensure_box_service()
    if not service:
        return jsonify({"message": "Box automation not configured"}), 503

    try:
        tagged_entries, untagged_entries, issues = service.collect_metadata_tagging_status()
    except BoxAutomationError as exc:
        logger.error("Failed to collect Box metadata status: %s", exc)
        return jsonify({"message": "Unable to collect metadata status", "error": str(exc)}), 500

    snapshot = {
        "tagged": tagged_entries,
        "untagged": untagged_entries,
        "issue_count": len(issues),
        "issues": issues,
        "total_tagged": len(tagged_entries),
        "total_untagged": len(untagged_entries),
        "total_scanned": len(tagged_entries) + len(untagged_entries),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }

    try:
        db.collection("box_folder_metadata").document("tagging_status").set(snapshot)
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to persist Box metadata status to Firestore: %s", exc)
        return jsonify({"message": "Failed to store metadata status", "error": str(exc)}), 500

    return jsonify(
        {
            "status": "ok",
            "counts": {
                "tagged": len(tagged_entries),
                "untagged": len(untagged_entries),
            },
            "issues": issues,
        }
    ), 200


@box_bp.route("/box/folder/metadata/status", methods=["GET"])
def box_folder_metadata_status_page():
    """Render cached metadata tagging status from Firestore."""
    guard = _ensure_logged_in()
    if guard is not None:
        return guard

    error_message = None
    tagged_entries: List[Dict[str, Optional[str]]] = []
    untagged_entries: List[Dict[str, Optional[str]]] = []
    issues: List[str] = []
    counts = {"tagged": 0, "untagged": 0, "issues": 0}
    updated_at_display: Optional[str] = None
    updated_at_raw: Optional[str] = None
    snapshot_exists = False

    assignment_slots: List[Dict[str, object]] = []

    if not USE_FIRESTORE:
        error_message = "Firestore is not enabled for this environment."
    else:
        db = get_firestore_client()
        if not db:
            error_message = "Firestore client unavailable."
        else:
            try:
                doc = db.collection("box_folder_metadata").document("tagging_status").get()
            except Exception as exc:  # noqa: BLE001
                logger.error("Failed to read Box metadata status from Firestore: %s", exc)
                error_message = "Unable to read metadata snapshot from Firestore."
            else:
                if doc.exists:
                    snapshot = doc.to_dict() or {}
                    raw_tagged = snapshot.get("tagged") or []
                    raw_untagged = snapshot.get("untagged") or []

                    def _normalize(entry):
                        if entry is None:
                            return None
                        if isinstance(entry, str):
                            folder_id = entry.strip()
                            if not folder_id:
                                return None
                            return {
                                "id": folder_id,
                                "name": "",
                                "path": "",
                                "url": f"https://app.box.com/folder/{folder_id}",
                                "tagged_at": None,
                            }
                        if isinstance(entry, dict):
                            folder_id = (
                                str(
                                    entry.get("id")
                                    or entry.get("folder_id")
                                    or entry.get("folderId")
                                    or ""
                                ).strip()
                            )
                            if not folder_id:
                                return None
                            name = str(entry.get("name") or entry.get("folder_name") or "").strip()
                            path = str(entry.get("path") or entry.get("display_path") or "").strip()
                            url = str(entry.get("url") or entry.get("folder_url") or "").strip()
                            if not url and folder_id:
                                url = f"https://app.box.com/folder/{folder_id}"
                            tagged_at = entry.get("tagged_at") or entry.get("timestamp")
                            return {
                                "id": folder_id,
                                "name": name,
                                "path": path,
                                "url": url or None,
                                "tagged_at": tagged_at,
                            }
                        return None

                    tagged_entries = [
                        normalized for item in raw_tagged if (normalized := _normalize(item))
                    ]
                    untagged_entries = [
                        normalized for item in raw_untagged if (normalized := _normalize(item))
                    ]
                    tagged_entries.sort(key=lambda item: item["id"])
                    untagged_entries.sort(key=lambda item: item["id"])
                    issues = list(snapshot.get("issues") or [])
                    counts = {
                        "tagged": len(tagged_entries),
                        "untagged": len(untagged_entries),
                        "issues": len(issues),
                    }
                    updated_at_raw = snapshot.get("updated_at")
                    if updated_at_raw:
                        try:
                            parsed = datetime.fromisoformat(updated_at_raw)
                            if parsed.tzinfo is None:
                                parsed = parsed.replace(tzinfo=timezone.utc)
                            updated_at_display = parsed.astimezone(SYDNEY_TZ).strftime(
                                "%Y-%m-%d %H:%M:%S %Z"
                            )
                        except ValueError:
                            updated_at_display = str(updated_at_raw)
                    snapshot_exists = True
                    service = ensure_box_service()
                    if service:
                        def _hydrate(entries: List[Dict[str, Optional[str]]]) -> None:
                            for entry in entries:
                                if not entry.get("id"):
                                    continue
                                needs_name = not entry.get("name")
                                needs_url = not entry.get("url")
                                if not (needs_name or needs_url):
                                    continue
                                info = service.get_folder_snapshot_info(entry["id"])
                                if not info:
                                    continue
                                if needs_name and info.get("name"):
                                    entry["name"] = info["name"]
                                if not entry.get("path") and info.get("path"):
                                    entry["path"] = info["path"]
                                if needs_url and info.get("url"):
                                    entry["url"] = info["url"]

                        _hydrate(tagged_entries)
                        _hydrate(untagged_entries)
                else:
                    snapshot_exists = False

    allow_refresh = error_message is None

    assignee_names = _parse_assignee_names(request.args.get("assignees"))
    slot_count = max(1, len(assignee_names))
    if not assignee_names:
        assignee_names = [f"Slot {idx + 1}" for idx in range(8)]
        slot_count = len(assignee_names)

    if untagged_entries:
        buckets: List[List[Dict[str, Optional[str]]]] = [[] for _ in range(slot_count)]
        for entry in untagged_entries:
            folder_id = entry.get("id")
            if not folder_id:
                continue
            bucket_index = _stable_bucket(str(folder_id), slot_count)
            buckets[bucket_index].append(entry)

        assignment_slots = []
        for idx, name in enumerate(assignee_names):
            folders = sorted(buckets[idx], key=lambda item: item.get("id") or "")
            assignment_slots.append(
                {
                    "index": idx,
                    "assignee": name,
                    "count": len(folders),
                    "folders": folders,
                }
            )

    return render_template(
        "box_metadata_status.html",
        today=sydney_today().isoformat(),
        sydney_time=sydney_now().strftime("%Y-%m-%d %H:%M:%S %Z"),
        app_version=os.environ.get("APP_VERSION", "1.0.0"),
        counts=counts,
        tagged_entries=tagged_entries,
        untagged_entries=untagged_entries,
        issues=issues,
        updated_at_display=updated_at_display,
        updated_at_raw=updated_at_raw,
        snapshot_exists=snapshot_exists,
        error_message=error_message,
        allow_refresh=allow_refresh,
        assignment_slots=assignment_slots,
        assignee_names=assignee_names,
    )


@box_bp.route("/box/collaborators/contact", methods=["GET"])
def box_collaborator_contact_details():
    """Return HubSpot contact and associated deal details for a given email."""
    email = (request.args.get("email") or "").strip()
    if not email:
        return jsonify({"message": "email query parameter is required"}), 400

    try:
        contact = _search_hubspot_contact_by_email(email)
    except requests.RequestException as exc:
        logger.error("HubSpot contact search failed for %s: %s", email, exc)
        return jsonify({"message": "HubSpot contact search failed", "error": str(exc)}), 502

    if not contact:
        return jsonify({"message": "Contact not found", "email": email}), 404

    contact_id = contact.get("id")
    properties = contact.get("properties") or {}

    warnings: List[Dict[str, str]] = []
    deals: List[dict] = []
    try:
        deal_ids = _fetch_contact_associated_deal_ids(contact_id)
    except requests.RequestException as exc:
        logger.error("HubSpot deal lookup failed for contact %s: %s", contact_id, exc)
        warnings.append(
            {
                "source": "hubspot",
                "scope": "deals",
                "message": "Unable to load associated deals from HubSpot.",
            }
        )
        deal_ids = []

    for deal_id in deal_ids:
        deal = _fetch_hubspot_deal(deal_id)
        if not deal:
            warnings.append(
                {
                    "source": "hubspot",
                    "scope": "deal",
                    "message": f"Deal {deal_id} could not be retrieved from HubSpot.",
                }
            )
            continue

        associated_contacts: List[dict] = []
        associated_status = "complete"
        try:
            deal_contacts = box_service.get_hubspot_deal_contacts(deal_id)
            for idx, contact_entry in enumerate(deal_contacts):
                contact_props = contact_entry.get("properties") or {}
                assoc_id = contact_entry.get("id")
                associated_contacts.append(
                    {
                        "id": assoc_id,
                        "firstname": contact_props.get("firstname"),
                        "lastname": contact_props.get("lastname"),
                        "email": contact_props.get("email"),
                        "display_name": box_service._format_contact_display(contact_entry, position=idx),
                        "url": _hubspot_contact_url(assoc_id),
                    }
                )
        except requests.RequestException as exc:
            logger.warning("Failed to load associated contacts for deal %s: %s", deal_id, exc)
            warnings.append(
                {
                    "source": "hubspot",
                    "scope": "associated_contacts",
                    "message": f"Associated contacts for deal {deal_id} could not be loaded.",
                }
            )
            associated_status = "partial"
        except Exception as exc:  # pragma: no cover
            logger.warning("Unexpected error loading associated contacts for deal %s: %s", deal_id, exc)
            warnings.append(
                {
                    "source": "internal",
                    "scope": "associated_contacts",
                    "message": f"Unexpected error loading contacts for deal {deal_id}.",
                }
            )
            associated_status = "partial"

        deals.append(
            {
                "id": deal_id,
                "properties": deal.get("properties", {}),
                "url": deal.get("url"),
                "associated_contacts": associated_contacts,
                "associated_contacts_status": associated_status,
            }
        )

    return jsonify(
        {
            "status": "ok",
            "contact": {
                "id": contact_id,
                "properties": properties,
                "url": _hubspot_contact_url(contact_id),
            },
            "deals": deals,
            "warnings": warnings,
        }
    ), 200



@box_bp.route("/box/create", methods=["GET"])
def box_folder_create_page():
    """Render UI for triggering Box folder creation."""
    guard = _ensure_logged_in()
    if guard is not None:
        return guard
    return render_template("box_folder_create.html", title="Create Box Folder")


@box_bp.route("/box/collaborators", methods=["GET"])
def box_folder_collaborators_page():
    """Render UI for listing folder collaborators and highlighting externals."""
    guard = _ensure_logged_in()
    if guard is not None:
        return guard
    return render_template(
        "box_collaborators.html",
        title="Box Collaborators",
        hubspot_deal_property=HUBSPOT_BOX_FOLDER_DEAL_PROPERTY,
        hubspot_contact_property=HUBSPOT_BOX_FOLDER_CONTACT_PROPERTY,
    )


@box_bp.route("/post/create_box_folder/preview", methods=["POST"])
def box_folder_preview():
    """Return preview information for Box folder creation."""
    if not request.is_json:
        logger.error("Invalid Content-Type for Box folder preview: Must be application/json")
        return jsonify({"message": "Invalid Content-Type"}), 415

    payload = request.get_json() or {}
    deal_id = _resolve_deal_id(payload)
    if not deal_id:
        logger.error("Box folder preview missing deal_id; payload keys=%s", list(payload.keys()))
        return jsonify({"message": "deal_id is required"}), 400

    deal_id = str(deal_id)
    metadata, contacts_hint, share_email = _build_metadata_from_payload(payload)

    contacts = contacts_hint or box_service.get_hubspot_deal_contacts(deal_id)
    formatted_contacts = [
        display
        for idx, contact in enumerate(contacts)
        if (display := box_service._format_contact_display(contact, position=idx))
    ]

    metadata_source = "payload"
    if not metadata:
        metadata = _fetch_deal_metadata(deal_id) or {}
        metadata_source = "hubspot"

    folder_name = box_service.build_client_folder_name(deal_id, contacts)

    response = {
        "deal_id": deal_id,
        "folder_name": folder_name,
        "contacts": formatted_contacts,
        "metadata": metadata,
        "metadata_source": metadata_source,
        "share_email": share_email,
    }
    logger.info("Box preview for deal %s: %s", deal_id, response)
    return jsonify(response), 200


__all__ = ["box_bp", "create_box_folder_webhook"]
