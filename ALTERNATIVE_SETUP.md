# Alternative Cloud Build Setup - Manual Trigger Creation

Due to GCP permission propagation delays with creating a new GitHub connection via CLI, here's the manual workaround:

## What's Already Done (Automated) ✅

- Cloud Storage cache bucket created
- IAM permissions granted to Cloud Build service accounts
- Configuration files ready (cloudbuild.yaml, requirements-test.txt)

## Manual Step Required

### Create Trigger Using GCP Console

Since the CLI approach is encountering permission delays, you need to create the trigger manually via the Google Cloud Console:

#### Steps:

1. **Open Cloud Build Triggers Console**
   - URL: https://console.cloud.google.com/cloud-build/triggers?project=pivot-digital-466902

2. **Click "Create Trigger"**

3. **If you see this message**: "No repositories configured"
   - This is expected - we need to connect GitHub first
   - Click **"Connect Repository"** or **"Connect new repository"**

4. **Select GitHub** as the repository host

5. **Authorize Cloud Build**
   - You'll be redirected to GitHub to authorize
   - Click "Authorize google-cloud-builds"
   - This authorizes the existing Cloud Build GitHub App

6. **Select Repository**
   - Choose: **Pivot-Wealth-Digital/adviser_allocation**
   - Click **"Connect selected repository"**

7. **Now create the trigger**
   - **Name**: `adviser-allocation-deploy`
   - **Event**: Push to a branch
   - **Branch**: `^main$` (only main branch)
   - **Build configuration file**: `cloudbuild.yaml`
   - Click **Create trigger**

## Why Manual?

The CLI approach (`gcloud builds connections create`) requires:
- A brand new GitHub app connection to be created
- IAM permissions to propagate through Google's system
- This can take 5-15 minutes to fully propagate

The console approach uses the **existing GitHub app** that Cloud Build already has (as evidenced by your previous successful builds), so it's instant.

## After Creating the Trigger

1. **Test it immediately**:
   ```bash
   echo "# Test" >> README.md
   git add README.md
   git commit -m "Test CI/CD trigger"
   git push origin main
   ```

2. **Monitor the build**:
   - Go to: https://console.cloud.google.com/cloud-build/builds?project=pivot-digital-466902
   - You should see your build start within 30 seconds

3. **Expected first build time**: 3-5 minutes

## Troubleshooting

### "No repositories configured" message
- Click "Connect Repository"
- GitHub will prompt you to authorize
- Select the Pivot-Wealth-Digital organization
- Select adviser_allocation repo
- Click Install

### Trigger doesn't fire on push
- Ensure branch regex is exactly: `^main$`
- Ensure you pushed to `main` branch (not a feature branch)
- Wait 30-60 seconds for webhook to trigger

### Build fails with permission error
- All required permissions have already been granted via CLI
- Wait 5 minutes for IAM to fully propagate
- Try the build again

## Once Trigger is Active

Your pipeline will:
1. Automatically run on every push to `main`
2. Restore cached dependencies (~10-30s on subsequent builds)
3. Install dependencies
4. Run tests with coverage
5. Deploy to App Engine if tests pass
6. Send you to: https://pivot-digital-466902.ts.r.appspot.com

## Files Ready for Use

- `cloudbuild.yaml` - Build pipeline (✅ ready)
- `requirements-test.txt` - Test dependencies (✅ ready)
- `.gcloudignore` - Deployment exclusions (✅ updated)
- Cache bucket `gs://pivot-digital-466902_cloudbuild_cache` (✅ created)
- Permissions (✅ granted to service accounts)

---

**Estimated time to complete**: 5 minutes
**Status**: Ready for manual trigger creation
