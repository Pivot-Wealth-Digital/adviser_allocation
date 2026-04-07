import hashlib
import logging
import os
import re
import time
from datetime import datetime, timezone
from functools import lru_cache
from pprint import pformat
from typing import Dict, List, Optional, Tuple

import requests
from flask import Blueprint, jsonify, redirect, render_template, request, session

from adviser_allocation.services import box_folder_service as box_service
from adviser_allocation.services.box_folder_service import (
    CLIENT_SHARING_ROLE,
    CLIENT_SHARING_SUBFOLDER,
    BoxAutomationError,
    ensure_box_service,
    provision_box_folder,
)
from adviser_allocation.utils.auth import require_hubspot_signature
from adviser_allocation.utils.common import (
    SYDNEY_TZ,
    sydney_now,
    sydney_today,
)
from adviser_allocation.utils.secrets import get_secret

logger = logging.getLogger(__name__)

BOX_METADATA_PREVIEW_COLLECTION = os.environ.get(
    "BOX_METADATA_PREVIEW_COLLECTION", "box_metadata_previews"
)
INTERNAL_EMAIL_DOMAIN = (
    (os.environ.get("PIVOT_INTERNAL_DOMAIN") or "@pivotwealth.com.au").strip().lower()
)
MISMATCH_SECTION_KEY = "mismatch_pending"
ISSUE_FOLDER_PATTERN = re.compile(r"folder\s+(\d+)", re.IGNORECASE)
REQUIRED_METADATA_FIELDS = (
    "deal_salutation",
    "household_type",
    "hs_contact_email",
    "hs_contact_firstname",
    "hs_contact_lastname",
    "hs_contact_id",
    "primary_contact_id",
    "primary_contact_link",
    "hs_spouse_id",
    "spouse_contact_link",
    "hs_spouse_firstname",
    "hs_spouse_lastname",
    "hs_spouse_email",
)

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


def _normalize_contact_record(record: dict) -> dict:
    props = record.get("properties") or {}
    contact_id = record.get("id") or props.get("hs_object_id") or ""
    return {
        "id": contact_id,
        "properties": props,
        "url": record.get("url") or _hubspot_contact_url(contact_id),
    }


def _fetch_hubspot_contact_by_id(contact_id: str) -> Optional[dict]:
    contact_id = (contact_id or "").strip()
    if not contact_id:
        return None
    params = {"properties": "firstname,lastname,email,phone"}
    try:
        response = requests.get(
            f"https://api.hubapi.com/crm/v3/objects/contacts/{contact_id}",
            params=params,
            headers=_hubspot_headers(),
            timeout=10,
        )
        response.raise_for_status()
    except requests.HTTPError as exc:
        status_code = exc.response.status_code if exc.response is not None else None
        if status_code == 404:
            return None
        logger.warning("HubSpot contact fetch failed for id %s: %s", contact_id, exc)
        return None
    except requests.RequestException as exc:
        logger.warning("HubSpot contact fetch failed for id %s: %s", contact_id, exc)
        return None
    data = response.json()
    if contact_id and not data.get("url"):
        data["id"] = data.get("id") or contact_id
        data["url"] = _hubspot_contact_url(contact_id)
    return data


def _load_contact_record_from_summary(summary: Optional[dict]) -> Optional[dict]:
    if not summary:
        return None
    contact_id = str(summary.get("id") or summary.get("hs_contact_id") or "").strip()
    email = (summary.get("email") or "").strip()
    contact = _fetch_hubspot_contact_by_id(contact_id) if contact_id else None
    if not contact and email:
        contact = _search_hubspot_contact_by_email(email)
    if contact:
        contact["url"] = contact.get("url") or _hubspot_contact_url(contact.get("id"))
    return contact


def _preview_missing_required_fields(preview_doc: dict) -> bool:
    if not isinstance(preview_doc, dict):
        return False
    metadata_fields = preview_doc.get("metadata_fields") or {}
    if not metadata_fields:
        return False
    if not metadata_fields.get("primary_contact_id"):
        return False
    household_type = (metadata_fields.get("household_type") or "").strip()
    return household_type == ""


def _preview_contact_only_requires_refresh(preview_doc: dict) -> bool:
    if not preview_doc.get("contact_only"):
        return False
    contact_summary = preview_doc.get("contact")
    if not contact_summary:
        contact_summary = (preview_doc.get("contacts") or {}).get("primary")
    email = None
    if isinstance(contact_summary, dict):
        email = contact_summary.get("email") or (contact_summary.get("properties") or {}).get(
            "email"
        )
    return _is_internal_email(email)


def _load_deals_for_contact_record(contact: dict) -> List[dict]:
    contact_id = contact.get("id") or (contact.get("properties") or {}).get("hs_object_id")
    if not contact_id:
        return []
    warnings: List[Dict[str, str]] = []
    try:
        deal_ids = _fetch_contact_associated_deal_ids(contact_id)
    except requests.RequestException as exc:
        logger.error("HubSpot deal lookup failed for contact %s: %s", contact_id, exc)
        return []
    logger.info("Contact %s: retrieved %d associated HubSpot deal ids", contact_id, len(deal_ids))
    deals: List[dict] = []
    for deal_id in deal_ids:
        deal = _fetch_hubspot_deal(deal_id)
        if deal:
            deals.append(deal)
        else:
            logger.warning(
                "Contact %s: associated deal %s could not be loaded", contact_id, deal_id
            )
    logger.info("Contact %s: loaded %d complete deal records", contact_id, len(deals))
    return deals


