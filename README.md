# Adviser Allocation Service

A Flask app that integrates Employment Hero (HR) and HubSpot to:

- Authenticate via Employment Hero OAuth and fetch employees and leave requests.
- Store employee and leave data in Firestore for fast lookup.
- Allocate HubSpot deals to the earliest-available adviser based on meetings, capacity, and leave.


## Overview

- `main.py`: Flask app, Employment Hero OAuth, data sync routes, and a HubSpot webhook for allocation.
- `allocate.py`: Core allocation logic and HubSpot queries (users, meetings, deals).
- `services/` and `routes/`: Present but currently unused; future place for refactors.

Firestore is used as the persistent store when configured. When Firestore is not configured or fails to initialize, the app degrades gracefully for OAuth token storage but will not persist employee/leave data (relevant endpoints respond with clear errors).


## Requirements

- Python 3.10+
- Google Cloud credentials if using Firestore (Application Default Credentials)
- HubSpot Private App token with required scopes to read Users, Meetings, and Deals and update Deals

Install dependencies:

```bash
pip install -r requirements.txt
```


## Environment Variables

- `SESSION_SECRET`: Flask session secret (any random string)
- `USE_FIRESTORE`: `true`/`false` to enable Firestore (default `true`)
- `EH_AUTHORIZE_URL`: Employment Hero authorize URL (default provided)
- `EH_TOKEN_URL`: Employment Hero token URL (default provided)
- `EH_CLIENT_ID`: Employment Hero OAuth Client ID
- `EH_CLIENT_SECRET`: Employment Hero OAuth Client Secret
- `EH_SCOPES`: EH scopes. Example defaults: `urn:mainapp:organisations:read urn:mainapp:employees:read urn:mainapp:leave_requests:read`
- `REDIRECT_URI`: Public callback URL for OAuth (e.g., `https://<PROJECT-ID>.appspot.com/auth/callback` or local tunnel)
- `HUBSPOT_TOKEN`: HubSpot Private App token (Bearer token)
- `PORT`: Optional port (default `8080`)
- `PRESTART_WEEKS`: Weeks before an adviser's `adviser_start_date` that they can receive allocations (default `3`).

Place them in `.env` for local dev.


## Running Locally

```bash
export FLASK_APP=main.py
python main.py
# or
flask run -p 8080
```

If using Firestore locally, ensure ADC is configured:

```bash
gcloud auth application-default login
```


## Employment Hero OAuth Flow

1. Visit `http://localhost:8080/auth/start` to begin OAuth.
2. After callback (`/auth/callback`), tokens are saved (Firestore preferred; session fallback).
3. You can then call protected routes that fetch data from EH.


## Data Sync Endpoints

These should run on a schedule (cron/job) rather than on-demand by user requests:

- `GET/POST /sync/employees`: Fetch employees from EH and persist into Firestore.
- `GET/POST /sync/leave_requests`: Fetch future approved leave requests from EH and persist under each employee.

Manual fetch endpoints (return data and persist):

- `GET /get/employees`
- `GET /get/leave_requests`

Lookup endpoints:

- `GET /get/employee_id?email=<email>` → `{ employee_id }`
- `GET /get/employee_leave_requests?employee_id=<id>` → `{ leave_requests: [...] }`
- `GET /get/leave_requests_by_email?email=<email>` → reads from Firestore only; instructs to run sync if missing.

### Global Holidays / Office Closures

To block weeks for everyone (e.g., Christmas shutdown), add documents to the Firestore collection `office_closures` with fields:

- `start_date`: ISO date `YYYY-MM-DD`
- `end_date`: ISO date `YYYY-MM-DD` (optional; defaults to `start_date`)
- `description`: short text explaining the closure
- `tags`: optional list of short labels (e.g., `["public", "office"]`), also accepted as a comma-separated string

The system classifies each affected week as `Full` (5 business days) or `Partial: N` and folds this into adviser availability the same way personal leave is handled. Weeks classified as `Full` set target capacity to 0 for all advisers.

Endpoints to manage closures:
- `GET /closures` → List current closures from `office_closures`.
- `POST /closures` (JSON) → Add a closure:
  - Body: `{ "start_date": "YYYY-MM-DD", "end_date": "YYYY-MM-DD", "description": "...", "tags": ["..."] }` (`end_date` optional; `tags` optional)
- `PUT /closures/<id>` (JSON) → Update a closure. Accepts `description`, `tags`, and dates.
- `DELETE /closures/<id>` → Delete a closure.
- `GET /closures/ui` → Admin UI to add and manage closures.

Admin UI highlights:
- Topbar “Today” picker
- Add Closure card with live workdays (Mon–Fri) and a Tags input (with quick tag chips) before Description
- Closures table with:
  - Columns: `#`, `Description` (shows tags as badges above text), `Start Date`, `End Date`, `Workdays`, `Actions`
  - Inline edit: edit tags and description in-row

