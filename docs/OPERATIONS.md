# Operations Guide

## Cloud Scheduler

Cloud Scheduler automatically runs sync jobs to keep Firestore data fresh. All jobs are configured and active in production.

**Project:** `pivot-digital-466902`
**Location:** `australia-southeast1`
**Timezone:** `Australia/Sydney`

### Employment Hero Sync Jobs

| Job ID | Schedule | Frequency | Endpoint | Target | Status |
|--------|----------|-----------|----------|--------|--------|
| `eh-employees-sync-daily` | `0 0 * * 1` (cron) | Weekly (Mondays @ 1:00 PM AEDT) | `GET /sync/employees` | App Engine | ENABLED |
| `eh-leave-requests-sync-daily` | `0 0 * * 1-5` (cron) | Weekdays @ 1:00 PM AEDT | `GET /sync/leave_requests` | App Engine | ENABLED |

**Retry Policy:**
- Max 1 retry for employees sync
- Max 1 retry for leave requests sync
- Backoff: 5s minimum, up to 1 hour maximum
- Max retry duration: 5s for leave requests, 0s for employees

**Last Successful Runs:**
- Employees: Mondays @ 1:00 PM AEDT
- Leave Requests: Weekdays @ 1:00 PM AEDT

### HubSpot Sync Jobs

| Job ID | Schedule | Frequency | Endpoint | Target | Status |
|--------|----------|-----------|----------|--------|--------|
| `hubspot-sync-users-daily` | `0 0 * * *` | Daily @ 1:00 PM AEDT | `/hubspot/incremental` | Cloud Run | ENABLED |
| `hubspot-sync-deals-daily` | `30 0 * * *` | Daily @ 1:30 PM AEDT | `/hubspot/incremental` | Cloud Run | ENABLED |
| `hubspot-sync-meetings-daily` | `0 1 * * *` | Daily @ 2:00 PM AEDT | `/hubspot/incremental` | Cloud Run | ENABLED |
| `hubspot-sync-contacts-daily` | `15 0 * * *` | Daily @ 1:15 PM AEDT | `/hubspot/incremental` | Cloud Run | ENABLED |
| `hubspot-sync-companies-daily` | `45 0 * * *` | Daily @ 1:45 PM AEDT | `/hubspot/incremental` | Cloud Run | ENABLED |

**Retry Policy:**
- Max 5 retries for all HubSpot jobs
- Backoff: 5s minimum, up to 1 hour maximum
- Attempt deadline: 180 seconds

---

## View and Manage Jobs

### List All Jobs

```bash
gcloud scheduler jobs list \
  --project=pivot-digital-466902 \
  --location=australia-southeast1
```

### View Job Details

```bash
gcloud scheduler jobs describe eh-employees-sync-daily \
  --project=pivot-digital-466902 \
  --location=australia-southeast1
```

### Trigger Job Manually

```bash
gcloud scheduler jobs run eh-employees-sync-daily \
  --project=pivot-digital-466902 \
  --location=australia-southeast1
```

### Pause Job

```bash
gcloud scheduler jobs pause eh-employees-sync-daily \
  --project=pivot-digital-466902 \
  --location=australia-southeast1
```

### Resume Job

```bash
gcloud scheduler jobs resume eh-employees-sync-daily \
  --project=pivot-digital-466902 \
  --location=australia-southeast1
```

### View Job Execution History

```bash
# Via Cloud Logging
gcloud logging read "resource.type=cloud_scheduler_job AND resource.labels.job_id=eh-employees-sync-daily" \
  --project=pivot-digital-466902 \
  --limit=50 \
  --format=json
```

---

## Monitoring

### Cloud Build Status

Monitor CI/CD pipeline:

```bash
# List recent builds
gcloud builds list \
  --project=pivot-digital-466902 \
  --limit=10

# View build logs
gcloud builds log BUILD_ID \
  --project=pivot-digital-466902 \
  --stream
```

