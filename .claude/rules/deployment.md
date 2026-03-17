---
paths:
  - "Dockerfile"
  - "cloudbuild.yaml"
  - ".github/workflows/*"
  - "Makefile"
---
# Deployment Rules

## Cloud Run Service

This repo deploys as a **Cloud Run Service** (web app).

```bash
# Deploy canary (0% traffic)
make deploy-canary

# Verify canary is working
curl https://pivot-ops-canary-xxxxx.a.run.app/health

# Promote to 100% traffic
make deploy-promote
```

## Region Requirement

- ALL resources MUST be in `australia-southeast1`
- NEVER use `us-central1`, `us-east1`, or any non-AU region
- This is a Privacy Act 1988 compliance requirement

## Before Deploying

1. All tests pass: `make test`
2. Lint passes: `make lint`
3. Type check passes: `make check`
4. No secrets in code

## Rollback

```bash
# List recent revisions
gcloud run revisions list --service=pivot-ops --region=australia-southeast1

# Route all traffic to previous revision
gcloud run services update-traffic pivot-ops \
  --to-revisions=pivot-ops-00005=100 \
  --region=australia-southeast1
```

## Protected Files

Before modifying these files, WARN the user:

- `Dockerfile` — container build config
- `cloudbuild.yaml` — CI/CD pipeline
- `.github/workflows/*` — GitHub Actions
- `requirements.txt` — pinned dependencies

Ask: "This is infrastructure config. Are you sure you want to modify it?"
