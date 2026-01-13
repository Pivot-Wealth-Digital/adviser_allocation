# CI/CD Pipeline Setup Guide

This document explains how to set up the enhanced Google Cloud Build CI/CD pipeline for automatic testing and deployment.

## Overview

The enhanced pipeline provides:
- ✅ Automatic tests on every commit to main
- ✅ Dependency caching for 30-60% faster builds
- ✅ Automatic deployment to Google App Engine
- ✅ Coverage reporting
- ✅ Branch filtering (only main branch deploys)

## What's Changed

### Files Updated
- **cloudbuild.yaml** - Enhanced with caching, coverage, and better structure
- **requirements-test.txt** - New file with test dependencies (pytest, pytest-cov)
- **.gcloudignore** - Updated with build cache and coverage exclusions

### Key Improvements
1. ✅ Python 3.12 (matches app.yaml runtime) - was 3.11
2. ✅ Dependency caching via Cloud Storage
3. ✅ Coverage reporting (--cov-report=xml)
4. ✅ Better step organization with IDs
5. ✅ E2_HIGHCPU_8 machine for faster builds
6. ✅ Proper error handling

## Setup Instructions

### Step 1: Create Cloud Storage Bucket for Cache

Run this command to create the cache bucket:

```bash
gsutil mb -p pivot-digital-466902 gs://pivot-digital-466902_cloudbuild_cache
```

**What this does**: Creates a Cloud Storage bucket to store Python dependency cache between builds. This reduces build time significantly.

**Cost**: ~$0.01/month for typical usage

### Step 2: Connect Your Git Repository to Cloud Build

#### Option A: Using Google Cloud Console (Easiest)

