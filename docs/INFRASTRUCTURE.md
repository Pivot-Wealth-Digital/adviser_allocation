# Infrastructure Guide

## GCP Services & Resources

| Service | Resource | Purpose | Status |
|---------|----------|---------|--------|
| **App Engine** | Project: `pivot-digital-466902`, Service: `default`, Region: `australia-southeast1` | Hosts Flask application (Python 3.12 runtime) | ✅ Active |
| **Firestore** | Database in `pivot-digital-466902` project | Primary data store (NoSQL) | ✅ Active |
| **Secret Manager** | Project ID: `307314618542` (Cross-project) | Stores credentials (EH, HubSpot, Box, passwords) | ✅ Active |
| **Cloud Logging** | Logs for App Engine service `default` | Application logging and monitoring | ✅ Active |
| **Cloud Build** | Trigger on git push to `main` branch | CI/CD pipeline (test gate + auto-deploy) | ✅ Active |
| **Cloud Scheduler** | Jobs in `australia-southeast1` | Triggers sync jobs on schedule | ✅ Active |
| **Cloud Run** | hubspot-incremental service | HubSpot sync (separate microservice) | ✅ Active |

## App Engine

### Deployment Details

**Current Configuration:**
- **Runtime:** Python 3.12 (Standard Environment)
- **Region:** `australia-southeast1` (locked)
- **Service:** `default` (main service)
- **Auto-Scaling:** Min 1, Max auto instances
- **Memory:** 512 MB per instance
- **Timeout:** 540 seconds (9 minutes)
- **Entry Point:** `gunicorn -b :$PORT main:app`

### Instance Management

```bash
# View active instances
gcloud app instances list --project=pivot-digital-466902

# View versions and traffic split
gcloud app versions list --project=pivot-digital-466902

# View service configuration
gcloud app services describe default --project=pivot-digital-466902

# View traffic distribution
gcloud app services describe default --project=pivot-digital-466902 --format=yaml | grep -A10 split
```

### Version Management

Versions are created automatically on each deployment (via Cloud Build).

```bash
# List all versions
gcloud app versions list --project=pivot-digital-466902

# View version details
gcloud app versions describe VERSION_ID --project=pivot-digital-466902

# Route traffic to specific version
gcloud app services set-traffic default \
  --splits=VERSION_ID=1.0 \
  --project=pivot-digital-466902

# Stop a version
gcloud app versions stop VERSION_ID --project=pivot-digital-466902

# Delete a version
gcloud app versions delete VERSION_ID --project=pivot-digital-466902
```

### Logs and Debugging

```bash
# Stream live logs
gcloud app logs tail -s default --project=pivot-digital-466902

# View logs for specific version
gcloud app logs tail -s default --version=MAIN_VERSION --project=pivot-digital-466902

# Search for errors
gcloud logging read "resource.type=gae_app AND severity=ERROR" \
  --project=pivot-digital-466902 \
  --limit=100
```

## Firestore Database

**Project:** `pivot-digital-466902`
**Database:** `(default)` in `australia-southeast1`

### Collections

| Collection | Purpose | Schema | Size |
|-----------|---------|--------|------|
| `employees` | Employee data from Employment Hero | `{id, name, email, organisation_id}` | ~100-500 docs |
| `employees/{id}/leave_requests` | Leave requests per employee | `{start_date, end_date, type, status}` | ~1,000-5,000 docs |
| `office_closures` | Global office closures/holidays | `{start_date, end_date, description, tags}` | ~20-50 docs |
| `adviser_capacity_overrides` | Manual capacity limits per adviser | `{adviser_email, effective_date, client_limit}` | ~50-200 docs |
| `allocation_requests` | Allocation history | `{deal_id, adviser_id, package, timestamp}` | ~100-10,000 docs |
| `eh_tokens` | Employment Hero OAuth tokens | `{access_token, refresh_token, expires_at}` | 1 doc |
| `box_metadata` | Box folder metadata tracking | `{folder_id, contact_ids, metadata_status}` | ~100-1,000 docs |
| `system_settings` | System configuration | `{box_template_path, ...}` | ~5-10 docs |

### Database Operations

```bash
# List collections
gcloud firestore collections list --project=pivot-digital-466902

# Count documents in collection
gcloud firestore documents list --collection=employees --project=pivot-digital-466902 | wc -l

# Export collection to Cloud Storage
gcloud firestore export gs://BUCKET_NAME/EXPORT_PATH \
  --collection-ids=COLLECTION_NAME \
  --project=pivot-digital-466902

# Import data
gcloud firestore import gs://BUCKET_NAME/IMPORT_PATH \
  --project=pivot-digital-466902

# Delete collection (⚠️ IRREVERSIBLE)
gcloud firestore databases delete-collection COLLECTION_NAME \
  --project=pivot-digital-466902 \
  --database=default
```

### Querying Firestore

```bash
# Query documents (via gcloud CLI)
gcloud firestore documents list --collection=allocation_requests \
  --project=pivot-digital-466902 \
  --limit=10

# Via Firebase Console
# https://console.firebase.google.com/u/0/project/pivot-digital-466902/firestore
```

### Monitoring Firestore

```bash
# View Firestore metrics (operations, errors, latency)
# Go to: GCP Console → Firestore → Monitoring tab

# Query index usage
gcloud firestore indexes list --project=pivot-digital-466902
```

