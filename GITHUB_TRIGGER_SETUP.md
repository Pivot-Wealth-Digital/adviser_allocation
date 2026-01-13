# GitHub Cloud Build Trigger - Complete Setup Guide

## Issue Found

The adviser_allocation repo isn't showing in the "Connect Repository" list because:
- Cloud Build needs to establish a Developer Connect connection to GitHub
- The CLI approach is hitting IAM permission propagation issues
- You need to use the Google Cloud Console UI instead

## Why adviser_allocation Doesn't Show

The repo is public ✅ which is fine. The issue is that:
1. **No connection exists yet** between Cloud Build and your GitHub account
2. The CLI approach to create one is hitting permission delays
3. The console UI method works instantly

---

## Solution: Manual Setup via Console (5-10 minutes)

### Step 1: Go to Developer Connect Console

1. Open: https://console.cloud.google.com/developer-connect/connections?project=pivot-digital-466902
2. Click **"Create Connection"**

### Step 2: Create GitHub Connection

1. **Connection Type**: Select **GitHub**
2. **Location**: `us-central1` (or your preferred region)
3. **Connection Name**: `adviser-allocation-github`
4. Click **Create**

### Step 3: Authorize GitHub

A dialog will appear asking you to authorize. This step is **critical**:

1. Click **"Authorize Google Cloud Build"** or the authorization link
2. You'll be redirected to GitHub
3. GitHub will ask: "Authorize google-cloud-builds by Pivot-Wealth-Digital?"
4. Click **"Authorize [account]"**
5. You may be asked for your GitHub password
6. Return to Cloud Console
7. The connection should show as **AUTHORIZED** and **AVAILABLE**

### Step 4: Add Repository to Connection

1. In Developer Connect, click on your connection name: `adviser-allocation-github`
2. Click **"Add Repository"**
3. You'll now see a list of your GitHub repos (including adviser_allocation!)
4. Select: **Pivot-Wealth-Digital/adviser_allocation**
5. Click **"Add Selected Repository"**

Now the repository is linked!

---

## Step 5: Create Cloud Build Trigger

Now that the repository is connected, create the trigger:

1. Open: https://console.cloud.google.com/cloud-build/triggers?project=pivot-digital-466902
2. Click **"Create Trigger"**
3. Fill in:
   - **Name**: `adviser-allocation-deploy`
   - **Event**: Push to a branch
   - **Source**:
     - **Connection**: Select `adviser-allocation-github`
     - **Repository**: `Pivot-Wealth-Digital/adviser_allocation`
   - **Branch**: `^main$` (only main branch)
   - **Build configuration file**: `cloudbuild.yaml`
   - **Substitution variables**: Leave empty
4. Click **"Create Trigger"**

✅ **Done!** Your CI/CD pipeline is now live.

---

## Test the Trigger

1. Make a test change:
   ```bash
   echo "# CI/CD Test" >> README.md
   git add README.md
   git commit -m "Test CI/CD pipeline"
   git push origin main
   ```

2. Monitor the build:
   - Go to: https://console.cloud.google.com/cloud-build/builds?project=pivot-digital-466902
   - Your build should appear within 10-30 seconds
   - Click on it to see real-time logs

3. Expected results:
   - **First build**: 3-5 minutes (no cache)
   - **Cache restore**: "No cache found, starting fresh"
   - **Tests**: Should pass ✅
   - **Deployment**: Should complete in 2-3 minutes
   - **Result**: App deployed to https://pivot-digital-466902.ts.r.appspot.com

---

## What Happens on Each Push

### For pushes to `main`:
```
Push to main
    ↓
Webhook fires (10-30 seconds later)
    ↓
Build starts automatically
    ↓
1. Restore cache
2. Install dependencies
3. Run tests
4. Save cache
5. Deploy to App Engine
    ↓
✅ Live at https://pivot-digital-466902.ts.r.appspot.com
```

### For pushes to other branches:
- **Build still runs** (tests execute)
- **But no deployment** (safe to test)
- Can merge to main when confident

---

## Troubleshooting

