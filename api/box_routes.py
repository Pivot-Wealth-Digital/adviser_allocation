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

BOX_METADATA_PREVIEW_COLLECTION = os.environ.get("BOX_METADATA_PREVIEW_COLLECTION", "box_metadata_previews")
INTERNAL_EMAIL_DOMAIN = (os.environ.get("PIVOT_INTERNAL_DOMAIN") or "@pivotwealth.com.au").strip().lower()
MISMATCH_SECTION_KEY = "mismatch_pending"

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


def _format_sydney_timestamp(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    normalized = text.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        return text
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(SYDNEY_TZ)
    return dt.strftime("%Y-%m-%d %H:%M %Z")


def _decorate_snapshot_entry(entry: Dict[str, object]) -> None:
    preview_ts = entry.get("preview_generated_at")
    tagged_ts = entry.get("tagged_at")
    preview_display = _format_sydney_timestamp(preview_ts) if preview_ts else None
    tagged_display = _format_sydney_timestamp(tagged_ts) if tagged_ts else None
    if preview_display:
        entry["preview_generated_at_display"] = preview_display
    if tagged_display:
        entry["tagged_at_display"] = tagged_display


def _sort_untagged_snapshot_entries(entries: List[Dict[str, Optional[str]]]) -> None:
    entries.sort(
        key=lambda item: (
            0 if item.get("preview_generated_at") else 1,
            item.get("id") or "",
        ),
    )


def _sort_tagged_snapshot_entries(entries: List[Dict[str, Optional[str]]]) -> None:
    entries.sort(
        key=lambda item: (
            -_parse_timestamp_value(item.get("tagged_at") or item.get("tagged_at_display")),
            item.get("id") or "",
        ),
    )


def _parse_timestamp_value(value) -> int:
    """
    Convert assorted HubSpot timestamp representations to milliseconds since epoch.
    """
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        val = int(value)
        return val if val > 1_000_000_000_000 else val * 1000
    text = str(value).strip()
    if not text:
        return 0
    if text.isdigit():
        num = int(text)
        return num if num > 1_000_000_000_000 else num * 1000
    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return 0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def _extract_deal_timestamp_value(deal: Optional[dict]) -> int:
    if not deal:
        return 0
    props = deal.get("properties") or {}
    for key in (
        "agreement_start_date",
        "closedate",
        "hs_closed_won_date",
        "updatedAt",
        "createdAt",
    ):
        value = props.get(key) or deal.get(key)
        ts = _parse_timestamp_value(value)
        if ts:
            return ts
    return 0


def _format_preview_date(ts_ms: int) -> str:
    if not ts_ms:
        return "n/a"
    try:
        dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
    except (OSError, ValueError):
        return "n/a"
    return dt.date().isoformat()


def _collect_folder_collaborators(service, folder_id: str) -> Tuple[List[str], Dict[str, dict]]:
    """
    Aggregate collaborators from the root folder and all immediate subfolders.
    Returns the ordered list of emails (first-seen) and a mapping of email -> collaborator summary.
    """
    order: List[str] = []
    collaborators: Dict[str, dict] = {}

    def _register(entry: dict, context: str) -> None:
        email = (entry.get("email") or "").strip().lower()
        if not email:
            return
        record = collaborators.get(email)
        if not record:
            record = {
                "email": email,
                "names": set(),
                "roles": set(),
                "statuses": set(),
                "contexts": [],
                "is_external": not email.endswith(INTERNAL_EMAIL_DOMAIN),
            }
            collaborators[email] = record
            order.append(email)
        name = (entry.get("name") or "").strip()
        if name:
            record["names"].add(name)
        role = entry.get("role")
        if role:
            record["roles"].add(role)
        status = entry.get("status")
        if status:
            record["statuses"].add(status)
        if context and context not in record["contexts"]:
            record["contexts"].append(context)

    # Root collaborators
    try:
        root_collaborators, _ = service.list_collaborators(folder_id)
    except BoxAutomationError as exc:
        logger.warning("Unable to list collaborators for %s: %s", folder_id, exc)
        raise
    for entry in root_collaborators:
        _register(entry, "Root folder")

    # Subfolders
    try:
        subfolders = service.list_subfolders(folder_id)
    except BoxAutomationError as exc:
        logger.warning("Unable to enumerate subfolders for %s: %s", folder_id, exc)
        subfolders = []

    for sub in subfolders:
        sub_id = sub.get("id")
        sub_name = sub.get("name") or sub_id or "Subfolder"
        if not sub_id:
            continue
        try:
            sub_collaborators, _ = service.list_collaborators(sub_id)
        except BoxAutomationError as exc:
            logger.debug("Skipping subfolder %s while aggregating collaborators: %s", sub_id, exc)
            continue
        for entry in sub_collaborators:
            _register(entry, f"Subfolder {sub_name}")

    # Convert sets to sorted lists for serialization
    for record in collaborators.values():
        record["names"] = sorted(record["names"])
        record["roles"] = sorted(record["roles"])
        record["statuses"] = sorted(record["statuses"])

    return order, collaborators


def _choose_collaborator_entry(order: List[str], collaborators: Dict[str, dict]) -> Optional[dict]:
    if not order:
        return None
    for email in order:
        entry = collaborators.get(email)
        if entry and entry.get("is_external"):
            return entry
    # Fallback to first entry
    for email in order:
        entry = collaborators.get(email)
        if entry:
            return entry
    return None


def _load_contact_details_with_deals(email: str) -> Tuple[Optional[dict], List[dict], List[dict]]:
    """
    Fetch HubSpot contact plus associated deals (with contact metadata) for a given email.
    """
    email = (email or "").strip()
    if not email:
        return None, [], [
            {
                "source": "input",
                "scope": "email",
                "message": "Email address is required to load contact details.",
            }
        ]

    try:
        contact = _search_hubspot_contact_by_email(email)
    except requests.RequestException as exc:
        logger.error("HubSpot contact search failed for %s: %s", email, exc)
        return None, [], [
            {
                "source": "hubspot",
                "scope": "contact",
                "message": "HubSpot contact search failed.",
            }
        ]

    if not contact:
        return None, [], [
            {
                "source": "hubspot",
                "scope": "contact",
                "message": f"No HubSpot contact found for {email}.",
            }
        ]

    contact_id = contact.get("id")
    warnings: List[Dict[str, str]] = []
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

    deals: List[dict] = []
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

            def _contact_by_label(label: str) -> Optional[dict]:
                target = label.strip().lower()
                for contact_entry in deal_contacts:
                    for assoc in contact_entry.get("association_types", []):
                        assoc_label = (assoc.get("label") or "").strip().lower()
                        if assoc_label == target:
                            return contact_entry
                return None

            primary_entry = _contact_by_label("Client") or (deal_contacts[0] if deal_contacts else None)
            spouse_entry = _contact_by_label("Client's Spouse")

            for idx, contact_entry in enumerate(deal_contacts):
                contact_props = contact_entry.get("properties") or {}
                assoc_id = contact_entry.get("id")
                labels = [
                    assoc.get("label")
                    for assoc in contact_entry.get("association_types", [])
                    if assoc.get("label")
                ]
                associated_contacts.append(
                    {
                        "id": assoc_id,
                        "firstname": contact_props.get("firstname"),
                        "lastname": contact_props.get("lastname"),
                        "email": contact_props.get("email"),
                        "display_name": box_service._format_contact_display(contact_entry, position=idx),
                        "url": _hubspot_contact_url(assoc_id),
                        "association_labels": labels,
                    }
                )

            deal_props = deal.setdefault("properties", {})
            if primary_entry:
                primary_props = primary_entry.get("properties") or {}
                if primary_entry.get("id"):
                    deal_props.setdefault("hs_contact_id", primary_entry.get("id"))
                if primary_props.get("firstname"):
                    deal_props.setdefault("hs_contact_firstname", primary_props.get("firstname"))
                if primary_props.get("lastname"):
                    deal_props.setdefault("hs_contact_lastname", primary_props.get("lastname"))
                if primary_props.get("email"):
                    deal_props.setdefault("hs_contact_email", primary_props.get("email"))

            if spouse_entry:
                spouse_props = spouse_entry.get("properties") or {}
                if spouse_entry.get("id"):
                    deal_props["hs_spouse_id"] = spouse_entry.get("id")
                if spouse_props.get("firstname"):
                    deal_props["hs_spouse_firstname"] = spouse_props.get("firstname")
                if spouse_props.get("lastname"):
                    deal_props["hs_spouse_lastname"] = spouse_props.get("lastname")
                if spouse_props.get("email"):
                    deal_props["hs_spouse_email"] = spouse_props.get("email")
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

    contact_payload = {
        "id": contact_id,
        "properties": contact.get("properties") or {},
        "url": _hubspot_contact_url(contact_id),
    }
    return contact_payload, deals, warnings


def _build_preview_base_payload(folder_id: str, contact: dict, selected_deal: dict) -> dict:
    payload: Dict[str, object] = {"folder_id": folder_id}
    deal_props = (selected_deal.get("properties") or {}) if selected_deal else {}
    deal_id = str(deal_props.get("hs_deal_record_id") or selected_deal.get("id") or "").strip()
    if deal_id:
        payload["deal_id"] = deal_id
        payload["hs_deal_record_id"] = deal_id

    contact_props = (contact.get("properties") or {}) if contact else {}
    contact_id = str(contact.get("id") or "").strip()
    if contact_id:
        payload["hs_contact_id"] = contact_id
    if contact_props.get("firstname"):
        payload["hs_contact_firstname"] = contact_props.get("firstname")
    if contact_props.get("lastname"):
        payload["hs_contact_lastname"] = contact_props.get("lastname")
    if contact_props.get("email"):
        payload["hs_contact_email"] = contact_props.get("email")

    for key in (
        "hs_spouse_id",
        "hs_spouse_firstname",
        "hs_spouse_lastname",
        "hs_spouse_email",
        "deal_salutation",
        "household_type",
    ):
        value = deal_props.get(key)
        if value:
            payload[key] = value

    # Attempt to populate spouse info from associated contacts if not provided
    if not payload.get("hs_spouse_id"):
        for assoc in selected_deal.get("associated_contacts") or []:
            labels = [str(label).strip().lower() for label in assoc.get("association_labels") or []]
            if any("spouse" in label for label in labels):
                if assoc.get("id"):
                    payload["hs_spouse_id"] = assoc["id"]
                props = assoc
                if props.get("firstname"):
                    payload["hs_spouse_firstname"] = props["firstname"]
                if props.get("lastname"):
                    payload["hs_spouse_lastname"] = props["lastname"]
                if props.get("email"):
                    payload["hs_spouse_email"] = props["email"]
                break

    return payload


def _build_preview_output(
    folder_id: str,
    metadata_fields: Dict[str, object],
    contact: dict,
    selected_deal: dict,
    root_info: dict,
) -> dict:
    contact_props = (contact.get("properties") or {}) if contact else {}
    primary_name = " ".join(filter(None, [contact_props.get("firstname"), contact_props.get("lastname")])).strip()
    primary_link = contact.get("url") or _hubspot_contact_url(contact.get("id"))
    primary_summary = {
        "id": contact.get("id") or "",
        "name": primary_name,
        "email": contact_props.get("email") or "",
        "link": primary_link or "",
        "firstname": contact_props.get("firstname") or "",
        "lastname": contact_props.get("lastname") or "",
    }

    spouse_id = str(metadata_fields.get("hs_spouse_id") or "").strip()
    spouse_link = metadata_fields.get("spouse_contact_link") or _hubspot_contact_url(spouse_id)
    spouse_name = " ".join(
        filter(
            None,
            [
                metadata_fields.get("hs_spouse_firstname"),
                metadata_fields.get("hs_spouse_lastname"),
            ],
        )
    ).strip()
    spouse_summary = {
        "id": spouse_id,
        "name": spouse_name,
        "email": metadata_fields.get("hs_spouse_email") or "",
        "link": spouse_link or "",
        "firstname": metadata_fields.get("hs_spouse_firstname") or "",
        "lastname": metadata_fields.get("hs_spouse_lastname") or "",
    }

    deal_props = (selected_deal.get("properties") or {}) if selected_deal else {}
    deal_ts = _extract_deal_timestamp_value(selected_deal)
    deal_summary = {
        "id": deal_props.get("hs_deal_record_id") or selected_deal.get("id") or "",
        "name": deal_props.get("dealname") or "",
        "stage": deal_props.get("dealstage") or "",
        "close_date": _format_preview_date(deal_ts),
        "url": selected_deal.get("url"),
    }

    return {
        "folder_id": folder_id,
        "deal_id": deal_summary["id"],
        "deal": deal_summary,
        "metadata_fields": metadata_fields,
        "contacts": {
            "primary": primary_summary,
            "spouse": spouse_summary,
            "additional": [],
        },
        "root_folder": root_info,
    }


def _get_folder_root_info(service, folder_id: str) -> dict:
    fallback = {
        "id": folder_id,
        "name": "",
        "path": "",
        "url": f"https://app.box.com/folder/{folder_id}",
    }
    try:
        details = service._get_folder_details(folder_id)  # noqa: SLF001
        if not details:
            return fallback
        return {
            "id": folder_id,
            "name": details.get("name") or "",
            "path": service._folder_display_path(details),  # noqa: SLF001
            "url": f"https://app.box.com/folder/{folder_id}",
        }
    except BoxAutomationError as exc:
        logger.warning("Unable to fetch folder details for %s: %s", folder_id, exc)
        return fallback


def _generate_folder_metadata_preview(folder_id: str, service, allow_contact_only: bool = False) -> dict:
    folder_id = (folder_id or "").strip()
    if not folder_id:
        raise ValueError("folder_id is required")

    root_info = _get_folder_root_info(service, folder_id)
    try:
        order, collaborators = _collect_folder_collaborators(service, folder_id)
    except BoxAutomationError as exc:
        return {
            "folder_id": folder_id,
            "status": "error",
            "root_folder": root_info,
            "error": f"Unable to list collaborators: {exc}",
        }
    collaborator = _choose_collaborator_entry(order, collaborators)
    if not collaborator:
        return {
            "folder_id": folder_id,
            "status": "error",
            "root_folder": root_info,
            "error": "No collaborators detected in folder or subfolders.",
        }

    contact, deals, warnings = _load_contact_details_with_deals(collaborator["email"])
    if not contact:
        message = warnings[0]["message"] if warnings else "HubSpot contact not found."
        return {
            "folder_id": folder_id,
            "status": "error",
            "root_folder": root_info,
            "error": message,
            "warnings": warnings,
            "collaborator": collaborator,
        }

    selected_deal = None
    if deals:
        selected_deal = max(deals, key=_extract_deal_timestamp_value)
    contact_only = False
    if not selected_deal and allow_contact_only:
        contact_props = (contact.get("properties") or {}) if contact else {}
        contact_name = " ".join(
            filter(None, [contact_props.get("firstname"), contact_props.get("lastname")])
        ).strip()
        pseudo_deal_id = contact.get("id") or contact_props.get("hs_object_id") or contact_props.get("email") or ""
        selected_deal = {
            "id": pseudo_deal_id,
            "properties": {
                "dealname": contact_name or "Contact Only",
                "dealstage": "contact_only",
                "hs_deal_record_id": pseudo_deal_id,
            },
            "url": contact.get("url") or _hubspot_contact_url(contact.get("id")),
            "contact_only": True,
        }
        contact_only = True
    if not selected_deal:
        return {
            "folder_id": folder_id,
            "status": "error",
            "root_folder": root_info,
            "error": "No associated deals found for selected contact.",
            "warnings": warnings,
            "collaborator": collaborator,
            "contact": contact,
        }

    base_payload = _build_preview_base_payload(folder_id, contact, selected_deal)
    metadata_payload, _, _ = _build_metadata_from_payload(base_payload)
    fetched_metadata = None
    deal_id = base_payload.get("deal_id") or base_payload.get("hs_deal_record_id")
    if deal_id:
        fetched_metadata = _fetch_deal_metadata(str(deal_id))
    merged_metadata = _merge_metadata(metadata_payload, fetched_metadata) or metadata_payload

    final_payload = dict(base_payload)
    for key, value in (merged_metadata or {}).items():
        if isinstance(value, str):
            if value.strip():
                final_payload[key] = value
        elif value not in (None, [], {}):
            final_payload[key] = value

    preview_data = _build_preview_output(folder_id, merged_metadata or {}, contact, selected_deal, root_info)
    preview_doc = {
        "folder_id": folder_id,
        "status": "ok",
        "root_folder": root_info,
        "preview": preview_data,
        "metadata_fields": merged_metadata or {},
        "base_payload": base_payload,
        "final_payload": final_payload,
        "collaborator": collaborator,
        "contact": preview_data["contacts"]["primary"],
        "spouse": preview_data["contacts"]["spouse"],
        "deal": preview_data.get("deal"),
        "warnings": warnings,
        "contact_only": contact_only,
    }
    return preview_doc


def _cache_metadata_previews(db, service, folder_entries: List[Dict[str, object]]) -> None:
    if not folder_entries:
        return
    doc_ids: Dict[str, Dict[str, object]] = {}
    for entry in folder_entries:
        folder_id = str(entry.get("id") or "").strip()
        if not folder_id or folder_id in doc_ids:
            continue
        doc_ids[folder_id] = entry

    collection = db.collection(BOX_METADATA_PREVIEW_COLLECTION)
    for idx, (folder_id, entry) in enumerate(doc_ids.items(), start=1):
        try:
            preview_doc = _generate_folder_metadata_preview(folder_id, service, allow_contact_only=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to generate metadata preview for %s: %s", folder_id, exc)
            preview_doc = {
                "folder_id": folder_id,
                "status": "error",
                "error": str(exc),
                "root_folder": {
                    "id": folder_id,
                    "name": entry.get("name") or "",
                    "path": entry.get("path") or "",
                    "url": entry.get("url") or f"https://app.box.com/folder/{folder_id}",
                },
            }
        preview_doc["folder_name"] = entry.get("name") or preview_doc.get("root_folder", {}).get("name", "")
        preview_doc["folder_path"] = entry.get("path") or preview_doc.get("root_folder", {}).get("path", "")
        preview_doc["folder_url"] = entry.get("url") or preview_doc.get("root_folder", {}).get(
            "url",
            f"https://app.box.com/folder/{folder_id}",
        )
        generated_at = datetime.now(timezone.utc)
        preview_doc["generated_at"] = generated_at
        preview_doc["generated_at_iso"] = generated_at.isoformat()
        try:
            collection.document(folder_id).set(preview_doc)
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to persist metadata preview for %s: %s", folder_id, exc)
        else:
            logger.debug("Cached metadata preview for %s (%d/%d)", folder_id, idx, len(doc_ids))


def _update_snapshot_no_deal_single(folder_id: str, preview_doc: dict) -> None:
    if not USE_FIRESTORE:
        return
    db = get_firestore_client()
    if not db:
        return
    doc_ref = db.collection("box_folder_metadata").document("tagging_status")
    snapshot = doc_ref.get()
    if not snapshot.exists:
        return
    payload = snapshot.to_dict() or {}
    untagged_entries = _normalize_snapshot_entries(payload.get("untagged"))
    untagged_no_deal_entries = _normalize_snapshot_entries(payload.get("untagged_no_deal"))
    mismatch_entries = _normalize_snapshot_entries(payload.get(MISMATCH_SECTION_KEY))
    untagged_by_id = {entry["id"]: entry for entry in untagged_entries if entry.get("id")}
    no_deal_by_id = {entry["id"]: entry for entry in untagged_no_deal_entries if entry.get("id")}
    mismatch_by_id = {entry["id"]: entry for entry in mismatch_entries if entry.get("id")}
    record = untagged_by_id.get(folder_id)
    from_no_deal = False
    if not record and folder_id in no_deal_by_id:
        record = no_deal_by_id.get(folder_id)
        from_no_deal = True
    if not record and folder_id in mismatch_by_id:
        record = mismatch_by_id.get(folder_id)
        mismatch_by_id.pop(folder_id, None)
    if not record:
        return
    root_info = preview_doc.get("root_folder") or {}
    preview_has_deal = bool(
        preview_doc.get("status") == "ok"
        and preview_doc.get("deal")
        and preview_doc["deal"].get("id")
    )
    if not preview_has_deal and preview_doc.get("contact_only"):
        preview_has_deal = True
    record["name"] = record.get("name") or root_info.get("name") or ""
    record["path"] = record.get("path") or root_info.get("path") or ""
    record["url"] = record.get("url") or root_info.get("url") or f"https://app.box.com/folder/{folder_id}"
    generated_at = preview_doc.get("generated_at_iso") or preview_doc.get("generated_at")
    if generated_at:
        record["preview_generated_at"] = generated_at
    if preview_has_deal:
        record.pop("preview_error", None)
        record.pop("preview_source", None)
        untagged_by_id[folder_id] = record
        no_deal_by_id.pop(folder_id, None)
    else:
        record["preview_error"] = preview_doc.get("error") or "No associated deals found in HubSpot."
        record["preview_source"] = preview_doc.get("source") or "cache"
        untagged_by_id.pop(folder_id, None)
        no_deal_by_id[folder_id] = record
    update_doc = {
        "untagged": sorted(untagged_by_id.values(), key=lambda item: item.get("id") or ""),
        "untagged_no_deal": sorted(no_deal_by_id.values(), key=lambda item: item.get("id") or ""),
        MISMATCH_SECTION_KEY: sorted(mismatch_by_id.values(), key=lambda item: item.get("id") or ""),
        "preview_updated_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        doc_ref.set(update_doc, merge=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to update snapshot preview status for %s: %s", folder_id, exc)


@box_bp.route("/box/folder/metadata/previews/run", methods=["POST"])
def box_folder_run_metadata_previews():
    """Iterate untagged folders (optionally per assignment slot) and cache metadata previews."""
    guard = _ensure_logged_in()
    if guard is not None:
        return guard

    if not USE_FIRESTORE:
        return jsonify({"message": "Firestore is not enabled; previews unavailable"}), 503

    db = get_firestore_client()
    if not db:
        return jsonify({"message": "Firestore client unavailable"}), 503

    service = ensure_box_service()
    if not service:
        return jsonify({"message": "Box automation not configured"}), 503

    snapshot_doc = db.collection("box_folder_metadata").document("tagging_status").get()
    if not snapshot_doc.exists:
        return jsonify({"message": "Metadata snapshot not found; run /box/folder/metadata/cache first."}), 400

    snapshot = snapshot_doc.to_dict() or {}
    untagged_entries = _normalize_snapshot_entries(snapshot.get("untagged"))
    untagged_by_id = {entry["id"]: entry for entry in untagged_entries if entry.get("id")}
    untagged_no_deal_entries = _normalize_snapshot_entries(snapshot.get("untagged_no_deal") or [])
    no_deal_by_id = {entry["id"]: entry for entry in untagged_no_deal_entries if entry.get("id")}
    if not untagged_entries:
        return jsonify({"status": "ok", "processed": 0, "message": "No untagged folders to process."}), 200

    request_json = request.get_json(silent=True) or {}
    assignees_param = request.args.get("assignees") or request_json.get("assignees")
    assignee_names = _parse_assignee_names(assignees_param)
    slot_count = max(1, len(assignee_names))
    buckets: List[List[Dict[str, Optional[str]]]] = [[] for _ in range(slot_count)]
    for entry in untagged_entries:
        folder_id = entry.get("id")
        if not folder_id:
            continue
        idx = _stable_bucket(folder_id, slot_count)
        buckets[idx].append(entry)
    for bucket in buckets:
        bucket.sort(key=lambda item: item.get("id") or "")

    slot_param = request.args.get("slot")
    if slot_param is None:
        slot_param = request_json.get("slot")
    slot_index: Optional[int] = None
    if slot_param not in (None, ""):
        try:
            slot_index = int(slot_param)
            if slot_index < 0 or slot_index >= slot_count:
                raise ValueError
        except (TypeError, ValueError):
            return jsonify({"message": "slot must be a valid assignment slot index"}), 400

    assignee_param = request.args.get("assignee") or request_json.get("assignee")
    if assignee_param and slot_index is None:
        normalized_assignee = assignee_param.strip().lower()
        for idx, name in enumerate(assignee_names):
            if name.strip().lower() == normalized_assignee:
                slot_index = idx
                break
        if slot_index is None:
            return jsonify({"message": f"Assignee '{assignee_param}' not found in assignment names"}), 400

    folder_ids_payload = request_json.get("folder_ids")
    if isinstance(folder_ids_payload, list):
        folder_ids_filter = {str(item).strip() for item in folder_ids_payload if str(item).strip()}
    else:
        folder_ids_filter = None

    limit_param = request.args.get("limit")
    if limit_param is None:
        limit_param = request_json.get("limit")
    try:
        limit = int(limit_param) if limit_param not in (None, "") else 0
    except (TypeError, ValueError):
        return jsonify({"message": "limit must be an integer"}), 400
    force_refresh = request.args.get("force") == "1" or bool(request_json.get("force"))

    collection = db.collection(BOX_METADATA_PREVIEW_COLLECTION)

    def _flatten_buckets() -> List[Dict[str, Optional[str]]]:
        ordered: List[Dict[str, Optional[str]]] = []
        for bucket in buckets:
            ordered.extend(bucket)
        return ordered

    if folder_ids_filter:
        targets = [entry for entry in _flatten_buckets() if entry.get("id") in folder_ids_filter]
    elif slot_index is not None:
        targets = list(buckets[slot_index])
    else:
        targets = _flatten_buckets()

    if limit > 0:
        targets = targets[:limit]

    processed = []
    skipped = []
    errors = []
    for entry in targets:
        folder_id = entry.get("id")
        if not folder_id:
            continue
        if not force_refresh:
            existing = collection.document(folder_id).get()
            if existing.exists:
                existing_doc = existing.to_dict() or {}
                if existing_doc.get("status") == "ok":
                    skipped.append({"folder_id": folder_id, "reason": "cached"})
                    continue
        try:
            preview_doc = _generate_folder_metadata_preview(folder_id, service)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Preview generation failed for %s: %s", folder_id, exc)
            errors.append({"folder_id": folder_id, "error": str(exc)})
            continue
        generated_at = datetime.now(timezone.utc)
        preview_doc["generated_at"] = generated_at
        preview_doc["generated_at_iso"] = generated_at.isoformat()
        try:
            collection.document(folder_id).set(preview_doc)
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to persist preview for %s: %s", folder_id, exc)
            errors.append({"folder_id": folder_id, "error": str(exc)})
            continue
        preview_has_deal = bool(
            preview_doc.get("status") == "ok"
            and preview_doc.get("deal")
            and preview_doc.get("deal", {}).get("id")
        )
        root_info = preview_doc.get("root_folder") or {}

        if preview_has_deal:
            record = no_deal_by_id.pop(folder_id, None) or untagged_by_id.get(folder_id) or dict(entry)
            record["id"] = folder_id
            record["name"] = record.get("name") or entry.get("name") or root_info.get("name") or ""
            record["path"] = record.get("path") or entry.get("path") or root_info.get("path") or ""
            record["url"] = record.get("url") or entry.get("url") or root_info.get("url") or f"https://app.box.com/folder/{folder_id}"
            record.pop("preview_error", None)
            record.pop("preview_source", None)
            preview_ts = preview_doc.get("generated_at_iso") or preview_doc.get("generated_at")
            if preview_ts:
                record["preview_generated_at"] = preview_ts
            untagged_by_id[folder_id] = record
        else:
            record = untagged_by_id.pop(folder_id, None) or no_deal_by_id.get(folder_id) or dict(entry)
            record["id"] = folder_id
            record["name"] = record.get("name") or entry.get("name") or root_info.get("name") or ""
            record["path"] = record.get("path") or entry.get("path") or root_info.get("path") or ""
            record["url"] = record.get("url") or entry.get("url") or root_info.get("url") or f"https://app.box.com/folder/{folder_id}"
            record["preview_error"] = preview_doc.get("error") or "No associated deals found in HubSpot."
            record["preview_generated_at"] = preview_doc.get("generated_at_iso") or preview_doc.get("generated_at")
            record["preview_source"] = preview_doc.get("source") or ("refreshed" if force_refresh else "cache")
            no_deal_by_id[folder_id] = record

        processed.append(
            {
                "folder_id": folder_id,
                "status": preview_doc.get("status"),
                "deal_id": preview_doc.get("deal", {}).get("id"),
            }
        )

    updated_untagged = sorted(untagged_by_id.values(), key=lambda item: item.get("id") or "")
    updated_no_deal = sorted(no_deal_by_id.values(), key=lambda item: item.get("id") or "")
    preview_success = sum(1 for item in processed if item.get("status") == "ok")
    preview_errors = len(errors)
    try:
        preview_update = {
            "untagged": updated_untagged,
            "untagged_no_deal": updated_no_deal,
            "preview_updated_at": datetime.now(timezone.utc).isoformat(),
            "preview_cached_total": len(processed),
            "preview_cached_success": preview_success,
            "preview_cached_errors": preview_errors,
        }
        db.collection("box_folder_metadata").document("tagging_status").set(preview_update, merge=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to update snapshot with preview results: %s", exc)

    return jsonify(
        {
            "status": "ok",
            "processed": len(processed),
            "skipped": len(skipped),
            "errors": errors,
            "processed_details": processed,
            "skipped_details": skipped,
            "slot_index": slot_index,
            "assignees": assignee_names,
            "limit": limit,
            "force": force_refresh,
            "preview_cached_total": len(processed),
            "preview_cached_success": preview_success,
            "preview_cached_errors": preview_errors,
        }
    ), 200


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

        def _contact_by_label(label: str) -> Optional[dict]:
            target = label.strip().lower()
            for contact in contacts:
                for assoc in contact.get("association_types", []):
                    assoc_label = (assoc.get("label") or "").strip().lower()
                    if assoc_label == target:
                        return contact
            return None

        associated_contact_ids: list[str] = [
            str(contact.get("id"))
            for contact in contacts
            if contact.get("id")
        ]

        primary_contact = _contact_by_label("Client") or (contacts[0] if contacts else None)
        spouse_contact = _contact_by_label("Client's Spouse")

        primary_contact_id = props.get("hs_contact_id") or (primary_contact.get("id") if primary_contact else None) or (
            associated_contact_ids[0] if associated_contact_ids else None
        )
        primary_contact_link = _hubspot_contact_url(primary_contact_id)

        spouse_id = props.get("hs_spouse_id") or (spouse_contact.get("id") if spouse_contact else None)
        spouse_link = _hubspot_contact_url(spouse_id)

        spouse_props = (spouse_contact or {}).get("properties") or {}
        primary_props = (primary_contact or {}).get("properties") or {}

        metadata: dict[str, object] = {}
        if props.get("household_type"):
            metadata["household_type"] = props.get("household_type")
        if props.get("deal_salutation"):
            metadata["deal_salutation"] = props.get("deal_salutation")
        if primary_contact_id:
            metadata["primary_contact_id"] = primary_contact_id
            metadata["primary_contact_link"] = primary_contact_link or primary_contact_id
            if primary_props.get("firstname"):
                metadata["hs_contact_firstname"] = primary_props.get("firstname")
            if primary_props.get("lastname"):
                metadata["hs_contact_lastname"] = primary_props.get("lastname")
            if primary_props.get("email"):
                metadata["hs_contact_email"] = primary_props.get("email")
        if spouse_id:
            metadata["hs_spouse_id"] = spouse_id
            if spouse_link:
                metadata["spouse_contact_link"] = spouse_link
            if spouse_props.get("firstname"):
                metadata["hs_spouse_firstname"] = spouse_props.get("firstname")
            if spouse_props.get("lastname"):
                metadata["hs_spouse_lastname"] = spouse_props.get("lastname")
            if spouse_props.get("email"):
                metadata["hs_spouse_email"] = spouse_props.get("email")
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
    """Reload the cached metadata snapshot from Firestore (no Box scan)."""
    guard = _ensure_logged_in()
    if guard is not None:
        return guard

    if not USE_FIRESTORE:
        return jsonify({"message": "Firestore is not enabled; cannot load metadata status"}), 503

    db = get_firestore_client()
    if not db:
        return jsonify({"message": "Firestore client unavailable"}), 503

    logger.info("Metadata cache refresh: starting snapshot reload")
    doc_ref = db.collection("box_folder_metadata").document("tagging_status")
    snapshot = doc_ref.get()
    if not snapshot.exists:
        logger.warning("Metadata cache refresh: snapshot doc missing")
        return jsonify({"message": "Metadata snapshot not found in Firestore."}), 404

    payload = snapshot.to_dict() or {}
    tagged_entries = _normalize_snapshot_entries(payload.get("tagged"))
    untagged_entries = _normalize_snapshot_entries(payload.get("untagged"))
    untagged_no_deal_entries = _normalize_snapshot_entries(payload.get("untagged_no_deal") or [])
    mismatch_entries = _normalize_snapshot_entries(payload.get(MISMATCH_SECTION_KEY) or [])
    issues = list(payload.get("issues") or [])
    counts = {
        "tagged": len(tagged_entries),
        "untagged": len(untagged_entries),
        "issues": len(issues),
        "mismatch": len(mismatch_entries),
    }

    preview_collection = db.collection(BOX_METADATA_PREVIEW_COLLECTION)
    refreshed_untagged: List[Dict[str, Optional[str]]] = []
    cached_total = 0
    cached_success = 0
    cached_errors = 0

    combined_entries: List[Tuple[Dict[str, Optional[str]], str]] = []
    for entry in untagged_entries:
        combined_entries.append((dict(entry), "untagged"))

    doc_refs = []
    seen_ids = set()
    for entry, _ in combined_entries:
        folder_id = entry.get("id")
        if not folder_id:
            continue
        if folder_id in seen_ids:
            continue
        seen_ids.add(folder_id)
        doc_refs.append(preview_collection.document(folder_id))

    preview_map = {}
    if doc_refs:
        logger.info("Metadata cache refresh: fetching %d preview docs in batch", len(doc_refs))
        try:
            for snap in db.get_all(doc_refs):
                if snap.exists:
                    preview_map[snap.id] = snap.to_dict() or {}
        except Exception as exc:  # noqa: BLE001
            logger.warning("Metadata cache refresh: bulk preview fetch failed: %s", exc)

    total_targets = len(combined_entries)
    for index, (entry, original_bucket) in enumerate(combined_entries, start=1):
        folder_id = entry.get("id")
        if not folder_id:
            continue
        preview_doc = preview_map.get(folder_id)
        if not preview_doc:
            if original_bucket == "untagged":
                refreshed_untagged.append(entry)
            continue
        root_info = preview_doc.get("root_folder") or {}
        has_deal = bool(
            preview_doc.get("status") == "ok"
            and preview_doc.get("deal")
            and preview_doc["deal"].get("id")
        )
        cached_total += 1
        if has_deal:
            cached_success += 1
        else:
            cached_errors += 1
        enriched = dict(entry)
        enriched["name"] = enriched.get("name") or root_info.get("name") or enriched.get("path") or ""
        enriched["path"] = enriched.get("path") or root_info.get("path") or ""
        enriched["url"] = enriched.get("url") or root_info.get("url") or f"https://app.box.com/folder/{folder_id}"
        generated_ts = preview_doc.get("generated_at_iso") or preview_doc.get("generated_at")
        if has_deal:
            enriched.pop("preview_error", None)
            enriched.pop("preview_source", None)
            if generated_ts:
                enriched["preview_generated_at"] = generated_ts
            refreshed_untagged.append(enriched)
        else:
            enriched["preview_error"] = preview_doc.get("error") or "No associated deals found in HubSpot."
            enriched["preview_source"] = preview_doc.get("source") or "cache"
            if generated_ts:
                enriched["preview_generated_at"] = generated_ts
            untagged_no_deal_entries.append(enriched)
        if index % 20 == 0 or index == total_targets:
            logger.info(
                "Metadata cache refresh: processed %d/%d folders (cached=%d, no_deal=%d)",
                index,
                total_targets,
                cached_success,
                cached_errors,
            )

    untagged_entries = refreshed_untagged
    counts["untagged"] = len(untagged_entries)

    refreshed_at = datetime.now(timezone.utc).isoformat()
    update_doc = {
        "untagged": untagged_entries,
        "untagged_no_deal": untagged_no_deal_entries,
        MISMATCH_SECTION_KEY: mismatch_entries,
        "last_checked_at": refreshed_at,
        "updated_at": refreshed_at,
        "total_untagged": len(untagged_entries),
        "preview_cached_total": cached_total,
        "preview_cached_success": cached_success,
        "preview_cached_errors": cached_errors,
        "preview_updated_at": refreshed_at if cached_total else payload.get("preview_updated_at"),
    }
    try:
        doc_ref.set(update_doc, merge=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to update snapshot during refresh: %s", exc)
    else:
        logger.info(
            "Metadata cache refresh complete: %d cached previews (%d success, %d issues)",
            cached_total,
            cached_success,
            cached_errors,
        )

    return jsonify(
        {
            "status": "ok",
            "counts": counts,
            "issues": issues,
            "message": "Snapshot refreshed from Firestore.",
        }
    ), 200


@box_bp.route("/box/folder/metadata/scan", methods=["POST"])
def box_folder_metadata_scan():
    """Scan Box active client folders and append any new entries to the snapshot."""
    guard = _ensure_logged_in()
    if guard is not None:
        return guard

    if not USE_FIRESTORE:
        return jsonify({"message": "Firestore is not enabled; cannot update metadata snapshot"}), 503

    db = get_firestore_client()
    if not db:
        return jsonify({"message": "Firestore client unavailable"}), 503

    service = ensure_box_service()
    if not service:
        return jsonify({"message": "Box automation not configured"}), 503

    doc_ref = db.collection("box_folder_metadata").document("tagging_status")
    snapshot = doc_ref.get()
    payload = snapshot.to_dict() if snapshot.exists else {}

    tagged_entries = _normalize_snapshot_entries(payload.get("tagged"))
    untagged_entries = _normalize_snapshot_entries(payload.get("untagged"))
    untagged_no_deal_entries = _normalize_snapshot_entries(payload.get("untagged_no_deal") or [])
    issues = list(payload.get("issues") or [])

    existing_ids = {
        entry["id"]
        for entry in tagged_entries + untagged_entries + untagged_no_deal_entries
        if entry.get("id")
    }

    try:
        box_tagged, box_untagged, box_issues = service.collect_metadata_tagging_status()
    except BoxAutomationError as exc:
        logger.error("Failed to scan Box metadata status: %s", exc)
        return jsonify({"message": "Unable to scan Box for metadata status", "error": str(exc)}), 500

    issues.extend(box_issues or [])
    new_tagged = 0
    new_untagged = 0
    now_iso = datetime.now(timezone.utc).isoformat()
    new_entries_for_preview: List[Tuple[str, Dict[str, object]]] = []

    def _normalize_entry(entry: dict) -> dict:
        normalized = {
            "id": entry.get("id"),
            "name": entry.get("name") or "",
            "path": entry.get("path") or "",
            "url": entry.get("url") or (f"https://app.box.com/folder/{entry.get('id')}" if entry.get("id") else ""),
        }
        return {key: value for key, value in normalized.items() if value}

    for entry in box_tagged:
        folder_id = entry.get("id")
        if not folder_id or folder_id in existing_ids:
            continue
        normalized = _normalize_entry(entry)
        tagged_entries.append(normalized)
        existing_ids.add(folder_id)
        new_tagged += 1
        new_entries_for_preview.append((folder_id, normalized))
        logger.info("Scan: discovered tagged folder %s (%s)", folder_id, normalized.get("name"))

    for entry in box_untagged:
        folder_id = entry.get("id")
        if not folder_id or folder_id in existing_ids:
            continue
        normalized = _normalize_entry(entry)
        untagged_entries.append(normalized)
        existing_ids.add(folder_id)
        new_untagged += 1
        new_entries_for_preview.append((folder_id, normalized))
        logger.info("Scan: discovered untagged folder %s (%s)", folder_id, normalized.get("name"))

    preview_collection = db.collection(BOX_METADATA_PREVIEW_COLLECTION)
    preview_cached = 0
    preview_errors = 0
    preview_unauthorized = 0
    preview_error_samples: List[Dict[str, str]] = []

    for folder_id, record in new_entries_for_preview:
        try:
            preview_doc = _generate_folder_metadata_preview(folder_id, service)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to cache preview for %s during scan: %s", folder_id, exc)
            preview_doc = {
                "folder_id": folder_id,
                "status": "error",
                "error": str(exc),
                "root_folder": {
                    "id": folder_id,
                    "name": record.get("name") or "",
                    "path": record.get("path") or "",
                    "url": record.get("url") or f"https://app.box.com/folder/{folder_id}",
                },
            }

        generated_at = datetime.now(timezone.utc)
        preview_doc["generated_at"] = generated_at
        preview_doc["generated_at_iso"] = generated_at.isoformat()
        try:
            preview_collection.document(folder_id).set(preview_doc)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Unable to persist preview for %s: %s", folder_id, exc)

        has_deal = bool(
            preview_doc.get("status") == "ok"
            and preview_doc.get("deal")
            and preview_doc.get("deal", {}).get("id")
        )
        preview_ts = preview_doc.get("generated_at_iso") or preview_doc.get("generated_at")
        if preview_ts:
            record["preview_generated_at"] = preview_ts

        if has_deal:
            preview_cached += 1
            record.pop("preview_error", None)
            record.pop("preview_source", None)
            logger.info("Scan: cached preview for %s (deal %s)", folder_id, preview_doc.get("deal", {}).get("id"))
        else:
            preview_errors += 1
            error_text = preview_doc.get("error") or "No associated deals found in HubSpot."
            record["preview_error"] = error_text
            record["preview_source"] = preview_doc.get("source") or "scan"
            if "401" in error_text:
                preview_unauthorized += 1
                issues.append(f"Box metadata preview unauthorized for folder {folder_id}")
                logger.warning("Scan: 401 while caching preview for %s", folder_id)
            if error_text and len(preview_error_samples) < 10:
                preview_error_samples.append({"folder_id": folder_id, "error": error_text})
            existing_no_deal = next((item for item in untagged_no_deal_entries if item.get("id") == folder_id), None)
            if not existing_no_deal:
                root_info = preview_doc.get("root_folder") or {}
                no_deal_entry = {
                    "id": folder_id,
                    "name": record.get("name") or root_info.get("name") or "",
                    "path": record.get("path") or root_info.get("path") or "",
                    "url": record.get("url") or root_info.get("url") or f"https://app.box.com/folder/{folder_id}",
                    "preview_error": error_text,
                    "preview_source": record.get("preview_source"),
                }
                if preview_ts:
                    no_deal_entry["preview_generated_at"] = preview_ts
                untagged_no_deal_entries.append(no_deal_entry)

    _sort_tagged_snapshot_entries(tagged_entries)
    _sort_untagged_snapshot_entries(untagged_entries)
    for entry in tagged_entries + untagged_entries + untagged_no_deal_entries:
        _decorate_snapshot_entry(entry)

    update_doc = {
        "tagged": tagged_entries,
        "untagged": untagged_entries,
        "untagged_no_deal": untagged_no_deal_entries,
        "issues": issues[-50:],
        "updated_at": now_iso,
        "last_scanned_at": now_iso,
        "total_tagged": len(tagged_entries),
        "total_untagged": len(untagged_entries),
        "preview_cached_total": (payload.get("preview_cached_total") or 0) + preview_cached,
        "preview_cached_success": (payload.get("preview_cached_success") or 0) + preview_cached,
        "preview_cached_errors": (payload.get("preview_cached_errors") or 0) + preview_errors,
        "preview_updated_at": now_iso,
    }

    try:
        doc_ref.set(update_doc, merge=True)
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to persist scan results to Firestore: %s", exc)
        return jsonify({"message": "Failed to store scan results", "error": str(exc)}), 500

    logger.info(
        "Box scan complete: new tagged=%d, new untagged=%d, previews cached=%d, preview errors=%d (401=%d)",
        new_tagged,
        new_untagged,
        preview_cached,
        preview_errors,
        preview_unauthorized,
    )

    return jsonify(
        {
            "status": "ok",
            "new_tagged": new_tagged,
            "new_untagged": new_untagged,
            "preview_cached": preview_cached,
            "preview_errors": preview_errors,
            "unauthorized": preview_unauthorized,
            "issues": issues[-5:],
            "errors": preview_error_samples,
            "message": "Box folders scanned and snapshot updated.",
        }
    ), 200


@box_bp.route("/box/folder/metadata/no-deal/retry", methods=["POST"])
def box_folder_metadata_retry_no_deal():
    """Re-run metadata preview generation for folders in the no-deal list."""
    guard = _ensure_logged_in()
    if guard is not None:
        return guard

    if not USE_FIRESTORE:
        return jsonify({"message": "Firestore is not enabled; cannot retry metadata previews"}), 503

    db = get_firestore_client()
    if not db:
        return jsonify({"message": "Firestore client unavailable"}), 503

    service = ensure_box_service()
    if not service:
        return jsonify({"message": "Box automation not configured"}), 503

    doc_ref = db.collection("box_folder_metadata").document("tagging_status")
    snapshot = doc_ref.get()
    if not snapshot.exists:
        return jsonify({"message": "Metadata snapshot not found in Firestore."}), 404

    payload = snapshot.to_dict() or {}
    no_deal_entries = _normalize_snapshot_entries(payload.get("untagged_no_deal") or [])
    if not no_deal_entries:
        return jsonify({"status": "ok", "processed": 0, "message": "No folders currently flagged as missing deals."}), 200

    request_payload = request.get_json(silent=True) or {}
    folder_ids = request_payload.get("folder_ids")
    limit = request_payload.get("limit")

    targets = no_deal_entries
    if folder_ids:
        folder_targets = {str(value).strip() for value in folder_ids if str(value).strip()}
        targets = [entry for entry in no_deal_entries if entry.get("id") in folder_targets]
    if isinstance(limit, int) and limit > 0:
        targets = targets[:limit]

    if not targets:
        return jsonify({"status": "ok", "processed": 0, "message": "Provided filters did not match any no-deal folders."}), 200

    preview_collection = db.collection(BOX_METADATA_PREVIEW_COLLECTION)
    processed = 0
    resolved = 0
    remaining = 0
    unauthorized = 0
    error_samples: List[Dict[str, str]] = []
    logger.info("Retrying metadata preview for %d no-deal folders", len(targets))

    for entry in targets:
        folder_id = entry.get("id")
        if not folder_id:
            continue
        try:
            preview_doc = _generate_folder_metadata_preview(folder_id, service)
        except Exception as exc:  # noqa: BLE001
            logger.warning("No-deal retry failed for %s: %s", folder_id, exc)
            preview_doc = {
                "folder_id": folder_id,
                "status": "error",
                "error": str(exc),
                "root_folder": {
                    "id": folder_id,
                    "name": entry.get("name") or "",
                    "path": entry.get("path") or "",
                    "url": entry.get("url") or f"https://app.box.com/folder/{folder_id}",
                },
            }

        generated_at = datetime.now(timezone.utc)
        preview_doc["generated_at"] = generated_at
        preview_doc["generated_at_iso"] = generated_at.isoformat()
        try:
            preview_collection.document(folder_id).set(preview_doc)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to persist retried preview for %s: %s", folder_id, exc)

        _update_snapshot_no_deal_single(folder_id, preview_doc)

        processed += 1
        has_deal = bool(
            preview_doc.get("status") == "ok"
            and preview_doc.get("deal")
            and preview_doc.get("deal", {}).get("id")
        )
        if has_deal:
            resolved += 1
        else:
            remaining += 1
            error_text = preview_doc.get("error") or ""
            if "401" in error_text:
                unauthorized += 1
            if error_text and len(error_samples) < 10:
                error_samples.append({"folder_id": folder_id, "error": error_text})

    retry_timestamp = datetime.now(timezone.utc).isoformat()
    try:
        doc_ref.set({"no_deal_retry_at": retry_timestamp}, merge=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to record no-deal retry timestamp: %s", exc)

    return jsonify(
        {
            "status": "ok",
            "processed": processed,
            "resolved": resolved,
            "remaining": remaining,
            "unauthorized": unauthorized,
            "errors": error_samples,
            "message": "Re-ran metadata preview for folders without associated deals. Refresh snapshot to view updates.",
        }
    ), 200


@box_bp.route("/box/folder/metadata/mismatch", methods=["POST"])
def box_folder_metadata_mark_mismatch():
    """Move a folder from active queues into the mismatch bucket for manual follow-up."""
    guard = _ensure_logged_in()
    if guard is not None:
        return guard

    payload = request.get_json(silent=True) or {}
    folder_id = str(payload.get("folder_id") or "").strip()
    reason = (payload.get("reason") or "").strip()
    if not folder_id:
        return jsonify({"message": "folder_id is required"}), 400

    if not USE_FIRESTORE:
        return jsonify({"message": "Firestore is not enabled; cannot mark mismatch"}), 503

    db = get_firestore_client()
    if not db:
        return jsonify({"message": "Firestore client unavailable"}), 503

    doc_ref = db.collection("box_folder_metadata").document("tagging_status")
    snapshot = doc_ref.get()
    if not snapshot.exists:
        return jsonify({"message": "Metadata snapshot not found"}), 404

    doc = snapshot.to_dict() or {}
    tagged_entries = _normalize_snapshot_entries(doc.get("tagged"))
    untagged_entries = _normalize_snapshot_entries(doc.get("untagged"))
    untagged_no_deal_entries = _normalize_snapshot_entries(doc.get("untagged_no_deal") or [])
    mismatch_entries = _normalize_snapshot_entries(doc.get(MISMATCH_SECTION_KEY) or [])

    def _pop_entry(entries: List[Dict[str, object]]) -> Optional[Dict[str, object]]:
        for idx, entry in enumerate(entries):
            if entry.get("id") == folder_id:
                return entries.pop(idx)
        return None

    record = (
        _pop_entry(untagged_entries)
        or _pop_entry(untagged_no_deal_entries)
        or _pop_entry(tagged_entries)
        or next((entry for entry in mismatch_entries if entry.get("id") == folder_id), None)
    )
    if not record:
        return jsonify({"message": f"Folder {folder_id} not found in snapshot"}), 404
    if record in mismatch_entries:
        return jsonify({"status": "ok", "message": "Folder already in mismatch list."}), 200

    mismatch_entry = dict(record)
    mismatch_entry["mismatch_reason"] = reason or "User flagged mismatch"
    mismatch_entry["mismatch_at"] = datetime.now(timezone.utc).isoformat()
    mismatch_entries.append(mismatch_entry)
    mismatch_entries = sorted(mismatch_entries, key=lambda item: item.get("id") or "")

    update_doc = {
        "tagged": tagged_entries,
        "untagged": untagged_entries,
        "untagged_no_deal": untagged_no_deal_entries,
        MISMATCH_SECTION_KEY: mismatch_entries,
        "mismatch_updated_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        doc_ref.set(update_doc, merge=True)
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to update mismatch list: %s", exc)
        return jsonify({"message": "Unable to update mismatch list", "error": str(exc)}), 500

    return jsonify(
        {
            "status": "ok",
            "message": "Folder moved to mismatch queue.",
            "mismatch_count": len(mismatch_entries),
        }
    ), 200


@box_bp.route("/box/folder/metadata/preview", methods=["GET"])
def box_folder_metadata_preview():
    """Return (and optionally refresh) the cached metadata preview for a folder."""
    guard = _ensure_logged_in()
    if guard is not None:
        return guard

    folder_id = (request.args.get("folder_id") or "").strip()
    if not folder_id:
        return jsonify({"message": "folder_id query parameter is required"}), 400

    refresh = request.args.get("refresh") == "1"

    if not USE_FIRESTORE:
        return jsonify({"message": "Firestore is not enabled; previews unavailable"}), 503

    db = get_firestore_client()
    if not db:
        return jsonify({"message": "Firestore client unavailable"}), 503

    collection = db.collection(BOX_METADATA_PREVIEW_COLLECTION)
    doc_ref = collection.document(folder_id)
    response_doc: Dict[str, object]
    source = "cache"

    def _serialize(doc: dict) -> dict:
        serialized = dict(doc)
        generated_at = serialized.get("generated_at")
        if isinstance(generated_at, datetime):
            serialized["generated_at"] = generated_at.isoformat()
        if serialized.get("generated_at_iso") and not serialized.get("generated_at"):
            serialized["generated_at"] = serialized["generated_at_iso"]
        return serialized

    if refresh:
        service = ensure_box_service()
        if not service:
            return jsonify({"message": "Box automation not configured"}), 503
        try:
            preview_doc = _generate_folder_metadata_preview(folder_id, service)
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to refresh metadata preview for %s: %s", folder_id, exc)
            return jsonify({"message": f"Unable to refresh preview for folder {folder_id}", "error": str(exc)}), 500
        generated_at = datetime.now(timezone.utc)
        preview_doc["generated_at"] = generated_at
        preview_doc["generated_at_iso"] = generated_at.isoformat()
        try:
            doc_ref.set(preview_doc)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to persist refreshed preview for %s: %s", folder_id, exc)
        response_doc = preview_doc
        source = "refreshed"
        _update_snapshot_no_deal_single(folder_id, preview_doc)
    else:
        snapshot = doc_ref.get()
        if snapshot.exists:
            response_doc = snapshot.to_dict() or {}
        else:
            service = ensure_box_service()
            if not service:
                return jsonify({"message": "Box automation not configured"}), 503
            try:
                preview_doc = _generate_folder_metadata_preview(folder_id, service)
            except Exception as exc:  # noqa: BLE001
                logger.error("Failed to build metadata preview for %s: %s", folder_id, exc)
                return jsonify({"message": f"Unable to generate preview for folder {folder_id}", "error": str(exc)}), 500
            generated_at = datetime.now(timezone.utc)
            preview_doc["generated_at"] = generated_at
            preview_doc["generated_at_iso"] = generated_at.isoformat()
            try:
                doc_ref.set(preview_doc)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to persist preview for %s: %s", folder_id, exc)
            response_doc = preview_doc
            source = "generated"
            _update_snapshot_no_deal_single(folder_id, preview_doc)

    serialized = _serialize(response_doc)
    status = serialized.get("status", "ok")
    serialized["status"] = status
    serialized["source"] = source
    return jsonify(serialized), 200 if status == "ok" else 409


@box_bp.route("/box/folder/metadata/status", methods=["GET"])
def box_folder_metadata_status_page():
    """Render cached metadata tagging status from Firestore."""
    guard = _ensure_logged_in()
    if guard is not None:
        return guard

    error_message = None
    tagged_entries: List[Dict[str, Optional[str]]] = []
    untagged_entries: List[Dict[str, Optional[str]]] = []
    untagged_no_deal_entries: List[Dict[str, Optional[str]]] = []
    mismatch_entries: List[Dict[str, Optional[str]]] = []
    issues: List[str] = []
    counts = {"tagged": 0, "untagged": 0, "issues": 0, "mismatch": 0}
    updated_at_display: Optional[str] = None
    updated_at_raw: Optional[str] = None
    snapshot_exists = False

    assignment_slots: List[Dict[str, object]] = []
    preview_cache_summary: Dict[str, Optional[str]] = {
        "total": None,
        "success": None,
        "errors": None,
        "updated_at": None,
    }

    db = None
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
                    tagged_entries = _normalize_snapshot_entries(raw_tagged)
                    untagged_entries = _normalize_snapshot_entries(raw_untagged)
                    untagged_no_deal_entries = _normalize_snapshot_entries(
                        snapshot.get("untagged_no_deal") or []
                    )
                    mismatch_entries = _normalize_snapshot_entries(
                        snapshot.get(MISMATCH_SECTION_KEY) or []
                    )
                    def _sort_untagged_entries(entries: List[Dict[str, Optional[str]]]) -> None:
                        entries.sort(
                            key=lambda item: (
                                0 if item.get("preview_generated_at") else 1,
                                item.get("id") or "",
                            ),
                        )

                    def _sort_tagged_entries(entries: List[Dict[str, Optional[str]]]) -> None:
                        entries.sort(
                            key=lambda item: (
                                -_parse_timestamp_value(item.get("tagged_at") or item.get("tagged_at_display")),
                                item.get("id") or "",
                            ),
                        )

                    _sort_tagged_entries(tagged_entries)
                    _sort_untagged_entries(untagged_entries)
                    for entry in tagged_entries:
                        _decorate_snapshot_entry(entry)
                    for entry in untagged_entries:
                        _decorate_snapshot_entry(entry)
                    for entry in untagged_no_deal_entries:
                        _decorate_snapshot_entry(entry)
                    for entry in mismatch_entries:
                        _decorate_snapshot_entry(entry)
                    issues = list(snapshot.get("issues") or [])
                    counts = {
                        "tagged": len(tagged_entries),
                        "untagged": len(untagged_entries),
                        "issues": len(issues),
                        "mismatch": len(mismatch_entries),
                    }
                    updated_at_raw = snapshot.get("updated_at")
                    updated_at_display = _format_sydney_timestamp(updated_at_raw) or updated_at_raw
                    snapshot_exists = True
                    preview_cache_summary = {
                        "total": snapshot.get("preview_cached_total"),
                        "success": snapshot.get("preview_cached_success"),
                        "errors": snapshot.get("preview_cached_errors"),
                        "updated_at": _format_sydney_timestamp(snapshot.get("preview_updated_at"))
                        or snapshot.get("preview_updated_at"),
                    }
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
            bucket_entries = list(buckets[idx])
            cached_folders: List[Dict[str, Optional[str]]] = []
            pending_folders: List[Dict[str, Optional[str]]] = []
            for entry in bucket_entries:
                target = cached_folders if entry.get("preview_generated_at") else pending_folders
                target.append(entry)

            cached_folders.sort(
                key=lambda item: (
                    -_parse_timestamp_value(item.get("preview_generated_at")),
                    item.get("id") or "",
                ),
            )
            pending_folders.sort(key=lambda item: item.get("id") or "")
            folders = cached_folders + pending_folders
            assignment_slots.append(
                {
                    "index": idx,
                    "assignee": name,
                    "count": len(folders),
                    "cached_count": len(cached_folders),
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
        untagged_no_deal_entries=untagged_no_deal_entries,
        mismatch_entries=mismatch_entries,
        issues=issues,
        updated_at_display=updated_at_display,
        updated_at_raw=updated_at_raw,
        snapshot_exists=snapshot_exists,
        error_message=error_message,
        allow_refresh=allow_refresh,
        assignment_slots=assignment_slots,
        assignee_names=assignee_names,
        preview_cache_enabled=USE_FIRESTORE,
        preview_cache_summary=preview_cache_summary,
    )


@box_bp.route("/box/folder/metadata/guide", methods=["GET"])
def box_folder_metadata_guide():
    guard = _ensure_logged_in()
    if guard is not None:
        return guard
    return render_template("box_metadata_tagging.html", title="Box Metadata Tagging Guide")


@box_bp.route("/box/collaborators/contact", methods=["GET"])
def box_collaborator_contact_details():
    """Return HubSpot contact and associated deal details for a given email."""
    email = (request.args.get("email") or "").strip()
    if not email:
        return jsonify({"message": "email query parameter is required"}), 400

    contact_payload, deals, warnings = _load_contact_details_with_deals(email)
    if not contact_payload:
        message = warnings[0]["message"] if warnings else f"Contact not found for {email}"
        return jsonify({"message": message, "email": email}), 404

    return jsonify(
        {
            "status": "ok",
            "contact": contact_payload,
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
