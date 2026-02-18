# Operations Guide

## Cloud Scheduler

Cloud Scheduler automatically runs sync jobs to keep Firestore data fresh. All jobs are configured and active in production.

**Project:** `pivot-digital-466902`
**Location:** `australia-southeast1`
**Timezone:** `Australia/Sydney`

### Employment Hero Sync Jobs

| Job ID | Schedule | Frequency | Endpoint | Target | Status |
|--------|----------|-----------|----------|--------|--------|
| `eh-employees-sync-daily` | `0 0 * * 1` (cron) | Weekly (Mondays @ 1:00 PM AEDT) | `GET /sync/employees` | Cloud Run | ENABLED |
| `eh-leave-requests-sync-daily` | `0 0 * * 1-5` (cron) | Weekdays @ 1:00 PM AEDT | `GET /sync/leave_requests` | Cloud Run | ENABLED |

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
- ✅ PASS: Tests passed, deployed to Cloud Run
- ❌ FAIL: Tests failed, deployment blocked
- ⏳ QUEUED: Waiting to build
- 🔨 BUILDING: Currently building

### Cloud Run Logs

View application logs:

```bash
# Stream live logs
gcloud run logs read --service=adviser-allocation \
  --region=australia-southeast1 \
  --project=pivot-digital-466902

# Tail logs (follow mode)
gcloud run logs tail --service=adviser-allocation \
  --region=australia-southeast1 \
  --project=pivot-digital-466902

# Search for errors
gcloud logging read "severity=ERROR AND resource.type=cloud_run_revision" \
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

### Cloud Run Service

Check deployment status:

```bash
# View service details
gcloud run services describe adviser-allocation \
  --region=australia-southeast1 \
  --project=pivot-digital-466902

# View revisions and traffic
gcloud run revisions list --service=adviser-allocation \
  --region=australia-southeast1 \
  --project=pivot-digital-466902

# View service URL and conditions
gcloud run services describe adviser-allocation \
  --region=australia-southeast1 \
  --project=pivot-digital-466902 \
  --format="yaml(status)"
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
gcloud run logs read --service=adviser-allocation --region=australia-southeast1 --project=pivot-digital-466902 | grep ERROR
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
gcloud logging read "resource.type=cloud_run_revision AND textPayload=~'allocation.*error'" \
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
# View Cloud Run service metrics
gcloud run services describe adviser-allocation \
  --region=australia-southeast1 \
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
# User visits: https://adviser-allocation-307314618542.australia-southeast1.run.app/auth/start

# Or manually re-auth via UI
# Visit admin dashboard and trigger re-authentication
```

---

## Emergency Fixes

### Rollback to Previous Version

If recent deployment breaks production:

```bash
# View available revisions
gcloud run revisions list --service=adviser-allocation \
  --region=australia-southeast1 \
  --project=pivot-digital-466902

# Check which revision is serving traffic
gcloud run services describe adviser-allocation \
  --region=australia-southeast1 \
  --project=pivot-digital-466902 \
  --format="yaml(status.traffic)"

# Route traffic to previous revision
gcloud run services update-traffic adviser-allocation \
  --to-revisions=REVISION=100 \
  --region=australia-southeast1 \
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
   gcloud run logs read --service=adviser-allocation --region=australia-southeast1 --project=pivot-digital-466902
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
   curl https://adviser-allocation-307314618542.australia-southeast1.run.app/availability/earliest
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
   - Employment Hero: Verify OAuth token not expired
   - Google Chat: Verify webhook URL valid

10. **Review CI/CD pipeline**
    - Check tests passed before deployment
    - Verify no recent deployment failures

---

## Health Check Endpoint

The application exposes a health check for monitoring:

```bash
curl https://adviser-allocation-307314618542.australia-southeast1.run.app/health
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