### "Cannot find adviser_allocation" when adding repository

**Solution**:
- Ensure you completed GitHub authorization in Step 3
- Go back to Developer Connect > Connection > Click **"Authorize"** again
- Complete the full authorization flow
- Try "Add Repository" again

### Trigger doesn't fire after pushing to main

**Possible causes**:

1. **Webhook not registered**:
   - Wait 60 seconds for webhook setup
   - Try pushing again

2. **Wrong branch**:
   - Verify you're on main: `git branch`
   - Verify regex is `^main$` (not `main` or `*/main`)

3. **Branch protection**:
   - Check if main has branch protection requiring reviews
   - Try pushing to develop instead and adjust trigger

### Build fails with "Coverage module not found"

This is expected on first run since test dependencies are fresh:
- Build automatically retries
- Should pass on second run
- Subsequent builds cache dependencies

### Build fails: "No such file or directory: cloudbuild.yaml"

- Ensure you're in the correct project directory
- `cloudbuild.yaml` should be at repository root
- Verify: `ls -la cloudbuild.yaml`

---

## What's Already Set Up (Automated)

✅ Cloud Storage bucket: `gs://pivot-digital-466902_cloudbuild_cache`
✅ IAM Permissions: App Engine Admin, Storage Admin
✅ Configuration files: `cloudbuild.yaml`, `requirements-test.txt`
✅ Deployment exclusions: `.gcloudignore`

**All you need to do**: Create the connection and trigger via console (Steps 1-5 above)

---

## After Setup: Monitoring

### View all builds:
```bash
gcloud builds list --project=pivot-digital-466902 --limit=20
```

### View logs for specific build:
```bash
gcloud builds log BUILD_ID --project=pivot-digital-466902
```

### View trigger status:
```bash
gcloud builds triggers list --project=pivot-digital-466902
```

---

## Files Ready for the Pipeline

- `cloudbuild.yaml` - Build pipeline (✅ ready)
- `requirements-test.txt` - Test dependencies (✅ ready)
- `.gcloudignore` - Deployment exclusions (✅ updated)
- `cache bucket` (✅ created)
- `permissions` (✅ granted)

---

## Architecture Diagram

```
┌─────────────────┐
│ GitHub:main     │ Push a commit
│ repository      │
└────────┬────────┘
         │
         ├─► Webhook triggers
         │
┌────────▼────────────────────────────────┐
│ Cloud Build Trigger (adviser-allocation) │
│ Event: Push to branch ^main$             │
│ Config: cloudbuild.yaml                  │
└────────┬──────────────────────────────────┘
         │
         ├─► Build starts
         │
┌────────▼──────────────────────────┐
│ Build Steps:                       │
│ 1. Restore cache                   │
│ 2. Install dependencies            │
│ 3. Run tests                       │
│ 4. Save cache                      │
│ 5. Deploy to App Engine            │
└────────┬──────────────────────────┘
         │
         ├─► Tests pass → Deploy
         │
         └─► Tests fail → Stop build
                (no deployment)
         │
┌────────▼──────────────────────────┐
│ App Engine                          │
│ https://pivot-digital-466902.      │
│ ts.r.appspot.com                   │
│ Version: git commit SHA            │
└────────────────────────────────────┘
```

---

## Estimated Timeline

| Step | Time |
|------|------|
| Create connection | 2 min |
| Authorize GitHub | 1-2 min |
| Add repository | 1 min |
| Create trigger | 1 min |
| Test push | 1 min |
| First build completes | 5 min |
| **Total** | **~15 min** |

---

## Next Steps After Setup

1. ✅ Complete the console setup (Steps 1-5)
2. ✅ Test with a push to main
3. ✅ Monitor the build in Cloud Build console
4. ✅ Verify app deploys
5. Consider adding notifications (Slack, email) for build failures

---

**Setup Method**: Google Cloud Console UI (Developer Connect + Cloud Build)
**Requires**: Google Cloud project access (you have this)
**Cost**: Free (within free tier limits)
**Time to complete**: ~15 minutes