**Build Status:**
- ✅ PASS: Tests passed, deployed to App Engine
- ❌ FAIL: Tests failed, deployment blocked
- ⏳ QUEUED: Waiting to build
- 🔨 BUILDING: Currently building

### App Engine Logs

View application logs:

```bash
# Stream live logs
gcloud app logs tail -s default \
  --project=pivot-digital-466902

# View logs for specific version
gcloud app logs tail -s default --version=main \
  --project=pivot-digital-466902

# Search for errors
gcloud logging read "severity=ERROR AND resource.type=gae_app" \
  --project=pivot-digital-466902 \
  --limit=50 \
  --format=json
```

### Firestore Metrics

Monitor Firestore usage:

```bash
# View billing and usage
gcloud firestore databases describe --database=default \
  --project=pivot-digital-466902

# Query collection sizes (estimate)
gcloud firestore databases describe \
  --project=pivot-digital-466902 \
  --region=australia-southeast1
```

**Dashboard:** [GCP Console → Firestore](https://console.cloud.google.com/firestore)

### App Engine Instances

Check deployment status:

```bash
# View active instances
gcloud app instances list \
  --project=pivot-digital-466902

# View versions and traffic
gcloud app versions list \
  --project=pivot-digital-466902

# View service details
gcloud app services describe default \
  --project=pivot-digital-466902
```

---

## Troubleshooting

### Sync Job Failed

**Symptoms:** Sync job shows failed status in Cloud Scheduler

**Check logs:**
```bash
# View job execution logs
gcloud logging read "resource.type=cloud_scheduler_job AND severity=ERROR" \
  --project=pivot-digital-466902 \
  --limit=10 \
  --format=json

# View app logs around failure time
gcloud app logs tail -s default --project=pivot-digital-466902 | grep ERROR
```

**Common Issues:**
- Employment Hero OAuth token expired → Refresh needed via `/auth/start`
- HubSpot API rate limit reached → Wait and retry
- Firestore quota exceeded → Check billing

**Resolution:**
```bash
# Manually trigger sync to retry
gcloud scheduler jobs run eh-employees-sync-daily \
  --project=pivot-digital-466902 \
  --location=australia-southeast1
```

### Allocation Failed

**Symptoms:** Deals not being allocated or allocation records have errors

**Check logs:**
```bash
# Find allocation errors
gcloud logging read "resource.type=gae_app AND textPayload=~'allocation.*error'" \
  --project=pivot-digital-466902 \
  --limit=20 \
  --format=json
```

**Common Causes:**
- Adviser not found in Firestore
- Service package mismatch
- Capacity calculation error

**Check data:**
```bash
# View allocation records
gcloud firestore documents list --collection=allocation_requests \
  --project=pivot-digital-466902
```

### Performance Issues

**Symptoms:** Slow response times, timeouts

**Check metrics:**
```bash
# View App Engine instance metrics
gcloud app services describe default \
  --project=pivot-digital-466902

# Check Firestore read/write metrics
# Dashboard: GCP Console → Firestore → Monitoring
```

**Resolution:**
- Check Firestore query performance (use composite indexes if needed)
- Verify Cloud Scheduler job intervals aren't too frequent
- Check HubSpot API rate limits

### OAuth Token Issues

**Symptoms:** "Token expired" or "Invalid credentials" errors

**Resolution:**
```bash
# Trigger OAuth flow to refresh
# User visits: https://pivot-digital-466902.ts.r.appspot.com/auth/start

# Or manually re-auth via UI
# Visit admin dashboard and trigger re-authentication
```

---

## Emergency Fixes

### Rollback to Previous Version

If recent deployment breaks production:

```bash
# View available versions
gcloud app versions list \
  --project=pivot-digital-466902

# Check which version is active (should be 100%)
gcloud app services describe default \
  --project=pivot-digital-466902

# Route traffic to previous version
gcloud app services set-traffic default \
  --splits=OLD_VERSION=1.0 \
  --project=pivot-digital-466902

# Then fix code and re-deploy
git revert BAD_COMMIT
git push origin main  # Cloud Build auto-deploys
```

### Pause Scheduled Jobs

If jobs are causing issues:

```bash
# Pause all sync jobs
gcloud scheduler jobs pause eh-employees-sync-daily \
  --project=pivot-digital-466902 \
  --location=australia-southeast1

gcloud scheduler jobs pause eh-leave-requests-sync-daily \
  --project=pivot-digital-466902 \
  --location=australia-southeast1

# Resume when fixed
gcloud scheduler jobs resume eh-employees-sync-daily \
  --project=pivot-digital-466902 \
  --location=australia-southeast1
```

### Clear Stale Data

**⚠️ WARNING: Destructive operation**

```bash
# Delete Firestore collection (NO UNDO!)
gcloud firestore databases delete-collection allocation_requests \
  --project=pivot-digital-466902 \
  --database=default

# Trigger fresh sync
gcloud scheduler jobs run eh-employees-sync-daily \
  --project=pivot-digital-466902 \
  --location=australia-southeast1
```

---

## Debug Checklist

If something is broken, follow this checklist:

1. **Check recent commits**
   ```bash
   git log --oneline -10
   ```

2. **View error logs**
   ```bash
   gcloud app logs tail -s default --project=pivot-digital-466902
   ```

3. **Check Cloud Build status**
   ```bash
   gcloud builds list --project=pivot-digital-466902 --limit=5
   ```

4. **Verify Firestore connection**
   ```bash
   gcloud firestore documents list --collection=employees --project=pivot-digital-466902 --limit=1
   ```

5. **Check OAuth tokens are fresh**
   - View Firestore `eh_tokens` collection
   - Check `expires_at` timestamp

6. **Test endpoints manually**
   ```bash
   curl https://pivot-digital-466902.ts.r.appspot.com/availability/earliest
   ```

7. **Review allocation algorithm state**
   - Check Firestore collections populated (employees, office_closures, capacity_overrides)
   - Verify leave requests synced from Employment Hero

8. **Check secrets are accessible**
   ```bash
   gcloud secrets list --project=pivot-digital-466902
   ```

9. **Verify integrations are working**
   - HubSpot: Check latest sync job execution
   - Box: Verify JWT token valid
   - Employment Hero: Verify OAuth token not expired
   - Google Chat: Verify webhook URL valid

10. **Review CI/CD pipeline**
    - Check tests passed before deployment
    - Verify no recent deployment failures

---

## Health Check Endpoint

The application exposes a health check for monitoring:

```bash
curl https://pivot-digital-466902.ts.r.appspot.com/health
# Returns: {"status": "healthy"} with HTTP 200
```

Use this in monitoring dashboards to alert if app is down.

---

## Performance Recommendations

### Database Optimization
- Use Firestore composite indexes for complex queries
- Enable caching for frequently accessed data
- Monitor collection sizes and prune old records

### API Rate Limiting
- Current limit: 50 requests/hour
- Adjust if needed in `middleware/rate_limiter.py`

### Job Scheduling
- Stagger job times to avoid simultaneous executions
- Current schedule: Jobs start at :00, :15, :30, :45 minutes
- Monitor job durations to adjust intervals if needed

---

## Documentation

- **Architecture:** [ARCHITECTURE.md](../ARCHITECTURE.md)
- **Configuration:** [CONFIGURATION.md](CONFIGURATION.md)
- **API Reference:** [API_REFERENCE.md](API_REFERENCE.md)
- **CI/CD Details:** [CI_CD_SUMMARY.md](../CI_CD_SUMMARY.md)
- **Deployment Guide:** [DEPLOYMENT_VERIFICATION.md](../DEPLOYMENT_VERIFICATION.md)
