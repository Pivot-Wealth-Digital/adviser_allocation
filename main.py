import os, time, secrets, json, re
from datetime import datetime, date, timedelta
import logging
from collections import Counter

from urllib.parse import urlencode
from flask import Flask, redirect, request, session, jsonify, render_template, render_template_string, url_for
import requests

from utils.common import sydney_now, sydney_today, SYDNEY_TZ, USE_FIRESTORE, get_firestore_client
from core.allocation import (
    get_adviser,
    get_users_earliest_availability,
    get_users_taking_on_clients,
    compute_user_schedule_by_email,
    week_label_from_ordinal,
    get_user_ids_adviser,
    build_service_household_matrix,
)
from utils.firestore_helpers import (
    get_employee_leaves as get_employee_leaves_from_firestore,
    get_employee_id as get_employee_id_from_firestore,
)
from services.allocation_service import store_allocation_record
from api.box_routes import box_bp
from api.allocation_routes import init_allocation_routes

from dotenv import load_dotenv

# Load variables from .env into environment
load_dotenv()

from utils.secrets import get_secret

LOG_LEVEL_NAME = (os.environ.get("LOG_LEVEL") or "INFO").upper()
LOG_LEVEL = getattr(logging, LOG_LEVEL_NAME, logging.INFO)

# Use Google Cloud Logging format for App Engine
try:
    import google.cloud.logging
    from google.cloud.logging.handlers import CloudLoggingHandler

    cloud_logging_client = google.cloud.logging.Client()
    cloud_handler = CloudLoggingHandler(cloud_logging_client)
    logging.root.addHandler(cloud_handler)
    logging.root.setLevel(LOG_LEVEL)
except (ImportError, Exception):
    # Fallback to standard logging if Cloud Logging not available
    LOG_FORMAT = os.environ.get("LOG_FORMAT", "[%(asctime)s] %(levelname)s in %(module)s: %(message)s")
    logging.basicConfig(level=LOG_LEVEL, format=LOG_FORMAT)
    logging.getLogger().setLevel(LOG_LEVEL)

# Initialize Firestore
db = get_firestore_client()

app = Flask(__name__)
app.secret_key = get_secret("SESSION_SECRET") or "change-me-please"  # set in app.yaml or .env

app.register_blueprint(box_bp)
app.register_blueprint(init_allocation_routes(db))

CHAT_WEBHOOK_URL = (
    get_secret("PIVOT-DIGITAL-CHAT-WEBHOOK-URL-ADVISER-ALGO")
    or os.environ.get("CHAT_WEBHOOK_URL")
)

# ---- Admin auth config (for managing closures) ----
ADMIN_USERNAME = get_secret("ADMIN_USERNAME") or os.environ.get("ADMIN_USERNAME")
ADMIN_PASSWORD = get_secret("ADMIN_PASSWORD") or os.environ.get("ADMIN_PASSWORD")

def is_authenticated():
    return bool(session.get("is_authenticated"))

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
        return view_func(*args, **kwargs)
    return wrapper

# Global before_request handler to protect all routes
@app.before_request
def require_login():
    # List of endpoints that don't require authentication
    public_endpoints = [
        'login', 
        'logout',
        'static',  # Static files (CSS, JS, images)
    ]
    
    # List of routes that don't require authentication (by path)
    public_paths = [
        '/webhook/allocation',  # HubSpot webhook for testing
        '/_ah/warmup',         # App Engine warmup
        '/post/allocate',    # Hubspot webhook
    ]
    
    # Check if current route is public
    if (request.endpoint in public_endpoints or 
        request.path in public_paths or
        request.path.startswith('/static/')):
        return  # Allow access
    
    # Require authentication for all other routes
    if not is_authenticated():
        if request.accept_mimetypes and "text/html" in request.accept_mimetypes:
            nxt = request.path
            return redirect(f"/login?next={nxt}")
        return jsonify({"error": "Unauthorized"}), 401

# ---- Employment Hero (HR) OAuth config ----
EH_AUTHORIZE_URL = os.environ.get("EH_AUTHORIZE_URL", "https://oauth.employmenthero.com/oauth2/authorize")
EH_TOKEN_URL     = os.environ.get("EH_TOKEN_URL",     "https://oauth.employmenthero.com/oauth2/token")
EH_CLIENT_ID     = get_secret("EH_CLIENT_ID")
EH_CLIENT_SECRET = get_secret("EH_CLIENT_SECRET")
EH_SCOPES        = os.environ.get("EH_SCOPES", "urn:mainapp:organisations:read urn:mainapp:employees:read urn:mainapp:leave_requests:read")

# Your app’s public callback URL, e.g. https://<PROJECT-ID>.appspot.com/auth/callback
REDIRECT_URI     = os.environ.get("REDIRECT_URI")

API_BASE = "https://api.employmenthero.com"  # HR API base
# For Payroll classic (KeyPay), swap the token URL and API base accordingly.

