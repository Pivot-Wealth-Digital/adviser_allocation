# Documentation Index

This folder contains all detailed project documentation. Start below based on your role.

---

## For End Users (Operators)

### [User Guide](USER_GUIDE.md)
How to use the adviser allocation platform, including:
- Checking adviser availability and schedules
- Managing allocations
- Admin tools (office closures, capacity overrides)

---

## For Developers

### [Architecture](../ARCHITECTURE.md)
System design and core algorithms:
- Allocation algorithm details
- Core modules and patterns
- Database schema design
- Caching strategy
- Rate limiting and retries

### [API Reference](API_REFERENCE.md)
All endpoints and webhooks:
- Production webhooks (HubSpot integration)
- Availability endpoints
- Data sync endpoints
- Admin endpoints
- Authentication endpoints
- Error responses and rate limiting

### [Configuration](CONFIGURATION.md)
Environment setup and secrets:
- 12 required environment variables
- Local development setup
- Google Secret Manager configuration
- OAuth setup for all integrations
- Firestore database setup

### [Operations](OPERATIONS.md)
Deployment, monitoring, and troubleshooting:
- Cloud Scheduler jobs and management
- Cloud Build status monitoring
- Cloud Run logs and metrics
- Firestore monitoring
- Troubleshooting guide
- Emergency fixes and rollbacks
- Debug checklist

### [Integrations](INTEGRATIONS.md)
Integration details and troubleshooting:
- **HubSpot CRM** - Portal ID, webhooks, data synced
- **Employment Hero** - OAuth flow, leave request syncing
- **Google Chat** - Webhook notifications
- Integration flow diagrams
- Troubleshooting for each service

### [Infrastructure](INFRASTRUCTURE.md)
GCP services and infrastructure:
- GCP services overview (Cloud Run, Firestore, Secrets, Build, Scheduler)
- Cloud Run configuration and management
- Firestore collections and queries
- Google Cloud Logging
- External APIs and rate limits
- Secret management
- Disaster recovery strategies

### [Contributing](CONTRIBUTING.md)
Development workflow and guidelines:
- Feature branch workflow
- Testing requirements (65 tests)
- Commit message format
- Common change scenarios (new feature, bug fix, dependencies, etc.)
- Deployment process
- Emergency fixes
- Code style guide (Python PEP 8, type hints, docstrings)
- What NOT to do (critical practices)

---

## Reference

### [CI/CD Summary](../CI_CD_SUMMARY.md)
Build pipeline configuration and details.

### [Deployment Verification](../DEPLOYMENT_VERIFICATION.md)
Post-deployment verification checklist.

### [Skills Framework](SKILLS_FRAMEWORK.md)
Custom skills system for the application.

### [Changelog](CHANGELOG.md)
Version history and release notes.

---

## Quick Navigation

**I need to...** | **Go to...**
---|---
Check adviser availability | [User Guide - Adviser Schedule](USER_GUIDE.md#adviser-schedule)
Manage office closures | [User Guide - Admin Tools](USER_GUIDE.md#admin-tools)
Set up a new integration | [Configuration](CONFIGURATION.md)
Understand the system | [Architecture](../ARCHITECTURE.md)
Find an API endpoint | [API Reference](API_REFERENCE.md)
Fix a production issue | [Operations - Troubleshooting](OPERATIONS.md#troubleshooting)
Deploy to production | [Contributing - Deployment](CONTRIBUTING.md#deployment-process)
Monitor the system | [Operations - Monitoring](OPERATIONS.md#monitoring)
Configure HubSpot | [Configuration - HubSpot](CONFIGURATION.md#hubspot-configuration)
Set up Employment Hero | [Configuration - Employment Hero](CONFIGURATION.md#employment-hero-oauth-setup)

---

## Main README

For a quick overview, see [README.md](../README.md).
