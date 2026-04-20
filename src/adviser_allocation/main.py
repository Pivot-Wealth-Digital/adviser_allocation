import json
import logging
import os
import re
import secrets
import time
from collections import Counter
from datetime import date, datetime, timedelta
from functools import lru_cache
from urllib.parse import urlencode

import requests
from authlib.integrations.flask_client import OAuth  # Added for Authlib
from dotenv import load_dotenv
from flask import (
    Blueprint,
    Flask,
    current_app,  # Added current_app for Authlib
    jsonify,
    redirect,
    render_template,
    render_template_string,
    request,
    send_from_directory,
    session,
    url_for,
)

# Import skill definitions to register all skills in the system
import adviser_allocation.skills.definitions
from adviser_allocation.api.skills_routes import skills_bp
from adviser_allocation.api.webhooks import init_webhooks
from adviser_allocation.core.allocation import (
    build_service_household_matrix,
    compute_user_schedule_by_email,
    get_monday_from_weeks_ago,
    get_user_ids_adviser,
    get_user_meeting_details,
    get_users_earliest_availability,
    refresh_capacity_override_cache,
    week_label_from_ordinal,
)
from adviser_allocation.services.allocation_service import store_allocation_record
from adviser_allocation.utils.common import (
    SYDNEY_TZ,
    get_cloudsql_db,
    sydney_now,
    sydney_today,
)

# Load variables from .env into environment
load_dotenv()

from adviser_allocation.utils.auth import require_api_key, require_oidc_token
from adviser_allocation.utils.secrets import get_secret

LOG_LEVEL_NAME = (os.environ.get("LOG_LEVEL") or "INFO").upper()
LOG_LEVEL = getattr(logging, LOG_LEVEL_NAME, logging.INFO)

# Use Google Cloud Logging format for Cloud Run
try:
    import google.cloud.logging
    from google.cloud.logging.handlers import CloudLoggingHandler

    cloud_logging_client = google.cloud.logging.Client()
    cloud_handler = CloudLoggingHandler(cloud_logging_client)
    logging.root.addHandler(cloud_handler)
    logging.root.setLevel(LOG_LEVEL)
except (ImportError, Exception):
    # Fallback to standard logging if Cloud Logging not available
    LOG_FORMAT = os.environ.get(
        "LOG_FORMAT", "[%(asctime)s] %(levelname)s in %(module)s: %(message)s"
    )
    logging.basicConfig(level=LOG_LEVEL, format=LOG_FORMAT)
    logging.getLogger().setLevel(LOG_LEVEL)

# Initialize logger
logger = logging.getLogger(__name__)

# Application metadata
APP_VERSION = os.environ.get("APP_VERSION", "1.0.0")


def is_authenticated():
    return bool(session.get("is_authenticated"))


def check_is_admin():
    """Check if the current session user is an admin. Cached in session."""
    if not is_authenticated():
        return False
    if "is_admin" in session:
        return session["is_admin"]
    email = session.get("user", {}).get("email", "")
    try:
        db = get_cloudsql_db()
        result = bool(db.is_admin(email))
        session["is_admin"] = result
        return result
    except Exception:
        logger.exception("Failed to check admin status for %s", email)
        return False


def admin_required(view_func):
    """Decorator that restricts access to admin users only."""
    from functools import wraps

    @wraps(view_func)
    def wrapper(*args, **kwargs):
        if not check_is_admin():
            if request.accept_mimetypes.best == "text/html":
                return render_template(
                    "error.html",
                    code=403,
                    message="You do not have permission to access this page.",
                ), 403
            return jsonify({"error": "Forbidden"}), 403
        return view_func(*args, **kwargs)

    return wrapper


def _safe_redirect_url(url: str) -> str:
    """Ensure redirect URL is a safe relative path (prevent open redirect)."""
    if not url or not url.startswith("/") or url.startswith("//"):
        return "/"
    return url


def login_required(view_func):
    from functools import wraps

    @wraps(view_func)
    def wrapper(*args, **kwargs):
        if not is_authenticated():
            # For API calls, return 401; for browser, redirect to login with next
            if request.accept_mimetypes and "text/html" in request.accept_mimetypes:
                nxt = request.path
                return redirect(f"/login?next={nxt}")
            return jsonify({"error": "Unauthorized"}), 401

        # Optional: Additional check to ensure it's specifically a Google domain user
        user = session.get("user")
        if user and user.get("email") and not user["email"].endswith("@pivotwealth.com.au"):
            # Technically shouldn't happen if auth/callback protects it, but as a secondary guard
            session.clear()
            return redirect("/login")

        return view_func(*args, **kwargs)

    return wrapper


# Create main blueprint for routes (app factory pattern)
main_bp = Blueprint("main", __name__)


# Global before_request handler to protect all routes
# MUST be defined before blueprint is registered to app
@main_bp.before_request
def require_login():
    # List of endpoints that don't require authentication
    public_endpoints = [
        "main.login",
        "main.login_google",
        "main.login_bypass",
        "main.google_auth_callback",
        "main.logout",
        "static",  # Static files (CSS, JS, images)
    ]

    # List of routes that don't require authentication (by path)
    # These paths bypass session auth — secured by their own decorators
    public_paths = [
        "/_ah/warmup",  # Cloud Run warmup
        "/health",  # Lightweight health check
        "/post/allocate",  # Hubspot webhook
        "/sync/employees",  # Cloud Scheduler sync
        "/sync/leave_requests",  # Cloud Scheduler sync
        "/sync/calendar_closures",  # Cloud Scheduler sync
        "/sync/seed-tokens",  # One-time token migration
        "/sync/token-health",  # Token status check
        "/sync/calendar_watch_renew",  # Cloud Scheduler watch renewal
        "/webhooks/calendar",  # Google Calendar push notifications
        "/jobs/compute-simulated-clarifies",  # Cloud Scheduler job
        "/box/folder/create",  # HubSpot workflow — Box folder creation
        "/box/folder/tag",  # HubSpot workflow — Box metadata tagging
        "/box/folder/tag/auto",  # HubSpot workflow — Box auto-tagging
    ]

    # Check if current route is public
    if (
        request.endpoint in public_endpoints
        or request.path in public_paths
        or request.path.startswith("/static/")
    ):
        return  # Allow access

    # Require authentication for all other routes
    if not is_authenticated():
        if request.accept_mimetypes and "text/html" in request.accept_mimetypes:
            nxt = request.path
            return redirect(f"/login?next={nxt}")
        return jsonify({"error": "Unauthorized"}), 401


# ---- Employment Hero (HR) OAuth config ----
EH_AUTHORIZE_URL = os.environ.get(
    "EH_AUTHORIZE_URL", "https://oauth.employmenthero.com/oauth2/authorize"
)
EH_TOKEN_URL = os.environ.get("EH_TOKEN_URL", "https://oauth.employmenthero.com/oauth2/token")
EH_CLIENT_ID = get_secret("EH_CLIENT_ID")
EH_CLIENT_SECRET = get_secret("EH_CLIENT_SECRET")
# Your app's public callback URL, e.g. https://adviser-allocation-<PROJECT-NUM>.<REGION>.run.app/auth/callback
REDIRECT_URI = os.environ.get("REDIRECT_URI")

API_BASE = "https://api.employmenthero.com"

HUBSPOT_TOKEN = get_secret("HUBSPOT_TOKEN")
HUBSPOT_HEADERS = {"Authorization": f"Bearer {HUBSPOT_TOKEN}", "Content-Type": "application/json"}
DEFAULT_PORTAL_ID = os.getenv("HUBSPOT_PORTAL_ID", "47011873")


@lru_cache(maxsize=1)
def meeting_object_type_id() -> str:
    """Return HubSpot object type id for meetings (fallback to standard id)."""
    if not HUBSPOT_TOKEN:
        return "0-4"
    try:
        resp = requests.get(
            "https://api.hubapi.com/crm/v3/schemas/meetings",
            headers=HUBSPOT_HEADERS,
            timeout=8,
        )
        resp.raise_for_status()
        return resp.json().get("objectTypeId") or "0-4"
    except Exception as e:
        logger.warning("Failed to fetch meetings schema: %s", e)
        return "0-4"


def ensure_eh_config():
    """Ensure EH OAuth config is present for current environment.

    Works with Cloud Run (env variables + Secret Manager) and local .env.

    Raises:
        RuntimeError: If any required variable is missing.
    """
    missing = []
    if not EH_CLIENT_ID:
        missing.append("EH_CLIENT_ID")
    if not EH_CLIENT_SECRET:
        missing.append("EH_CLIENT_SECRET")
    if not REDIRECT_URI:
        missing.append("REDIRECT_URI")
    if missing:
        raise RuntimeError(
            "Missing required Employment Hero OAuth config: "
            + ", ".join(missing)
            + ". In production, set env vars via Cloud Run config or Secret Manager. "
            "Locally, set values in .env."
        )


# ---- Token management using CloudSQL ----
from adviser_allocation.services.oauth_service import (
    get_access_token,
    load_tokens,
    save_tokens,
    update_tokens,
)


# ---- OAuth flow ----
@main_bp.route("/")
def index():
    """Homepage with navigation to all available features."""
    return render_template(
        "homepage.html",
        today=sydney_today().isoformat(),
        week_num=f"{sydney_today().isocalendar()[1]:02d}",
        environment=os.environ.get("K_SERVICE", "development"),
        sydney_time=sydney_now().strftime("%Y-%m-%d %H:%M:%S %Z"),
        app_version=APP_VERSION,
    )


@main_bp.route("/workflows")
def workflows():
    """Show curated workflow documentation links."""
    return render_template(
        "workflows.html",
        today=sydney_today().isoformat(),
        app_version=APP_VERSION,
    )