Admin authentication:
- Set `ADMIN_USERNAME` and `ADMIN_PASSWORD` (env or Secret Manager) to enable admin login.
- `GET/POST /admin/login` → Sign in to manage closures.
- `GET /admin/logout` → Sign out.
- The UI (`/closures/ui`) and modifying endpoints (`POST /closures`, `PUT/DELETE /closures/<id>`) require admin.


## HubSpot Allocation Webhook

- Endpoint: `POST /post/allocate`
- Expected payload: HubSpot workflow webhook for a Deal that includes `fields.service_package` and `fields.hs_deal_record_id`.
- Behavior:
  - Finds eligible advisers (HubSpot Users) taking on clients for the given `service_package`.
  - Pulls each adviser’s recent meetings, deals that have no Clarify booked, and EH leave; then computes capacity and earliest availability.
  - Assigns the Deal to the adviser with the earliest open week (sets HubSpot owner on the Deal).

### Allocation Logic (Summary)

- Weeks are keyed by Monday ordinal and printed as `YYYY-Www` (ISO week) for clarity.
- Earliest week never occurs within 2 weeks of “now” (buffer enforced).
- Future starters can receive allocations up to `PRESTART_WEEKS` before their start date.
- Capacity projection covers at least 52 weeks ahead to avoid premature cutoffs.
- Deals without a Clarify are allocated before picking the earliest week:
  - Initialize backlog with all such deals prior to the baseline week.
  - Walk forward in non-overlapping fortnights. For each 2-week block:
    - Add new deals from the block to the backlog.
    - Compute spare capacity = fortnight target − clarifies(block).
    - Consume the backlog by the spare (no double counting).
  - The earliest available week is the first block where backlog falls to zero (clamped to the 2‑week buffer).
- Fortnight target is derived from `client_limit_monthly / 2` (new advisers or solo pods may have a lower limit).

This approach prevents double counting and keeps allocations within target per fortnight.

Example cURL (replace placeholders):

```bash
curl -X POST http://localhost:8080/post/allocate \
  -H "Content-Type: application/json" \
  -d '{
        "object": {"objectType": "deal"},
        "fields": {
          "service_package": "<SERVICE_PACKAGE>",
          "hs_deal_record_id": "<DEAL_ID>"
        }
      }'
```


## Other Useful Routes

- `GET /` → Basic status and route list
- `GET /test/organisations` → EH organisations (requires OAuth)
- `GET /get/leave_requests_list` → Raw EH leave requests listing
- `GET /availability/earliest` → HTML table of earliest week availability for all advisers taking on clients. Columns: Email, Service Packages, Pod Type, Client Monthly Limit, Earliest Open Week.
- `GET /availability/schedule` → UI to pick an adviser by email and view a weekly schedule table (Week label, Monday Date, Clarify Count, OOO, Deal No Clarify, Target, Actual). Highlights the earliest available week.
- `GET /_ah/warmup` → Healthcheck


## Scheduling (Recommended)

Run these on a schedule to keep Firestore fresh:

- Every 6–12 hours: `POST /sync/employees`
- Every 30–60 minutes (business hours): `POST /sync/leave_requests`

Example App Engine `cron.yaml` (if deployed on GAE):

```yaml
cron:
- description: sync employees
  url: /sync/employees
  schedule: every 12 hours

- description: sync leave requests
  url: /sync/leave_requests
  schedule: every 1 hours
```


## Troubleshooting

- Missing HUBSPOT token: Many HubSpot calls will raise "HUBSPOT_TOKEN is not configured".
- Firestore not configured: Data sync/lookup endpoints that persist/read will error clearly. Use `USE_FIRESTORE=false` for local-only OAuth testing.
- OAuth state mismatch: Clear cookies or restart flow from `/auth/start`.
- Employment Hero: Ensure `REDIRECT_URI` exactly matches your app’s registered URI.

Logs to watch:
- Allocation steps (per adviser) and capacity table output in the server logs.
- Firestore init warnings (fall back to session when Firestore isn’t available).

Common allocation questions:
- “Earliest week is too soon”: The 2‑week buffer clamps earliest possible week; ensure system time and timezone are correct.
- “Earliest week is far in the future”: Usually reflects backlog vs. target. Verify deals-without-clarify volume and adviser target; the system projects at least 52 weeks ahead and consumes backlog per fortnight target.
- “All advisers show the same week”: Ensure inputs differ (meetings, leaves, deals). The search starts from the first capacity key on/after the buffered week, not index 0.


## Notes And Future Work

- Firestore helpers are duplicated in `main.py` and `allocate.py`. Consider moving them to `services/firestore_service.py` and importing from there.
- Consider adding retry/backoff on HubSpot API calls.
- `services/` and `routes/` scaffolds are empty and can be used for a cleaner structure.
- Add tests for date helpers (`get_monday_from_weeks_ago`, `classify_leave_weeks`) and capacity computations.


## License

Internal project. No license specified.
