# API Reference

## Production Webhooks (HubSpot Integration)

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/post/allocate` | POST | Assign deal to earliest-available adviser |
| `/post/create_box_folder` | POST | Create Box client folder from deal data |
| `/box/folder/tag/auto` | POST | Apply metadata to Box folder |

### POST /post/allocate

**Purpose:** Automatically assigns a HubSpot deal to the earliest-available adviser based on capacity, availability, and service package matching.

**Request Payload (from HubSpot workflow webhook):**
```json
{
  "fields": {
    "service_package": "Series A",
    "hs_deal_record_id": "123456",
    "household_type": "Single",
    "agreement_start_date": "2026-01-20"
  }
}
```

**Response:**
- ✅ Success: HTTP 200 with allocation details
- ❌ Error: HTTP 400/500 with error message

**Data Stored:**
- Allocation record in Firestore (`allocation_requests` collection)
- Deal owner updated in HubSpot
- Google Chat notification sent

**Documentation:** See [Architecture](../ARCHITECTURE.md#allocation-algorithm) for algorithm details

### POST /post/create_box_folder

**Purpose:** Creates a client folder in Box from HubSpot deal data.

**Request Payload:**
```json
{
  "fields": {
    "firstname": "John",
    "lastname": "Smith",
    "salutation": "Mr"
  }
}
```

**Response:**
- Folder ID and URL

**Documentation:** See [Box Integration](INTEGRATIONS.md#box-integration)

### POST /box/folder/tag/auto

**Purpose:** Automatically applies metadata to a Box folder by fetching missing data from HubSpot.

**Request Payload:**
```json
{
  "hs_deal_id": "123456",
  "box_folder_id": "789"
}
```

**Response:**
- Metadata applied successfully

**Documentation:** See [Box Integration](INTEGRATIONS.md#box-integration)

---

## Availability & Monitoring Endpoints

| Endpoint | Method | Purpose | Authentication |
|----------|--------|---------|-----------------|
| `/availability/earliest` | GET | Show earliest-available advisers for each service package | Public |
| `/availability/schedule?email=X&compute=1` | GET | View weekly capacity breakdown for specific adviser | Public |
| `/availability/meetings?email=X` | GET | List Clarify/Kick Off meetings for adviser | Public |
| `/allocations/history` | GET | Dashboard of allocation history with filters | Admin |

### GET /availability/earliest

**Query Parameters:**
- None (optional: `compute=1` to force fresh calculation)

**Response:**
Table showing all advisers with:
- Earliest available week for taking on clients
- Service packages supported
- Household types supported
- Current capacity utilization

### GET /availability/schedule?email=EMAIL&compute=1

**Query Parameters:**
- `email` (required) - Adviser email address
- `compute` (optional) - `1` to force recalculation

**Response:**
Weekly schedule breakdown:
- Clarify meetings count per week
- Out-of-office status
- Deal backlog
- Capacity utilization
- Highlighted earliest available week

### GET /availability/meetings?email=EMAIL

**Query Parameters:**
- `email` (required) - Adviser email address

**Response:**
List of Clarify/Kick Off meetings with:
- Date and time
- Meeting title
- Outcome/status
- HubSpot links

### GET /allocations/history

**Query Parameters:**
- `status` - Filter by allocation status
- `adviser` - Filter by adviser email
- `date_from` / `date_to` - Date range filter
- `page` - Pagination

**Response:**
Allocation history dashboard with:
- Filtered allocation records
- Analytics widgets (status breakdown, top service packages, etc.)
- Pagination controls

---

## Data Sync Endpoints

| Endpoint | Method | Purpose | Frequency |
|----------|--------|---------|-----------|
| `/sync/employees` | GET/POST | Sync employee data from Employment Hero | Weekly (Mondays) |
| `/sync/leave_requests` | GET/POST | Sync leave requests from Employment Hero | Weekdays |

### GET /sync/employees

**Purpose:** Fetches all employees from Employment Hero and stores in Firestore.

**Trigger:** Cloud Scheduler (weekly Mondays @ 1:00 PM AEDT)

**Response:**
- Number of employees synced
- Timestamp
- Status

**Data Stored:**
- Firestore `employees` collection
- Fields: id, full_name, company_email, account_email, organisation_id

### GET /sync/leave_requests

**Purpose:** Fetches future approved leave requests from Employment Hero and stores in Firestore.

**Trigger:** Cloud Scheduler (weekdays @ 1:00 PM AEDT)

**Response:**
- Number of leave requests synced
- Timestamp
- Status

**Data Stored:**
- Firestore `employees/{id}/leave_requests` subcollection
- Fields: start_date, end_date, type, status

---

## Admin UI Endpoints (Protected)

| Endpoint | Purpose | Authentication |
|----------|---------|-----------------|
| `/closures/ui` | Manage office closures (holidays) | Admin |
| `/capacity_overrides/ui` | Manage adviser capacity overrides | Admin |
| `/employees/ui` | View employee directory | Admin |
| `/leave_requests/ui` | Calendar view of upcoming leave | Admin |
| `/box/create` | Box folder creation UI | Admin |

### GET/POST /closures/ui

**Purpose:** Admin interface to manage global office closures.

**API Methods:**
- `GET /closures` - List all closures
- `POST /closures` - Add new closure
- `PUT /closures/<id>` - Update closure
- `DELETE /closures/<id>` - Delete closure

**Request Body (POST/PUT):**
```json
{
  "start_date": "2026-12-25",
  "end_date": "2026-12-27",
  "description": "Christmas Break",
  "tags": ["public", "office"]
}
```

**Data Stored:**
- Firestore `office_closures` collection

### GET/POST /capacity_overrides/ui

**Purpose:** Admin interface to set adviser-specific capacity limits.

**Request Body:**
```json
{
  "adviser_email": "john@example.com",
  "client_limit_monthly": 5,
  "effective_date": "2026-01-20",
  "pod_type": "Solo Adviser",
  "notes": "Reduced capacity due to mentoring duties"
}
```

**Data Stored:**
- Firestore `adviser_capacity_overrides` collection

### GET /employees/ui

**Purpose:** View all synced employees from Employment Hero.

### GET /leave_requests/ui

**Purpose:** Calendar view of approved leave requests across all employees.

### GET /box/create

**Purpose:** UI to manually create Box client folders.

---

## Authentication Endpoints

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/login` | GET/POST | Admin login page (session-based) |
| `/auth/start` | GET | Start Employment Hero OAuth flow |
| `/auth/callback` | GET | OAuth callback handler |