def _search_hubspot_contact_by_name(first: str, last: str) -> Optional[dict]:
    first = (first or "").strip()
    last = (last or "").strip()
    if not first or not last:
        return None
    payload = {
        "filterGroups": [
            {
                "filters": [
                    {
                        "propertyName": "firstname",
                        "operator": "CONTAINS_TOKEN",
                        "value": first,
                    },
                    {
                        "propertyName": "lastname",
                        "operator": "CONTAINS_TOKEN",
                        "value": last,
                    },
                ]
            }
        ],
        "properties": ["firstname", "lastname", "email", "hs_object_id"],
        "limit": 5,
    }
    response = requests.post(
        "https://api.hubapi.com/crm/v3/objects/contacts/search",
        headers=_hubspot_headers(),
        json=payload,
        timeout=10,
    )
    response.raise_for_status()
    results = response.json().get("results") or []
    if not results:
        return None
    record = results[0]
    record["url"] = _hubspot_contact_url(record.get("id"))
    return record


def _parse_folder_name_candidates(folder_name: Optional[str]) -> List[Tuple[str, str]]:
    candidates: List[Tuple[str, str]] = []
    if not folder_name:
        return candidates
    text = folder_name.strip()
    if "," not in text:
        return candidates
    last_part, rest = text.split(",", 1)
    last_tokens = [token.strip() for token in re.split(r"[-/&]", last_part) if token.strip()]
    first_parts = [token.strip() for token in rest.split("&") if token.strip()]
    for first in first_parts:
        first_token = first.split()[0]
        for last in last_tokens:
            last_token = last.split()[0]
            candidates.append((first_token, last_token))
    return candidates


def _match_contact_for_folder_entry(entry: dict) -> Optional[dict]:
    folder_name = entry.get("name") or entry.get("path") or ""
    for first, last in _parse_folder_name_candidates(folder_name):
        try:
            contact = _search_hubspot_contact_by_name(first, last)
        except requests.RequestException as exc:
            logger.warning("HubSpot name search failed for %s %s: %s", first, last, exc)
            continue
        if contact:
            contact_email = ((contact.get("properties") or {}).get("email") or "").strip().lower()
            if _is_internal_email(contact_email):
                logger.info(
                    "Skipping internal contact %s for folder %s while matching name %s %s",
                    contact_email,
                    entry.get("id"),
                    first,
                    last,
                )
                continue
            logger.info(
                "Contact match found for folder %s via %s %s -> contact %s",
                entry.get("id"),
                first,
                last,
                contact.get("id"),
            )
            return contact
    return None


def _ensure_required_metadata_fields(
    metadata_fields: Optional[dict], contact: Optional[dict]
) -> dict:
    metadata = dict(metadata_fields or {})
    contact = contact or {}
    contact_props = contact.get("properties") or {}
    email = (
        metadata.get("hs_contact_email") or contact.get("email") or contact_props.get("email") or ""
    )
    metadata["hs_contact_email"] = email
    metadata["hs_contact_firstname"] = (
        metadata.get("hs_contact_firstname")
        or contact_props.get("firstname")
        or contact.get("firstname")
        or ""
    )
    metadata["hs_contact_lastname"] = (
        metadata.get("hs_contact_lastname")
        or contact_props.get("lastname")
        or contact.get("lastname")
        or ""
    )
    metadata["hs_contact_id"] = metadata.get("hs_contact_id") or contact.get("id") or ""
    primary_id = metadata.get("primary_contact_id") or contact.get("id") or ""
    metadata["primary_contact_id"] = primary_id
    primary_link = metadata.get("primary_contact_link") or _hubspot_contact_url(primary_id) or ""
    metadata["primary_contact_link"] = primary_link
    metadata["deal_salutation"] = metadata.get("deal_salutation") or ""
    metadata["household_type"] = metadata.get("household_type") or ""
    for key in REQUIRED_METADATA_FIELDS:
        metadata[key] = metadata.get(key) or ""
    return metadata


def _move_folder_to_mismatch(folder_id: str, reason: str) -> None:
    return


def _extract_folder_ids_from_issues(issue_messages: List[str]) -> List[str]:
    if not issue_messages:
        return []
    folder_ids: List[str] = []
    for message in issue_messages:
        if not isinstance(message, str):
            continue
        matches = ISSUE_FOLDER_PATTERN.findall(message)
        for match in matches:
            if match not in folder_ids:
                folder_ids.append(match)
    return folder_ids


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