@main_bp.route("/workflows/adviser-allocation")
def workflows_adviser_allocation():
    return render_template(
        "workflows_adviser_allocation.html",
        today=sydney_today().isoformat(),
        app_version=APP_VERSION,
    )


@main_bp.route("/docs/<path:filename>")
def serve_docs(filename: str):
    """Serve workflow documentation assets."""
    return send_from_directory("docs", filename)


@main_bp.route("/auth/start")
def auth_start():
    """Initiate Employment Hero OAuth and redirect to the authorize URL."""
    ensure_eh_config()
    # Create CSRF state & a simple user_key
    state = secrets.token_urlsafe(24)
    session["oauth_state"] = state
    session.setdefault("user_key", secrets.token_hex(8))

    params = {
        "client_id": EH_CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "state": state,
    }
    authorize_url = f"{EH_AUTHORIZE_URL}?{urlencode(params)}"
    logger.info("Redirecting to Employment Hero authorize URL: %s", authorize_url)
    return redirect(authorize_url)


@main_bp.route("/auth/callback")
def auth_callback():
    """Handle OAuth callback and exchange authorization code for tokens.

    Returns:
        flask.Response: JSON payload indicating success or error.
    """
    ensure_eh_config()
    # Validate state
    state = request.args.get("state")
    if not state or state != session.get("oauth_state"):
        return jsonify({"ok": False, "error": "state_mismatch"}), 400

    code = request.args.get("code")
    if not code:
        return jsonify({"ok": False, "error": "missing_code"}), 400

    # Exchange code → tokens (form-encoded)
    data = {
        "client_id": EH_CLIENT_ID,
        "client_secret": EH_CLIENT_SECRET,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
    }
    resp = requests.post(EH_TOKEN_URL, data=data, timeout=30)
    if resp.status_code != 200:
        return jsonify({"ok": False, "error": "token_exchange_failed", "details": resp.text}), 400

    tokens = resp.json()
    save_tokens(tokens)
    return jsonify({"ok": True, "message": "Employment Hero connected. Tokens saved."})


def get_org_id(headers):
    """Fetch the first organisation ID from Employment Hero.

    Args:
        headers (dict): HTTP headers including Authorization bearer token.

    Returns:
        str: Organisation ID.
    """
    r = requests.get(f"{API_BASE}/api/v1/organisations", headers=headers, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"Refresh failed: {r.status_code} {r.text}")
    return r.json()["data"]["items"][0]["id"]


@main_bp.route("/get/employees")
def get_employees():
    """Fetch employees for the organisation and persist to CloudSQL.

    Returns:
        tuple: (list of employee dicts, HTTP status, headers)
    """
    access_token = get_access_token()
    headers = {"Authorization": f"Bearer {access_token}"}
    params = {
        "item_per_page": 100,
    }

    org_id = get_org_id(headers)
    r_emps = requests.get(
        f"{API_BASE}/api/v1/organisations/{org_id}/employees",
        headers=headers,
        params=params,
        timeout=30,
    )
    if r_emps.status_code != 200:
        raise RuntimeError(f"Refresh failed: {r_emps.status_code} {r_emps.text}")

    employees = []
    cloudsql_db = get_cloudsql_db()

    for emp in r_emps.json()["data"]["items"]:
        item = {
            "id": emp.get("id"),
            "name": emp.get("full_name"),  # Using 'full_name' for 'name'
            "company_email": emp.get("company_email"),
            "account_email": emp.get("account_email"),
        }
        cloudsql_db.upsert_employee_dict(item)
        employees.append(item)

    return (employees, r_emps.status_code, {"Content-Type": "application/json"})


@main_bp.route("/get/employee_id")
def get_employee_id():
    """HTTP endpoint to retrieve an employee ID by email.

    Query Param:
        email (str): The employee's company email.

    Returns:
        tuple: JSON payload and HTTP status code.
    """
    search_email = request.args.get("email")

    if not search_email:
        return {"error": "Email parameter is missing"}, 400

    cloudsql_db = get_cloudsql_db()
    employee_id = cloudsql_db.get_employee_id_by_email(search_email)

    if employee_id:
        return {"employee_id": employee_id}, 200
    else:
        return {"error": "Employee not found"}, 404


@main_bp.route("/get/leave_requests")
def get_leave_requests():
    """Fetch future approved leave requests and persist to CloudSQL.

    Returns:
        tuple: (list of leave requests, HTTP status, headers)
    """
    access_token = get_access_token()
    headers = {"Authorization": f"Bearer {access_token}"}

    now_date = sydney_today()

    page = 1
    total_pages = 9

    org_id = get_org_id(headers)
    cloudsql_db = get_cloudsql_db()

    leave_requests = []
    while page <= total_pages:
        params = {
            "item_per_page": 100,
            "page_index": page,
        }
        e = requests.get(
            f"{API_BASE}/api/v1/organisations/{org_id}/leave_requests",
            headers=headers,
            params=params,
            timeout=30,
        )
        if e.status_code != 200:
            raise RuntimeError(f"Refresh failed: {e.status_code} {e.text}")

        for leave_request in e.json()["data"]["items"]:
            start_date_obj = datetime.fromisoformat(leave_request["start_date"]).date()
            if (leave_request["status"] == "Approved") and (start_date_obj > now_date):
                item = {
                    "leave_request_id": leave_request.get("id"),
                    "employee_id": leave_request.get("employee_id"),
                    "start_date": leave_request.get("start_date"),
                    "end_date": leave_request.get("end_date"),
                    "status": "approved",
                }
                leave_requests.append(item)
                cloudsql_db.upsert_leave_request_dict(item)

        page += 1
        total_pages = e.json()["data"]["total_pages"]

    # Remove stale leave records (cancelled/moved in EH but still in CloudSQL)
    synced_ids = [lr["leave_request_id"] for lr in leave_requests]
    deleted = cloudsql_db.delete_stale_future_leave(synced_ids, now_date)
    if deleted:
        logger.info("Deleted %d stale future leave records", deleted)

    return (leave_requests, e.status_code, {"Content-Type": "application/json"})


@main_bp.route("/get/employee_leave_requests")
def get_employee_leave_requests():
    """HTTP endpoint to list leave requests by employee ID from CloudSQL.

    Query Param:
        employee_id (str): The employee document ID.

    Returns:
        tuple: JSON payload and HTTP status code.
    """
    employee_id = request.args.get("employee_id")

    if not employee_id:
        return {"error": "Employee ID parameter is missing"}, 400

    cloudsql_db = get_cloudsql_db()
    leaves = cloudsql_db.get_employee_leaves_as_dicts(employee_id)

    # Return the list with a 200 OK status, even if it's empty
    return {"leave_requests": leaves}, 200


@main_bp.route("/get/leave_requests_by_email")
def get_leave_requests_by_email():
    """HTTP endpoint to list leave requests by employee email.

    Uses existing CloudSQL data; does not trigger sync work.
    """
    email = request.args.get("email")
    if not email:
        return {"error": "Email parameter is missing"}, 400

    cloudsql_db = get_cloudsql_db()

    # Look up in existing data only; populate via /sync endpoints or a scheduler
    employee_id = cloudsql_db.get_employee_id_by_email(email)
    if not employee_id:
        return {"error": "Employee not found in store. Run /sync/employees."}, 404

    employee_leaves = cloudsql_db.get_employee_leaves_as_dicts(employee_id)
    return {"leave_requests": employee_leaves}, 200


# Lightweight sync endpoints to be triggered by a scheduler
@main_bp.route("/sync/employees", methods=["POST", "GET"])
@require_oidc_token
def sync_employees():
    """Trigger an on-demand employee sync (suitable for schedulers)."""
    try:
        data, status, headers = get_employees()
        # Backfill company_email from HubSpot owners for employees missing it
        backfilled = 0
        try:
            cloudsql_db = get_cloudsql_db()
            backfilled = cloudsql_db.backfill_company_emails_from_hubspot()
            if backfilled:
                logger.info("Backfilled company_email for %d employees from HubSpot", backfilled)
        except Exception as exc:
            logger.warning("Failed to backfill company emails: %s", exc)
        return jsonify({"synced": len(data), "emails_backfilled": backfilled}), status
    except Exception as e:
        logger.error("Failed to sync employees: %s", e)
        return jsonify({"error": "Internal server error"}), 500


@main_bp.route("/sync/leave_requests", methods=["POST", "GET"])
@require_oidc_token
def sync_leave_requests():
    """Trigger an on-demand leave requests sync (suitable for schedulers)."""
    try:
        data, status, headers = get_leave_requests()
        return jsonify({"synced": len(data)}), status
    except Exception as e:
        logger.error("Failed to sync leave requests: %s", e)
        return jsonify({"error": "Internal server error"}), 500


@main_bp.route("/admin/sync/employees", methods=["POST"])
@admin_required
def admin_sync_employees():
    """Admin-triggered employee sync from Employment Hero."""
    try:
        data, status, _headers = get_employees()
        return jsonify({"synced": len(data)}), status
    except Exception:
        logger.exception("Admin sync employees failed")
        return jsonify({"error": "Sync failed"}), 500


@main_bp.route("/admin/sync/leave_requests", methods=["POST"])
@admin_required
def admin_sync_leave_requests():
    """Admin-triggered leave request sync from Employment Hero."""
    try:
        data, status, _headers = get_leave_requests()
        return jsonify({"synced": len(data)}), status
    except Exception:
        logger.exception("Admin sync leave requests failed")
        return jsonify({"error": "Sync failed"}), 500


