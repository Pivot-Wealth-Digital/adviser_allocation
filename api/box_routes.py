import logging
import os
from typing import Optional

import requests
from flask import Blueprint, jsonify, redirect, render_template, request, session

from services.box_folder_service import (
    BoxAutomationError,
    create_box_folder_for_deal,
)
from services import box_folder_service as box_service
from utils.secrets import get_secret

logger = logging.getLogger(__name__)

box_bp = Blueprint("box_api", __name__)

HUBSPOT_PORTAL_ID = get_secret("HUBSPOT_PORTAL_ID") or os.environ.get("HUBSPOT_PORTAL_ID")


def _hubspot_contact_url(contact_id: Optional[str]) -> Optional[str]:
    contact_id = (contact_id or "").strip()
    if not contact_id or not HUBSPOT_PORTAL_ID:
        return None
    return f"https://app.hubspot.com/contacts/{HUBSPOT_PORTAL_ID}/record/0-1/{contact_id}"


def _resolve_deal_id(payload: dict) -> Optional[str]:
    return (
        payload.get("deal_id")
        or payload.get("hs_deal_record_id")
        or payload.get("dealId")
        or payload.get("id")
        or (payload.get("object") or {}).get("id")
        or (payload.get("fields") or {}).get("hs_deal_record_id")
    )


def _hubspot_headers() -> dict:
    token = get_secret("HUBSPOT_TOKEN") or os.environ.get("HUBSPOT_TOKEN")
    if not token:
        raise RuntimeError("HUBSPOT_TOKEN is not configured")
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _ensure_logged_in():
    """Enforce session auth for UI views served by this blueprint."""
    if session.get("is_authenticated"):
        return None
    accepts_html = not request.accept_mimetypes or "text/html" in request.accept_mimetypes
    if accepts_html:
        nxt = request.path
        return redirect(f"/login?next={nxt}")
    return jsonify({"error": "Unauthorized"}), 401


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


@box_bp.route("/post/create_box_folder", methods=["POST", "GET"])
def create_box_folder_webhook():
    """Webhook to create the Box client folder for a HubSpot deal."""
    if request.method == "GET":
        return {"message": "Hi, please use POST request."}, 200

    if not request.is_json:
        logger.error("Invalid Content-Type for Box folder webhook: Must be application/json")
        return jsonify({"message": "Invalid Content-Type"}), 415

    payload = request.get_json() or {}
    deal_id = _resolve_deal_id(payload)
    if not deal_id:
        logger.error("Box folder webhook missing deal_id; payload keys=%s", list(payload.keys()))
        return jsonify({"message": "deal_id is required"}), 400

    deal_id = str(deal_id)
    folder_name_override = (payload.get("folder_name") or "").strip() or None
    metadata_override = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else None
    override_keys = sorted(metadata_override.keys()) if metadata_override else []
    logger.info(
        "Received Box folder request for deal %s (folder override=%s, metadata override keys=%s)",
        deal_id,
        folder_name_override or "<auto>",
        override_keys,
    )
    try:
        metadata = _fetch_deal_metadata(deal_id)
        merged_metadata = _merge_metadata(metadata, metadata_override)
        result = create_box_folder_for_deal(deal_id, merged_metadata, folder_name_override)
        status = result.get("status") if isinstance(result, dict) else "unknown"
        response_body = {
            "message": "Processed Box folder request",
            "box": result,
            "deal_id": deal_id,
            "metadata_keys": sorted((result or {}).get("metadata", {}).keys()) if isinstance(result, dict) else [],
        }
        logger.info(
            "Box folder response for deal %s status=%s folder_id=%s",
            deal_id,
            status,
            result.get("folder", {}).get("id") if isinstance(result, dict) else None,
        )
        if status in {"created", "existing"}:
            return jsonify(response_body), 200
        return jsonify(response_body), 202
    except BoxAutomationError as exc:
        logger.error("Box automation error for deal %s: %s", deal_id, exc)
        return jsonify({"message": "Box folder creation failed", "error": str(exc)}), 500
    except RuntimeError as exc:
        logger.error("Configuration error during Box folder creation: %s", exc)
        return jsonify({"message": "Configuration error", "error": str(exc)}), 500
    except Exception as exc:  # pragma: no cover
        logger.exception("Unexpected error while creating Box folder for deal %s", deal_id)
        return jsonify({"message": "Internal Server Error"}), 500


@box_bp.route("/box/create", methods=["GET"])
def box_folder_create_page():
    """Render UI for triggering Box folder creation."""
    guard = _ensure_logged_in()
    if guard is not None:
        return guard
    return render_template("box_folder_create.html", title="Create Box Folder")


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
    contacts = box_service.get_hubspot_deal_contacts(deal_id)
    formatted_contacts = [
        display
        for idx, contact in enumerate(contacts)
        if (display := box_service._format_contact_display(contact, position=idx))
    ]
    folder_name = box_service.build_client_folder_name(deal_id, contacts)
    metadata = _fetch_deal_metadata(deal_id)

    response = {
        "deal_id": deal_id,
        "folder_name": folder_name,
        "contacts": formatted_contacts,
        "metadata": metadata,
    }
    logger.info("Box preview for deal %s: %s", deal_id, response)
    return jsonify(response), 200


__all__ = ["box_bp", "create_box_folder_webhook"]
