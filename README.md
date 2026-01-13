# Adviser Allocation Service

A production Flask app that allocates HubSpot deals to advisers based on capacity and availability.

**Key Features:**
- Employment Hero OAuth integration for employee and leave data
- Firestore-backed persistence for fast lookup
- Intelligent allocation algorithm (earliest-available adviser)
- Rate limiting, automatic retries, TTL-based caching
- 65 tests (100% pass rate), E2E coverage with Playwright

**Deployment:** Live at https://pivot-digital-466902.ts.r.appspot.com (CI/CD via Cloud Build)


## Requirements

- Python 3.12+ (for deployment; local dev supports 3.10+)
- Google Cloud credentials if using Firestore (Application Default Credentials)
- HubSpot Private App token with required scopes to read Users, Meetings, and Deals and update Deals

Install dependencies:

```bash
pip install -r requirements.txt
```

Install test dependencies:

```bash
pip install -r requirements-test.txt
```


## Configuration

Required environment variables (via `.env` or Secret Manager):

| Variable | Purpose | Example |
|----------|---------|---------|
| `EH_CLIENT_ID` | Employment Hero OAuth | (from EH) |
| `EH_CLIENT_SECRET` | Employment Hero OAuth secret | (from EH) |
| `HUBSPOT_TOKEN` | HubSpot API token | `pat-...` |
| `REDIRECT_URI` | OAuth callback URL | `https://app.example.com/auth/callback` |
| `BOX_JWT_CONFIG_JSON` | Box service account config | (JSON in Secret Manager) |
| `SESSION_SECRET` | Flask session encryption | (any random string) |

Optional:
- `USE_FIRESTORE`: Enable Firestore (default `true`)
- `BOX_IMPERSONATION_USER`: Box user email to impersonate
- `PRESTART_WEEKS`: Adviser start buffer (default `3` weeks)
- `PORT`: Server port (default `8080`)


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

### Running Tests Locally

```bash
pytest --verbose
# with coverage
pytest --cov=. --cov-report=term-missing
```


## CI/CD Pipeline

**Cloud Build** automatically tests and deploys on every push:

```
Git Push → Run 65 Tests → IF PASS → Deploy to App Engine ✅
                       → IF FAIL → Block Deployment ❌
```

- **Testing Gate**: Deployment blocked if any test fails (prevents bad code in production)
- **Build Time**: ~1-3 minutes (with caching)
- **Current Status**: 9/10 score (see [CI_CD_SUMMARY.md](CI_CD_SUMMARY.md) for details)

Monitor builds:
```bash
gcloud builds list --limit=10
gcloud builds log BUILD_ID --stream
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

### Allocation Algorithm

Finds the earliest week an adviser can take the deal by:
1. Checking capacity constraints (meetings, leave, deal backlog)
2. Respecting a 2-week buffer before allocating
3. Projecting 52 weeks ahead to avoid bottlenecks

See [OPTIMIZATION_SUMMARY.md](OPTIMIZATION_SUMMARY.md) for technical details.


## Other Useful Routes

- `GET /` → Basic status and route list
- `GET /test/organisations` → EH organisations (requires OAuth)
- `GET /get/leave_requests_list` → Raw EH leave requests listing
- `POST /post/create_box_folder` → HubSpot-triggered Box client folder creation (contact-based naming, template copy)
- `GET /availability/earliest` → HTML table of earliest week availability for all advisers taking on clients. Columns: Email, Service Packages, Pod Type, Client Monthly Limit, Earliest Open Week.
- `GET /availability/schedule` → UI to pick an adviser by email and view a weekly schedule table (Week label, Monday Date, Clarify Count, OOO, Deal No Clarify, Target, Actual). Highlights the earliest available week.
- `GET /_ah/warmup` → Healthcheck


## Box Integration

- `POST /post/create_box_folder` provisions client folders (from HubSpot workflows)
- Credentials stored in Secret Manager (`BOX_JWT_CONFIG_JSON`), not in code
- Local dev: Set `BOX_JWT_CONFIG_PATH` to `config/box_jwt_config.json` (git-ignored)


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

| Issue | Solution |
|-------|----------|
| OAuth fails | Verify `REDIRECT_URI` matches EH config exactly |
| Firestore errors | Set `USE_FIRESTORE=false` for local OAuth testing only |
| Allocation too early/late | Check system time, timezone, and adviser meeting data |
| Test failures | Run `pytest --verbose` to see full output; check Cloud Build logs for CI issues |


## Architecture

**Core Modules:**
- `core/allocation.py` - Deal allocation algorithm (capacity, availability)
- `services/oauth_service.py` - OAuth token lifecycle management
- `utils/http_client.py` - HTTP requests with auto-retry (3x, exponential backoff)
- `utils/cache_utils.py` - TTL-based caching (replaces indefinite @lru_cache)
- `middleware/rate_limiter.py` - Rate limiting (50 req/hour default)
- `utils/firestore_helpers.py` - Firestore CRUD operations

**Infrastructure:**
- **Database**: Firestore (employees, leaves, closures, capacity overrides)
- **APIs**: Employment Hero, HubSpot, Box (with automatic retries)
- **Testing**: 65 tests (unit + E2E with Playwright), 100% pass rate
- **Deployment**: Google App Engine with Cloud Build CI/CD

## Documentation

- [OPTIMIZATION_SUMMARY.md](OPTIMIZATION_SUMMARY.md) - Full optimization details and refactoring history
- [CI_CD_SUMMARY.md](CI_CD_SUMMARY.md) - CI/CD pipeline explanation (9/10 score)
- [DEPLOYMENT_VERIFICATION.md](DEPLOYMENT_VERIFICATION.md) - How to verify deployment and monitor health

## License

Internal project.
