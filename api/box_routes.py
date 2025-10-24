import logging
from typing import Optional

from flask import Blueprint, jsonify, request

from services.box_folder_service import (
    BoxAutomationError,
    create_box_folder_for_deal,
)

logger = logging.getLogger(__name__)

box_bp = Blueprint("box_api", __name__)


def _resolve_deal_id(payload: dict) -> Optional[str]:
    return (
        payload.get("deal_id")
        or payload.get("hs_deal_record_id")
        or payload.get("dealId")
        or payload.get("id")
        or (payload.get("object") or {}).get("id")
        or (payload.get("fields") or {}).get("hs_deal_record_id")
    )


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
    try:
        result = create_box_folder_for_deal(deal_id)
        status = result.get("status") if isinstance(result, dict) else "unknown"
        response_body = {
            "message": "Processed Box folder request",
            "box": result,
            "deal_id": deal_id,
        }
        if status == "created":
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


__all__ = ["box_bp", "create_box_folder_webhook"]
