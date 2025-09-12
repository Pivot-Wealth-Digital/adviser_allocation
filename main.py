import os, time, secrets, json, re
from datetime import datetime, date
import logging

from urllib.parse import urlencode
from flask import Flask, redirect, request, session, jsonify, render_template, url_for
import requests

from allocate import (
    get_adviser,
    get_employee_leaves_from_firestore,
    get_employee_id_from_firestore,
    get_users_earliest_availability,
    get_users_taking_on_clients,
    compute_user_schedule_by_email,
    week_label_from_ordinal,
)

from dotenv import load_dotenv

# Load variables from .env into environment
load_dotenv()

from utils.secrets import get_secret
    

# Optional: persist tokens in Firestore (recommended on App Engine)
USE_FIRESTORE = os.environ.get("USE_FIRESTORE", "true").lower() == "true"
db = None
if USE_FIRESTORE:
    try:
        from google.cloud import firestore
        db = firestore.Client()  # Uses App Engine default credentials
    except Exception as e:
        # Fall back gracefully if Firestore is not available/configured
        logging.warning(f"Firestore client init failed, falling back to session store: {e}")
        db = None
        USE_FIRESTORE = False

app = Flask(__name__)
app.secret_key = get_secret("SESSION_SECRET") or "change-me-please"  # set in app.yaml or .env

# ---- Admin auth config (for managing closures) ----
ADMIN_USERNAME = get_secret("ADMIN_USERNAME") or os.environ.get("ADMIN_USERNAME")
ADMIN_PASSWORD = get_secret("ADMIN_PASSWORD") or os.environ.get("ADMIN_PASSWORD")

def is_admin():
    return bool(session.get("is_admin"))

def admin_required(view_func):
    from functools import wraps
    @wraps(view_func)
    def wrapper(*args, **kwargs):
        if not is_admin():
            # For API calls, return 401; for browser, redirect to login with next
            if request.accept_mimetypes and "text/html" in request.accept_mimetypes:
                nxt = request.path
                return redirect(f"/admin/login?next={nxt}")
            return jsonify({"error": "Unauthorized"}), 401
        return view_func(*args, **kwargs)
    return wrapper

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

# def load_tokens():
#     if USE_FIRESTORE and db:
#         doc = db.collection("eh_tokens").document(token_key()).get()
#         return doc.to_dict() if doc.exists else None
#     return session.get("eh_tokens")

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
    """Basic index route reporting service status and helpful routes."""
    return jsonify({
        "ok": True,
        "routes": ["/auth/start", "/auth/callback", "/test/organisations"]
    })

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
    print(f"{EH_AUTHORIZE_URL}?{urlencode(params)}")
    return redirect(f"{EH_AUTHORIZE_URL}?{urlencode(params)}")

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

    now_date = date.today()

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


def get_employee_leaves_from_firestore(employee_id):
    """
    Queries Firestore to find all leave requests for a given employee ID.
    
    Args:
        employee_id (str): The ID of the employee to search for.
        
    Returns:
        list: A list of dictionaries, where each dictionary is a leave request.
    """
    list_leaves = []
    
    try:
        # Get the collection reference and create the stream
        if not db:
            raise RuntimeError("Firestore is not configured; cannot read leave requests.")
        leaves_ref = db.collection('employees').document(employee_id).collection('leave_requests')
        docs = leaves_ref.stream()
        
        # Firestore returns a stream of documents, even if only one is expected
        for doc in docs:
            list_leaves.append(doc.to_dict())
            
    except Exception as e:
        # Log the error for internal debugging
        print(f"Firestore query failed: {e}")
        
    return list_leaves

    
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
        if not is_admin():
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
    if not is_admin():
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
@admin_required
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

    return render_template('closures_ui.html', closures=closures_data, today=date.today().isoformat(), closures_for_js=closures_for_js)


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    """Simple admin login to manage closures."""
    # If already logged in, go to UI or 'next'
    nxt = request.args.get("next") or "/closures/ui"
    if request.method == "POST":
        username = request.form.get("username") or ""
        password = request.form.get("password") or ""
        if not ADMIN_USERNAME or not ADMIN_PASSWORD:
            return ("<p>Admin credentials not configured. Set ADMIN_USERNAME and ADMIN_PASSWORD.</p>", 500)
        if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
            session["is_admin"] = True
            return redirect(nxt)
        # invalid
        error = "Invalid credentials"
    else:
        error = ""

    html = (
        "<html><head><title>Admin Login</title>"
        "<style>body{font-family:sans-serif;max-width:420px;margin:60px auto;padding:0 12px}.f{display:flex;flex-direction:column;gap:10px}label{font-size:.9em;color:#333}input{padding:8px;border:1px solid #bbb;border-radius:4px}button{padding:8px 12px;border:1px solid #0a7;background:#0a7;color:#fff;border-radius:4px;cursor:pointer}.err{color:#a00;margin-top:8px}</style>"
        "</head><body>"
        "<h3>Admin Login</h3>"
        f"<form class=\"f\" method=\"POST\" action=\"/admin/login?next={nxt}\">"
        "<div><label>Username</label><input name=\"username\" autocomplete=\"username\"></div>"
        "<div><label>Password</label><input name=\"password\" type=\"password\" autocomplete=\"current-password\"></div>"
        "<div><button type=\"submit\">Sign in</button></div>"
        f"<div class=\"err\">{error}</div>"
        "</form>"
        "</body></html>"
    )
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/admin/logout")
def admin_logout():
    session.pop("is_admin", None)
    return redirect("/admin/login")


