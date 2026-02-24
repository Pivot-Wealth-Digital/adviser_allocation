---
globs:
  - "**/firestore/**"
  - "**/models.py"
  - "**/*_db.py"
---
# Database Rules — adviser_allocation

## Data Store: Firestore (NOT CloudSQL)

This repo uses Firestore, not the shared CloudSQL instance.

### This repo OWNS (read/write):
- Firestore collection: `employees`
- Firestore collection: `leave`
- Firestore collection: `closures`
- Firestore collection: `capacity_overrides`
- Firestore collection: `allocation_history`
- Firestore collection: `eh_tokens`

### This repo can READ (read-only from CloudSQL):
- `hubspot_deals` — via HubSpot API, not direct DB access
- `hubspot_owners` — via HubSpot API, not direct DB access

### NEVER write to:
- Any CloudSQL table — this repo uses Firestore only
- Any `hubspot_*` table — use HubSpot API instead
- Any `box_*` table — not used by this repo

## Firestore Patterns
- Use batch writes for multiple document updates
- Always use transactions for read-then-write operations
- Document IDs should be meaningful (employee email, date, etc.)

## External APIs (not databases)
- HubSpot: Read deals, owners, users, meetings
- Employment Hero: Read employees, leave requests
- Box: Create folders (write via Box API, not database)
- Google Chat: Send alerts