def _resolve_spouse_contact_from_deal(
    deal_id: Optional[str], primary_contact_id: Optional[str]
) -> Optional[dict]:
    """Resolve spouse contact via contact associations when possible, falling back to deal contacts."""
    primary_contact_id = (primary_contact_id or "").strip()
    spouse = _fetch_spouse_from_contact_associations(primary_contact_id)
    if spouse:
        return spouse

    deal_id = (deal_id or "").strip()
    if not deal_id:
        return None
    try:
        contacts = box_service.get_hubspot_deal_contacts(deal_id)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Unable to load deal contacts for spouse resolution on %s: %s", deal_id, exc)
        return None

    normalized_primary = primary_contact_id
    for contact in contacts:
        contact_id = str(contact.get("id") or "").strip()
        for assoc in contact.get("association_types", []):
            label = (
                (assoc.get("label") or contact.get("primaryAssociationLabel") or "").strip().lower()
            )
            if "spouse" in label and contact_id and contact_id != normalized_primary:
                return contact
    return None


def _fetch_spouse_from_contact_associations(contact_id: Optional[str]) -> Optional[dict]:
    contact_id = (contact_id or "").strip()
    if not contact_id:
        return None

    params = {
        "associations": "contacts",
        "properties": ["firstname", "lastname", "email"],
    }
    try:
        resp = requests.get(
            f"https://api.hubapi.com/crm/v3/objects/contacts/{contact_id}",
            headers=_hubspot_headers(),
            params=params,
            timeout=10,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        logger.debug("Unable to load contact associations for %s: %s", contact_id, exc)
        return None

    data = resp.json()
    associations = data.get("associations", {}).get("contacts", {}).get("results", [])
    spouse_id = None
    for assoc in associations:
        assoc_type = (assoc.get("type") or "").strip().lower()
        assoc_id = (assoc.get("id") or "").strip()
        if assoc_type == "couple" and assoc_id and assoc_id != contact_id:
            spouse_id = assoc_id
            break

    if not spouse_id:
        return None

    try:
        spouse_resp = requests.get(
            f"https://api.hubspot.com/crm/v3/objects/contacts/{spouse_id}",
            headers=_hubspot_headers(),
            params={"properties": ["firstname", "lastname", "email"]},
            timeout=10,
        )
        spouse_resp.raise_for_status()
    except requests.RequestException as exc:
        logger.debug("Unable to load spouse contact %s: %s", spouse_id, exc)
        return None

    payload = spouse_resp.json()
    payload.setdefault("id", spouse_id)
    return payload


def _load_contact_details_with_deals(email: str) -> Tuple[Optional[dict], List[dict], List[dict]]:
    """
    Fetch HubSpot contact plus associated deals (with contact metadata) for a given email.
    """
    email = (email or "").strip()
    if not email:
        return (
            None,
            [],
            [
                {
                    "source": "input",
                    "scope": "email",
                    "message": "Email address is required to load contact details.",
                }
            ],
        )

    try:
        contact = _search_hubspot_contact_by_email(email)
    except requests.RequestException as exc:
        logger.error("HubSpot contact search failed for %s: %s", email, exc)
        return (
            None,
            [],
            [
                {
                    "source": "hubspot",
                    "scope": "contact",
                    "message": "HubSpot contact search failed.",
                }
            ],
        )

    if not contact:
        return (
            None,
            [],
            [
                {
                    "source": "hubspot",
                    "scope": "contact",
                    "message": f"No HubSpot contact found for {email}.",
                }
            ],
        )

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

            def _contact_by_label(label: str, contacts=deal_contacts) -> Optional[dict]:
                target = label.strip().lower()
                for contact_entry in contacts:
                    for assoc in contact_entry.get("association_types", []):
                        assoc_label = (assoc.get("label") or "").strip().lower()
                        if assoc_label == target:
                            return contact_entry
                return None

            primary_entry = _contact_by_label("Client") or (
                deal_contacts[0] if deal_contacts else None
            )
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
                        "display_name": box_service._format_contact_display(
                            contact_entry, position=idx
                        ),
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
            logger.warning(
                "Unexpected error loading associated contacts for deal %s: %s", deal_id, exc
            )
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

    if payload.get("deal_id") and contact_id and str(payload.get("deal_id")).strip() == contact_id:
        payload.pop("deal_id", None)
        payload.pop("hs_deal_record_id", None)

    return payload


def _build_preview_output(
    folder_id: str,
    metadata_fields: Dict[str, object],
    contact: dict,
    selected_deal: dict,
    root_info: dict,
) -> dict:
    contact_props = (contact.get("properties") or {}) if contact else {}
    primary_name = " ".join(
        filter(None, [contact_props.get("firstname"), contact_props.get("lastname")])
    ).strip()
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


def _assemble_preview_document(
    folder_id: str,
    service,
    contact: dict,
    deal: dict,
    *,
    contact_only: bool = False,
    warnings: Optional[List[dict]] = None,
) -> dict:
    root_info = _get_folder_root_info(service, folder_id)
    base_payload = _build_preview_base_payload(folder_id, contact, deal)
    metadata_payload, _, _ = _build_metadata_from_payload(base_payload)
    metadata_payload = _ensure_required_metadata_fields(metadata_payload, contact)
    preview_data = _build_preview_output(
        folder_id, metadata_payload or {}, contact, deal, root_info
    )
    generated_at = datetime.now(timezone.utc)
    doc = {
        "folder_id": folder_id,
        "status": "ok",
        "root_folder": root_info,
        "preview": preview_data,
        "metadata_fields": metadata_payload or {},
        "base_payload": base_payload,
        "final_payload": metadata_payload or {},
        "contact": preview_data["contacts"]["primary"],
        "deal": preview_data.get("deal"),
        "warnings": warnings or [],
        "generated_at": generated_at,
        "generated_at_iso": generated_at.isoformat(),
    }
    if contact_only:
        doc["contact_only"] = True
    return doc


def _build_preview_from_contact_and_deal(
    folder_id: str, service, contact: dict, deal: dict
) -> dict:
    return _assemble_preview_document(folder_id, service, contact, deal)


def _build_contact_only_preview(folder_id: str, service, contact: dict) -> dict:
    contact_props = (contact.get("properties") or {}) if contact else {}
    contact_name = " ".join(
        filter(None, [contact_props.get("firstname"), contact_props.get("lastname")])
    ).strip()
    pseudo_deal_id = f"{folder_id}_contact_only"
    pseudo_deal = {
        "id": pseudo_deal_id,
        "properties": {
            "dealname": contact_name or "Contact Only",
            "dealstage": "contact_only",
            "hs_deal_record_id": pseudo_deal_id,
        },
        "url": contact.get("url") or _hubspot_contact_url(contact.get("id")),
    }
    return _assemble_preview_document(
        folder_id,
        service,
        contact,
        pseudo_deal,
        contact_only=True,
        warnings=[
            {
                "source": "hubspot",
                "scope": "deal",
                "message": "Contact-only metadata preview (no associated deal).",
            }
        ],
    )


def _get_folder_root_info(service, folder_id: str) -> dict:
    fallback = {
        "id": folder_id,
        "name": "",
        "path": "",
        "url": f"https://app.box.com/folder/{folder_id}",
    }
    max_attempts = 2
    for attempt in range(1, max_attempts + 1):
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
            message = str(exc).lower()
            retryable = any(
                keyword in message for keyword in ("connection reset", "connection aborted")
            )
            if retryable and attempt < max_attempts:
                logger.info(
                    "Retrying folder details for %s after transient error: %s", folder_id, exc
                )
                time.sleep(0.4)
                continue
            logger.warning("Unable to fetch folder details for %s: %s", folder_id, exc)
            break
        except Exception as exc:  # noqa: BLE001
            logger.warning("Unexpected error fetching folder %s: %s", folder_id, exc)
            break
    return fallback


def _generate_folder_metadata_preview(
    folder_id: str, service, allow_contact_only: bool = False
) -> dict:
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
        pseudo_deal_id = (
            contact.get("id")
            or contact_props.get("hs_object_id")
            or contact_props.get("email")
            or ""
        )
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
    ensured_metadata = _ensure_required_metadata_fields(merged_metadata, contact)

    final_payload = dict(base_payload)
    for key, value in ensured_metadata.items():
        final_payload[key] = value
    if not final_payload.get("folder_id"):
        final_payload["folder_id"] = folder_id

    preview_data = _build_preview_output(
        folder_id, ensured_metadata, contact, selected_deal, root_info
    )
    preview_doc = {
        "folder_id": folder_id,
        "status": "ok",
        "root_folder": root_info,
        "preview": preview_data,
        "metadata_fields": ensured_metadata,
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
            preview_doc = _generate_folder_metadata_preview(
                folder_id, service, allow_contact_only=True
            )
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
        preview_doc["folder_name"] = entry.get("name") or preview_doc.get("root_folder", {}).get(
            "name", ""
        )
        preview_doc["folder_path"] = entry.get("path") or preview_doc.get("root_folder", {}).get(
            "path", ""
        )
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
    return


@box_bp.route("/box/folder/metadata/previews/run", methods=["POST"])
def box_folder_run_metadata_previews():
    """Iterate untagged folders (optionally per assignment slot) and cache metadata previews."""
    guard = _ensure_logged_in()
    if guard is not None:
        return guard

    return jsonify({"message": "Not available (requires database)"}), 503


def _record_metadata_snapshot_tag(folder_id: str) -> None:
    return


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


def _update_hubspot_contacts_with_folder_url(
    metadata: dict, metadata_payload: dict, folder_url: str
) -> None:
    """Best-effort update of HubSpot contact records with the Box folder URL."""
    contact_ids = set()
    primary_contact_id = str(
        metadata.get("primary_contact_id") or metadata_payload.get("hs_contact_id") or ""
    ).strip()
    spouse_contact_id = str(
        metadata.get("hs_spouse_id") or metadata_payload.get("hs_spouse_id") or ""
    ).strip()
    if primary_contact_id:
        contact_ids.add(primary_contact_id)
    if spouse_contact_id:
        contact_ids.add(spouse_contact_id)
    for contact_id in contact_ids:
        if not _update_hubspot_contact_property(
            contact_id, HUBSPOT_BOX_FOLDER_CONTACT_PROPERTY, folder_url
        ):
            logger.warning(
                "Unable to update HubSpot contact %s with box folder url %s",
                contact_id,
                folder_url,
            )


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
    household_type = _extract_payload_value(payload, "household_type") or ""

    if deal_salutation:
        metadata["deal_salutation"] = deal_salutation
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
        return {"id": contact_id or "", "properties": props}

    contacts.append(_make_contact(primary_contact_id, primary_first, primary_last, primary_email))

    contacts.append(_make_contact(spouse_id, spouse_first, spouse_last, spouse_email))

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
            str(contact.get("id")) for contact in contacts if contact.get("id")
        ]

        primary_contact = _contact_by_label("Client") or (contacts[0] if contacts else None)
        spouse_contact = _contact_by_label("Client's Spouse")

        primary_contact_id = (
            props.get("hs_contact_id")
            or (primary_contact.get("id") if primary_contact else None)
            or (associated_contact_ids[0] if associated_contact_ids else None)
        )
        primary_contact_link = _hubspot_contact_url(primary_contact_id)

        spouse_id = props.get("hs_spouse_id") or (
            spouse_contact.get("id") if spouse_contact else None
        )
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
        contacts.append(_make_contact(spouse_id, spouse_first, spouse_last, spouse_email))

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
    logger.info("HubSpot associations query: GET %s", url)
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
                "deal_salutation",
                "household_type",
                "hs_spouse_id",
                "hs_spouse_firstname",
                "hs_spouse_lastname",
                "hs_spouse_email",
                "hs_contact_id",
                "hs_contact_firstname",
                "hs_contact_lastname",
                "hs_contact_email",
            ]
        }
        resp = requests.get(url, headers=_hubspot_headers(), params=params, timeout=10)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        data = resp.json()
        data["url"] = (
            f"https://app.hubspot.com/contacts/{_hubspot_portal_id()}/record/0-3/{deal_id}"
        )
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
            logger.warning(
                "HubSpot deal %s not found while updating properties %s",
                deal_id,
                list(properties.keys()),
            )
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
            logger.warning(
                "HubSpot contact %s not found when updating %s", contact_id, property_name
            )
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
@require_hubspot_signature
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
@require_hubspot_signature
def box_folder_apply_metadata():
    """Apply metadata template to an existing Box folder."""
    if not request.is_json:
        return jsonify({"message": "Invalid Content-Type"}), 415

    payload = request.get_json() or {}
    folder_id = _extract_folder_id(payload)
    if not folder_id:
        return jsonify({"message": "folder_id is required"}), 400

    deal_id = _resolve_deal_id(payload)
    logger.info(
        "Manual metadata apply requested: folder=%s deal=%s payload_keys=%s",
        folder_id,
        deal_id,
        sorted(payload.keys()),
    )
    missing_payload_fields = [
        key for key in REQUIRED_METADATA_PAYLOAD_FIELDS if not _extract_payload_value(payload, key)
    ]
    if missing_payload_fields:
        logger.error(
            "Manual metadata apply missing payload fields for folder=%s deal=%s missing=%s",
            folder_id,
            deal_id,
            missing_payload_fields,
        )
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
    metadata_override = (
        payload.get("metadata") if isinstance(payload.get("metadata"), dict) else None
    )
    metadata = _merge_metadata(metadata_payload, metadata_override) or {}
    metadata = _ensure_required_metadata_fields(metadata, None)

    missing_metadata = _missing_metadata_fields(metadata)
    if missing_metadata:
        logger.error(
            "Manual metadata apply missing metadata fields for folder=%s deal=%s missing=%s",
            folder_id,
            deal_id,
            missing_metadata,
        )
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
        return jsonify({"message": "Box metadata apply failed", "error": str(exc)}), 502
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "Unexpected error during metadata apply for folder %s deal %s", folder_id, deal_id
        )
        return jsonify(
            {"message": "Unexpected server error during metadata apply", "error": str(exc)}
        ), 500

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
        if not _update_hubspot_deal_properties(
            deal_id, {HUBSPOT_BOX_FOLDER_DEAL_PROPERTY: folder_url}
        ):
            logger.warning(
                "Unable to update HubSpot deal %s with box folder url %s",
                deal_id,
                folder_url,
            )
    _update_hubspot_contacts_with_folder_url(metadata, metadata_payload, folder_url)
    return jsonify(response), 200


