import os
import re
import json
import logging
from datetime import datetime
from typing import Optional

import requests
from flask import Blueprint, jsonify, request

from core.allocation import get_adviser
from services.allocation_service import store_allocation_record
from services.box_folder_service import create_box_folder_for_deal, BoxAutomationError
from utils.common import SYDNEY_TZ, sydney_now
from utils.secrets import get_secret

allocation_bp = Blueprint("allocation_api", __name__)
logger = logging.getLogger(__name__)

HUBSPOT_TOKEN = get_secret("HUBSPOT_TOKEN") or os.environ.get("HUBSPOT_TOKEN")
HUBSPOT_HEADERS = {
    "Authorization": f"Bearer {HUBSPOT_TOKEN}" if HUBSPOT_TOKEN else None,
    "Content-Type": "application/json",
}
# Google Chat webhook for allocation alerts (hardcoded)
CHAT_WEBHOOK_URL = "https://chat.googleapis.com/v1/spaces/AAQADqcOrjo/messages?key=AIzaSyDdI0hCZtE6vySjMm-WEfRq3CPzqKqqsHI&token=TICoV6PHW8ED_C_9RQUV0JftTn9SKCfk7Ns9euSWnAw"

_db = None


def init_allocation_routes(db):
    global _db
    _db = db
    return allocation_bp


def _format_display_name(email: str) -> str:
    local = (email or "").split("@")[0]
    parts = re.split(r"[._-]+", local)
    return " ".join(part.capitalize() for part in parts if part) or (email or "")


def _format_tag_list(raw: str) -> list[str]:
    parts = [p.strip() for p in re.split(r"[;,/|]+", raw or "") if p.strip()]
    formatted = []
    for part in parts:
        formatted.append(part.upper() if part.upper() == "IPO" else part.title())
    return formatted


def send_chat_alert(payload: dict):
    if not CHAT_WEBHOOK_URL:
        logger.info("CHAT_WEBHOOK_URL not configured; skipping chat alert")
        return
    try:
        resp = requests.post(CHAT_WEBHOOK_URL, json=payload, timeout=10)
        if resp.status_code >= 400:
            logger.error("Chat webhook returned %s: %s", resp.status_code, resp.text)
        else:
            logger.info("Sent chat alert successfully")
    except Exception as exc:  # pragma: no cover
        logger.error("Failed to send chat alert: %s", exc)


def build_chat_card_payload(title: str, sections: list[dict]) -> dict:
    card_sections = []
    for section in sections:
        card_sections.append(
            {
                "header": section.get("header"),
                "widgets": [
                    {"textParagraph": {"text": text}} for text in section.get("lines", [])
                ]
                or [],
            }
        )
    return {"cards": [{"header": {"title": title}, "sections": card_sections}]}


def format_agreement_start(agreement_value):
    if not agreement_value:
        return ""
    try:
        value = str(agreement_value).strip()
        if not value:
            return ""
        if value.isdigit():
            dt = datetime.fromtimestamp(int(value) / 1000, tz=SYDNEY_TZ)
        else:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=SYDNEY_TZ)
            dt = parsed.astimezone(SYDNEY_TZ)
        return dt.date().isoformat()
    except Exception as exc:  # pragma: no cover
        logger.warning("Failed to parse agreement_start_date '%s': %s", agreement_value, exc)
        return ""


def _hubspot_headers() -> dict:
    if not HUBSPOT_HEADERS.get("Authorization"):
        raise RuntimeError("HUBSPOT_TOKEN is not configured")
    return HUBSPOT_HEADERS


def _fetch_deal_metadata(deal_id: str) -> Optional[dict]:
    """Fetch deal metadata from HubSpot for Box folder.

    Args:
        deal_id: HubSpot deal ID

    Returns:
        Dict with deal metadata or None if not found
    """
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
            logger.warning("HubSpot deal %s not found", deal_id)
            return None
        resp.raise_for_status()
        deal_data = resp.json()
        props = deal_data.get("properties", {})

        metadata = {
            "hs_deal_record_id": props.get("hs_deal_record_id"),
            "service_package": props.get("service_package"),
            "agreement_start_date": props.get("agreement_start_date"),
            "household_type": props.get("household_type"),
            "hs_spouse_id": props.get("hs_spouse_id"),
            "hs_contact_id": props.get("hs_contact_id"),
            "deal_salutation": props.get("deal_salutation"),
        }
        logger.info("Fetched deal metadata for %s: %s", deal_id, metadata)
        return metadata
    except requests.RequestException as exc:
        logger.error("Failed to fetch deal metadata for %s: %s", deal_id, exc)
        return None


