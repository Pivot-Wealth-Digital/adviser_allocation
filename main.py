import os, time, secrets, json
from datetime import datetime, date
import logging

from urllib.parse import urlencode
from flask import Flask, redirect, request, session, jsonify
import requests

from allocate import (
    get_adviser,
    get_employee_leaves_from_firestore,
    get_employee_id_from_firestore,
    get_users_earliest_availability,
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
    """Display earliest available week per adviser as an HTML table.

    Columns: Email | Service Packages | Earliest Open Week
    """
    try:
        results = get_users_earliest_availability()
        # Sort by email alphabetically (case-insensitive)
        results = sorted(results, key=lambda r: (r.get("email") or "").lower())

        # Build simple HTML table with sortable headers
        header_row = (
            "<tr>"
            "<th class=\"sortable\" data-col=\"0\" data-type=\"string\">Email <span class=\"indicator\">⇅</span></th>"
            "<th class=\"sortable\" data-col=\"1\" data-type=\"string\">Service Packages <span class=\"indicator\">⇅</span></th>"
            "<th class=\"sortable\" data-col=\"2\" data-type=\"string\">Pod Type <span class=\"indicator\">⇅</span></th>"
            "<th class=\"sortable\" data-col=\"3\" data-type=\"number\">Client Monthly Limit <span class=\"indicator\">⇅</span></th>"
            "<th class=\"sortable\" data-col=\"4\" data-type=\"string\">Earliest Open Week <span class=\"indicator\">⇅</span></th>"
            "<th class=\"sortable\" data-col=\"5\" data-type=\"date\">Monday Date <span class=\"indicator\">⇅</span></th>"
            "</tr>"
        )
        body_rows = []
        for item in results:
            earliest_wk_ordinal = item.get("earliest_open_week")
            monday_str = date.fromordinal(earliest_wk_ordinal).isoformat() if isinstance(earliest_wk_ordinal, int) else ""
            email = item.get("email") or ""
            svc = item.get("service_packages") or ""
            pod = item.get("pod_type") or ""
            limit = item.get("client_limit_monthly") or ""
            wk = item.get("earliest_open_week_label") or (item.get("error") or "")
            body_rows.append(f"<tr><td>{email}</td><td>{svc}</td><td>{pod}</td><td>{limit}</td><td>{wk}</td><td>{monday_str}</td></tr>")

        html = (
            "<html><head><title>Earliest Availability</title>"
            "<style>table{border-collapse:collapse}td,th{border:1px solid #ccc;padding:6px 10px;text-align:left;font-family:sans-serif}th.sortable{cursor:pointer;user-select:none}th.sortable:hover{background:#f5f5f5}th.sorted{background:#eef}th .indicator{margin-left:6px;opacity:.7;font-size:.9em}</style>"
            "</head><body>"
            "<h3>Adviser Earliest Availability</h3>"
            "<table id=\"avail-table\"><thead>" + header_row + "</thead><tbody>" + "".join(body_rows) + "</tbody></table>"
            "<script>(function(){const table=document.getElementById('avail-table');function getCell(tr,i){return tr.children[i]?.innerText||tr.children[i]?.textContent||''}function cmp(i,type,asc){return function(a,b){const v1=getCell(asc?a:b,i),v2=getCell(asc?b:a,i);if(type==='number'){return (parseFloat(v1)||0)-(parseFloat(v2)||0)}if(type==='date'){return new Date(v1)-new Date(v2)}return v1.localeCompare(v2)}}const headers=Array.from(document.querySelectorAll('#avail-table th.sortable'));function resetHeaders(active){headers.forEach(h=>{h.classList.remove('sorted');const ind=h.querySelector('.indicator');if(ind){ind.textContent='⇅';}if(h!==active){h.removeAttribute('data-asc');}});}headers.forEach(th=>{th.addEventListener('click',()=>{const i=parseInt(th.dataset.col||'0',10);const type=th.dataset.type||'string';const asc=th.dataset.asc!=='true';th.dataset.asc=asc?'true':'false';const tbody=table.tBodies[0];Array.from(tbody.querySelectorAll('tr')).sort(cmp(i,type,asc)).forEach(tr=>tbody.appendChild(tr));resetHeaders(th);th.classList.add('sorted');const ind=th.querySelector('.indicator');if(ind){ind.textContent=asc?'▲':'▼';}});});})();</script>"
            "</body></html>"
        )
        return html, 200, {"Content-Type": "text/html; charset=utf-8"}
    except Exception as e:
        logging.error(f"Failed to compute earliest availability: {e}")
        return jsonify({"error": str(e)}), 500

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