## Google Cloud Logging

**Log Source:** `projects/pivot-digital-466902/logs/appengine.googleapis.com/default`

### View Logs

```bash
# Stream live logs (real-time)
gcloud app logs tail -s default --project=pivot-digital-466902

# View logs with filters
gcloud logging read "resource.type=gae_app" \
  --project=pivot-digital-466902 \
  --limit=50 \
  --format=json

# Export logs to BigQuery or GCS
gcloud logging sinks create LOG_SINK \
  DESTINATION \
  --log-filter="resource.type=gae_app" \
  --project=pivot-digital-466902
```

### Log Analysis

```bash
# Find errors in last 24 hours
gcloud logging read "severity=ERROR AND timestamp>=$(date -d '-24 hours' '+%Y-%m-%dT%H:%M:%S')" \
  --project=pivot-digital-466902 \
  --limit=100

# Search for specific keywords
gcloud logging read "textPayload=~'allocation'" \
  --project=pivot-digital-466902 \
  --limit=50

# Count errors by type
gcloud logging read "severity=ERROR" \
  --project=pivot-digital-466902 \
  --format="table(textPayload)" \
  --limit=1000 | sort | uniq -c
```

## External APIs

| API | Purpose | Authentication | Rate Limits | Status |
|-----|---------|-----------------|-------------|--------|
| **Employment Hero** | Employee/leave data sync | OAuth 2.0 | ~100 req/min | ✅ Active |
| **HubSpot** | Deal allocation, meetings, contacts | Private App Token | 100/s | ✅ Active |
| **Box** | Folder creation and metadata | JWT Service Account | 10 req/s | ✅ Active |
| **Google Chat** | Allocation notifications | Webhook | 100 msg/min | ✅ Active |

### API Health

```bash
# Test HubSpot API
curl -H "Authorization: Bearer $HUBSPOT_TOKEN" \
  https://api.hubapi.com/crm/v3/objects/deals?limit=1

# Test Box API (requires JWT auth)
# See Box SDK docs

# Test Employment Hero API
# Requires valid OAuth token from Firestore
```

## Secret Management

**Project:** `307314618542` (cross-project access from `pivot-digital-466902`)

### Secrets

| Secret | Access | Status |
|--------|--------|--------|
| `EH_CLIENT_ID` | via app config | ✅ Active |
| `EH_CLIENT_SECRET` | via app config | ✅ Active |
| `HUBSPOT_TOKEN` | via app config | ✅ Active |
| `BOX_JWT_CONFIG_JSON` | via app config | ✅ Active |
| `SESSION_SECRET` | via app config | ✅ Active |
| `ADMIN_USERNAME` | via app config | ✅ Active |
| `ADMIN_PASSWORD` | via app config | ✅ Active |
| `CHAT_WEBHOOK_URL` | via app config | ✅ Active |

### Manage Secrets

```bash
# List secrets
gcloud secrets list --project=307314618542

# View secret value (be careful!)
gcloud secrets versions access latest --secret="SECRET_NAME" --project=307314618542

# Rotate secret (create new version)
echo "new-value" | gcloud secrets versions add SECRET_NAME \
  --data-file=- \
  --project=307314618542

# Grant access to App Engine service account
gcloud secrets add-iam-policy-binding SECRET_NAME \
  --member=serviceAccount:APP_ENGINE_SA@appspot.gserviceaccount.com \
  --role=roles/secretmanager.secretAccessor \
  --project=307314618542
```

## Disaster Recovery

### Backup Strategy

- **Firestore:** Automatic backups via Google Cloud
- **Secrets:** Managed by Secret Manager (versioned)
- **Code:** Git repository (GitHub)

### Disaster Scenarios

**Data Loss (Firestore):**
1. Check backup exists in Cloud Console
2. Restore from backup (if available)
3. Otherwise, re-sync from Employment Hero and HubSpot

**Credential Compromise:**
1. Rotate secret: `gcloud secrets versions add ...`
2. Update external systems (Employment Hero, HubSpot, Box)
3. Monitor for unauthorized access

**Complete Outage:**
1. Verify Cloud Build pipeline (push to trigger redeploy)
2. Check App Engine quota/billing
3. View logs for root cause
4. Rollback to previous version if needed

## Performance Optimization

### Caching Strategy

- **TTL-based caching:** 5-15 minute TTL for frequently accessed data
- **Firestore queries:** Indexed for common patterns
- **API responses:** Cached to reduce external API calls

### Database Optimization

- Composite indexes for complex queries
- Document structure optimized for access patterns
- Regular cleanup of old allocation records

### Monitoring

```bash
# Check App Engine quota
gcloud compute project-info describe --project=pivot-digital-466902

# View Firestore read/write metrics
# GCP Console → Firestore → Monitoring

# Check Cloud Scheduler job execution times
gcloud scheduler jobs describe JOB_NAME \
  --location=australia-southeast1 \
  --project=pivot-digital-466902
```

---

## Links

- **GCP Console:** https://console.cloud.google.com/appengine?project=pivot-digital-466902
- **Firestore Console:** https://console.firebase.google.com/u/0/project/pivot-digital-466902/firestore
- **Cloud Logging:** https://console.cloud.google.com/logs
- **Cloud Build:** https://console.cloud.google.com/cloud-build
- **Cloud Scheduler:** https://console.cloud.google.com/cloudscheduler