# ---- assign adviser to open deal ----
@app.route('/post/allocate', methods=['POST', 'GET'])
def handle_webhook():
    """
    Endpoint to receive and handle the HubSpot webhook payload.
    """

    if request.method == 'GET':
        return {"message": "Hi, please use POST request."}, 200

    # Check for the correct Content-Type
    if not request.is_json:
        logging.error("Invalid Content-Type: Must be application/json")
        return jsonify({'message': 'Invalid Content-Type'}), 415

    try:
        # Get the JSON data from the request body
        print(request.json)
        event = request.json
        logging.info(f"Received event from HubSpot.")
        logging.info(event)
        
        print("Deal ID: ", event.get('fields', {}).get('hs_deal_record_id', ''))
        print("Service Package: ", event["fields"]["service_package"])
        
        # Check for 'deal' object type as this is for a deal workflow.
        if event.get('object', {}).get('objectType', ''):
            service_package = event["fields"]["service_package"]
            user = get_adviser(service_package)
            hubspot_owner_id = user["properties"]["hubspot_owner_id"]
            print(hubspot_owner_id)
            # hubspot_owner_id = '81859793'  
            deal_id = event.get('fields', {}).get('hs_deal_record_id', '')
            print(deal_id)

            try:
                deal_update_url = f"https://api.hubapi.com/crm/v3/objects/deals/{deal_id}"

                # Prepare the payload to update the deal owner
                payload = {
                    "properties": {
                        "advisor": hubspot_owner_id
                    }
                }

                # Make the PATCH request to update the deal
                if not HUBSPOT_TOKEN:
                    raise RuntimeError("HUBSPOT_TOKEN is not configured")
                response = requests.patch(deal_update_url, headers=HUBSPOT_HEADERS, data=json.dumps(payload), timeout=10)
                response.raise_for_status()

                logging.info(f"Successfully assigned deal ID {deal_id} to owner ID {hubspot_owner_id}.")
                print(f"Successfully assigned deal ID {deal_id} to owner ID {hubspot_owner_id}.")
            except requests.exceptions.HTTPError as http_err:
                logging.error(f"HTTP error during deal update: {http_err}")
                logging.error(f"Response content: {response.text}")
            except Exception as err:
                logging.error(f"An unexpected error occurred during deal update: {err}")
        
        # Return a success response. HubSpot expects a 200 OK.
        return jsonify({'message': 'Webhook received successfully'}), 200

    except Exception as e:
        logging.error(f"Failed to process webhook: {e}")
    return jsonify({'message': 'Internal Server Error'}), 500


## Removed /closures/ui_v2 and /availability/schedule_v2


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


# ---- Availability ----
@app.route("/availability/earliest")
def availability_earliest():
    """Uniform templated view of earliest availability with tags and topbar."""
    try:
        results = get_users_earliest_availability()
        results = sorted(results, key=lambda r: (r.get("email") or "").lower())

        # Build rows for template
        rows = []
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
            # Canonicalize to title case for consistency across rows
            tags = [t.title() for t in tags]
            rows.append({
                "email": item.get("email") or "",
                "tags": tags,
                "pod": item.get("pod_type") or "",
                "limit": item.get("client_limit_monthly") or "",
                "wk_label": item.get("earliest_open_week_label") or (item.get("error") or ""),
                "monday": monday_str,
            })

        # Enforce a consistent tag order across all rows
        preferred_order = [
            "Seed", "Series A", "Series B", "Series C", "Series D", "Series E", "Series F", "Series G", "Ipo"
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
        for r in rows:
            r["tag_items"] = [{"name": t, "cls": tag_color_map.get(t, color_cycle[0])} for t in (r.get("tags") or [])]

        return render_template(
            "availability_earliest.html",
            rows=rows,
            today=date.today().isoformat(),
            week_num=f"{date.today().isocalendar()[1]:02d}",
        )
    except Exception as e:
        logging.error(f"Failed to compute earliest availability: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/availability/schedule")
def availability_schedule():
    """UI to view an adviser's weekly schedule with shared layout/topbar."""
    try:
        advisers = get_users_taking_on_clients()
    except Exception as e:
        return (f"<p>Failed to load advisers: {e}</p>", 500, {"Content-Type": "text/html; charset=utf-8"})

    emails = sorted([(u.get("properties") or {}).get("hs_email") or "" for u in advisers if (u.get("properties") or {}).get("hs_email")])

    def pretty_name(email: str) -> str:
        local = (email or "").split("@")[0]
        return " ".join(part.capitalize() for part in local.replace(".", " ").replace("_", " ").split()) or email

    selected = request.args.get("email")

    rows = []
    earliest_week = None
    if selected:
        try:
            res = compute_user_schedule_by_email(selected)
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
    for e in emails:
        sel_attr = " selected" if selected == e else ""
        option_items.append(f"<option value=\"{e}\"{sel_attr}>{pretty_name(e)}</option>")
    options_html = "".join(option_items)

    return render_template(
        "availability_schedule.html",
        rows=rows,
        options_html=options_html,
        today=date.today().isoformat(),
        week_num=f"{date.today().isocalendar()[1]:02d}",
    )

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
