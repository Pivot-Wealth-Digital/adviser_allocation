# Configuration Guide

## Environment Variables

Required environment variables can be provided via `.env` file or Google Secret Manager:

| Variable | Purpose | Example | Required |
|----------|---------|---------|----------|
| `EH_CLIENT_ID` | Employment Hero OAuth Client ID | (from EH portal) | ✅ Yes |
| `EH_CLIENT_SECRET` | Employment Hero OAuth Client Secret | (from EH portal) | ✅ Yes |
| `HUBSPOT_TOKEN` | HubSpot Private App token | `pat-...` | ✅ Yes |
| `REDIRECT_URI` | OAuth callback URL (must match EH config exactly) | `https://app.example.com/auth/callback` | ✅ Yes |
| `SESSION_SECRET` | Flask session encryption key | (any random string) | ✅ Yes |
| `ADMIN_USERNAME` | Admin UI login username | (any value) | ✅ Yes |
| `ADMIN_PASSWORD` | Admin UI login password | (any value) | ✅ Yes |
| `CHAT_WEBHOOK_URL` | Google Chat webhook URL | `https://chat.googleapis.com/v1/spaces/.../messages?key=...` | ✅ Yes |
| `USE_FIRESTORE` | Enable Firestore (default: true) | `true` or `false` | ❌ No |
| `PRESTART_WEEKS` | Adviser start buffer before allocating (default: 3) | `3` (weeks) | ❌ No |
| `PORT` | Server port (default: 8080) | `8080` | ❌ No |

### Optional Configuration

- **`USE_FIRESTORE`** - Set to `false` for local OAuth testing without Firestore
- **`PRESTART_WEEKS`** - Weeks buffer before adviser can be allocated deals (default 3)
- **`PORT`** - HTTP server port (default 8080)

---

## Local Development Setup

### 1. Create `.env` file

```bash
# .env (git-ignored)
EH_CLIENT_ID=your_eh_client_id
EH_CLIENT_SECRET=your_eh_client_secret
HUBSPOT_TOKEN=pat-...
REDIRECT_URI=http://localhost:8080/auth/callback
SESSION_SECRET=your-secret-key
ADMIN_USERNAME=admin
ADMIN_PASSWORD=password
CHAT_WEBHOOK_URL=https://chat.googleapis.com/...
USE_FIRESTORE=true
PORT=8080
```

### 2. Set up Google Cloud credentials

For Firestore access:

```bash
# Set Application Default Credentials
gcloud auth application-default login

# Verify (should show your Google account)
gcloud auth application-default print-access-token
```

### 3. Run locally

```bash
export FLASK_APP=main.py
python main.py
# or
flask run -p 8080
```

---

## Google Secret Manager Setup

Secrets are stored in Google Secret Manager and retrieved via Google Cloud client libraries.

### Format

Secret path format: `projects/{PROJECT_ID}/secrets/{SECRET_NAME}/versions/latest`

**Cross-project secret access:**
- Cloud Run project: `pivot-digital-466902`
- Secret Manager project: `307314618542`

### Required Secrets

| Secret | Description | Value |
|--------|-------------|-------|
| `EH_CLIENT_ID` | Employment Hero OAuth Client ID | OAuth credentials from EH |
| `EH_CLIENT_SECRET` | Employment Hero OAuth Client Secret | OAuth credentials from EH |
| `HUBSPOT_TOKEN` | HubSpot Private App Token | Token from HubSpot portal |
| `SESSION_SECRET` | Flask session encryption key | Random string |
| `ADMIN_USERNAME` | Admin login username | Any value |
| `ADMIN_PASSWORD` | Admin login password | Any value |
| `CHAT_WEBHOOK_URL` | Google Chat webhook for notifications | Webhook URL from Chat space |

### Add/Update a Secret

```bash
# Create new secret
echo "secret-value" | gcloud secrets create SECRET_NAME --data-file=-

# Update existing secret
echo "new-value" | gcloud secrets versions add SECRET_NAME --data-file=-

# View a secret (use carefully!)
gcloud secrets versions access latest --secret="SECRET_NAME"

# List all secrets
gcloud secrets list --project=pivot-digital-466902
```

---

## Employment Hero OAuth Setup

### 1. Obtain OAuth Credentials

1. Log in to Employment Hero
2. Navigate to Settings → Integrations → OAuth
3. Create an OAuth Application
4. Note down:
   - `Client ID`
   - `Client Secret`
   - Authorized redirect URI (must match `REDIRECT_URI` exactly)

### 2. Configure Redirect URI

Set `REDIRECT_URI` to match your deployment URL:

**Local:** `http://localhost:8080/auth/callback`

**Production:** `https://adviser-allocation-307314618542.australia-southeast1.run.app/auth/callback`

⚠️ **CRITICAL:** Must match EXACTLY (including protocol, domain, path, trailing slash)

### 3. Store Credentials

```bash
# Local (.env)
EH_CLIENT_ID=your_client_id
EH_CLIENT_SECRET=your_client_secret
REDIRECT_URI=http://localhost:8080/auth/callback

# Production (Secret Manager)
gcloud secrets create EH_CLIENT_ID --data-file=-
gcloud secrets create EH_CLIENT_SECRET --data-file=-
```