@main_bp.route("/sync/seed-tokens", methods=["POST"])
@require_oidc_token
def sync_seed_tokens():
    """Seed EH OAuth tokens into CloudSQL (one-time migration from Firestore).

    Accepts a JSON body with refresh_token. Exchanges for fresh access token
    and saves to CloudSQL.
    """
    try:
        body = request.get_json(force=True)
        refresh_token = body.get("refresh_token")
        if not refresh_token:
            return jsonify({"error": "refresh_token required"}), 400

        ensure_eh_config()
        # Exchange refresh token for a fresh access token
        data = {
            "client_id": EH_CLIENT_ID,
            "client_secret": EH_CLIENT_SECRET,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }
        r = requests.post(EH_TOKEN_URL, data=data, timeout=30)
        if r.status_code != 200:
            return jsonify({"error": "refresh_failed", "details": r.text}), 400

        tokens = r.json()
        if "refresh_token" not in tokens:
            tokens["refresh_token"] = refresh_token
        save_tokens(tokens)
        logger.info("EH tokens seeded into CloudSQL via /sync/seed-tokens")
        return jsonify({"ok": True, "message": "Tokens seeded into CloudSQL"}), 200
    except Exception as e:
        logger.error("Failed to seed tokens: %s", e)
        return jsonify({"error": "Internal server error"}), 500


@main_bp.route("/sync/token-health", methods=["GET"])
@require_oidc_token
def sync_token_health():
    """Return EH OAuth token status without triggering a refresh."""
    tok = load_tokens()
    if not tok:
        return jsonify({"status": "missing"}), 404
    expires_at = tok.get("_expires_at", 0)
    remaining = expires_at - time.time()
    return jsonify(
        {
            "status": "valid" if remaining > 0 else "expired",
            "expires_in_seconds": int(remaining),
            "has_refresh_token": bool(tok.get("refresh_token")),
        }
    )


@main_bp.route("/sync/calendar_closures", methods=["POST", "GET"])
@require_oidc_token
def sync_calendar_closures():
    """Sync office closures from Google Calendar (suitable for schedulers).

    Reads events from the Pivot closures calendar and optionally the
    Australian public holidays calendar, then upserts into aa_office_closures.
    """
    try:
        from adviser_allocation.services.calendar_sync_service import (
            get_calendar_sources,
        )
        from adviser_allocation.services.calendar_sync_service import (
            sync_calendar_closures as _sync,
        )

        sources = get_calendar_sources()
        if not sources:
            return jsonify({"error": "GOOGLE_CALENDAR_ID not configured"}), 500

        cloudsql_db = get_cloudsql_db()
        result = _sync(calendar_sources=sources, db=cloudsql_db)
        return jsonify(result), 200
    except Exception as e:
        logger.error("Failed to sync calendar closures: %s", e, exc_info=True)
        return jsonify({"error": "Internal server error"}), 500


@main_bp.route("/sync/calendar_watch_renew", methods=["POST", "GET"])
@require_oidc_token
def renew_calendar_watches():
    """Renew Google Calendar push notification channels approaching expiry.

    Intended to run daily via Cloud Scheduler. Also registers watches
    for any calendar not yet being watched.
    """
    try:
        from adviser_allocation.services.calendar_sync_service import (
            get_calendar_sources,
        )
        from adviser_allocation.services.calendar_watch_service import (
            renew_expiring_watches,
        )

        sources = get_calendar_sources()
        if not sources:
            return jsonify({"error": "GOOGLE_CALENDAR_ID not configured"}), 500

        result = renew_expiring_watches(calendar_sources=sources)
        return jsonify(result), 200
    except Exception as exc:
        logger.error("Failed to renew calendar watches: %s", exc, exc_info=True)
        return jsonify({"error": "Internal server error"}), 500


@main_bp.route("/jobs/compute-simulated-clarifies", methods=["POST", "GET"])
@require_oidc_token
def job_compute_simulated_clarifies():
    """Compute simulated clarifies using capacity algorithm.

    This job places deals without Clarify meetings into capacity-respecting weeks.
    Run daily via Cloud Scheduler or manually to update the clarify chart data.
    """
    try:
        from adviser_allocation.jobs.compute_simulated_clarifies import run_computation

        advisers_processed, deals_assigned = run_computation()
        return jsonify(
            {
                "ok": True,
                "advisers_processed": advisers_processed,
                "deals_assigned": deals_assigned,
                "timestamp": sydney_now().isoformat(),
            }
        ), 200
    except Exception as e:
        logger.error("Failed to compute simulated clarifies: %s", e, exc_info=True)
        return jsonify({"error": "Internal server error"}), 500


# ---- Global Closures (Holidays) ----
@main_bp.route("/closures", methods=["GET", "POST"])
@admin_required
def closures():
    """Manage global office closures/holidays.

    GET: List closures from CloudSQL.
    POST: Add a closure. JSON body: { start_date: YYYY-MM-DD, end_date?: YYYY-MM-DD, description?: str, tags?: [str] | str }
    """
    cloudsql_db = get_cloudsql_db()

    if request.method == "GET":
        try:
            items = cloudsql_db.get_global_closures()
            return jsonify({"count": len(items), "closures": items}), 200
        except Exception as e:
            logger.error("Failed to list closures: %s", e)
            return jsonify({"error": "Internal server error"}), 500

    # POST (create) requires admin
    try:
        if not request.is_json:
            return jsonify({"error": "Expected application/json"}), 415
        if not is_authenticated():
            return jsonify({"error": "Unauthorized"}), 401
        payload = request.get_json() or {}
        start_date = payload.get("start_date")
        end_date = payload.get("end_date") or start_date
        # Accept legacy 'reason' but persist as 'description'
        description = payload.get("description")
        if description is None:
            description = payload.get("reason") or ""
        # Normalize tags: accept array or comma-separated string
        raw_tags = payload.get("tags")
        tags = []
        if isinstance(raw_tags, list):
            tags = [str(t).strip() for t in raw_tags if str(t).strip()]
        elif isinstance(raw_tags, str):
            tags = [t.strip() for t in raw_tags.split(",") if t.strip()]
        if not start_date:
            return jsonify({"error": "start_date is required (YYYY-MM-DD)"}), 400
        # Basic format sanity (YYYY-MM-DD)
        try:
            start_date_obj = datetime.strptime(start_date, "%Y-%m-%d").date()
            end_date_obj = datetime.strptime(end_date, "%Y-%m-%d").date()
        except Exception:
            return jsonify({"error": "Invalid date format; use YYYY-MM-DD"}), 400

        closure_id = cloudsql_db.insert_office_closure(
            start_date=start_date_obj,
            end_date=end_date_obj,
            description=description,
            tags=tags,
        )
        return (
            jsonify(
                {
                    "id": closure_id,
                    "start_date": start_date,
                    "end_date": end_date,
                    "description": description,
                    "tags": tags,
                }
            ),
            201,
        )
    except Exception as e:
        logger.error("Failed to create closure: %s", e)
        return jsonify({"error": "Internal server error"}), 500


@main_bp.route("/closures/<closure_id>", methods=["PUT", "DELETE"])
@admin_required
def closures_item(closure_id):
    """Update or delete a specific closure document (admin only)."""
    if not is_authenticated():
        return jsonify({"error": "Unauthorized"}), 401

    cloudsql_db = get_cloudsql_db()

    if request.method == "DELETE":
        try:
            cloudsql_db.delete_office_closure(closure_id)
            return jsonify({"ok": True}), 200
        except Exception as e:
            logger.error("Failed to delete closure %s: %s", closure_id, e)
            return jsonify({"error": "Internal server error"}), 500

    # PUT
    try:
        if not request.is_json:
            return jsonify({"error": "Expected application/json"}), 415
        payload = request.get_json() or {}
        start_date = payload.get("start_date")
        end_date = payload.get("end_date") or start_date
        # Accept both, prefer 'description'
        description = payload.get("description")
        if description is None and "reason" in payload:
            description = payload.get("reason")
        # Normalize tags if provided
        tags = None
        if "tags" in payload:
            raw_tags = payload.get("tags")
            if isinstance(raw_tags, list):
                tags = [str(t).strip() for t in raw_tags if str(t).strip()]
            elif isinstance(raw_tags, str):
                tags = [t.strip() for t in raw_tags.split(",") if t.strip()]
        if not start_date:
            return jsonify({"error": "start_date is required (YYYY-MM-DD)"}), 400
        try:
            start_date_obj = datetime.strptime(start_date, "%Y-%m-%d").date()
            end_date_obj = datetime.strptime(end_date, "%Y-%m-%d").date()
        except Exception:
            return jsonify({"error": "Invalid date format; use YYYY-MM-DD"}), 400

        cloudsql_db.update_office_closure(
            closure_id=closure_id,
            start_date=start_date_obj,
            end_date=end_date_obj,
            description=description,
            tags=tags,
        )
        resp = {"id": closure_id, "start_date": start_date, "end_date": end_date}
        if description is not None:
            resp["description"] = description
        if tags is not None:
            resp["tags"] = tags
        return jsonify(resp), 200
    except Exception as e:
        logger.error("Failed to update closure %s: %s", closure_id, e)
        return jsonify({"error": "Internal server error"}), 500


