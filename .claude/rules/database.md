# Database Rules — adviser_allocation

## Data Store: CloudSQL (client_pipeline database)

This repo uses CloudSQL via `get_cloudsql_db()` singleton.
All data access goes through `AdviserAllocationDB` in `db/repository.py`.

### This repo OWNS (read/write):
- `aa_employees` — synced from Employment Hero
- `aa_leave_requests` — synced from Employment Hero
- `aa_office_closures` — manual + Google Calendar sync
- `aa_capacity_overrides` — adviser capacity overrides
- `aa_allocation_requests` — allocation history
- `aa_oauth_tokens` — Employment Hero OAuth tokens

### This repo can READ (read-only):
- `hubspot_deals` — via HubSpot API, not direct DB access
- `hubspot_owners` — via HubSpot API, not direct DB access

### NEVER write to:
- Any table not prefixed with `aa_` — owned by other repos
- Any `hubspot_*` table — use HubSpot API instead
- Any `box_*` table — not used by this repo

## CloudSQL Patterns
- Use parameterised queries via SQLAlchemy `text()` — never f-strings
- Access DB via `get_cloudsql_db()` from `utils/common.py`
- One `db = get_cloudsql_db()` call per function, not per query

## External APIs (not databases)
- HubSpot: Read deals, owners, users, meetings
- Employment Hero: Read employees, leave requests
- Google Calendar: Read office closures
- Google Chat: Send alerts
