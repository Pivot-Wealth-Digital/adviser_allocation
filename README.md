# Adviser Allocation Service

A production Flask app that allocates HubSpot deals to advisers based on capacity and availability.

**Key Features:**
- Employment Hero OAuth integration for employee and leave data
- Firestore-backed persistence for fast lookup
- Intelligent allocation algorithm (earliest-available adviser)
- Rate limiting, automatic retries, TTL-based caching
- 65 tests (100% pass rate), E2E coverage with Playwright

**Deployment:** Live at https://pivot-digital-466902.ts.r.appspot.com (CI/CD via Cloud Build)


## Quick Facts

| What | Details |
|------|---------|
| **Deployed At** | https://pivot-digital-466902.ts.r.appspot.com (Google App Engine) |
| **Database** | Google Cloud Firestore |
| **Integrations** | Employment Hero (HR), HubSpot (CRM), Box (Storage) |
| **Authentication** | Employment Hero OAuth + Session-based admin |
| **CI/CD** | Cloud Build (auto-deploy on push, 65 tests as gate) |
| **Secrets** | Google Secret Manager |


## Features & Capabilities

### 🎯 Allocation System
- Automatically assigns HubSpot deals to advisers based on earliest availability
- Considers capacity limits, meetings, leave requests, service packages
- Respects 2-week buffer and 52-week projection
- Sends Google Chat notifications on allocation
- Tracks allocation history with analytics dashboard

### 👥 HR Integration (Employment Hero)
- OAuth-based employee data sync
- Leave request tracking (future approved only)
- Automated sync endpoints for scheduling
- Employee directory with search

### 📊 Availability Management
- Real-time adviser availability calculation
- Weekly capacity schedule breakdown
- Meeting tracking (Clarify/Kick Off)
- Service package and household type matrix
- Capacity override management
- Office closure/holiday tracking

### 📁 Box Document Management
- Automated client folder creation from HubSpot workflows
- Metadata tagging (service package, deal info, contacts)
- Client sharing automation
- Metadata compliance scanning and repair
- Collaborator management

### 🔐 Admin & Configuration
- Office closure management (public holidays)
- Adviser capacity overrides
- Session-based authentication
- Allocation history dashboard


## Key Endpoints

### Production Webhooks (HubSpot Integration)

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/post/allocate` | POST | Assign deal to earliest-available adviser |
| `/post/create_box_folder` | POST | Create Box client folder from deal data |
| `/box/folder/tag/auto` | POST | Apply metadata to Box folder |

### Availability & Monitoring

| Endpoint | Purpose |
|----------|---------|
| `/availability/earliest` | Show earliest-available advisers for each service package |
| `/availability/schedule?email=X&compute=1` | View weekly capacity for specific adviser |
| `/availability/meetings?email=X` | List Clarify/Kick Off meetings for adviser |
| `/allocations/history` | Dashboard of allocation history with filters |

### Data Sync (Scheduler-Friendly)

| Endpoint | Purpose |
|----------|---------|
| `/sync/employees` | Sync employee data from Employment Hero |
| `/sync/leave_requests` | Sync leave requests from Employment Hero |

### Admin UI (Protected)

| Endpoint | Purpose |
|----------|---------|
| `/closures/ui` | Manage office closures (holidays) |
| `/capacity_overrides/ui` | Manage adviser capacity overrides |
| `/employees/ui` | View employee directory |
| `/leave_requests/ui` | Calendar view of upcoming leave |
| `/box/create` | Box folder creation UI |

### Authentication

| Endpoint | Purpose |
|----------|---------|
| `/login` | Admin login page |
| `/auth/start` | Start Employment Hero OAuth flow |
| `/auth/callback` | OAuth callback handler |


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


## Infrastructure & Storage

### Database (Firestore)

Collections:
- `employees` - Employee data from Employment Hero
- `employees/{id}/leave_requests` - Leave requests per employee
- `office_closures` - Global holidays/office closures
- `adviser_capacity_overrides` - Manual capacity limits
- `allocation_requests` - Allocation history and analytics
- `eh_tokens` - OAuth tokens

### External APIs

- **Employment Hero** - OAuth employee/leave data (https://api.employmenthero.com)
- **HubSpot** - CRM data and deal allocation (Portal ID: 47011873)
- **Box** - Document storage and metadata (Enterprise 260686117)
- **Google Chat** - Allocation notifications

### Secrets (Secret Manager)

- `EH_CLIENT_ID`, `EH_CLIENT_SECRET` - Employment Hero OAuth
- `HUBSPOT_TOKEN` - HubSpot API access
- `BOX_JWT_CONFIG_JSON` - Box service account
- `SESSION_SECRET` - Flask session encryption


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


## Global Holidays / Office Closures

To block weeks for everyone (e.g., Christmas shutdown), add documents to the Firestore collection `office_closures` with fields:

- `start_date`: ISO date `YYYY-MM-DD`
- `end_date`: ISO date `YYYY-MM-DD` (optional; defaults to `start_date`)
- `description`: short text explaining the closure
- `tags`: optional list of short labels (e.g., `["public", "office"]`), also accepted as a comma-separated string

The system classifies each affected week as `Full` (5 business days) or `Partial: N` and folds this into adviser availability the same way personal leave is handled. Weeks classified as `Full` set target capacity to 0 for all advisers.

### Management Endpoints

- `GET /closures` → List current closures from `office_closures`.
- `POST /closures` (JSON) → Add a closure:
  - Body: `{ "start_date": "YYYY-MM-DD", "end_date": "YYYY-MM-DD", "description": "...", "tags": ["..."] }` (`end_date` optional; `tags` optional)
- `PUT /closures/<id>` (JSON) → Update a closure. Accepts `description`, `tags`, and dates.
- `DELETE /closures/<id>` → Delete a closure.
- `GET /closures/ui` → Admin UI to add and manage closures.

### Admin UI

- Topbar "Today" picker
- Add Closure card with live workdays (Mon–Fri) and a Tags input (with quick tag chips) before Description
- Closures table with:
  - Columns: `#`, `Description` (shows tags as badges above text), `Start Date`, `End Date`, `Workdays`, `Actions`
  - Inline edit: edit tags and description in-row

### Authentication

- Set `ADMIN_USERNAME` and `ADMIN_PASSWORD` (env or Secret Manager) to enable admin login.
- `GET/POST /admin/login` → Sign in to manage closures.
- `GET /admin/logout` → Sign out.
- The UI (`/closures/ui`) and modifying endpoints (`POST /closures`, `PUT/DELETE /closures/<id>`) require admin.


## HubSpot Allocation Webhook

- Endpoint: `POST /post/allocate`
- Expected payload: HubSpot workflow webhook for a Deal that includes `fields.service_package` and `fields.hs_deal_record_id`.
- Behavior:
  - Finds eligible advisers (HubSpot Users) taking on clients for the given `service_package`.
  - Pulls each adviser's recent meetings, deals that have no Clarify booked, and EH leave; then computes capacity and earliest availability.
  - Assigns the Deal to the adviser with the earliest open week (sets HubSpot owner on the Deal).

### Allocation Algorithm

Finds the earliest week an adviser can take the deal by:
1. Checking capacity constraints (meetings, leave, deal backlog)
2. Respecting a 2-week buffer before allocating
3. Projecting 52 weeks ahead to avoid bottlenecks

See [OPTIMIZATION_SUMMARY.md](OPTIMIZATION_SUMMARY.md) for technical details.


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