@main_bp.route("/capacity_overrides", methods=["GET", "POST"])
@admin_required
def capacity_overrides():
    """Manage adviser capacity overrides stored in CloudSQL."""
    cloudsql_db = get_cloudsql_db()

    if request.method == "GET":
        try:
            items = cloudsql_db.get_capacity_overrides()
            items.sort(
                key=lambda item: (item.get("adviser_email") or "", item.get("effective_date") or "")
            )
            return jsonify({"count": len(items), "overrides": items}), 200
        except Exception as exc:
            logger.error("Failed to load capacity overrides: %s", exc)
            return jsonify({"error": "Internal server error"}), 500

    if not is_authenticated():
        return jsonify({"error": "Unauthorized"}), 401

    if not request.is_json:
        return jsonify({"error": "Expected application/json"}), 415

    payload = request.get_json() or {}
    adviser_email = (payload.get("adviser_email") or "").strip().lower()
    effective_date = (payload.get("effective_date") or "").strip()
    pod_type = (payload.get("pod_type") or "").strip()
    notes = (payload.get("notes") or "").strip()

    if not adviser_email:
        return jsonify({"error": "adviser_email is required"}), 400
    if not effective_date:
        return jsonify({"error": "effective_date is required (YYYY-MM-DD)"}), 400
    try:
        effective_date_obj = datetime.strptime(effective_date, "%Y-%m-%d").date()
    except Exception:
        return jsonify({"error": "Invalid effective_date; use YYYY-MM-DD"}), 400

    limit_value = payload.get("client_limit_monthly")
    try:
        client_limit_monthly = int(limit_value)
    except (TypeError, ValueError):
        return jsonify({"error": "client_limit_monthly must be an integer"}), 400
    if client_limit_monthly <= 0:
        return jsonify({"error": "client_limit_monthly must be positive"}), 400

    try:
        override_id = cloudsql_db.insert_capacity_override(
            adviser_email=adviser_email,
            effective_date=effective_date_obj,
            client_limit_monthly=client_limit_monthly,
            pod_type=pod_type,
            notes=notes,
        )
        refresh_capacity_override_cache()
        doc = {
            "id": override_id,
            "adviser_email": adviser_email,
            "effective_date": effective_date,
            "client_limit_monthly": client_limit_monthly,
            "pod_type": pod_type,
            "notes": notes,
            "created_at": sydney_now().isoformat(),
        }
        return jsonify(doc), 201
    except Exception as exc:
        logger.error("Failed to create capacity override: %s", exc)
        return jsonify({"error": "Internal server error"}), 500


@main_bp.route("/capacity_overrides/<override_id>", methods=["PUT", "DELETE"])
@admin_required
def capacity_overrides_item(override_id: str):
    """Update or delete a specific adviser capacity override document."""
    if not is_authenticated():
        return jsonify({"error": "Unauthorized"}), 401

    cloudsql_db = get_cloudsql_db()

    if request.method == "DELETE":
        try:
            cloudsql_db.delete_capacity_override(override_id)
            refresh_capacity_override_cache()
            return jsonify({"ok": True}), 200
        except Exception as exc:
            logger.error("Failed to delete capacity override %s: %s", override_id, exc)
            return jsonify({"error": "Internal server error"}), 500

    if not request.is_json:
        return jsonify({"error": "Expected application/json"}), 415

    payload = request.get_json() or {}
    update_params = {}

    if "adviser_email" in payload:
        email = (payload.get("adviser_email") or "").strip().lower()
        if not email:
            return jsonify({"error": "adviser_email cannot be blank"}), 400
        update_params["adviser_email"] = email

    if "effective_date" in payload:
        effective_date = (payload.get("effective_date") or "").strip()
        if not effective_date:
            return jsonify({"error": "effective_date cannot be blank"}), 400
        try:
            update_params["effective_date"] = datetime.strptime(effective_date, "%Y-%m-%d").date()
        except Exception:
            return jsonify({"error": "Invalid effective_date; use YYYY-MM-DD"}), 400

    if "client_limit_monthly" in payload:
        try:
            limit_value = int(payload.get("client_limit_monthly"))
        except (TypeError, ValueError):
            return jsonify({"error": "client_limit_monthly must be an integer"}), 400
        if limit_value <= 0:
            return jsonify({"error": "client_limit_monthly must be positive"}), 400
        update_params["client_limit_monthly"] = limit_value

    if "pod_type" in payload:
        update_params["pod_type"] = (payload.get("pod_type") or "").strip()

    if "notes" in payload:
        update_params["notes"] = (payload.get("notes") or "").strip()

    if not update_params:
        return jsonify({"error": "No valid fields to update"}), 400

    try:
        cloudsql_db.update_capacity_override(override_id=override_id, **update_params)
        refresh_capacity_override_cache()
        response = {"id": override_id, "updated_at": sydney_now().isoformat()}
        # Convert date back to string for response
        for key, value in update_params.items():
            if hasattr(value, "isoformat"):
                response[key] = value.isoformat()
            else:
                response[key] = value
        return jsonify(response), 200
    except Exception as exc:
        logger.error("Failed to update capacity override %s: %s", override_id, exc)
        return jsonify({"error": "Internal server error"}), 500


@main_bp.route("/capacity_overrides/ui")
@admin_required
def capacity_overrides_ui():
    """UI for managing adviser capacity overrides."""
    cloudsql_db = get_cloudsql_db()

    overrides = []
    try:
        overrides = cloudsql_db.get_capacity_overrides()
    except Exception as exc:
        logger.warning("Failed to load capacity overrides for UI: %s", exc)

    # Enrich overrides for display (sort by effective date within adviser)
    normalized = []
    for idx, item in enumerate(
        sorted(
            overrides, key=lambda o: (o.get("adviser_email") or "", o.get("effective_date") or "")
        ),
        start=1,
    ):
        normalized.append(
            {
                "idx": idx,
                "id": item.get("id"),
                "adviser_email": item.get("adviser_email", ""),
                "effective_date": item.get("effective_date", ""),
                "client_limit_monthly": item.get("client_limit_monthly"),
                "pod_type": item.get("pod_type", ""),
                "notes": item.get("notes", ""),
            }
        )

    pod_type_options = [
        "Standard Pod",
        "Solo Adviser",
        "Team Pod",
    ]

    adviser_options = []
    try:
        adviser_users = get_user_ids_adviser()
        adviser_map = {}
        for user in adviser_users:
            props = user.get("properties") or {}
            email = (props.get("hs_email") or "").strip()
            if not email:
                continue
            taking_raw = props.get("taking_on_clients")
            if taking_raw is None:
                continue
            taking_normalized = str(taking_raw).strip().lower()
            if taking_normalized not in {"true", "false"}:
                continue
            first = (props.get("firstname") or props.get("first_name") or "").strip()
            last = (props.get("lastname") or props.get("last_name") or "").strip()
            name_parts = [part.title() for part in [first, last] if part]
            display_name = " ".join(name_parts)
            label = f"{display_name} ({email})" if display_name else email
            adviser_map[email] = label
        adviser_options = [
            {"email": email, "label": label}
            for email, label in sorted(adviser_map.items(), key=lambda item: item[1].lower())
        ]
    except Exception as exc:  # pragma: no cover - optional data
        logger.warning("Failed to load adviser list for overrides UI: %s", exc)

    return render_template(
        "capacity_overrides.html",
        overrides=normalized,
        today=sydney_today().isoformat(),
        pod_type_options=pod_type_options,
        adviser_options=adviser_options,
    )


@main_bp.route("/closures/ui")
@admin_required
def closures_ui():
    """Simple UI to create and list global office closures (holidays)."""
    cloudsql_db = get_cloudsql_db()

    # Preload closures for display
    closures = []
    try:
        closures = cloudsql_db.get_global_closures()
        # Sort by start_date
        closures.sort(key=lambda c: c.get("start_date") or "")
    except Exception as e:
        logger.warning("Failed to load closures for UI: %s", e)

    # Helper to count business days (Mon-Fri) inclusive
    def workdays_count(s: str, e: str) -> int:
        try:
            sd = datetime.strptime(s, "%Y-%m-%d").date()
            ed = datetime.strptime(e or s, "%Y-%m-%d").date()
        except Exception:
            return 0
        if ed < sd:
            sd, ed = ed, sd
        days = 0
        cur = sd
        while cur <= ed:
            if cur.weekday() < 5:  # Mon-Fri
                days += 1
            cur = date.fromordinal(cur.toordinal() + 1)
        return days

    # Render via Jinja template with static assets (stable UI)
    def normalize_tags(v):
        if isinstance(v, list):
            return [str(t).strip() for t in v if str(t).strip()]
        if isinstance(v, str):
            return [t.strip() for t in v.split(",") if t.strip()]
        return []

    closures_data = []
    for idx, c in enumerate(closures, start=1):
        start_val = c.get("start_date", "")
        end_val = c.get("end_date", "") or start_val
        closures_data.append(
            {
                "idx": idx,
                "id": c.get("id"),
                "start_date": start_val,
                "end_date": end_val,
                "description": (c.get("description") or c.get("reason") or ""),
                "tags": normalize_tags(c.get("tags")),
                "workdays": workdays_count(start_val, end_val),
            }
        )

    # Build color map for tags (same cycle as availability pages)
    color_cycle = ["blue", "green", "purple", "orange", "pink", "teal"]
    all_tags = []
    for c in closures_data:
        all_tags.extend(c.get("tags") or [])
    tag_color_map = {}
    for i, t in enumerate(list(dict.fromkeys(all_tags))):
        tag_color_map[t] = color_cycle[i % len(color_cycle)]

    # Attach colored tag items for template and build mini-calendar payload
    closures_for_js = []
    for c in closures_data:
        tags = c.get("tags") or []
        c["tag_items"] = [{"name": t, "cls": tag_color_map.get(t, color_cycle[0])} for t in tags]
        color = tag_color_map.get(tags[0], "blue") if tags else "blue"
        closures_for_js.append(
            {"start_date": c["start_date"], "end_date": c["end_date"], "color": color}
        )

    return render_template(
        "closures_ui.html",
        closures=closures_data,
        today=sydney_today().isoformat(),
        closures_for_js=closures_for_js,
    )