HUBSPOT_TOKEN = get_secret("HUBSPOT_TOKEN")
HUBSPOT_HEADERS = {"Authorization": f"Bearer {HUBSPOT_TOKEN}", "Content-Type": "application/json"}

def ensure_eh_config():
    """Ensure EH OAuth config is present for current environment.

    Works with App Engine (env variables + Secret Manager) and local .env.

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
            + ". In production, set env_variables in app.yaml (use Secret Manager resource paths for secrets). "
              "Locally, set values in .env."
        )

# ---- Simple token store: Firestore (per session); swap to your user key strategy ----
def token_key():
    """Return the key used to store OAuth tokens.

    Prefers a per-session/user key from Flask session; falls back to a fixed
    development key when unavailable.

    Returns:
        str: Token partition key.
    """
    # Prefer a per-user/session key; fall back to a fixed dev key
    # ideally session.get("user_key") or "e268304d2ad0444c"
    return "e268304d2ad0444c"

def save_tokens(tokens: dict):
    """Persist OAuth tokens and compute absolute expiry.

    Stores tokens in Firestore when configured, otherwise in Flask session.

    Args:
        tokens (dict): Token response containing access/refresh and expires_in.
    """
    tokens = dict(tokens)
    # Track absolute expiry (subtract 60s for clock skew)
    tokens["_expires_at"] = time.time() + max(0, int(tokens.get("expires_in", 0)) - 60)
    if USE_FIRESTORE and db:
        db.collection("eh_tokens").document(token_key()).set(tokens)
    else:
        session["eh_tokens"] = tokens


def load_tokens():
    """Load stored OAuth tokens from Firestore or session.

    Returns:
        dict | None: Token payload if found, else None.
    """
    return db.collection("eh_tokens").document(token_key()).get().to_dict() if db else session.get("eh_tokens")
    

def update_tokens(tokens: dict):
    """Update stored tokens by delegating to save_tokens.

    Args:
        tokens (dict): New token payload to persist.
    """
    save_tokens(tokens)  # same logic

# ---- OAuth flow ----
@app.route("/")
def index():
    """Homepage with navigation to all available features."""
    return render_template(
        "homepage.html",
        today=sydney_today().isoformat(),
        week_num=f"{sydney_today().isocalendar()[1]:02d}",
        environment=os.environ.get("GAE_ENV", "development"),
        sydney_time=sydney_now().strftime("%Y-%m-%d %H:%M:%S %Z")
    )

@app.route("/auth/start")
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
        # "scope": EH_SCOPES,
        "state": state,
    }
    authorize_url = f"{EH_AUTHORIZE_URL}?{urlencode(params)}"
    logging.info("Redirecting to Employment Hero authorize URL: %s", authorize_url)
    return redirect(authorize_url)

@app.route("/auth/callback")
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

# ---- Token helper: always return a fresh access token ----
def get_access_token():
    """Return a valid Employment Hero access token, refreshing if needed.

    Raises:
        RuntimeError: If tokens are missing or refresh fails.

    Returns:
        str: Bearer access token.
    """
    ensure_eh_config()
    tok = load_tokens()
    if not tok:
        raise RuntimeError("No tokens found. Start at /auth/start")

    if time.time() >= tok.get("_expires_at", 0):
        # Refresh
        data = {
            "client_id": EH_CLIENT_ID,
            "client_secret": EH_CLIENT_SECRET,
            "grant_type": "refresh_token",
            "refresh_token": tok["refresh_token"],
        }
        r = requests.post(EH_TOKEN_URL, data=data, timeout=30)
        if r.status_code != 200:
            raise RuntimeError(f"Refresh failed: {r.status_code} {r.text}")
        new_tok = r.json()
        # Replace refresh token if the server returns a new one
        if "refresh_token" not in new_tok:
            # Some providers return only access_token on refresh; keep the old refresh_token
            new_tok["refresh_token"] = tok["refresh_token"]
        update_tokens(new_tok)
        return new_tok["access_token"]

    return tok["access_token"]


# ---- Get Employee ID and email then store firestore ----
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
    return r.json()['data']['items'][0]['id']

@app.route("/get/employees")
def get_employees():
    """Fetch employees for the organisation and persist to Firestore.

    Returns:
        tuple: (list of employee dicts, HTTP status, headers)
    """
    access_token = get_access_token()
    headers = {"Authorization": f"Bearer {access_token}"}
    params = {
        "item_per_page": 100,
    }

    org_id = get_org_id(headers)
    r_emps = requests.get(f"{API_BASE}/api/v1/organisations/{org_id}/employees", headers=headers, params=params, timeout=30)
    if r_emps.status_code != 200:
        raise RuntimeError(f"Refresh failed: {r_emps.status_code} {r_emps.text}")

    employees = []

    for emp in r_emps.json()['data']['items']:
        item = {
            'id': emp.get('id'),
            'name': emp.get('full_name'),  # Using 'full_name' for 'name'
            'company_email': emp.get('company_email'),
            'account_email': emp.get('account_email')
        }
        if not db:
            raise RuntimeError("Firestore is not configured; cannot persist employees.")
        db.collection("employees").document(item['id']).set(item)
        employees.append(item)
    
    return (employees, r_emps.status_code, {"Content-Type": "application/json"})


@app.route("/get/employee_id")
def get_employee_id():
    """HTTP endpoint to retrieve an employee ID by email.

    Query Param:
        email (str): The employee's company email.

    Returns:
        tuple: JSON payload and HTTP status code.
    """
    search_email = request.args.get('email')
    
    if not search_email:
        return {"error": "Email parameter is missing"}, 400

    employee_id = get_employee_id_from_firestore(search_email)
    
    if employee_id:
        return {"employee_id": employee_id}, 200
    else:
        return {"error": "Employee not found"}, 404


@app.route("/get/leave_requests")
def get_leave_requests():
    """Fetch future approved leave requests and persist under each employee.

    Returns:
        tuple: (list of leave requests, HTTP status, headers)
    """
    access_token = get_access_token()
    headers = {"Authorization": f"Bearer {access_token}"}

    now_date = sydney_today()

    page = 1
    total_pages = 9

    org_id = get_org_id(headers)

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

        for leave_request in e.json()['data']['items']:
            start_date_obj = datetime.fromisoformat(leave_request['start_date']).date()
            if (leave_request['status'] == 'Approved') and (start_date_obj > now_date):
                item = {
                        'leave_request_id': leave_request.get('id'),
                        'employee_id': leave_request.get('employee_id'),  # Using 'full_name' for 'name'
                        'start_date': leave_request.get('start_date'),
                        'end_date': leave_request.get('end_date')
                    }
                leave_requests.append(item)
                if not db:
                    raise RuntimeError("Firestore is not configured; cannot persist leave requests.")
                db.collection("employees").document(item['employee_id']).collection("leave_requests").document(item['leave_request_id']).set(item)


        page += 1
        total_pages = e.json()['data']['total_pages']

    return (leave_requests, e.status_code, {"Content-Type": "application/json"})


@app.route("/get/employee_leave_requests")
def get_employee_leave_requests():
    """HTTP endpoint to list leave requests by employee ID from Firestore.

    Query Param:
        employee_id (str): The employee document ID.

    Returns:
        tuple: JSON payload and HTTP status code.
    """
    employee_id = request.args.get('employee_id')
    
    if not employee_id:
        return {"error": "Employee ID parameter is missing"}, 400

    leaves = get_employee_leaves_from_firestore(employee_id)
    
    # Return the list with a 200 OK status, even if it's empty
    return {"leave_requests": leaves}, 200


@app.route("/get/leave_requests_by_email")
def get_leave_requests_by_email():
    """HTTP endpoint to list leave requests by employee email.

    Uses existing Firestore data; does not trigger sync work.
    """
    email = request.args.get('email')
    if not email:
        return {"error": "Email parameter is missing"}, 400

    # Look up in existing data only; populate via /sync endpoints or a scheduler
    employee_id = get_employee_id_from_firestore(email)
    if not employee_id:
        return {"error": "Employee not found in store. Run /sync/employees."}, 404

    employee_leaves = get_employee_leaves_from_firestore(employee_id)
    return {"leave_requests": employee_leaves}, 200


# Lightweight sync endpoints to be triggered by a scheduler
@app.route("/sync/employees", methods=["POST", "GET"])
def sync_employees():
    """Trigger an on-demand employee sync (suitable for schedulers)."""
    try:
        data, status, headers = get_employees()
        return jsonify({"synced": len(data)}), status
    except Exception as e:
        logging.error(f"Failed to sync employees: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/sync/leave_requests", methods=["POST", "GET"])
def sync_leave_requests():
    """Trigger an on-demand leave requests sync (suitable for schedulers)."""
    try:
        data, status, headers = get_leave_requests()
        return jsonify({"synced": len(data)}), status
    except Exception as e:
        logging.error(f"Failed to sync leave requests: {e}")
        return jsonify({"error": str(e)}), 500


# ---- Global Closures (Holidays) ----
@app.route("/closures", methods=["GET", "POST"])
def closures():
    """Manage global office closures/holidays.

    GET: List closures from Firestore collection 'office_closures'.
    POST: Add a closure. JSON body: { start_date: YYYY-MM-DD, end_date?: YYYY-MM-DD, description?: str, tags?: [str] | str }
    """
    if not db:
        return jsonify({"error": "Firestore is not configured"}), 400

    if request.method == "GET":
        try:
            items = []
            for doc in db.collection("office_closures").stream():
                d = doc.to_dict() or {}
                d["id"] = doc.id
                items.append(d)
            return jsonify({"count": len(items), "closures": items}), 200
        except Exception as e:
            logging.error(f"Failed to list closures: {e}")
            return jsonify({"error": str(e)}), 500

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
            tags = [t.strip() for t in raw_tags.split(',') if t.strip()]
        if not start_date:
            return jsonify({"error": "start_date is required (YYYY-MM-DD)"}), 400
        # Basic format sanity (YYYY-MM-DD)
        try:
            datetime.strptime(start_date, "%Y-%m-%d")
            datetime.strptime(end_date, "%Y-%m-%d")
        except Exception:
            return jsonify({"error": "Invalid date format; use YYYY-MM-DD"}), 400

        doc_ref = db.collection("office_closures").document()
        doc_ref.set({"start_date": start_date, "end_date": end_date, "description": description, "tags": tags})
        return jsonify({"id": doc_ref.id, "start_date": start_date, "end_date": end_date, "description": description, "tags": tags}), 201
    except Exception as e:
        logging.error(f"Failed to create closure: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/closures/<closure_id>", methods=["PUT", "DELETE"])
def closures_item(closure_id):
    """Update or delete a specific closure document (admin only)."""
    if not db:
        return jsonify({"error": "Firestore is not configured"}), 400
    if not is_authenticated():
        return jsonify({"error": "Unauthorized"}), 401

    if request.method == "DELETE":
        try:
            db.collection("office_closures").document(closure_id).delete()
            return jsonify({"ok": True}), 200
        except Exception as e:
            logging.error(f"Failed to delete closure {closure_id}: {e}")
            return jsonify({"error": str(e)}), 500

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
                tags = [t.strip() for t in raw_tags.split(',') if t.strip()]
        if not start_date:
            return jsonify({"error": "start_date is required (YYYY-MM-DD)"}), 400
        try:
            datetime.strptime(start_date, "%Y-%m-%d")
            datetime.strptime(end_date, "%Y-%m-%d")
        except Exception:
            return jsonify({"error": "Invalid date format; use YYYY-MM-DD"}), 400
        update_doc = {"start_date": start_date, "end_date": end_date}
        if description is not None:
            update_doc["description"] = description
        if tags is not None:
            update_doc["tags"] = tags
        db.collection("office_closures").document(closure_id).set(update_doc, merge=True)
        resp = {"id": closure_id, "start_date": start_date, "end_date": end_date}
        if description is not None:
            resp["description"] = description
        if tags is not None:
            resp["tags"] = tags
        return jsonify(resp), 200
    except Exception as e:
        logging.error(f"Failed to update closure {closure_id}: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/closures/ui")
def closures_ui():
    """Simple UI to create and list global office closures (holidays)."""
    if not db:
        return (
            "<html><body><p>Firestore is not configured; cannot manage closures.</p></body></html>",
            400,
            {"Content-Type": "text/html; charset=utf-8"},
        )

    # Preload closures for display
    closures = []
    try:
        for doc in db.collection("office_closures").order_by("start_date").stream():
            d = doc.to_dict() or {}
            d["id"] = doc.id
            closures.append(d)
    except Exception as e:
        logging.warning(f"Failed to load closures for UI: {e}")

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
            return [t.strip() for t in v.split(',') if t.strip()]
        return []

    closures_data = []
    for idx, c in enumerate(closures, start=1):
        start_val = c.get('start_date','')
        end_val = c.get('end_date','') or start_val
        closures_data.append({
            'idx': idx,
            'id': c.get('id'),
            'start_date': start_val,
            'end_date': end_val,
            'description': (c.get('description') or c.get('reason') or ''),
            'tags': normalize_tags(c.get('tags')),
            'workdays': workdays_count(start_val, end_val),
        })

    # Build color map for tags (same cycle as availability pages)
    color_cycle = ["blue", "green", "purple", "orange", "pink", "teal"]
    all_tags = []
    for c in closures_data:
        all_tags.extend(c.get('tags') or [])
    tag_color_map = {}
    for i, t in enumerate(list(dict.fromkeys(all_tags))):
        tag_color_map[t] = color_cycle[i % len(color_cycle)]

    # Attach colored tag items for template and build mini-calendar payload
    closures_for_js = []
    for c in closures_data:
        tags = c.get('tags') or []
        c["tag_items"] = [{"name": t, "cls": tag_color_map.get(t, color_cycle[0])} for t in tags]
        color = tag_color_map.get(tags[0], 'blue') if tags else 'blue'
        closures_for_js.append({'start_date': c['start_date'], 'end_date': c['end_date'], 'color': color})

    return render_template('closures_ui.html', closures=closures_data, today=sydney_today().isoformat(), closures_for_js=closures_for_js)


@app.route("/login", methods=["GET", "POST"])
def login():
    """Site-wide login for all access."""
    # If already logged in, go to home or 'next'
    nxt = request.args.get("next") or "/"
    if is_authenticated():
        return redirect(nxt)
        
    if request.method == "POST":
        username = request.form.get("username") or ""
        password = request.form.get("password") or ""
        if not ADMIN_USERNAME or not ADMIN_PASSWORD:
            return ("<p>Login credentials not configured. Set ADMIN_USERNAME and ADMIN_PASSWORD.</p>", 500)
        if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
            session["is_authenticated"] = True
            return redirect(nxt)
        # invalid
        error = "Invalid credentials"
    else:
        error = ""

    html = (
        "<html><head><title>Login - Adviser Allocation System</title>"
        "<style>body{font-family:sans-serif;max-width:420px;margin:60px auto;padding:0 12px}.f{display:flex;flex-direction:column;gap:10px}label{font-size:.9em;color:#333}input{padding:8px;border:1px solid #bbb;border-radius:4px}button{padding:8px 12px;border:1px solid #0a7;background:#0a7;color:#fff;border-radius:4px;cursor:pointer}.err{color:#a00;margin-top:8px}h3{color:#333;text-align:center}</style>"
        "</head><body>"
        "<h3>🔐 Adviser Allocation System</h3>"
        "<p style='text-align:center;color:#666;margin-bottom:20px'>Please sign in to continue</p>"
        f"<form class=\"f\" method=\"POST\" action=\"/login?next={nxt}\">"
        "<div><label>Username</label><input name=\"username\" autocomplete=\"username\" required></div>"
        "<div><label>Password</label><input name=\"password\" type=\"password\" autocomplete=\"current-password\" required></div>"
        "<div><button type=\"submit\">Sign In</button></div>"
        f"<div class=\"err\">{error}</div>"
        "</form>"
        "</body></html>"
    )
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}

# ---- Chat notification helper ----
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
    """Send a notification to Google Chat about the allocation."""
    if not CHAT_WEBHOOK_URL:
        return

    try:
        resp = requests.post(CHAT_WEBHOOK_URL, json=payload, timeout=10)
        if resp.status_code >= 400:
            logging.error("Chat webhook returned %s: %s", resp.status_code, resp.text)
    except Exception as exc:  # pragma: no cover
        logging.error("Failed to send chat alert: %s", exc)


def build_chat_card_payload(title: str, sections: list[dict]) -> dict:
    """Return Google Chat card payload."""
    card_sections = []
    for section in sections:
        card_sections.append({
            "header": section.get("header"),
            "widgets": [{"textParagraph": {"text": text}} for text in section.get("lines", [])] or [],
        })
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
        logging.warning("Failed to parse agreement_start_date '%s': %s", agreement_value, exc)
        return ""


@app.route("/logout")
def logout():
    session.pop("is_authenticated", None)
    return redirect("/login")

# ---- Example API call ----
@app.route("/test/organisations")
def list_orgs():
    """List Employment Hero organisations for the connected account."""
    access_token = get_access_token()
    headers = {"Authorization": f"Bearer {access_token}"}
    r = requests.get(f"{API_BASE}/api/v1/organisations", headers=headers, timeout=30)
    return (r.text, r.status_code, {"Content-Type": "application/json"})

@app.route("/get/leave_requests_list")
def list_leave_requests():
    """List raw Employment Hero leave requests for the account."""
    access_token = get_access_token()
    headers = {"Authorization": f"Bearer {access_token}"}
    r = requests.get(f"{API_BASE}/api/v1/leave_requests", headers=headers, timeout=30)
    return (r.json(), r.status_code, {"Content-Type": "application/json"})

@app.route("/employees/ui")
def employees_ui():
    """UI to view employees from Firestore with consistent styling."""
    try:
        if not db:
            return render_template_string("""
                <div style="padding: 20px; text-align: center;">
                    <h3>⚠️ Firestore Not Configured</h3>
                    <p>Database connection is not available.</p>
                </div>
            """), 500
        
        # Fetch employees from Firestore
        employees = []
        for doc in db.collection("employees").stream():
            emp_data = doc.to_dict()
            emp_data['doc_id'] = doc.id
            employees.append(emp_data)
        
        # Sort employees by name
        employees.sort(key=lambda x: (x.get('name') or '').lower())
        
        return render_template(
            "employees_ui.html",
            employees=employees,
            today=sydney_today().isoformat(),
            week_num=f"{sydney_today().isocalendar()[1]:02d}",
            total_count=len(employees)
        )
        
    except Exception as e:
        return render_template_string(f"""
            <div style="padding: 20px; text-align: center;">
                <h3>❌ Error Loading Employees</h3>
                <p>{str(e)}</p>
            </div>
        """), 500

@app.route("/leave_requests/ui")
def leave_requests_ui():
    """UI to view leave requests from Firestore with filtering and calendar view."""
    try:
        if not db:
            return render_template_string("""
                <div style="padding: 20px; text-align: center;">
                    <h3>⚠️ Firestore Not Configured</h3>
                    <p>Database connection is not available.</p>
                </div>
            """), 500
        
        # Get filter parameters
        selected_employee = request.args.get('employee', '')
        status_filter = request.args.get('status', '')
        
        # Fetch employees first for dropdown (optimized query)
        employees = []
        employees_dict = {}
        for emp_doc in db.collection("employees").stream():
            emp_data = emp_doc.to_dict()
            emp_id = emp_doc.id
            emp_name = emp_data.get('name', 'Unknown')
            emp_email = emp_data.get('company_email', emp_data.get('account_email', ''))
            
            employees.append({
                'id': emp_id,
                'name': emp_name,
                'email': emp_email
            })
            employees_dict[emp_id] = {'name': emp_name, 'email': emp_email}
        
        employees.sort(key=lambda x: x['name'].lower())
        
        # Fetch leave requests (with optional filtering)
        leave_requests = []
        calendar_events = []
        
        if selected_employee:
            # Fetch only for selected employee (faster)
            emp_ref = db.collection("employees").document(selected_employee)
            emp_data = employees_dict.get(selected_employee, {'name': 'Unknown', 'email': ''})
            
            for leave_doc in emp_ref.collection("leave_requests").stream():
                leave_data = leave_doc.to_dict()
                leave_data['employee_id'] = selected_employee
                leave_data['employee_name'] = emp_data['name']
                leave_data['employee_email'] = emp_data['email']
                leave_data['doc_id'] = leave_doc.id
                
                # Apply status filter
                if not status_filter or leave_data.get('status', '').lower() == status_filter.lower():
                    leave_requests.append(leave_data)
                    
                # Add to calendar events if approved
                if leave_data.get('status', '').lower() == 'approved':
                    calendar_events.append({
                        'title': f"{emp_data['name']} - {leave_data.get('leave_type', 'Leave')}",
                        'start': leave_data.get('start_date', ''),
                        'end': leave_data.get('end_date', ''),
                        'employee': emp_data['name'],
                        'type': leave_data.get('leave_type', 'Leave')
                    })
        else:
            # Fetch all leave requests (existing logic but optimized)
            for emp_id, emp_data in employees_dict.items():
                emp_ref = db.collection("employees").document(emp_id)
                
                for leave_doc in emp_ref.collection("leave_requests").stream():
                    leave_data = leave_doc.to_dict()
                    leave_data['employee_id'] = emp_id
                    leave_data['employee_name'] = emp_data['name']
                    leave_data['employee_email'] = emp_data['email']
                    leave_data['doc_id'] = leave_doc.id
                    
                    # Apply status filter
                    if not status_filter or leave_data.get('status', '').lower() == status_filter.lower():
                        leave_requests.append(leave_data)
                        
                    # Add to calendar events if approved
                    if leave_data.get('status', '').lower() == 'approved':
                        calendar_events.append({
                            'title': f"{emp_data['name']} - {leave_data.get('leave_type', 'Leave')}",
                            'start': leave_data.get('start_date', ''),
                            'end': leave_data.get('end_date', ''),
                            'employee': emp_data['name'],
                            'type': leave_data.get('leave_type', 'Leave')
                        })
        
        # Sort by start date (most recent first)
        leave_requests.sort(key=lambda x: x.get('start_date', ''), reverse=True)
        
        return render_template(
            "leave_requests_ui.html",
            leave_requests=leave_requests,
            employees=employees,
            calendar_events=calendar_events,
            selected_employee=selected_employee,
            status_filter=status_filter,
            today=sydney_today().isoformat(),
            week_num=f"{sydney_today().isocalendar()[1]:02d}",
            total_count=len(leave_requests)
        )
        
    except Exception as e:
        return render_template_string(f"""
            <div style="padding: 20px; text-align: center;">
                <h3>❌ Error Loading Leave Requests</h3>
                <p>{str(e)}</p>
            </div>
        """), 500

@app.route("/webhook/allocation", methods=["POST"])
def allocation_webhook():
    """Webhook endpoint to receive and store allocation requests."""
    try:
        if not db:
            return jsonify({"error": "Firestore not configured"}), 500

        # Get request data
        data = request.get_json()
        if not data:
            return jsonify({"error": "No JSON data provided"}), 400

        extra = {
            "ip_address": request.remote_addr,
            "user_agent": request.headers.get("User-Agent", ""),
        }
        doc_id = store_allocation_record(db, data, source="webhook", raw_request=data, extra_fields=extra)

        if doc_id:
            return jsonify({
                "success": True,
                "id": doc_id,
                "timestamp": sydney_now().isoformat()
            }), 201
        else:
            return jsonify({"error": "Failed to store allocation data"}), 500

    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/allocations/history")
def allocation_history_ui():
    """Dashboard view of allocation history with pagination."""
    try:
        if not db:
            return render_template_string(
                """
                <div style=\"padding: 20px; text-align: center;\">
                    <h3>⚠️ Firestore Not Configured</h3>
                    <p>Database connection is not available.</p>
                </div>
                """
            ), 500

        status_filter = request.args.get("status", "")
        deal_filter = request.args.get("deal", "")
        adviser_filter = request.args.get("adviser", "")
        days_filter = int(request.args.get("days", 30) or 30)
        page = max(1, int(request.args.get("page", 1) or 1))
        try:
            page_size = int(request.args.get("page_size", 25) or 25)
        except ValueError:
            page_size = 25
        page_size = max(5, min(page_size, 200))

        cutoff_date = sydney_now() - timedelta(days=days_filter)

        allocations_ref = db.collection("allocation_requests")
        allocations: list[dict] = []

        for doc in allocations_ref.order_by("timestamp", direction="DESCENDING").stream():
            row = doc.to_dict() or {}
            row["doc_id"] = doc.id

            ts_value = row.get("timestamp")
            if ts_value:
                try:
                    parsed_ts = datetime.fromisoformat(str(ts_value).replace("Z", "+00:00"))
                    if parsed_ts < cutoff_date:
                        continue
                except Exception:
                    pass

            if status_filter and (row.get("status") or "").lower() != status_filter.lower():
                continue
            if deal_filter and str(row.get("deal_id") or "") != deal_filter:
                continue
            if adviser_filter:
                searchable = " ".join([
                    row.get("adviser_name", ""),
                    row.get("adviser_email", ""),
                ]).lower()
                if adviser_filter.lower() not in searchable:
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

            for svc in _format_tag_list(row.get("service_package_raw") or row.get("service_package")):
                service_counter[svc] += 1

            for hh in _format_tag_list(row.get("household_type_raw") or row.get("household_type")):
                household_counter[hh] += 1

            adviser_label = row.get("adviser_name") or _format_display_name(row.get("adviser_email") or "")
            if adviser_label:
                adviser_counter[adviser_label] += 1

            source_label = row.get("source") or "webhook"
            source_counter[source_label] += 1

            if row.get("client_email"):
                client_emails.add(row["client_email"].lower())

        unique_statuses = sorted({(row.get("status") or "").lower() for row in allocations if row.get("status")})
        unique_deals = sorted({str(row.get("deal_id")) for row in allocations if row.get("deal_id")})
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
            "hubspot_portal_id": os.getenv('HUBSPOT_PORTAL_ID', '21983344'),
            "dashboard_counts": dashboard_counts,
            "status_stats": [{"label": label, "count": count} for label, count in status_counter.most_common()],
            "service_stats": [{"label": label, "count": count} for label, count in service_counter.most_common(5)],
            "household_stats": [{"label": label, "count": count} for label, count in household_counter.most_common(5)],
            "adviser_stats": [{"label": label, "count": count} for label, count in adviser_counter.most_common(5)],
            "source_stats": [{"label": label.replace('_', ' ').title(), "count": count} for label, count in source_counter.most_common()],
        }

        return render_template("allocation_history_ui.html", **context)

    except Exception as e:
        return render_template_string(
            f"""
            <div style=\"padding: 20px; text-align: center;\">
                <h3>❌ Error Loading Allocation History</h3>
                <p>{str(e)}</p>
            </div>
            """
        ), 500


# ---- Availability ----
@app.route("/availability/earliest")
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
                agreement_start_date = datetime(parsed_date.year, parsed_date.month, parsed_date.day, tzinfo=SYDNEY_TZ)
                default_date = agreement_start_date_param
            except ValueError:
                logging.warning(
                    "availability_earliest invalid agreement_start_date parameter: %s",
                    agreement_start_date_param,
                )
                return jsonify({"error": "Invalid agreement_start_date format. Use YYYY-MM-DD"}), 400

        logging.info(
            "availability_earliest request compute=%s include_no=%s agreement_start_date=%s",
            compute,
            include_no,
            agreement_start_date.isoformat() if agreement_start_date else "default",
        )

        # Only compute if requested
        rows = []
        if compute:
            results = get_users_earliest_availability(agreement_start_date=agreement_start_date, include_no=include_no)
            results = sorted(results, key=lambda r: (r.get("email") or "").lower())
            logging.debug(
                "availability_earliest retrieved %d adviser rows", len(results)
            )

            # Build rows for template
            for item in results:
                earliest_wk_ordinal = item.get("earliest_open_week")
                monday_str = date.fromordinal(earliest_wk_ordinal).isoformat() if isinstance(earliest_wk_ordinal, int) else ""
                svc_raw = (item.get("service_packages") or "")
                # Split into clean tags by common delimiters and preserve order without duplicates
                parts = [p.strip() for p in re.split(r"[;,/|]+", svc_raw) if p.strip()] if svc_raw else []
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
                rows.append({
                    "email": item.get("email") or "",
                    "name": _format_display_name(item.get("email") or ""),
                    "tags": tags,
                    "pod": item.get("pod_type") or "",
                    "household_type": item.get("household_type") or "",
                    "limit": item.get("client_limit_monthly") or "",
                    "wk_label": item.get("earliest_open_week_label") or (item.get("error") or ""),
                    "monday": monday_str,
                    "taking_on_clients": toc_label,
                    "taking_on_clients_sort": 1 if toc_bool else 0,
                })

            # Enforce a consistent tag order across all rows
            preferred_order = [
                "Seed", "Series A", "Series B", "Series C", "Series D", "Series E", "Series F", "Series G", "IPO"
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
                for t in (r.get("tags") or []):
                    if t not in tag_color_map:
                        tag_color_map[t] = color_cycle[next_idx % len(color_cycle)]
                        next_idx += 1

            # Prepare rows with colored tags using the global map
            household_cycle = ["orange", "pink", "teal", "purple", "green", "blue"]
            household_color_map = {}
            household_idx = 0
            for r in rows:
                r["tag_items"] = [{"name": t, "cls": tag_color_map.get(t, color_cycle[0])} for t in (r.get("tags") or [])]
                household_raw = r.get("household_type") or ""
                household_parts = [p.strip() for p in re.split(r"[;]+", household_raw) if p.strip()]
                items = []
                for part in household_parts:
                    key = part.lower()
                    if key not in household_color_map:
                        household_color_map[key] = household_cycle[household_idx % len(household_cycle)]
                        household_idx += 1
                    items.append({"name": part, "cls": household_color_map[key]})
                r["household_items"] = items

            logging.info(
                "availability_earliest computed %d rows for rendering",
                len(rows),
            )
        else:
            logging.debug("availability_earliest compute flag not set; returning cached form")

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
        logging.error(f"Failed to compute earliest availability: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/availability/schedule")
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
                agreement_start_date = datetime(parsed_date.year, parsed_date.month, parsed_date.day, tzinfo=SYDNEY_TZ)
                default_date = agreement_start_date_param
            except ValueError:
                return jsonify({"error": "Invalid agreement_start_date format. Use YYYY-MM-DD"}), 400

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
        return (f"<p>Failed to load advisers: {e}</p>", 500, {"Content-Type": "text/html; charset=utf-8"})

    def pretty_name(email: str) -> str:
        local = (email or "").split("@")[0]
        return " ".join(part.capitalize() for part in local.replace(".", " ").replace("_", " ").split()) or email

    display_advisers = []
    for user in advisers:
        props = user.get("properties") or {}
        email = props.get("hs_email") or ""
        if not email:
            continue
        display_advisers.append({
            "email": email,
            "name": pretty_name(email),
            "service_packages": props.get("client_types") or "",
            "household_type": props.get("household_type") or "",
        })

    emails = sorted([item["email"] for item in display_advisers])

    selected = request.args.get("email")

    rows = []
    earliest_week = None
    if selected and compute:
        logging.info(
            "availability_schedule requested for %s (agreement_start_date=%s)",
            selected,
            agreement_start_date.isoformat() if agreement_start_date else "default",
        )
        try:
            res = compute_user_schedule_by_email(selected, agreement_start_date=agreement_start_date)
            capacity = res.get("capacity") or {}
            earliest_week = res.get("earliest_open_week")
            for wk in sorted(capacity.keys()):
                vals = capacity[wk]
                rows.append({
                    "wk_label": week_label_from_ordinal(wk),
                    "monday": date.fromordinal(wk).isoformat(),
                    "clarify": str(vals[0]) if len(vals) > 0 else "0",
                    "ooo": str(vals[2]) if len(vals) > 2 else "No",
                    "deals": str(vals[3]) if len(vals) > 3 else "0",
                    "target": str(vals[4]) if len(vals) > 4 else "0",
                    "actual": str(vals[5]) if len(vals) > 5 else "0",
                    "diff": str(vals[6]) if len(vals) > 6 else "0",
                    "is_earliest": isinstance(earliest_week, int) and wk == earliest_week,
                })
        except Exception as e:
            return (f"<p>Failed to compute schedule for {selected}: {e}</p>", 500, {"Content-Type": "text/html; charset=utf-8"})

    # Build the adviser dropdown options HTML (kept simple for template)
    option_items = ["<option value=\"\">-- Select adviser --</option>"]
    for entry in sorted(display_advisers, key=lambda item: item["name"].lower()):
        e = entry["email"]
        sel_attr = " selected" if selected == e else ""
        label = f"{entry['name']}"
        option_items.append(f"<option value=\"{e}\"{sel_attr}>{label}</option>")
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


@app.route("/availability/matrix")
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
        logging.error("Failed to build availability matrix: %s", e)
        return render_template_string(
            """
            <div style=\"padding: 20px; text-align: center;\">
                <h3>❌ Error Loading Availability Matrix</h3>
                <p>{{ error }}</p>
            </div>
            """,
            error=str(e),
        ), 500

# Healthcheck
@app.route("/_ah/warmup")
def warmup():
    """Healthcheck endpoint for platform warmup probes."""
    return ("", 200)

if __name__ == "__main__":
    from dotenv import load_dotenv

    # Load variables from .env into environment
    load_dotenv()
    app.run(host="0.0.0.0", debug=True, port=int(os.environ.get("PORT", "8080")))
