# Adviser Allocation Service

A production Flask application that automatically allocates HubSpot deals to advisers based on capacity and availability, with integrated Box folder provisioning and HR data synchronization.

**Deployment:** Live at https://pivot-digital-466902.ts.r.appspot.com | **CI/CD:** Cloud Build (65 tests as deployment gate)

---

## Quick Start

### Prerequisites
- Python 3.12+
- Google Cloud credentials (Application Default Credentials)
- HubSpot Private App token
- Employment Hero OAuth credentials

### Run Locally

```bash
pip install -r requirements.txt
export FLASK_APP=main.py
python main.py
# Visit http://localhost:8080
```

### Deploy to App Engine

```bash
gcloud app deploy
```

See [Configuration Guide](docs/CONFIGURATION.md) for environment variable setup.

---

## Features at a Glance

| Feature | Purpose | Documentation |
|---------|---------|---------------|
| **Adviser Allocation** | Automatically assign HubSpot deals to advisers based on earliest availability | [Architecture](ARCHITECTURE.md) |
| **Availability Dashboard** | Real-time view of adviser schedules, capacity, and meetings | [User Guide](docs/user-guide.md) |
| **Box Folder Management** | Create and tag client folders from HubSpot deals | [User Guide](docs/user-guide.md) |
| **Admin Tools** | Manage office closures, capacity overrides, Box settings | [User Guide](docs/user-guide.md) |
| **HR Integration** | Sync employees and leave requests from Employment Hero | [Integrations](docs/INTEGRATIONS.md) |

---

## Quick Facts

| Category | Details |
|----------|---------|
| **Platform** | Google App Engine Standard (Python 3.12) |
| **Project** | `pivot-digital-466902` (Region: australia-southeast1) |
| **Database** | Google Cloud Firestore |
| **CI/CD** | Cloud Build (auto-deploy on main; 65 tests required) |
| **Integrations** | HubSpot (CRM), Employment Hero (HR), Box (Documents), Google Chat (Notifications) |
| **Authentication** | Employment Hero OAuth 2.0 + Session-based admin |
| **Deployment** | https://pivot-digital-466902.ts.r.appspot.com |

**Requirements:** Python 3.12+, Google Cloud credentials, HubSpot/Employment Hero/Box API access

---

## Documentation

### Quick Links by Role

**👤 For End Users (Operators):**
- **View Availability** - Check adviser schedules and earliest available weeks
- **Manage Allocations** - See allocation history and deal owner assignments
- **Admin Tools** - Add office closures, adjust capacity overrides, configure Box

👉 Start with [User Guide](docs/user-guide.md) | [Box Workflow](docs/box-folder-workflow.md)

**🛠️ For Developers:**
- **Architecture** - System design, allocation algorithm, core modules
- **API Reference** - All endpoints, webhooks, request/response formats
- **Configuration** - Environment variables, secrets, OAuth setup
- **Operations** - Cloud Scheduler, monitoring, troubleshooting
- **Integrations** - HubSpot, Employment Hero, Box, Google Chat

👉 Start with [Documentation Index](docs/README.md)

**📚 Reference:**
- [CI/CD Summary](CI_CD_SUMMARY.md) - Build pipeline details
- [Deployment Verification](DEPLOYMENT_VERIFICATION.md) - Verification checklist
- [Skills Framework](docs/SKILLS_FRAMEWORK.md) - Custom skills system
- [Changelog](docs/CHANGELOG.md) - Version history

For detailed system diagrams and algorithm explanations, see [Architecture Guide](ARCHITECTURE.md).

---

## Key Integrations

| Service | Purpose | Setup |
|---------|---------|-------|
| **HubSpot** | CRM (deals, contacts, meetings) | [HubSpot Setup](docs/CONFIGURATION.md#hubspot-configuration) |
| **Employment Hero** | HR (employees, leave requests) | [EH Setup](docs/CONFIGURATION.md#employment-hero-oauth-setup) |
| **Box** | Document storage (client folders) | [Box Setup](docs/CONFIGURATION.md#box-configuration) |
| **Google Chat** | Notifications (allocation alerts) | [Chat Setup](docs/CONFIGURATION.md#google-chat-integration) |

---

## Troubleshooting

| Issue | Solution |
|-------|----------|
| OAuth fails | Verify `REDIRECT_URI` matches Employment Hero config exactly |
| Firestore errors | Set `USE_FIRESTORE=false` for local OAuth testing |
| Tests fail | Run `pytest tests/ -v` to see full output |
| Allocation not working | Check HubSpot API token is valid and advisers exist in Firestore |

👉 **Full troubleshooting guide:** [Operations](docs/OPERATIONS.md#troubleshooting)

---

## License

Internal project.