@main_bp.route("/login")
def login():
    """Site-wide login page (shows the login UI)."""
    nxt = _safe_redirect_url(request.args.get("next") or "/")
    if is_authenticated():
        return redirect(nxt)

    # Store 'next' in session for safety during OAuth redirect
    session["next"] = nxt
    return render_template("login.html")


@main_bp.route("/login/google")
def login_google():
    """Initiates Google OAuth flow when the user clicks 'Sign in with Google'."""
    if is_authenticated():
        return redirect("/")

    # Redirect to Google OAuth authorization endpoint
    redirect_uri = url_for("main.google_auth_callback", _external=True)
    return current_app.oauth.google.authorize_redirect(redirect_uri)


@main_bp.route("/auth")
def google_auth_callback():
    """Callback route that handles the response from Google Identity."""
    try:
        token = current_app.oauth.google.authorize_access_token()
        user_info = token.get("userinfo")

        if not user_info:
            return "Failed to fetch user information from Google.", 400

        # 1. Enforce Domain Restriction
        email = user_info.get("email", "")
        if not email.endswith("@pivotwealth.com.au"):
            return "Unauthorized domain. You must use a @pivotwealth.com.au account.", 403

        # 2. Grant Access
        session["is_authenticated"] = True
        session["user"] = {
            "name": user_info.get("name"),
            "email": email,
            "picture": user_info.get("picture"),
        }

    except Exception as e:
        logger.error("Google OAuth Error: %s", e)
        return "Authentication failed. Please try again.", 400

    # Retrieve where they were trying to go
    nxt = _safe_redirect_url(session.pop("next", "/"))
    return redirect(nxt)


@main_bp.route("/login_bypass", methods=["POST"])
def login_bypass():
    """Bypass route to simulate a logged-in Pivot Wealth user (local dev only)."""
    if not current_app.debug and not os.environ.get("DEV_LOGIN_ENABLED"):
        logger.warning("login_bypass attempted in non-debug mode from %s", request.remote_addr)
        return jsonify({"error": "Not found"}), 404

    session["is_authenticated"] = True
    session["user"] = {
        "name": "Dev User (Bypass)",
        "email": "noel.pinton@pivotwealth.com.au",
        "picture": "https://ui-avatars.com/api/?name=Dev+User&background=F08354&color=fff",
    }
    nxt = _safe_redirect_url(request.args.get("next") or "/")
    return redirect(nxt)


# ---- Chat notification helper ----
def _format_display_name(email: str) -> str:
    local = (email or "").split("@")[0]
    parts = re.split(r"[._-]+", local)
    return " ".join(part.capitalize() for part in parts if part) or (email or "")


def _format_tag_list(raw) -> list[str]:
    """Format a tag list from various input types (str, list, etc.)."""
    if isinstance(raw, list):
        parts = raw
    else:
        parts = [p.strip() for p in re.split(r"[;,/|]+", str(raw or "")) if p.strip()]
    formatted = []
    for part in parts:
        part_str = str(part).strip()
        if part_str:
            formatted.append(part_str.upper() if part_str.upper() == "IPO" else part_str.title())
    return formatted


@main_bp.route("/logout")
def logout():
    session.pop("is_authenticated", None)
    return redirect("/login")


@main_bp.route("/employees/ui")
@admin_required
def employees_ui():
    """UI to view employees from CloudSQL with consistent styling."""
    try:
        cloudsql_db = get_cloudsql_db()

        # Fetch employees from CloudSQL
        employees = cloudsql_db.get_all_employees(active_only=False)

        # Sort employees by name
        employees.sort(key=lambda x: (x.get("name") or "").lower())

        return render_template(
            "employees_ui.html",
            employees=employees,
            today=sydney_today().isoformat(),
            week_num=f"{sydney_today().isocalendar()[1]:02d}",
            total_count=len(employees),
        )

    except Exception as e:
        logger.exception("Error loading employees UI")
        return render_template(
            "error.html", code=500, message="Error loading employees. Please try again."
        ), 500


@main_bp.route("/leave_requests/ui")
@admin_required
def leave_requests_ui():
    """UI to view leave requests from CloudSQL with filtering and calendar view."""
    try:
        cloudsql_db = get_cloudsql_db()

        # Get filter parameters
        selected_employee = request.args.get("employee", "")
        status_filter = request.args.get("status", "")

        # Fetch employees first for dropdown
        all_employees = cloudsql_db.get_all_employees(active_only=False)
        employees = []
        employees_dict = {}
        for emp in all_employees:
            emp_id = emp.get("employee_id")
            emp_name = emp.get("name", "Unknown")
            emp_email = emp.get("company_email", emp.get("account_email", ""))

            employees.append({"id": emp_id, "name": emp_name, "email": emp_email})
            employees_dict[emp_id] = {"name": emp_name, "email": emp_email}

        employees.sort(key=lambda x: x["name"].lower())

        # Fetch leave requests (with optional filtering)
        leave_requests = []
        calendar_events = []

        if selected_employee:
            # Fetch only for selected employee (faster)
            emp_data = employees_dict.get(selected_employee, {"name": "Unknown", "email": ""})
            leaves = cloudsql_db.get_employee_leaves_as_dicts(selected_employee)

            for leave_data in leaves:
                leave_data["employee_name"] = emp_data["name"]
                leave_data["employee_email"] = emp_data["email"]
                leave_data["doc_id"] = leave_data.get("leave_request_id")

                # Apply status filter
                if (
                    not status_filter
                    or leave_data.get("status", "").lower() == status_filter.lower()
                ):
                    leave_requests.append(leave_data)

                # Add to calendar events if approved
                if leave_data.get("status", "").lower() == "approved":
                    calendar_events.append(
                        {
                            "title": f"{emp_data['name']} - {leave_data.get('leave_type', 'Leave')}",
                            "start": leave_data.get("start_date", ""),
                            "end": leave_data.get("end_date", ""),
                            "employee": emp_data["name"],
                            "type": leave_data.get("leave_type", "Leave"),
                        }
                    )
        else:
            # Fetch all leave requests in a single query
            all_leaves = cloudsql_db.get_all_leaves_as_dicts()

            for leave_data in all_leaves:
                emp_data = employees_dict.get(
                    leave_data.get("employee_id"), {"name": "Unknown", "email": ""}
                )
                leave_data["employee_name"] = emp_data["name"]
                leave_data["employee_email"] = emp_data["email"]
                leave_data["doc_id"] = leave_data.get("leave_request_id")

                # Apply status filter
                if (
                    not status_filter
                    or leave_data.get("status", "").lower() == status_filter.lower()
                ):
                    leave_requests.append(leave_data)

                # Add to calendar events if approved
                if leave_data.get("status", "").lower() == "approved":
                    calendar_events.append(
                        {
                            "title": f"{emp_data['name']} - {leave_data.get('leave_type', 'Leave')}",
                            "start": leave_data.get("start_date", ""),
                            "end": leave_data.get("end_date", ""),
                            "employee": emp_data["name"],
                            "type": leave_data.get("leave_type", "Leave"),
                        }
                    )

        # Sort by start date (most recent first)
        leave_requests.sort(key=lambda x: x.get("start_date", ""), reverse=True)

        return render_template(
            "leave_requests_ui.html",
            leave_requests=leave_requests,
            employees=employees,
            calendar_events=calendar_events,
            selected_employee=selected_employee,
            status_filter=status_filter,
            today=sydney_today().isoformat(),
            week_num=f"{sydney_today().isocalendar()[1]:02d}",
            total_count=len(leave_requests),
        )

    except Exception as e:
        logger.exception("Error loading leave requests UI")
        return render_template(
            "error.html", code=500, message="Error loading leave requests. Please try again."
        ), 500


@main_bp.route("/webhook/allocation", methods=["POST"])
@require_api_key
def allocation_webhook():
    """Webhook endpoint to receive and store allocation requests."""
    try:
        # Get request data
        data = request.get_json()
        if not data:
            return jsonify({"error": "No JSON data provided"}), 400

        extra = {
            "ip_address": request.remote_addr,
            "user_agent": request.headers.get("User-Agent", ""),
        }
        doc_id = store_allocation_record(
            None, data, source="webhook", raw_request=data, extra_fields=extra
        )

        if doc_id:
            return (
                jsonify({"success": True, "id": doc_id, "timestamp": sydney_now().isoformat()}),
                201,
            )
        else:
            return jsonify({"error": "Failed to store allocation data"}), 500

    except Exception as e:
        logger.error("Failed to store webhook allocation: %s", e)
        return jsonify({"error": "Internal server error"}), 500