### GET /auth/start

**Purpose:** Initiates Employment Hero OAuth 2.0 flow.

**Redirect Flow:**
1. User visits `/auth/start`
2. Redirects to Employment Hero authorization endpoint
3. User logs in and authorizes
4. Returns to `/auth/callback`

### GET /auth/callback

**Query Parameters:**
- `code` - Authorization code from Employment Hero

**Process:**
1. Exchanges code for access token
2. Stores tokens in Firestore (`eh_tokens` collection)
3. Redirects to dashboard

**Token Storage:**
- Firestore `eh_tokens` collection with:
  - `access_token` - Current access token
  - `refresh_token` - Token for refreshing
  - `expires_at` - Expiration timestamp

---

## Error Responses

All endpoints return standard error responses:

```json
{
  "error": "Error message describing what went wrong",
  "status": 400,
  "timestamp": "2026-01-20T10:30:00Z"
}
```

**Common Status Codes:**
- `200` - Success
- `400` - Bad request (invalid parameters)
- `401` - Unauthorized (missing/invalid auth)
- `403` - Forbidden (insufficient permissions)
- `404` - Not found
- `500` - Server error

---

## Rate Limiting

**Default:** 50 requests per hour

**Headers:**
- `X-RateLimit-Limit` - Total requests allowed
- `X-RateLimit-Remaining` - Requests remaining
- `X-RateLimit-Reset` - Unix timestamp when limit resets

When exceeded: HTTP 429 (Too Many Requests)

---

## Webhook Configuration (HubSpot)

To set up webhooks in HubSpot:

1. Go to HubSpot Workflows
2. Create new workflow
3. Add webhook action
4. Configure URL: `https://[your-app-url]/post/allocate`
5. Method: `POST`
6. Include deal fields: `service_package`, `hs_deal_record_id`, `household_type`

**Security:** Webhooks are unauthenticated but expect valid HubSpot payload structure.