@box_bp.route("/box/folder/tag/auto", methods=["POST"])
@require_hubspot_signature
def box_folder_apply_metadata_auto():
    """Apply metadata using minimal payload, fetching missing fields from HubSpot if required."""
    if not request.is_json:
        return jsonify({"message": "Invalid Content-Type"}), 415

    payload = request.get_json() or {}
    folder_id = _extract_folder_id(payload)
    if not folder_id:
        return jsonify({"message": "folder_id is required"}), 400

    deal_id = _resolve_deal_id(payload)

    logger.info(
        "Auto metadata apply requested: folder=%s deal=%s payload_keys=%s",
        folder_id,
        deal_id,
        sorted(payload.keys()),
    )
    metadata_payload, _, _ = _build_metadata_from_payload(payload)
    metadata_override = (
        payload.get("metadata") if isinstance(payload.get("metadata"), dict) else None
    )
    metadata = _merge_metadata(metadata_payload, metadata_override) or {}
    metadata = _ensure_required_metadata_fields(metadata, None)
    metadata_source = "payload" if metadata else ""

    missing_metadata = _missing_metadata_fields(metadata)
    if missing_metadata and deal_id:
        fetched_metadata = _fetch_deal_metadata(str(deal_id)) or {}
        if fetched_metadata:
            metadata = _merge_metadata(metadata, fetched_metadata) or fetched_metadata
            metadata_source = "payload+hubspot" if metadata_source == "payload" else "hubspot"
        missing_metadata = _missing_metadata_fields(metadata)

    if missing_metadata:
        logger.error(
            "Auto metadata apply missing metadata fields for folder=%s deal=%s missing=%s metadata=%s",
            folder_id,
            deal_id,
            missing_metadata,
            metadata,
        )
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
        logger.error(
            "Auto metadata apply failed for folder %s deal %s: %s", folder_id, deal_id, exc
        )
        return jsonify({"message": "Box metadata apply failed", "error": str(exc)}), 502
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "Unexpected error during auto metadata apply for folder %s deal %s", folder_id, deal_id
        )
        return jsonify(
            {"message": "Unexpected server error during metadata apply", "error": str(exc)}
        ), 500

    folder_url = f"https://app.box.com/folder/{folder_id}"
    response = {
        "deal_id": str(deal_id) if deal_id else None,
        "folder_id": folder_id,
        "metadata_fields": sorted(metadata.keys()),
        "status": "tagged",
        "metadata_source": metadata_source or "hubspot",
        "box_folder_url": folder_url,
    }
    try:
        _record_metadata_snapshot_tag(folder_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Unable to update metadata snapshot after auto-tagging %s: %s", folder_id, exc
        )
    if HUBSPOT_BOX_FOLDER_DEAL_PROPERTY and deal_id:
        if not _update_hubspot_deal_properties(
            str(deal_id), {HUBSPOT_BOX_FOLDER_DEAL_PROPERTY: folder_url}
        ):
            logger.warning(
                "Unable to update HubSpot deal %s with box folder url %s",
                deal_id,
                folder_url,
            )
    _update_hubspot_contacts_with_folder_url(metadata, metadata_payload, folder_url)
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

    if not _update_hubspot_deal_properties(
        str(deal_id), {HUBSPOT_BOX_FOLDER_DEAL_PROPERTY: folder_url}
    ):
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

        subfolders = service.list_subfolders(target_folder_id) if include_subfolders else []
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
    """Reload the cached metadata snapshot (no Box scan). Disabled: database removed."""
    guard = _ensure_logged_in()
    if guard is not None:
        return guard

    return jsonify({"message": "Not available (requires database)"}), 503