@main_bp.route("/allocations/history")
@login_required
def allocation_history_ui():
    """Dashboard view of allocation history with pagination."""
    try:
        cloudsql_db = get_cloudsql_db()

        status_filter = request.args.get("status", "")
        deal_filter = request.args.get("deal", "")
        adviser_filter = request.args.get("adviser", "")
        try:
            days_filter = int(request.args.get("days", 30) or 30)
        except (ValueError, TypeError):
            days_filter = 30
        try:
            page = max(1, int(request.args.get("page", 1) or 1))
        except (ValueError, TypeError):
            page = 1
        try:
            page_size = int(request.args.get("page_size", 25) or 25)
        except ValueError:
            page_size = 25
        page_size = max(5, min(page_size, 200))

        cutoff_date = sydney_now() - timedelta(days=days_filter)

        # Fetch allocation history from CloudSQL
        raw_allocations = cloudsql_db.get_allocation_history(
            deal_id=deal_filter if deal_filter else None,
            adviser_email=adviser_filter if adviser_filter else None,
            limit=1000,  # Get more than needed for filtering
        )
        allocations: list[dict] = []

        for row in raw_allocations:
            ts_value = row.get("timestamp")
            if ts_value:
                try:
                    if isinstance(ts_value, str):
                        parsed_ts = datetime.fromisoformat(ts_value.replace("Z", "+00:00"))
                    else:
                        parsed_ts = ts_value
                    if parsed_ts.tzinfo is None:
                        parsed_ts = parsed_ts.replace(tzinfo=SYDNEY_TZ)
                    if parsed_ts < cutoff_date:
                        continue
                except Exception:
                    pass

            if status_filter and (row.get("status") or "").lower() != status_filter.lower():
                continue

            allocations.append(row)

        total_count = len(allocations)
        total_pages = max(1, (total_count + page_size - 1) // page_size)
        page = min(page, total_pages)
        start_idx = (page - 1) * page_size
        end_idx = start_idx + page_size
        paged_allocations = allocations[start_idx:end_idx]

        status_counter = Counter()
        service_counter = Counter()
        household_counter = Counter()
        adviser_counter = Counter()
        source_counter = Counter()
        client_emails = set()

        for row in allocations:
            status_raw = (row.get("status") or "").strip()
            if status_raw:
                lower = status_raw.lower()
                if lower in ("completed", "success"):
                    status_label = "Completed"
                elif lower in ("failed", "error"):
                    status_label = "Failed"
                elif lower in ("processing", "pending"):
                    status_label = "Processing"
                else:
                    status_label = status_raw.replace("_", " ").title()
                status_counter[status_label] += 1

            for svc in _format_tag_list(
                row.get("service_package_raw") or row.get("service_package")
            ):
                service_counter[svc] += 1

            for hh in _format_tag_list(row.get("household_type_raw") or row.get("household_type")):
                household_counter[hh] += 1

            adviser_label = row.get("adviser_name") or _format_display_name(
                row.get("adviser_email") or ""
            )
            if adviser_label:
                adviser_counter[adviser_label] += 1

            source_label = row.get("source") or "webhook"
            source_counter[source_label] += 1

            if row.get("client_email"):
                client_emails.add(row["client_email"].lower())

        unique_statuses = sorted(
            {(row.get("status") or "").lower() for row in allocations if row.get("status")}
        )
        unique_deals = sorted(
            {str(row.get("deal_id")) for row in allocations if row.get("deal_id")}
        )
        unique_advisers = sorted(adviser_counter.keys())

        dashboard_counts = {
            "total": total_count,
            "completed": status_counter.get("Completed", 0),
            "failed": status_counter.get("Failed", 0),
            "processing": status_counter.get("Processing", 0),
            "unique_advisers": len(adviser_counter),
            "unique_deals": len(unique_deals),
            "unique_clients": len(client_emails),
        }

        start_record = start_idx + 1 if total_count else 0
        end_record = min(end_idx, total_count)

        context = {
            "allocations": paged_allocations,
            "unique_deals": unique_deals,
            "unique_advisers": unique_advisers,
            "unique_statuses": unique_statuses,
            "status_filter": status_filter,
            "deal_filter": deal_filter,
            "adviser_filter": adviser_filter,
            "days_filter": days_filter,
            "today": sydney_today().isoformat(),
            "week_num": f"{sydney_today().isocalendar()[1]:02d}",
            "total_count": total_count,
            "page": page,
            "total_pages": total_pages,
            "page_size": page_size,
            "start_record": start_record,
            "end_record": end_record,
            "hubspot_portal_id": os.getenv("HUBSPOT_PORTAL_ID", "47011873"),
            "dashboard_counts": dashboard_counts,
            "status_stats": [
                {"label": label, "count": count} for label, count in status_counter.most_common()
            ],
            "service_stats": [
                {"label": label, "count": count} for label, count in service_counter.most_common(5)
            ],
            "household_stats": [
                {"label": label, "count": count}
                for label, count in household_counter.most_common(5)
            ],
            "adviser_stats": [
                {"label": label, "count": count} for label, count in adviser_counter.most_common(5)
            ],
            "source_stats": [
                {"label": label.replace("_", " ").title(), "count": count}
                for label, count in source_counter.most_common()
            ],
        }

        return render_template("allocation_history_ui.html", **context)

    except Exception as e:
        logger.error("Error in allocation_history_ui: %s", e, exc_info=True)
        return render_template(
            "error.html", code=500, message="Error loading allocation history. Please try again."
        ), 500


# ---- Availability ----
@main_bp.route("/availability/earliest")
def availability_earliest():
    """Uniform templated view of earliest availability with tags and topbar."""
    try:
        compute = request.args.get("compute") == "1"
        include_no = str(request.args.get("include_no", "0")).lower() in ("1", "true", "yes", "on")

        # Parse agreement_start_date parameter (default to Sydney now if not provided)
        agreement_start_date_param = request.args.get("agreement_start_date")
        agreement_start_date = None
        default_date = sydney_today().isoformat()

        if agreement_start_date_param:
            try:
                # Parse date string as YYYY-MM-DD and convert to Sydney timezone datetime
                parsed_date = datetime.strptime(agreement_start_date_param, "%Y-%m-%d").date()
                agreement_start_date = datetime(
                    parsed_date.year, parsed_date.month, parsed_date.day, tzinfo=SYDNEY_TZ
                )
                default_date = agreement_start_date_param
            except ValueError:
                logger.warning(
                    "availability_earliest invalid agreement_start_date parameter: %s",
                    agreement_start_date_param,
                )
                return (
                    jsonify({"error": "Invalid agreement_start_date format. Use YYYY-MM-DD"}),
                    400,
                )

        logger.info(
            "availability_earliest request compute=%s include_no=%s agreement_start_date=%s",
            compute,
            include_no,
            agreement_start_date.isoformat() if agreement_start_date else "default",
        )

        # Only compute if requested
        rows = []
        if compute:
            results = get_users_earliest_availability(
                agreement_start_date=agreement_start_date, include_no=include_no
            )
            results = sorted(results, key=lambda r: (r.get("email") or "").lower())
            logger.debug("availability_earliest retrieved %d adviser rows", len(results))

            # Build rows for template
            for item in results:
                earliest_wk_ordinal = item.get("earliest_open_week")
                monday_str = (
                    date.fromordinal(earliest_wk_ordinal).isoformat()
                    if isinstance(earliest_wk_ordinal, int)
                    else ""
                )
                svc_raw = item.get("service_packages") or ""
                # Split into clean tags by common delimiters and preserve order without duplicates
                parts = (
                    [p.strip() for p in re.split(r"[;,/|]+", svc_raw) if p.strip()]
                    if svc_raw
                    else []
                )
                seen = set()
                tags = []
                for p in parts:
                    if p not in seen:
                        seen.add(p)
                        tags.append(p)

                # Canonicalize to title case for consistency across rows, preserving special acronyms
                def format_tag(tag):
                    if tag.upper() == "IPO":
                        return "IPO"
                    return tag.title()

                tags = [format_tag(t) for t in tags]
                toc_raw = item.get("taking_on_clients")
                # Normalize taking_on_clients to a boolean-like value and label
                toc_bool = True if str(toc_raw).lower() == "true" else False
                toc_label = "Yes" if toc_bool else "No"
                limit_value = item.get("client_limit_monthly")
                limit_label = str(limit_value) if limit_value not in (None, "") else ""
                override_status = item.get("capacity_override_status")
                override_effective = item.get("capacity_override_effective_label") or item.get(
                    "capacity_override_effective_date"
                )
                status_text = None
                if override_status == "active":
                    status_text = "Active override"
                elif override_status == "upcoming":
                    status_text = "Scheduled override"
                limit_hint = None
                if status_text:
                    limit_hint = status_text
                    if override_effective:
                        limit_hint = f"{status_text} ({override_effective})"
                rows.append(
                    {
                        "email": item.get("email") or "",
                        "name": _format_display_name(item.get("email") or ""),
                        "tags": tags,
                        "pod": item.get("pod_type") or "",
                        "household_type": item.get("household_type") or "",
                        "limit": limit_label,
                        "limit_hint": limit_hint,
                        "wk_label": item.get("earliest_open_week_label")
                        or (item.get("error") or ""),
                        "monday": monday_str,
                        "taking_on_clients": toc_label,
                        "taking_on_clients_sort": 1 if toc_bool else 0,
                    }
                )

            # Enforce a consistent tag order across all rows
            preferred_order = [
                "Seed",
                "Series A",
                "Series B",
                "Series C",
                "Series D",
                "Series E",
                "Series F",
                "Series G",
                "IPO",
            ]
            order_index = {name.lower(): i for i, name in enumerate(preferred_order)}

            def tag_sort_key(t: str):
                tl = t.lower()
                return (0, order_index[tl]) if tl in order_index else (1, tl)

            for r in rows:
                r["tags"] = sorted((r.get("tags") or []), key=tag_sort_key)

            # Assign consistent colors per tag across all rows
            color_cycle = ["blue", "green", "purple", "orange", "pink", "teal"]
            tag_color_map = {}
            next_idx = 0
            for r in rows:
                for t in r.get("tags") or []:
                    if t not in tag_color_map:
                        tag_color_map[t] = color_cycle[next_idx % len(color_cycle)]
                        next_idx += 1

            # Prepare rows with colored tags using the global map
            household_cycle = ["orange", "pink", "teal", "purple", "green", "blue"]
            household_color_map = {}
            household_idx = 0
            for r in rows:
                r["tag_items"] = [
                    {"name": t, "cls": tag_color_map.get(t, color_cycle[0])}
                    for t in (r.get("tags") or [])
                ]
                household_raw = r.get("household_type") or ""
                household_parts = [p.strip() for p in re.split(r"[;]+", household_raw) if p.strip()]
                items = []
                for part in household_parts:
                    key = part.lower()
                    if key not in household_color_map:
                        household_color_map[key] = household_cycle[
                            household_idx % len(household_cycle)
                        ]
                        household_idx += 1
                    items.append({"name": part, "cls": household_color_map[key]})
                r["household_items"] = items

            logger.info(
                "availability_earliest computed %d rows for rendering",
                len(rows),
            )
        else:
            logger.debug("availability_earliest compute flag not set; returning cached form")

        return render_template(
            "availability_earliest.html",
            rows=rows,
            today=sydney_today().isoformat(),
            week_num=f"{sydney_today().isocalendar()[1]:02d}",
            include_no=include_no,
            default_date=default_date,
            compute=compute,
        )
    except Exception as e:
        logger.error("Failed to compute earliest availability: %s", e)
        return jsonify({"error": "Internal server error"}), 500


@main_bp.route("/availability/schedule")
def availability_schedule():
    """UI to view an adviser's weekly schedule with shared layout/topbar."""
    try:
        # Check if computation is requested
        compute = request.args.get("compute") == "1"

        # For schedule endpoint, include all advisers (taking and not taking on clients)
        include_no = True

        # Parse agreement_start_date parameter (default to Sydney now if not provided)
        agreement_start_date_param = request.args.get("agreement_start_date")
        agreement_start_date = None
        default_date = sydney_today().isoformat()

        if agreement_start_date_param:
            try:
                # Parse date string as YYYY-MM-DD and convert to Sydney timezone datetime
                parsed_date = datetime.strptime(agreement_start_date_param, "%Y-%m-%d").date()
                agreement_start_date = datetime(
                    parsed_date.year, parsed_date.month, parsed_date.day, tzinfo=SYDNEY_TZ
                )
                default_date = agreement_start_date_param
            except ValueError:
                return (
                    jsonify({"error": "Invalid agreement_start_date format. Use YYYY-MM-DD"}),
                    400,
                )

        users = get_user_ids_adviser()
        advisers = []
        for user in users:
            props = user.get("properties") or {}
            taking_on_clients_value = props.get("taking_on_clients")

            # Skip users with blank/None taking_on_clients
            if taking_on_clients_value is None or str(taking_on_clients_value).strip() == "":
                continue

            taking_on_clients = str(taking_on_clients_value).lower() == "true"

            if include_no:
                # Include advisers with taking_on_clients = true OR false (but not blank)
                advisers.append(user)
            else:
                # Only include advisers who are taking on clients
                if taking_on_clients:
                    advisers.append(user)

    except Exception as e:
        logger.error("Failed to load advisers: %s", e, exc_info=True)
        return render_template(
            "error.html", code=500, message="Failed to load advisers. Please try again."
        ), 500

    display_advisers = []
    for user in advisers:
        props = user.get("properties") or {}
        email = props.get("hs_email") or ""
        if not email:
            continue
        display_advisers.append(
            {
                "email": email,
                "name": _format_display_name(email),
                "service_packages": props.get("client_types") or "",
                "household_type": props.get("household_type") or "",
            }
        )

    emails = sorted([item["email"] for item in display_advisers])

    selected = request.args.get("email")

    rows = []
    earliest_week = None
    if selected and compute:
        logger.info(
            "availability_schedule requested for %s (agreement_start_date=%s)",
            selected,
            agreement_start_date.isoformat() if agreement_start_date else "default",
        )
        try:
            res = compute_user_schedule_by_email(
                selected, agreement_start_date=agreement_start_date
            )
            capacity = res.get("capacity") or {}
            earliest_week = res.get("earliest_open_week")
            for wk in sorted(capacity.keys()):
                vals = capacity[wk]
                rows.append(
                    {
                        "wk_label": week_label_from_ordinal(wk),
                        "monday": date.fromordinal(wk).isoformat(),
                        "clarify": str(vals[0]) if len(vals) > 0 else "0",
                        "ooo": str(vals[2]) if len(vals) > 2 else "No",
                        "deals": str(vals[3]) if len(vals) > 3 else "0",
                        "target": str(vals[4]) if len(vals) > 4 else "0",
                        "actual": str(vals[5]) if len(vals) > 5 else "0",
                        "diff": str(vals[6]) if len(vals) > 6 else "0",
                        "is_earliest": isinstance(earliest_week, int) and wk == earliest_week,
                    }
                )
        except Exception as e:
            logger.error("Failed to compute schedule for %s: %s", selected, e, exc_info=True)
            return render_template(
                "error.html", code=500, message="Failed to compute schedule. Please try again."
            ), 500

    # Build the adviser dropdown options HTML (kept simple for template)
    option_items = ['<option value="">-- Select adviser --</option>']
    for entry in sorted(display_advisers, key=lambda item: item["name"].lower()):
        e = entry["email"]
        sel_attr = " selected" if selected == e else ""
        label = f"{entry['name']}"
        option_items.append(f'<option value="{e}"{sel_attr}>{label}</option>')
    options_html = "".join(option_items)

    return render_template(
        "availability_schedule.html",
        rows=rows,
        options_html=options_html,
        today=sydney_today().isoformat(),
        week_num=f"{sydney_today().isocalendar()[1]:02d}",
        selected=selected or "",
        default_date=default_date,
        compute=compute,
    )


@main_bp.route("/availability/meetings")
def availability_meetings():
    """UI to view an adviser's individual Clarify/Kick Off meetings."""
    try:
        compute = request.args.get("compute") == "1"
        weeks_back_param = request.args.get("weeks_back", "8")
        try:
            weeks_back = max(1, min(52, int(weeks_back_param)))
        except ValueError:
            weeks_back = 8

        users = get_user_ids_adviser()
        advisers = []
        for user in users:
            props = user.get("properties") or {}
            taking_on_clients_value = props.get("taking_on_clients")

            # Skip users with blank/None taking_on_clients
            if taking_on_clients_value is None or str(taking_on_clients_value).strip() == "":
                continue

            advisers.append(user)
    except Exception as e:
        logger.error("Failed to load advisers: %s", e, exc_info=True)
        return render_template(
            "error.html", code=500, message="Failed to load advisers. Please try again."
        ), 500

    display_advisers = []
    for user in advisers:
        props = user.get("properties") or {}
        email = props.get("hs_email") or ""
        if not email:
            continue
        display_advisers.append(
            {
                "email": email,
                "name": _format_display_name(email),
                "service_packages": props.get("client_types") or "",
                "household_type": props.get("household_type") or "",
            }
        )

    selected = request.args.get("email")
    rows = []
    error_msg = None
    since_label = None
    meeting_object_type = meeting_object_type_id()
    portal_id = os.getenv("HUBSPOT_PORTAL_ID", DEFAULT_PORTAL_ID)

    if selected and compute:
        target_user = next(
            (
                u
                for u in advisers
                if (u.get("properties", {}).get("hs_email") or "").lower() == selected.lower()
            ),
            None,
        )

        if not target_user:
            error_msg = f"Adviser {selected} not found."
        else:
            start_ts = get_monday_from_weeks_ago(n=weeks_back)
            since_label = datetime.fromtimestamp(start_ts / 1000, tz=SYDNEY_TZ).date().isoformat()
            try:
                target_user = get_user_meeting_details(target_user, start_ts)
            except Exception as e:
                logger.error("Failed to fetch meetings for %s: %s", selected, e)
                error_msg = "Failed to fetch meetings: Internal server error"
            else:
                meetings_raw = (target_user.get("meetings") or {}).get("results", [])
                parsed = []
                for item in meetings_raw:
                    props = item.get("properties") or {}
                    meeting_id = item.get("id")
                    start_raw = props.get("hs_meeting_start_time") or ""
                    start_dt = None
                    try:
                        start_dt = datetime.fromisoformat(start_raw.replace("Z", "+00:00"))
                    except Exception:
                        start_dt = None
                    start_syd = start_dt.astimezone(SYDNEY_TZ) if start_dt else None
                    activity_link = (
                        f"https://app.hubspot.com/contacts/{portal_id}/activities/{meeting_id}"
                        if meeting_id
                        else ""
                    )
                    record_link = (
                        f"https://app.hubspot.com/contacts/{portal_id}/record/{meeting_object_type}/{meeting_id}"
                        if meeting_id
                        else ""
                    )
                    parsed.append(
                        {
                            "id": meeting_id,
                            "date": start_syd.strftime("%Y-%m-%d") if start_syd else start_raw,
                            "time": start_syd.strftime("%H:%M") if start_syd else "",
                            "start_iso": start_syd.isoformat() if start_syd else start_raw,
                            "type": props.get("hs_activity_type") or "",
                            "title": props.get("hs_meeting_title") or "",
                            "outcome": props.get("hs_meeting_outcome") or "",
                            "link": activity_link or record_link,
                            "record_link": record_link,
                            "activity_link": activity_link,
                            "raw_start": start_raw,
                            "_sort": start_syd,
                        }
                    )
                parsed.sort(key=lambda m: m["_sort"] or datetime.max.replace(tzinfo=SYDNEY_TZ))
                for item in parsed:
                    item.pop("_sort", None)
                rows = parsed

    option_items = ['<option value="">-- Select adviser --</option>']
    for entry in sorted(display_advisers, key=lambda item: item["name"].lower()):
        e = entry["email"]
        sel_attr = " selected" if selected == e else ""
        label = f"{entry['name']}"
        option_items.append(f'<option value="{e}"{sel_attr}>{label}</option>')
    options_html = "".join(option_items)

    return render_template(
        "availability_meetings.html",
        rows=rows,
        options_html=options_html,
        today=sydney_today().isoformat(),
        week_num=f"{sydney_today().isocalendar()[1]:02d}",
        selected=selected or "",
        weeks_back=weeks_back,
        since_label=since_label,
        compute=compute,
        error_msg=error_msg,
    )


@main_bp.route("/availability/clarify-chart")
def availability_clarify_chart():
    """UI page showing Clarify meetings chart by adviser and week."""
    try:
        # Get adviser list for dropdown
        users = get_user_ids_adviser()
        advisers = []
        for user in users:
            props = user.get("properties") or {}
            email = props.get("hs_email") or ""
            if not email:
                continue
            taking_on_clients_value = props.get("taking_on_clients")
            if taking_on_clients_value is None or str(taking_on_clients_value).strip() == "":
                continue
            # Format name from email
            local = email.split("@")[0]
            name = " ".join(
                part.capitalize() for part in local.replace(".", " ").replace("_", " ").split()
            )
            advisers.append({"email": email, "name": name or email})

        advisers.sort(key=lambda a: a["name"].lower())

        return render_template(
            "availability_clarify_chart.html",
            advisers=advisers,
            today=sydney_today().isoformat(),
            week_num=f"{sydney_today().isocalendar()[1]:02d}",
        )
    except Exception as e:
        logger.error("Failed to load clarify chart page: %s", e)
        return jsonify({"error": "Internal server error"}), 500


@main_bp.route("/api/clarify-chart-data")
def api_clarify_chart_data():
    """API endpoint for Clarify chart data.

    Returns JSON in format expected by Chart.js: {labels, booked, simulated}
    Optional query params:
        - weeks: Number of weeks to show (default 12)
        - adviser: Filter to specific adviser email
    """
    try:
        weeks_param = request.args.get("weeks", "12")
        try:
            weeks = max(1, min(52, int(weeks_param)))
        except ValueError:
            weeks = 12

        adviser_filter = request.args.get("adviser", "").strip().lower()

        cloudsql_db = get_cloudsql_db()
        rows = cloudsql_db.get_clarify_chart_data(weeks=weeks, adviser_email=adviser_filter or None)

        # Aggregate by week (sum across advisers if no filter)
        week_data = {}
        for row in rows:
            week = row.get("week_commencing")
            if not week:
                continue
            week_str = week.isoformat() if hasattr(week, "isoformat") else str(week)
            if week_str not in week_data:
                week_data[week_str] = {"booked": 0, "simulated": 0}
            week_data[week_str]["booked"] += int(row.get("booked_clarifies") or 0)
            week_data[week_str]["simulated"] += int(row.get("simulated_clarifies") or 0)

        # Sort by week and build arrays
        sorted_weeks = sorted(week_data.keys())
        labels = sorted_weeks
        booked = [week_data[w]["booked"] for w in sorted_weeks]
        simulated = [week_data[w]["simulated"] for w in sorted_weeks]

        return jsonify(
            {
                "labels": labels,
                "booked": booked,
                "simulated": simulated,
            }
        )

    except Exception as e:
        logger.error("Failed to fetch clarify chart data: %s", e)
        return jsonify({"error": "Internal server error"}), 500


@main_bp.route("/availability/matrix")
def availability_matrix():
    try:
        services, households, matrix = build_service_household_matrix()
        rows = [
            {
                "service": svc,
                "columns": [
                    {"household": hh, "advisers": matrix.get(svc, {}).get(hh, [])}
                    for hh in households
                ],
            }
            for svc in services
        ]
        unique_advisers = {
            adviser["email"]
            for svc_map in matrix.values()
            for advisers in svc_map.values()
            for adviser in advisers
        }
        return render_template(
            "availability_matrix.html",
            services=services,
            households=households,
            matrix=matrix,
            rows=rows,
            adviser_count=len(unique_advisers),
            today=sydney_today().isoformat(),
            week_num=f"{sydney_today().isocalendar()[1]:02d}",
        )
    except Exception as e:
        logger.error("Failed to build availability matrix: %s", e, exc_info=True)
        return render_template(
            "error.html", code=500, message="Error loading availability matrix. Please try again."
        ), 500


@main_bp.route("/meeting/owner", methods=["POST"])
def update_meeting_owner():
    """Change the HubSpot owner of a meeting (Clarify/Kick Off)."""
    if not HUBSPOT_TOKEN:
        return jsonify({"error": "HUBSPOT_TOKEN is not configured"}), 500

    payload = request.get_json(silent=True) or {}
    meeting_id = str(payload.get("meeting_id") or payload.get("id") or "").strip()
    new_owner_id = str(payload.get("new_owner_id") or payload.get("owner_id") or "").strip()

    if not meeting_id or not new_owner_id:
        return (
            jsonify(
                {
                    "error": "meeting_id and new_owner_id are required",
                    "payload": payload,
                }
            ),
            400,
        )

    url = f"https://api.hubapi.com/crm/v3/objects/meetings/{meeting_id}"
    try:
        resp = requests.patch(
            url,
            headers=HUBSPOT_HEADERS,
            json={"properties": {"hubspot_owner_id": new_owner_id}},
            timeout=15,
        )
        resp.raise_for_status()
        body = resp.json()
        return jsonify(
            {
                "ok": True,
                "meeting_id": meeting_id,
                "new_owner_id": new_owner_id,
                "properties": body.get("properties", {}),
            }
        )
    except requests.exceptions.HTTPError as e:
        logger.error("Meeting owner update HTTP error: %s", e)
        status = resp.status_code if "resp" in locals() else 500
        return jsonify(
            {"error": "Internal server error", "details": "See server logs for details"}
        ), status
    except Exception as e:
        logger.error("Failed to update meeting owner: %s", e)
        return jsonify({"error": "Internal server error"}), 500


# Healthcheck
@main_bp.route("/_ah/warmup")
def warmup():
    """Healthcheck endpoint for platform warmup probes.

    Verifies CloudSQL connectivity so unhealthy instances don't receive traffic.
    """
    try:
        from sqlalchemy import text as _text

        db = get_cloudsql_db()
        with db.engine.connect() as conn:
            conn.execute(_text("SELECT 1"))
        return ("OK", 200)
    except Exception as exc:
        logger.error("Warmup health check failed: %s", exc)
        return ("UNHEALTHY", 503)


@main_bp.route("/health")
def health():
    """Lightweight health check for external monitors."""
    return jsonify({"status": "ok"}), 200


# ===== APP INITIALIZATION =====
# Create Flask app and register blueprints
# This MUST happen after all blueprint handlers and routes are defined
# Calculate paths to templates and static (at project root, 3 levels up from main.py)
from pathlib import Path as _Path

_main_dir = _Path(__file__).parent.parent.parent


def create_app(config_overrides=None):
    app = Flask(
        __name__,
        template_folder=str(_main_dir / "templates"),
        static_folder=str(_main_dir / "static"),
    )

    # App configuration
    secret_key = get_secret("SESSION_SECRET")
    if not secret_key and os.environ.get("K_SERVICE"):
        raise RuntimeError("SESSION_SECRET must be set in production")
    app.secret_key = secret_key or "dev-only-session-key"

    # Secure session cookies (HTTPS-only in production)
    is_production = bool(os.environ.get("K_SERVICE"))
    app.config.update(
        {
            "SESSION_COOKIE_SECURE": is_production,
            "SESSION_COOKIE_HTTPONLY": True,
            "SESSION_COOKIE_SAMESITE": "Lax",
            "PERMANENT_SESSION_LIFETIME": 3600,
            "PREFERRED_URL_SCHEME": "https" if is_production else "http",
            "DEV_LOGIN_ENABLED": not is_production,
        }
    )

    # Initialize Google OAuth Authlib
    google_client_id = os.environ.get("GOOGLE_CLIENT_ID")
    google_client_secret = os.environ.get("GOOGLE_CLIENT_SECRET")
    if is_production and (not google_client_id or not google_client_secret):
        raise RuntimeError("GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET must be set in production")
    app.oauth = OAuth(app)
    app.oauth.register(
        name="google",
        client_id=google_client_id or "mock_client_id",
        client_secret=google_client_secret or "mock_client_secret",
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"},
    )

    if config_overrides:
        app.config.update(config_overrides)

    # Security headers for all responses
    @app.after_request
    def set_security_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com; "
            "img-src 'self' https://ui-avatars.com data:; "
            "connect-src 'self'"
        )
        if is_production:
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response

    # Trust Cloud Run's reverse proxy headers so url_for() generates https:// URIs
    if is_production:
        from werkzeug.middleware.proxy_fix import ProxyFix

        app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

    app.register_blueprint(main_bp)
    app.register_blueprint(init_webhooks())
    app.register_blueprint(skills_bp)

    # Box folder creation (called by HubSpot workflows)
    try:
        from adviser_allocation.api.box_routes import box_bp

        app.register_blueprint(box_bp)
        logger.info("Box routes registered")
    except Exception as exc:
        logger.warning("Box routes not loaded (missing deps?): %s", exc)

    @app.context_processor
    def inject_admin_flag():
        return {"is_admin": check_is_admin()}

    @app.errorhandler(404)
    def not_found(e):
        if request.accept_mimetypes.best == "text/html":
            return render_template("error.html", code=404, message="Page not found."), 404
        return jsonify({"error": "Not found"}), 404

    @app.errorhandler(500)
    def server_error(e):
        if request.accept_mimetypes.best == "text/html":
            try:
                return (
                    render_template("error.html", code=500, message="Something went wrong."),
                    500,
                )
            except Exception:
                return "<h1>500</h1><p>Something went wrong.</p>", 500
        return jsonify({"error": "Internal server error"}), 500

    return app


# Default instance for WSGI and tests
app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", debug=True, port=int(os.environ.get("PORT", "8080")))
