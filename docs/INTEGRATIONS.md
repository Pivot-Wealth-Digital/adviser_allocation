# Integrations Guide

## HubSpot CRM Integration

### Overview

**Portal ID:** `47011873`

HubSpot is the primary CRM system. The application:
- Reads deal data (service package, household type, contact info)
- Reads adviser profiles (Users object)
- Reads meeting data (Clarify and Kick Off meetings)
- Updates deal owner (allocates adviser)
- Stores Box folder URLs in custom properties

### Authentication

**Method:** Private App Token

**Required Scopes:**
- `crm.objects.deals.read` - Read deal data
- `crm.objects.deals.write` - Update deal owner
- `crm.objects.contacts.read` - Read contact information
- `crm.objects.users.read` - Read adviser profiles
- `crm.objects.custom_objects.read` - Read custom fields
- `crm.schemas.deals.read` - Read deal schema

**Setup:** See [Configuration Guide](CONFIGURATION.md#hubspot-configuration)

### Data Synced (via Cloud Scheduler)

**Sync Jobs:**
- `hubspot-sync-users-daily` - Daily @ 1:00 PM AEDT
- `hubspot-sync-deals-daily` - Daily @ 1:30 PM AEDT
- `hubspot-sync-meetings-daily` - Daily @ 2:00 PM AEDT
- `hubspot-sync-contacts-daily` - Daily @ 1:15 PM AEDT
- `hubspot-sync-companies-daily` - Daily @ 1:45 PM AEDT

**Data Retrieved:**
- **Deals:** ID, name, service package, household type, client name, client email
- **Users (Advisers):** ID, name, email, service packages supported, household types supported
- **Meetings:** Meeting date, time, title, attendees, outcome, deal association
- **Contacts:** Name, email, phone, company, properties

### Webhook Configuration

HubSpot workflows can trigger webhooks to the application:

#### Setup Webhook in HubSpot

1. Go to **HubSpot Workflows**
2. Create new workflow
3. Set trigger: Deal created or updated
4. Add action: **Webhook**
5. Configure:
   - **URL:** `https://pivot-digital-466902.ts.r.appspot.com/post/allocate`
   - **Method:** `POST`
   - **Payload:** Include deal fields:
     - `service_package`
     - `hs_deal_record_id`
     - `household_type` (if applicable)
     - `agreement_start_date` (if applicable)

#### Webhook Payload Structure

```json
{
  "fields": {
    "hs_deal_record_id": "123456789",
    "dealname": "Acme Corp - Series A",
    "service_package": "Series A",
    "household_type": "Single",
    "agreement_start_date": "2026-02-01",
    "client_email": "client@acme.com"
  }
}
```

#### Webhook Response

**Success (HTTP 200):**
```json
{
  "status": "success",
  "allocation": {
    "deal_id": "123456789",
    "adviser_email": "john@example.com",
    "earliest_available_week": "2026-02-03"
  }
}
```

**Error (HTTP 400/500):**
```json
{
  "error": "No available advisers for this service package",
  "status": 400
}
```

### Custom Properties

The application updates these HubSpot properties:

- **`box_folder_url`** - Link to Box client folder (set by Box integration)
- **Deal Owner** - Allocated adviser (set by allocation algorithm)

---

## Employment Hero Integration

### Overview

**Organization ID:** Configured during OAuth

Employment Hero is the HR system for employee and leave data:
- Syncs employee information (names, emails)
- Syncs approved leave requests (dates, types)
- Provides out-of-office (OOO) status for capacity calculations

### Authentication

**Method:** OAuth 2.0

**Flow:**
1. User visits `/auth/start`
2. Redirected to Employment Hero authorization
3. User authorizes application
4. Callback with authorization code
5. Code exchanged for access and refresh tokens
6. Tokens stored in Firestore (`eh_tokens` collection)
7. Tokens automatically refreshed before expiry

**Token Management:**
- Stored: Firestore `eh_tokens` collection
- Auto-refresh: 60 seconds before expiry
- Fallback: Session storage (local dev only)

### Setup OAuth

1. **Register App:** Log in to Employment Hero → API Portal
2. **Get Credentials:**
   - Client ID
   - Client Secret
   - Note: Authorized Redirect URI
3. **Configure:** See [Configuration Guide](CONFIGURATION.md#employment-hero-oauth-setup)

### Data Synced (via Cloud Scheduler)

**Sync Jobs:**
- `eh-employees-sync-daily` - Weekly Mondays @ 1:00 PM AEDT
- `eh-leave-requests-sync-daily` - Weekdays @ 1:00 PM AEDT

**Frequency:**
- Employees: Once weekly (Monday)
- Leave Requests: Business days (Mon-Fri)

**Data Retrieved:**

**Employees Collection:**
- Employee ID
- Full name
- Company email
- Account email
- Organisation ID

**Leave Requests Subcollection** (`employees/{id}/leave_requests`):
- Leave request ID
- Start date
- End date
- Leave type (sick, annual, unpaid, etc.)
- Status (approved, pending, rejected)
- Notes

### Usage in Allocation

Leave requests are used to:
1. Identify weeks adviser is unavailable (OOO)
2. Reduce available capacity for those weeks
3. Factor into earliest-available calculation

**Example:**
- Adviser John has approved leave: Feb 3-7, 2026
- Allocation algorithm excludes week of Feb 3 from availability
- Looks for next available week after leave ends

---

## Box Integration

### Overview

**Enterprise ID:** `260686117`

Box is the document storage system for client folders:
- Creates client folders from deal data
- Applies metadata (contact info, household type, service package)
- Shares folders with clients
- Tracks metadata status

### Authentication

**Method:** JWT Service Account

**Credentials:** JSON configuration from Box

**Scopes:**
- Manage documents, files, folders
- Read all files
- Collaborator management

**Setup:** See [Configuration Guide](CONFIGURATION.md#box-configuration)

### Workflows

#### 1. Folder Creation

**Trigger:** HubSpot workflow webhook to `/post/create_box_folder`

**Process:**
1. Receive deal data (client name, contact info)
2. Copy Box template folder
3. Rename to: `[First Name] [Last Name]`
4. Handle naming conflicts (add suffix: (2), (3), etc.)
5. Return folder ID and URL to HubSpot

**Template Folder:**
- Path: `/Templates/ClientTemplate` (configurable)
- Contains: Subfolders and initial documents for new clients
- Copied entire structure to client-specific folder

#### 2. Metadata Tagging

**Trigger:** HubSpot webhook to `/box/folder/tag/auto` or `/box/folder/tag`

**Process:**
1. Fetch deal data from HubSpot (if not provided)
2. Build metadata dictionary:
   - `primary_contact_id` - HubSpot contact ID
   - `spouse_contact_id` - Spouse contact ID (if applicable)
   - `household_type` - Single/Couple
   - `service_package` - Series A/B/Seed/etc
   - `client_name` - Full name
   - `client_email` - Email address
   - `phone` - Phone number
3. Apply Box metadata template
4. Update HubSpot deal with Box folder URL
5. Share client subfolder with client email

**Metadata Template:**
Custom Box metadata template with fields:
- Contact information
- Household type
- Service package
- Deal status

#### 3. Folder Sharing

**Endpoint:** `POST /box/folder/share`

**Process:**
1. Add client as collaborator
2. Role: `viewer` (read-only)
3. Grant access to client-specific subfolder only

### Admin Features

**Metadata Compliance Scanning:**
- Check folders have all required metadata
- Identify missing fields
- List non-compliant folders

**Mismatch Detection:**
- Compare HubSpot contact ID with Box metadata
- Detect contact relationship mismatches
- Flag for manual review

**Repair Tool:**
- Update missing metadata from HubSpot
- Fix contact mismatches
- Re-apply metadata template

---

## Google Chat Integration

### Overview

Google Chat is used for real-time notifications:
- Notifies when deals are allocated to advisers
- Provides allocation summary (deal name, adviser, availability)
- Enables team visibility into allocation events

### Webhook Setup

**Space:** `AAQADqcOrjo` (configured webhook space)

**Webhook URL:** `https://chat.googleapis.com/v1/spaces/.../messages?key=...`

**Setup:** See [Configuration Guide](CONFIGURATION.md#google-chat-integration)

### Notification Format

When a deal is successfully allocated:

```
🎯 Adviser Allocated

Deal: Acme Corp - Series A
Adviser: John Smith (john@example.com)
Service Package: Series A
Household Type: Single
Earliest Available: Week of Feb 3, 2026
Deal URL: [Link to HubSpot deal]
```

### Webhook Payload

Sent to Google Chat when allocation succeeds:

```json
{
  "text": "🎯 Adviser Allocated\n\nDeal: Acme Corp - Series A\n...",
  "cards": [{
    "header": {
      "title": "Allocation Notification",
      "imageUrl": "[Logo URL]"
    },
    "sections": [{
      "widgets": [
        {"textParagraph": {"text": "Deal: Acme Corp - Series A"}},
        {"textParagraph": {"text": "Adviser: John Smith"}},
        ...
      ]
    }]
  }]
}
```

### Troubleshooting

**Webhook not sending messages:**
1. Verify webhook URL is valid
2. Check Google Chat space still exists
3. Verify app permissions in Chat
4. Check application logs for errors

```bash
gcloud app logs tail -s default | grep "Chat\|webhook"
```

---

## Integration Troubleshooting

### HubSpot Issues

**Deal not being allocated:**
- Check HubSpot API token is valid
- Verify deal has required fields (service_package, hs_deal_record_id)
- Check allocation algorithm logs

**Webhook not triggering:**
- Verify workflow is active in HubSpot
- Test webhook URL manually
- Check HubSpot audit log for workflow execution

**Custom properties not updating:**
- Verify token has write permissions
- Check property name matches exactly
- View HubSpot API logs

### Employment Hero Issues

**OAuth failing:**
- Verify Client ID and Client Secret
- Check REDIRECT_URI matches exactly
- Ensure app is authorized in Employment Hero

**Leave requests not syncing:**
- Check EH OAuth token is fresh (not expired)
- Verify API permissions include leave requests
- Check Firestore has space for new documents

**Employees list incomplete:**
- Verify all employees are active in Employment Hero
- Check if some employees are filtered out
- Review sync job logs

### Box Issues

**Folder creation fails:**
- Verify JWT credentials are valid
- Check Box enterprise limits not exceeded
- Verify template folder path exists

**Metadata not applying:**
- Check metadata template exists in Box
- Verify field names match template
- Ensure Box app has metadata permissions

**Sharing fails:**
- Verify client email is valid
- Check Box user exists
- Verify folder exists before sharing

### Google Chat Issues

**Notifications not appearing:**
- Verify webhook URL is correct
- Check Chat space still exists
- Verify bot has permission to post
- Check application error logs

**Format issues:**
- Verify deal data complete
- Check fields contain valid data
- Review Chat API documentation

---

## Integration Flow Diagram

```
HubSpot Deal Created
      ↓
   Webhook to /post/allocate
      ↓
Allocation Algorithm
      ↓
┌─────────────────────────────┐
│ Box Folder Creation         │
│ /post/create_box_folder     │
├─────────────────────────────┤
│ 1. Copy template folder     │
│ 2. Rename to client name    │
│ 3. Return folder URL        │
└──────────┬──────────────────┘
           ↓
    Update HubSpot
    (box_folder_url)
           ↓
┌─────────────────────────────┐
│ Metadata Tagging            │
│ /box/folder/tag/auto        │
├─────────────────────────────┤
│ 1. Fetch data from HubSpot  │
│ 2. Build metadata dict      │
│ 3. Apply to Box folder      │
│ 4. Share with client        │
└──────────┬──────────────────┘
           ↓
      Google Chat
      Notification
      Sent to Team
```

---

## Integration Monitoring

Monitor integration health:

```bash
# Check recent integrations logs
gcloud logging read "resource.type=gae_app AND textPayload=~'HubSpot|Box|Chat|EH'" \
  --project=pivot-digital-466902 \
  --limit=50 \
  --format=json
```

**Dashboard:** [Operations Guide](OPERATIONS.md) → Monitoring section