### 4. OAuth Flow

1. User visits `/auth/start`
2. Redirected to Employment Hero authorization page
3. User authorizes application
4. Callback to `/auth/callback` with authorization code
5. Code exchanged for access token
6. Token stored in Firestore (`eh_tokens` collection)
7. Token auto-refreshed on expiry

---

## HubSpot Configuration

### 1. Create Private App

1. Go to HubSpot Portal Settings → Apps and Integrations → Private Apps
2. Create new private app: "Adviser Allocation"
3. Set scopes (minimum required):
   - `crm.objects.deals.read`
   - `crm.objects.deals.write`
   - `crm.objects.contacts.read`
   - `crm.objects.users.read`
   - `crm.schemas.deals.read`
   - `crm.objects.custom_objects.read`

4. Copy access token → `HUBSPOT_TOKEN`

### 2. Store Token

```bash
# Local (.env)
HUBSPOT_TOKEN=pat-eu1-xxxxxxxxxxxxx

# Production (Secret Manager)
gcloud secrets create HUBSPOT_TOKEN --data-file=-
```

### 3. Configure Webhooks

HubSpot workflows can trigger webhooks:

1. Create HubSpot workflow
2. Add webhook action
3. URL: `https://[app-url]/post/allocate`
4. Method: `POST`
5. Include deal fields in payload:
   - `service_package`
   - `hs_deal_record_id`
   - `household_type`
   - `agreement_start_date`

---

## Google Chat Integration

### 1. Create Chat Webhook

1. In Google Chat, go to space settings
2. Create a new webhook
3. Copy webhook URL

### 2. Store Webhook URL

```bash
# Local (.env)
CHAT_WEBHOOK_URL=https://chat.googleapis.com/v1/spaces/...

# Production (Secret Manager)
gcloud secrets create CHAT_WEBHOOK_URL --data-file=-
```

### 3. Notification Format

When deals are allocated, a message is sent:

```
🎯 Adviser Allocated
Deal: [Deal Name]
Adviser: [Adviser Name]
Service Package: [Package]
Earliest Available: [Week]
```

---

## Firestore Database Setup

### Collections

| Collection | Purpose | Auto-created |
|-----------|---------|---------------|
| `employees` | Employee data from Employment Hero | ✅ Yes (on sync) |
| `employees/{id}/leave_requests` | Employee leave requests | ✅ Yes (on sync) |
| `office_closures` | Global office closures | ✅ Yes (on first closure added) |
| `adviser_capacity_overrides` | Adviser-specific capacity limits | ✅ Yes (on first override) |
| `allocation_requests` | Allocation history | ✅ Yes (on first allocation) |
| `eh_tokens` | Employment Hero OAuth tokens | ✅ Yes (on first auth) |

---

## Local Development Environment Checklist

- [ ] Python 3.12+ installed
- [ ] `uv pip install -r requirements.txt` completed
- [ ] Google Cloud credentials configured (`gcloud auth application-default login`)
- [ ] `.env` file created with all required variables
- [ ] Firestore emulator running (optional) or Firestore access confirmed
- [ ] `flask run -p 8080` starts without errors
- [ ] Can access `http://localhost:8080`

---

## Production Deployment Checklist

- [ ] All secrets added to Secret Manager
- [ ] `REDIRECT_URI` matches HubSpot OAuth app config
- [ ] Google Chat webhook URL configured
- [ ] Firestore database created in `pivot-digital-466902` project
- [ ] Cloud Build configured for CI/CD
- [ ] Cloud Run runtime: Python 3.12
- [ ] Cloud Run region: `australia-southeast1`

---

## Troubleshooting

### OAuth Redirect URL Mismatch

**Error:** `REDIRECT_URI_MISMATCH` or invalid callback

**Solution:**
1. Verify `REDIRECT_URI` matches exactly in:
   - Employment Hero OAuth app settings
   - Environment variable
   - HubSpot webhook configuration (if applicable)
2. Check protocol (http vs https)
3. Check trailing slashes
4. Redeploy if changed

### Firestore Connection Errors

**Error:** `Unable to connect to Firestore`

**Solution:**
1. Verify `gcloud auth application-default login` was run
2. Check Firestore database exists in project
3. Check service account has Firestore permissions
4. For local dev: Set `USE_FIRESTORE=false` to skip

### Missing Required Secrets

**Error:** `Secret not found in Secret Manager`

**Solution:**
1. Verify secret name is correct
2. Check secret exists: `gcloud secrets list`
3. Check permissions: Service account can access secret
4. For cross-project: Ensure service account in target project

---

## Environment-Specific Notes

### Local Development

- Use `.env` file for secrets (git-ignored)
- `USE_FIRESTORE=true` to use real Firestore or `false` for testing
- `PORT=8080` (or any available port)

### Staging/Preview

- Use same Secret Manager as production
- Point to staging GCP project
- Use different HubSpot private app (if available)

### Production

- All secrets in Secret Manager (`pivot-digital-466902` project)
- Firestore region: `australia-southeast1`
- Cloud Run region: `australia-southeast1`
- Auto-deployment on `main` branch via Cloud Build