1. Go to [Cloud Build Console](https://console.cloud.google.com/cloud-build/triggers)
2. Click **Create Trigger**
3. Select your repository (GitHub, GitLab, or Bitbucket)
4. Configure:
   - **Name**: `deploy-production`
   - **Event**: Push to branch
   - **Branch**: `^main$`
   - **Build configuration**: Cloud Build configuration file (cloudbuild.yaml)
5. Click **Create**

#### Option B: Using gcloud CLI

```bash
gcloud builds connect --repository-name=adviser_allocation
```

Then follow the prompts to authenticate and connect your repository.

### Step 3: Verify Cloud Build Permissions

The Cloud Build service account needs permission to deploy to App Engine. Run:

```bash
# Get your project number
PROJECT_NUMBER=$(gcloud projects describe pivot-digital-466902 --format='value(projectNumber)')

# Grant App Engine Admin role
gcloud projects add-iam-policy-binding pivot-digital-466902 \
  --member=serviceAccount:${PROJECT_NUMBER}@cloudbuild.gserviceaccount.com \
  --role=roles/appengine.appAdmin

# Grant Storage Admin role for cache
gcloud projects add-iam-policy-binding pivot-digital-466902 \
  --member=serviceAccount:${PROJECT_NUMBER}@cloudbuild.gserviceaccount.com \
  --role=roles/storage.admin
```

**What this does**: Allows Cloud Build to deploy your app and manage the cache bucket.

### Step 4: Test the Pipeline

1. Make a small change to your main branch (e.g., update README.md)
2. Commit and push to main:
   ```bash
   git add README.md
   git commit -m "Test CI/CD pipeline"
   git push origin main
   ```
3. Watch the build in [Cloud Build History](https://console.cloud.google.com/cloud-build/builds)

### Step 5: Monitor Build Progress

You should see these steps execute:
1. ✅ Restore cache (may fail first time - that's OK)
2. ✅ Install production dependencies
3. ✅ Install test dependencies
4. ✅ Run tests with coverage
5. ✅ Save cache for next build
6. ✅ Deploy to App Engine

**Expected build times**:
- First build: ~3-5 minutes (no cache)
- Subsequent builds: ~1-3 minutes (with cache)

## Pipeline Architecture

### Build Stages

```
┌─────────────────────────────────────────────────────┐
│ Push to main branch                                 │
└────────────────────┬────────────────────────────────┘
                     │
        ┌────────────▼───────────┐
        │ Step 1: Restore Cache  │ (gsutil rsync)
        └────────────┬───────────┘
                     │
        ┌────────────▼──────────────────┐
        │ Step 2: Install Dependencies  │ (pip install)
        └────────────┬──────────────────┘
                     │
        ┌────────────▼────────────────────────┐
        │ Step 3: Install Test Dependencies   │ (pytest, pytest-cov)
        └────────────┬────────────────────────┘
                     │
        ┌────────────▼──────────────────────┐
        │ Step 4: Run Tests with Coverage   │ (pytest --cov)
        └────────────┬──────────────────────┘
                     │ (if tests pass)
        ┌────────────▼──────────────────┐
        │ Step 5: Save Cache             │ (gsutil rsync)
        └────────────┬──────────────────┘
                     │
        ┌────────────▼────────────────────┐
        │ Step 6: Deploy to App Engine    │ (gcloud app deploy)
        └────────────┬────────────────────┘
                     │
        ┌────────────▼───────────────────────────────┐
        │ Deployment Complete ✅                      │
        │ App live at: https://pivot-digital-466902. │
        │            ts.r.appspot.com                │
        └───────────────────────────────────────────┘
```

### Build Environment

- **Machine Type**: E2_HIGHCPU_8 (8 CPU, 32GB RAM)
- **Language**: Python 3.12
- **Timeout**: 2000 seconds (33 minutes)
- **Logging**: Cloud Logging

## How the Cache Works

### First Build (No Cache)
```
Requirements download + install: ~2-3 minutes
Tests: ~30-60 seconds
Deploy: ~2-3 minutes
Total: ~5-6 minutes
```

### Subsequent Builds (With Cache)
```
Requirements restored from cache: ~10-30 seconds
Tests: ~30-60 seconds
Deploy: ~2-3 minutes
Total: ~3-4 minutes
```

**Savings**: 30-40% faster builds after first build.

## Monitoring and Troubleshooting

### View Build Logs

```bash
# List recent builds
gcloud builds list --limit=10

# View logs for a specific build
gcloud builds log BUILD_ID
```

### Common Issues and Solutions

#### Issue: "Cache bucket not found"
```
gsutil mb -p pivot-digital-466902 gs://pivot-digital-466902_cloudbuild_cache
```

#### Issue: "Permission denied" on App Engine deploy
```bash
PROJECT_NUMBER=$(gcloud projects describe pivot-digital-466902 --format='value(projectNumber)')
gcloud projects add-iam-policy-binding pivot-digital-466902 \
  --member=serviceAccount:${PROJECT_NUMBER}@cloudbuild.gserviceaccount.com \
  --role=roles/appengine.appAdmin
```

#### Issue: "Tests failed" but need to deploy anyway
Edit the trigger to skip the build configuration and run `gcloud app deploy` manually.

#### Issue: Build takes too long
- First build is normal (no cache)
- Check if tests are slow: `pytest --durations=10`
- Consider splitting tests into separate test files

### View Build Metrics

In [Cloud Build Console](https://console.cloud.google.com/cloud-build/builds):
- Build duration
- Success/failure rate
- Resource usage

## Environment Variables

### Available in Builds

The pipeline sets:
- `USE_FIRESTORE=false` - Disables Firestore for test isolation
- `PROJECT_ID` - Your GCP project (pivot-digital-466902)
- `SHORT_SHA` - Git commit hash (first 7 chars)

### Secrets

Secrets are fetched automatically from Google Secret Manager:
- `EH_CLIENT_ID`
- `EH_CLIENT_SECRET`
- `HUBSPOT_TOKEN`
- `SESSION_SECRET`
- `BOX_JWT_CONFIG_JSON`
- etc.

These are configured in `app.yaml` and work automatically on App Engine.

## Branch Filtering

By default, only pushes to the `main` branch trigger deployment.

### To Deploy Other Branches

Edit the trigger:
1. Go to [Cloud Build Triggers](https://console.cloud.google.com/cloud-build/triggers)
2. Click `deploy-production`
3. Change **Branch regex** from `^main$` to:
   - `^(main|develop)$` for main and develop
   - `.*` for all branches

## Cost Breakdown

### Free Tier
- Cloud Build: 120 build-minutes/day free
- Storage: 5GB free

### Typical Costs
| Item | Frequency | Cost |
|------|-----------|------|
| Cloud Build | 10 builds/day × 3 min | Free* |
| Storage (cache) | ~100MB | <$0.01/month |
| App Engine | Same as before | No change |

*Free tier covers most teams

## Next Steps

1. ✅ Create the cache bucket
2. ✅ Connect your Git repository
3. ✅ Push a test commit to main
4. ✅ Monitor the build in Cloud Build console
5. Optional: Set up notifications (Slack, email)

## Advanced Configuration

### Add Slack Notifications

Create a Cloud Function to notify Slack on build failures:

```bash
gcloud functions deploy notify-slack \
  --runtime python39 \
  --trigger-topic=cloud-builds
```

### Add Multiple Environments

To deploy develop → staging and main → production:

1. Create a second trigger for `develop` branch
2. Use substitutions in cloudbuild.yaml:
   ```yaml
   args: ['app', 'deploy', '--region=${_REGION}']
   ```

### Add Security Scanning

Add to cloudbuild.yaml:
```yaml
- name: 'gcr.io/cloud-builders/docker'
  args: ['scan', '--severity=CRITICAL']
```

## Support

For issues or questions:
- [Cloud Build Docs](https://cloud.google.com/build/docs)
- [App Engine Docs](https://cloud.google.com/appengine/docs)
- GCP Project: `pivot-digital-466902`

---

**Last Updated**: January 2026
**Version**: 1.0
