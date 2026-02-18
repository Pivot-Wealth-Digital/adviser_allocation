# Chat Alerts Configuration Fix

## Problem

Chat alerts were not being sent in production, even though the allocation system was working correctly.

### Root Cause

The `CHAT_WEBHOOK_URL` environment variable was **missing from Cloud Run env config**, so it was never passed to Cloud Run deployment.

## How Chat Alerts Work

The allocation flow has two stages:

1. **`/post/allocate` endpoint** (Primary allocation handler)
   - Receives deal allocation request from HubSpot
   - Selects best adviser using `get_adviser()` function
   - Updates deal owner in HubSpot
   - **Calls `send_chat_alert()` if enabled** ← This sends the Google Chat notification

2. **`send_chat_alert()` function** (in `allocation_routes.py`)
   - Checks if `CHAT_WEBHOOK_URL` is configured
   - If not set, logs "CHAT_WEBHOOK_URL not configured; skipping chat alert"
   - If set, posts allocation details to Google Chat webhook
   - Handles errors gracefully (logs but doesn't fail allocation)

## The Missing Configuration

### Before Fix (Production was broken)
```yaml
# Cloud Run env config - MISSING CHAT_WEBHOOK_URL
env_variables:
  HUBSPOT_TOKEN: "..."
  EH_CLIENT_ID: "..."
  # ... other vars
  # ❌ CHAT_WEBHOOK_URL not here!
```

Result: Every allocation would skip chat alerts silently because the URL was missing.

### After Fix (Production will work)
```yaml
# Cloud Run env config - ADDED CHAT_WEBHOOK_URL
env_variables:
  HUBSPOT_TOKEN: "..."
  EH_CLIENT_ID: "..."
  # ... other vars
  CHAT_WEBHOOK_URL: "projects/307314618542/secrets/CHAT_WEBHOOK_URL/versions/latest"
```

## Deployment Steps

To fix chat alerts in production:

1. **Verify Secret Manager has the webhook**
   ```bash
   gcloud secrets list --project=pivot-digital-466902 | grep -i chat
   ```

   Should show: `CHAT_WEBHOOK_URL`

2. **Update Cloud Run env config** (already done in migration branch)
   ```yaml
   CHAT_WEBHOOK_URL: "projects/307314618542/secrets/CHAT_WEBHOOK_URL/versions/latest"
   ```

3. **Redeploy to Cloud Run**
   ```bash
   gcloud run deploy adviser-allocation --region=australia-southeast1 --tag=chat-alerts-fix --no-traffic
   ```

4. **Test the fix**
   ```bash
   python3 test_chat_alerts.py
   ```

5. **Monitor logs**
   ```bash
   gcloud run logs read --service=adviser-allocation --region=australia-southeast1 --limit=50 --project=pivot-digital-466902
   ```

   Look for: `"Sent chat alert successfully"` or `"Chat alert flag=true"`

## Verification

The fix includes three levels of verification:

### 1. Configuration Test
```python
# Check if CHAT_WEBHOOK_URL is loaded
from adviser_allocation.api.allocation_routes import CHAT_WEBHOOK_URL
assert CHAT_WEBHOOK_URL is not None  # ✅
```

### 2. Code Test
```python
# Verify send_chat_alert function exists
from adviser_allocation.api.allocation_routes import send_chat_alert
send_chat_alert({...})  # Will log or send depending on URL
```

### 3. Integration Test
```bash
# Test allocation with chat alerts enabled
curl -X POST http://localhost:9000/post/allocate?send_chat_alert=1 \
  -H "Content-Type: application/json" \
  -d '{...allocation payload...}'
```

## Chat Alert Flow

```
HubSpot Deal Created
    ↓
POST /post/allocate
    ↓
Select Adviser (get_adviser)
    ↓
Update HubSpot Deal
    ↓
send_chat_alert_flag == true?
    ├─ YES → send_chat_alert()
    │         ↓
    │         Check CHAT_WEBHOOK_URL
    │         ├─ Set → POST to Google Chat ✅
    │         └─ Not Set → Log warning ❌ (this was the issue)
    └─ NO → Skip alert (caller requested it)
```

## Files Modified

- **Cloud Run env config**: Added `CHAT_WEBHOOK_URL` environment variable
- **Cloud Run env config.example**: Added for documentation
- **test_chat_alerts.py**: New test suite to verify configuration

## Monitoring After Fix

Watch these logs to confirm alerts are working:

```bash
# All allocation events
gcloud run logs read --service=adviser-allocation --region=australia-southeast1 --grep="allocation" --project=pivot-digital-466902

# Chat alert success
gcloud run logs read --service=adviser-allocation --region=australia-southeast1 --grep="Sent chat alert" --project=pivot-digital-466902

# Chat alert errors
gcloud run logs read --service=adviser-allocation --region=australia-southeast1 --grep="Failed to send chat alert" --project=pivot-digital-466902

# Alert skips
gcloud run logs read --service=adviser-allocation --region=australia-southeast1 --grep="CHAT_WEBHOOK_URL not configured" --project=pivot-digital-466902
```

## Testing Locally

```bash
# Run configuration tests
python3 test_chat_alerts.py

# Monitor local server logs while sending allocation
tail -f /tmp/adviser-allocation.log &
python3 test_local_full.py
```

## Summary

✅ **Issue**: Chat alerts not sent in production
✅ **Root Cause**: Missing `CHAT_WEBHOOK_URL` in Cloud Run env config
✅ **Fix**: Added environment variable to Cloud Run env config
✅ **Testing**: Created `test_chat_alerts.py` to verify
✅ **Deployment**: Ready for next Cloud Run deployment

Next Step: Deploy to staging, then promote to production.