@box_bp.route("/box/folder/metadata/scan", methods=["POST"])
def box_folder_metadata_scan():
    """Scan Box active client folders and append any new entries to the snapshot. Disabled: database removed."""
    guard = _ensure_logged_in()
    if guard is not None:
        return guard

    return jsonify({"message": "Not available (requires database)"}), 503


@box_bp.route("/box/folder/metadata/no-deal/retry", methods=["POST"])
def box_folder_metadata_retry_no_deal():
    """Re-run metadata preview generation for folders in the no-deal list. Disabled: database removed."""
    guard = _ensure_logged_in()
    if guard is not None:
        return guard

    return jsonify({"message": "Not available (requires database)"}), 503


@box_bp.route("/box/folder/metadata/issues/retry", methods=["POST"])
def box_folder_metadata_retry_issues():
    """Extract folder IDs mentioned in snapshot issues and re-run their previews. Disabled: database removed."""
    guard = _ensure_logged_in()
    if guard is not None:
        return guard

    return jsonify({"message": "Not available (requires database)"}), 503


@box_bp.route("/box/folder/metadata/previews/cache/fix", methods=["POST"])
def box_folder_metadata_fix_cache():
    """Re-run cached previews missing household fields and tidy contact-only data. Disabled: database removed."""
    guard = _ensure_logged_in()
    if guard is not None:
        return guard

    return jsonify({"message": "Not available (requires database)"}), 503