@allocation_bp.route("/post/allocate", methods=["POST", "GET"])
def handle_allocation():
    if request.method == "GET":
        return {"message": "Hi, please use POST request."}, 200

    if not request.is_json:
        logger.error("Invalid Content-Type: Must be application/json")
        return jsonify({"message": "Invalid Content-Type"}), 415

    try:
        event = request.get_json()
        logger.info(
            "----- /post/allocate start %s -----",
            sydney_now().isoformat(),
        )
        logger.info(
            "Received allocation payload: %s",
            json.dumps(event, indent=2, sort_keys=True),
        )
        box_creation_result = None

        if event.get("object", {}).get("objectType", ""):
            service_package = event["fields"]["service_package"]
            household_type = event["fields"].get("household_type", "")
            agreement_start_date = event.get("fields", {}).get("agreement_start_date", "")
            selected_user, candidate_list = get_adviser(service_package, agreement_start_date, household_type)
            create_box_folder_flag = (
                str(request.args.get("create_box_folder", "1")).lower()
                not in ("0", "false", "no", "off")
            )
            send_chat_alert_flag = (
                str(request.args.get("send_chat_alert", "1")).lower()
                not in ("0", "false", "no", "off")
            )
            user = selected_user
            chosen_email = (user.get("properties") or {}).get("hs_email")
            hubspot_owner_id = user["properties"]["hubspot_owner_id"]
            deal_id = event.get("fields", {}).get("hs_deal_record_id", "")
            logger.info(
                "Assigning deal %s to %s (%s)",
                deal_id,
                chosen_email,
                hubspot_owner_id,
            )

            adviser_props = user.get("properties") or {}
            adviser_name = _format_display_name(chosen_email)
            adviser_service_tags = _format_tag_list(
                adviser_props.get("client_types") or ""
            )
            adviser_household_tags = _format_tag_list(
                adviser_props.get("household_type") or ""
            )
            deal_service_display = (
                ", ".join(_format_tag_list(service_package))
                or (service_package or "Unknown")
            )
            deal_household_display = (
                ", ".join(_format_tag_list(household_type))
                or (household_type or "Not provided")
            )
            agreement_start_display = format_agreement_start(agreement_start_date)

            try:
                deal_update_url = (
                    f"https://api.hubapi.com/crm/v3/objects/deals/{deal_id}"
                )
                payload = {"properties": {"advisor": hubspot_owner_id}}

                response = requests.patch(
                    deal_update_url,
                    headers=_hubspot_headers(),
                    data=json.dumps(payload),
                    timeout=10,
                )
                response.raise_for_status()

                logger.info(
                    "Successfully assigned deal %s to owner %s",
                    deal_id,
                    hubspot_owner_id,
                )
                logger.info(
                    "create_box_folder flag=%s",
                    create_box_folder_flag,
                )

                if deal_id and create_box_folder_flag:
                    try:
                        # Fetch deal metadata for Box folder
                        deal_metadata = _fetch_deal_metadata(deal_id)
                        box_creation_result = create_box_folder_for_deal(deal_id, deal_metadata)
                        logger.info(
                            "Box folder result for deal %s: %s",
                            deal_id,
                            box_creation_result,
                        )
                    except BoxAutomationError as exc:
                        logger.error(
                            "Box folder creation failed for deal %s: %s",
                            deal_id,
                            exc,
                        )
                        box_creation_result = {"status": "error", "error": str(exc)}
                elif not create_box_folder_flag:
                    logger.info(
                        "Skipping Box folder creation for deal %s due to request flag",
                        deal_id,
                    )

                logger.info("Persisting allocation record for deal %s", deal_id)
                store_allocation_record(
                    _db,
                    {
                        "client_email": event.get("fields", {}).get("client_email", ""),
                        "adviser_email": chosen_email,
                        "adviser_name": adviser_name,
                        "adviser_hubspot_id": hubspot_owner_id,
                        "adviser_service_packages": adviser_service_tags,
                        "adviser_household_types": adviser_household_tags,
                        "deal_id": deal_id,
                        "service_package": deal_service_display,
                        "service_package_raw": service_package,
                        "household_type": deal_household_display,
                        "household_type_raw": household_type,
                        "agreement_start_date": agreement_start_display,
                        "agreement_start_raw": agreement_start_date,
                        "allocation_result": "completed",
                        "status": "completed",
                    },
                    source="hubspot_webhook",
                    raw_request=event,
                    extra_fields={
                        "ip_address": request.remote_addr,
                        "user_agent": request.headers.get("User-Agent", ""),
                        "box_folder": box_creation_result,
                    },
                )

                candidate_lines = []
                for cand in candidate_list:
                    cand_services = ", ".join(
                        _format_tag_list(cand.get("service_packages"))
                    ) or "Not specified"
                    cand_households = ", ".join(
                        _format_tag_list(cand.get("household_type"))
                    ) or "Not specified"
                    cand_earliest = (
                        cand.get("earliest_open_week_label") or "Unknown"
                    )
                    candidate_lines.append(
                        f"<b>{cand.get('name')}</b> ({cand.get('email')})<br>"
                        f"<i>Services:</i> {cand_services}<br>"
                        f"<i>Households:</i> {cand_households}<br>"
                        f"<i>Earliest Week:</i> {cand_earliest}"
                    )
                if not candidate_lines:
                    candidate_lines = ["No eligible advisers"]

                selected_services = (
                    ", ".join(adviser_service_tags)
                    if adviser_service_tags
                    else "Not specified"
                )
                selected_households = (
                    ", ".join(adviser_household_tags)
                    if adviser_household_tags
                    else "Not specified"
                )
                selected_entry = next(
                    (c for c in candidate_list if c.get("email") == chosen_email),
                    None,
                )
                selected_earliest = (
                    selected_entry.get("earliest_open_week_label")
                    if selected_entry
                    else None
                ) or "Unknown"

                deal_section = [
                    f"<b>Deal ID:</b> `{deal_id}`",
                    f"<b>Service Package:</b> {deal_service_display}",
                    f"<b>Household Type:</b> {deal_household_display}",
                ]

                selected_section = [
                    f"<b>{adviser_name}</b> ({chosen_email})",
                    f"<i>Service Packages:</i> {selected_services}",
                    f"<i>Household Types:</i> {selected_households}",
                    f"<i>Earliest Week:</i> {selected_earliest}",
                ]

                payload = build_chat_card_payload(
                    "Deal Allocation",
                    [
                        {"header": "Deal Details", "lines": deal_section},
                        {"header": "Eligible Advisers", "lines": candidate_lines},
                        {"header": "Selected Adviser", "lines": selected_section},
                    ],
                )
                if send_chat_alert_flag:
                    send_chat_alert(payload)
                    logger.info("Chat alert flag=%s", send_chat_alert_flag)
                else:
                    logger.info("Skipping chat alert for deal %s due to request flag", deal_id)
            except requests.exceptions.HTTPError as http_err:
                logger.error("HTTP error during deal update: %s", http_err)
                logger.error("Response content: %s", response.text)

                store_allocation_record(
                    _db,
                    {
                        "client_email": event.get("fields", {}).get("client_email", ""),
                        "adviser_email": chosen_email,
                        "adviser_name": adviser_name,
                        "adviser_hubspot_id": hubspot_owner_id,
                        "adviser_service_packages": adviser_service_tags,
                        "adviser_household_types": adviser_household_tags,
                        "deal_id": deal_id,
                        "service_package": deal_service_display,
                        "service_package_raw": service_package,
                        "household_type": deal_household_display,
                        "household_type_raw": household_type,
                        "agreement_start_date": agreement_start_display,
                        "agreement_start_raw": agreement_start_date,
                        "allocation_result": "failed",
                        "status": "failed",
                        "error_message": str(http_err),
                    },
                    source="hubspot_webhook",
                    raw_request=event,
                    extra_fields={
                        "ip_address": request.remote_addr,
                        "user_agent": request.headers.get("User-Agent", ""),
                        "box_folder": box_creation_result,
                    },
                )

            except Exception as err:
                logger.error("An unexpected error occurred during deal update: %s", err)

                store_allocation_record(
                    _db,
                    {
                        "client_email": event.get("fields", {}).get("client_email", ""),
                        "adviser_email": chosen_email if chosen_email else "",
                        "adviser_name": adviser_name if "adviser_name" in locals() else "",
                        "adviser_hubspot_id": hubspot_owner_id
                        if "hubspot_owner_id" in locals()
                        else "",
                        "adviser_service_packages": adviser_service_tags
                        if "adviser_service_tags" in locals()
                        else [],
                        "adviser_household_types": adviser_household_tags
                        if "adviser_household_tags" in locals()
                        else [],
                        "deal_id": deal_id if "deal_id" in locals() else "",
                        "service_package": deal_service_display
                        if "deal_service_display" in locals()
                        else (
                            service_package
                            if "service_package" in locals()
                            else ""
                        ),
                        "service_package_raw": service_package
                        if "service_package" in locals()
                        else "",
                        "household_type": deal_household_display
                        if "deal_household_display" in locals()
                        else (
                            household_type
                            if "household_type" in locals()
                            else ""
                        ),
                        "household_type_raw": household_type
                        if "household_type" in locals()
                        else "",
                        "agreement_start_date": agreement_start_display
                        if "agreement_start_display" in locals()
                        else (
                            format_agreement_start(agreement_start_date)
                            if "agreement_start_date" in locals()
                            else ""
                        ),
                        "agreement_start_raw": agreement_start_date
                        if "agreement_start_date" in locals()
                        else "",
                        "allocation_result": "failed",
                        "status": "failed",
                        "error_message": str(err),
                    },
                    source="hubspot_webhook",
                    raw_request=event,
                    extra_fields={
                        "ip_address": request.remote_addr,
                        "user_agent": request.headers.get("User-Agent", ""),
                        "box_folder": box_creation_result,
                    },
                )

        logger.info(
            "----- /post/allocate end %s -----",
            sydney_now().isoformat(),
        )
        return jsonify({"message": "Webhook received successfully", "box_folder": box_creation_result}), 200

    except Exception as exc:  # pragma: no cover
        logger.error("Failed to process webhook: %s", exc)
        return jsonify({"message": "Internal Server Error"}), 500


__all__ = ["allocation_bp", "init_allocation_routes"]
