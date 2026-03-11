# Adviser Allocation

Adviser allocation algorithm for Pivot Wealth. Matches new clients to financial advisers based on capacity, availability, service packages, and leave schedules.

## Architecture

- **Runtime**: Cloud Run (Python 3.12 + Flask + Gunicorn)
- **Database**: CloudSQL (PostgreSQL, `client_pipeline` database, `aa_*` tables)
- **CI/CD**: GitHub Actions → Cloud Build → Cloud Run canary deploy
- **Region**: `australia-southeast1` only (Privacy Act 1988)

## Key Paths

| Area | Path |
|------|------|
| Entry point | `src/adviser_allocation/main.py` |
| Allocation logic | `src/adviser_allocation/core/allocation.py` |
| DB queries | `src/adviser_allocation/db/repository.py` |
| DB connection | `src/adviser_allocation/db/connection.py` |
| DB singleton | `src/adviser_allocation/utils/common.py` (`get_cloudsql_db()`) |
| Services | `src/adviser_allocation/services/` |
| API webhooks | `src/adviser_allocation/api/webhooks.py` |
| Templates | `templates/` |
| Tests | `tests/` |

## Development

```bash
# Start Cloud SQL Proxy
~/projects/git/google-cloud-sdk/bin/cloud-sql-proxy \
  pivot-digital-466902:australia-southeast1:client-pipeline-db \
  --port=5432 --auto-iam-authn &

# Run tests
CLOUD_SQL_USE_PROXY=true uv run pytest tests/ --ignore=tests/test_app_e2e.py -x -v

# Lint
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
```

## Data Flow

```
HubSpot API → allocation.py → adviser match → webhook response
                  ↓
Employment Hero → aa_employees + aa_leave_requests (CloudSQL)
Google Calendar → aa_office_closures (CloudSQL)
                  ↓
            capacity calculation → earliest availability
```

## Tables (aa_* prefix, this repo owns all)

- `aa_employees` — adviser profiles synced from Employment Hero
- `aa_leave_requests` — leave data per employee
- `aa_office_closures` — office closure dates (manual + calendar sync)
- `aa_capacity_overrides` — per-adviser capacity limits
- `aa_allocation_requests` — allocation history log
- `aa_oauth_tokens` — Employment Hero OAuth tokens
- `aa_simulated_clarifies` — simulated clarify meeting placements per adviser/week

## Rules

See `.claude/rules/` for detailed rules on:
- `laws.md` — hard rules (no secrets in git, PRs only, CI must pass)
- `quality.md` — naming, function size, error handling
- `security.md` — secrets, SQL injection, input validation
- `database.md` — table ownership, CloudSQL patterns
- `protected-files.md` — files requiring confirmation before edit