@box_bp.route("/box/folder/metadata/mismatch/fix-contacts", methods=["POST"])
def box_folder_metadata_fix_mismatch_contacts():
    """Attempt to resolve mismatch folders by using collaborator emails or folder names to find contacts."""
    guard = _ensure_logged_in()
    if guard is not None:
        return guard

    return jsonify({"message": "Not available (requires database)"}), 503


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

    return jsonify({"message": "Not available (requires database)"}), 503


@box_bp.route("/box/folder/metadata/contact-match", methods=["POST"])
def box_folder_metadata_contact_match():
    """Match folder name to HubSpot contact and refresh cached metadata."""
    guard = _ensure_logged_in()
    if guard is not None:
        return guard

    payload = request.get_json(silent=True) or {}
    folder_id = str(payload.get("folder_id") or "").strip()
    if not folder_id:
        return jsonify({"message": "folder_id is required"}), 400

    return jsonify({"message": "Not available (requires database)"}), 503


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

    return jsonify({"message": "Not available (requires database)"}), 503


@box_bp.route("/box/folder/metadata/status", methods=["GET"])
def box_folder_metadata_status_page():
    """Render cached metadata tagging status."""
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

    error_message = "Metadata status requires database."

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
            contact_only_folders: List[Dict[str, Optional[str]]] = []
            pending_folders: List[Dict[str, Optional[str]]] = []
            for entry in bucket_entries:
                if entry.get("contact_only"):
                    contact_only_folders.append(entry)
                elif entry.get("preview_generated_at"):
                    cached_folders.append(entry)
                else:
                    pending_folders.append(entry)

            cached_folders.sort(
                key=lambda item: (
                    -_parse_timestamp_value(item.get("preview_generated_at")),
                    item.get("id") or "",
                ),
            )
            contact_only_folders.sort(
                key=lambda item: (
                    -_parse_timestamp_value(item.get("preview_generated_at")),
                    item.get("id") or "",
                ),
            )
            pending_folders.sort(key=lambda item: item.get("id") or "")
            assignment_slots.append(
                {
                    "index": idx,
                    "assignee": name,
                    "count": len(bucket_entries),
                    "cached_count": len(cached_folders),
                    "contact_only_count": len(contact_only_folders),
                    "folders": {
                        "cached": cached_folders,
                        "contact_only": contact_only_folders,
                        "pending": pending_folders,
                    },
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
        preview_cache_enabled=False,
        preview_cache_summary=preview_cache_summary,
    )


@box_bp.route("/box/folder/metadata/guide", methods=["GET"])
def box_folder_metadata_guide():
    guard = _ensure_logged_in()
    if guard is not None:
        return guard
    return render_template("box_metadata_tagging.html", title="Box Metadata Tagging Guide")


@box_bp.route("/box/folder/metadata/manual", methods=["GET"])
def box_folder_metadata_manual():
    guard = _ensure_logged_in()
    if guard is not None:
        return guard
    return render_template("box_metadata_manual.html", title="Manual Metadata Tagging")


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


__all__ = ["box_bp"]


def _is_internal_email(email: Optional[str]) -> bool:
    return (email or "").strip().lower().endswith(INTERNAL_EMAIL_DOMAIN)


@box_bp.route("/box/folder/metadata/manual-preview", methods=["POST"])
def box_folder_metadata_manual_preview():
    """Build a metadata preview using a specified HubSpot contact (and optional deal) without applying it."""
    guard = _ensure_logged_in()
    if guard is not None:
        return guard

    payload = request.get_json(silent=True) or {}
    return _process_manual_preview_request(payload)


@box_bp.route("/box/folder/metadata/manual-apply", methods=["POST"])
def box_folder_metadata_manual_apply():
    """Apply metadata for a folder using previously previewed payload."""
    guard = _ensure_logged_in()
    if guard is not None:
        return guard

    payload = request.get_json(silent=True) or {}
    payload["auto_apply"] = True
    return _process_manual_preview_request(payload)


def _process_manual_preview_request(payload: dict):
    folder_id = str(payload.get("folder_id") or "").strip()
    contact_id = str(payload.get("contact_id") or "").strip()
    override_deal_id = str(payload.get("deal_id") or "").strip()
    if not folder_id:
        return jsonify({"message": "folder_id is required"}), 400
    if not contact_id:
        return jsonify({"message": "contact_id is required"}), 400

    service = ensure_box_service()
    if not service:
        return jsonify({"message": "Box automation not configured"}), 503

    contact = _fetch_hubspot_contact_by_id(contact_id)
    if not contact:
        return jsonify({"message": f"HubSpot contact {contact_id} not found."}), 404

    deals: List[dict] = []
    if override_deal_id:
        deal = _fetch_hubspot_deal(override_deal_id)
        if deal:
            deals = [deal]
    if not deals:
        deals = _load_deals_for_contact_record(contact)

    selected_deal = None
    if override_deal_id:
        selected_deal = next(
            (deal for deal in deals if str(deal.get("id")) == override_deal_id), None
        )
    if not selected_deal and deals:
        selected_deal = max(deals, key=_extract_deal_timestamp_value)

    try:
        if selected_deal:
            preview_doc = _build_preview_from_contact_and_deal(
                folder_id, service, contact, selected_deal
            )
            preview_doc.pop("contact_only", None)
        else:
            preview_doc = _build_contact_only_preview(folder_id, service, contact)
    except Exception as exc:  # noqa: BLE001
        logger.error("Manual preview: unable to build preview for %s: %s", folder_id, exc)
        return jsonify({"message": f"Preview generation failed: {exc}"}), 500

    metadata_fields = preview_doc.get("metadata_fields") or {}
    final_payload = preview_doc.get("final_payload") or {}
    final_payload["folder_id"] = folder_id
    if not metadata_fields.get("primary_contact_id"):
        metadata_fields["primary_contact_id"] = contact_id
    if not metadata_fields.get("hs_contact_id"):
        metadata_fields["hs_contact_id"] = contact_id
    if selected_deal:
        deal_identifier = str(
            selected_deal.get("properties", {}).get("hs_deal_record_id")
            or selected_deal.get("id")
            or ""
        )
        if deal_identifier:
            final_payload["deal_id"] = deal_identifier
            final_payload["hs_deal_record_id"] = deal_identifier
        deal_props = selected_deal.get("properties") or {}
        for key in (
            "household_type",
            "deal_salutation",
            "hs_spouse_id",
            "hs_spouse_firstname",
            "hs_spouse_lastname",
            "hs_spouse_email",
            "hs_contact_id",
            "hs_contact_firstname",
            "hs_contact_lastname",
            "hs_contact_email",
        ):
            if key in deal_props and deal_props.get(key):
                metadata_fields[key] = deal_props[key]
                final_payload[key] = deal_props[key]

        if (
            metadata_fields.get("hs_spouse_id")
            and metadata_fields.get("hs_spouse_id") == contact_id
        ):
            metadata_fields.pop("hs_spouse_id", None)
            metadata_fields.pop("hs_spouse_firstname", None)
            metadata_fields.pop("hs_spouse_lastname", None)
            metadata_fields.pop("hs_spouse_email", None)
            final_payload.pop("hs_spouse_id", None)
            final_payload.pop("hs_spouse_firstname", None)
            final_payload.pop("hs_spouse_lastname", None)
            final_payload.pop("hs_spouse_email", None)
        spouse_needed = (metadata_fields.get("household_type") or "").strip().lower() == "couple"
        missing_spouse = not metadata_fields.get("hs_spouse_id")
        if spouse_needed and missing_spouse:
            spouse_entry = _resolve_spouse_contact_from_deal(
                final_payload.get("deal_id"),
                metadata_fields.get("primary_contact_id") or contact_id,
            )
            if spouse_entry:
                props = spouse_entry.get("properties") or {}
                metadata_fields["hs_spouse_id"] = spouse_entry.get("id")
                metadata_fields["hs_spouse_firstname"] = props.get("firstname") or ""
                metadata_fields["hs_spouse_lastname"] = props.get("lastname") or ""
                metadata_fields["hs_spouse_email"] = props.get("email") or ""
                metadata_fields["spouse_contact_link"] = (
                    _hubspot_contact_url(spouse_entry.get("id")) or ""
                )
                final_payload["hs_spouse_id"] = metadata_fields["hs_spouse_id"]
                final_payload["hs_spouse_firstname"] = metadata_fields["hs_spouse_firstname"]
                final_payload["hs_spouse_lastname"] = metadata_fields["hs_spouse_lastname"]
                final_payload["hs_spouse_email"] = metadata_fields["hs_spouse_email"]
                final_payload["spouse_contact_link"] = metadata_fields["spouse_contact_link"]
    root_info = preview_doc.get("root_folder") or _get_folder_root_info(service, folder_id)
    rebuilt_preview = _build_preview_output(
        folder_id,
        metadata_fields,
        contact,
        selected_deal or preview_doc.get("deal"),
        root_info,
    )
    preview_doc["preview"] = rebuilt_preview

    preview_doc["source"] = "manual_contact_preview"
    preview_doc["generated_at"] = datetime.now(timezone.utc)
    preview_doc["generated_at_iso"] = preview_doc["generated_at"].isoformat()

    response_payload = {
        "status": "ok",
        "folder_id": folder_id,
        "contact_id": contact_id,
        "deal_id": final_payload.get("deal_id"),
        "preview": preview_doc.get("preview"),
        "metadata_fields": metadata_fields,
        "final_payload": final_payload,
    }

    if payload.get("auto_apply"):
        metadata_payload_built, _, _ = _build_metadata_from_payload(final_payload)
        metadata_override = (
            final_payload.get("metadata")
            if isinstance(final_payload.get("metadata"), dict)
            else None
        )
        metadata_to_apply = (
            _merge_metadata(metadata_payload_built, metadata_override) or metadata_override or {}
        )
        metadata_to_apply = _ensure_required_metadata_fields(metadata_to_apply, contact)
        missing_metadata = _missing_metadata_fields(metadata_to_apply)
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

        try:
            service.apply_metadata_template(folder_id, metadata_to_apply)
        except BoxAutomationError as exc:
            logger.error("Manual preview auto apply failed for %s: %s", folder_id, exc)
            return jsonify({"message": "Box metadata apply failed", "error": str(exc)}), 500

        folder_url = f"https://app.box.com/folder/{folder_id}"
        try:
            _record_metadata_snapshot_tag(folder_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Manual preview: unable to update snapshot tag for %s: %s", folder_id, exc
            )
        if HUBSPOT_BOX_FOLDER_DEAL_PROPERTY and final_payload.get("deal_id"):
            if not _update_hubspot_deal_properties(
                final_payload["deal_id"], {HUBSPOT_BOX_FOLDER_DEAL_PROPERTY: folder_url}
            ):
                logger.warning(
                    "Manual preview: unable to update HubSpot deal %s with url %s",
                    final_payload["deal_id"],
                    folder_url,
                )
        for contact_identifier in {
            metadata_to_apply.get("primary_contact_id"),
            metadata_to_apply.get("hs_spouse_id"),
        }:
            contact_identifier = str(contact_identifier or "").strip()
            if contact_identifier:
                _update_hubspot_contact_property(
                    contact_identifier, HUBSPOT_BOX_FOLDER_CONTACT_PROPERTY, folder_url
                )

        response_payload["status"] = "tagged"
        response_payload["box_folder_url"] = folder_url

    return jsonify(response_payload), 200


@box_bp.route("/box/folder/metadata/fix-spouses", methods=["POST"])
def box_folder_metadata_fix_spouses():
    """Ensure spouse IDs differ from the primary contact by rematching or clearing."""
    guard = _ensure_logged_in()
    if guard is not None:
        return guard

    return jsonify({"message": "Not available (requires database)"}), 503
