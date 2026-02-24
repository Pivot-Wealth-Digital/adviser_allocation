# Contributing to adviser_allocation

Welcome to the team! This guide covers our coding standards, workflow, and the laws that keep production stable.

## Quick Start

```bash
# Clone and setup
git clone git@github.com:pivot-wealth/adviser_allocation.git
cd adviser_allocation

# Install dependencies (Python 3.12)
uv sync

# Install pre-commit hooks (required)
pre-commit install

# Copy environment template
cp .env.example .env
# Edit .env with your local settings

# Run tests to verify setup
uv run pytest tests/ -v
```

## The Laws

These are non-negotiable. Violating them breaks production, leaks data, or creates unrecoverable mess.

### 1. No secrets in git — ever

No API keys, tokens, passwords, or credentials in source code, config files, or commit messages.

- **Do:** Use `os.getenv("SECRET_NAME")` or Google Secret Manager
- **Do:** Store local secrets in `.env` (gitignored)
- **Don't:** Hardcode `HUBSPOT_TOKEN = "pat-abc123..."` anywhere

The `detect-secrets` pre-commit hook will block commits containing secrets.

### 2. No direct writes to data stores you don't own

Each repo owns specific data stores. Only the owning repo writes to its stores.

| Data Store | Owner (writes) | Consumers (read-only) |
|------------|----------------|----------------------|
| CloudSQL `hubspot_*`, `box_*` tables | gcs-data-lake | pivotapp, **adviser_allocation** |
| CloudSQL `client_documents`, etc. | pivotapp | — |
| Firestore (employees, leave, capacity, allocations) | **adviser_allocation** | — |
| CloudSQL `zoom_*` tables | zoom-bot | — |

**This repo owns:** Firestore collections (`employees`, `leave_requests`, `office_closures`, `capacity_overrides`, `allocation_history`)

**This repo reads (read-only):** CloudSQL `hubspot_deals`, `hubspot_owners`

### 3. All changes to main via PR

No direct push to main. Every change goes through a pull request with at least 1 approval.

```bash
# Correct workflow
git checkout -b feature/my-change
# ... make changes ...
git commit -m "feat: add new feature"
git push -u origin feature/my-change
# Create PR on GitHub
```

Emergency hotfixes still go via PR — flag as urgent for fast review.

### 4. CI must pass before merge

All CI checks (lint, type check, tests) must pass before merge. No exceptions.

- **Don't:** Use `--no-verify` to skip hooks
- **Don't:** Merge with "I'll fix it later"
- **Do:** If CI is broken, fix CI first

Cloud Build auto-deploys on push to main — tests must pass first.

### 5. Every production-impacting change includes a rollback plan

Any change that can impact production (deployments, migrations, backfills, infra changes) must include explicit rollback steps in the PR description.

Example rollback plan:
```
## Rollback
1. Revert to previous App Engine version: `gcloud app versions migrate <previous-version>`
2. If Firestore data was modified: restore from backup or manual correction
```

### 6. Data migrations in the owning repo only

This repo owns Firestore collections. CloudSQL schema changes belong to gcs-data-lake.

### 7. Australian region only

All GCP resources must be in `australia-southeast1`. No exceptions. This is a Privacy Act 1988 compliance requirement.

### 8. Respect the change budget

Keep PRs reviewable and rollback-safe.

| Metric | Soft Limit | Hard Limit |
|--------|-----------|------------|
| Files changed | 15 | 25 |
| Lines changed | 400 | 800 |
| New dependencies | 2 | 5 |

**Scope rules:**
- Bug fix → max 5 files
- Typo/docs → 1-2 files
- Refactor PR → no new features
- Feature PR → no unrelated refactoring

Exceeding soft limit? Add a comment explaining why. Exceeding hard limit? Split the PR.

---

## Code Standards

### Formatting & Linting

| Tool | Config | Command |
|------|--------|---------|
| black | 100 char lines | `uv run black .` |
| flake8 | `.flake8` | `uv run flake8` |
| mypy | `pyproject.toml` | `uv run mypy src/` |
| isort | `pyproject.toml` | `uv run isort .` |

Run all checks: `pre-commit run --all-files`

### Naming

```python
# Bad
data = fetch_data()
result = process(data)
timeout = 30

# Good
adviser_capacities = fetch_adviser_capacities()
eligible_advisers = filter_by_availability(adviser_capacities)
timeout_seconds = 30
```

Avoid:
- Generic names: `data`, `info`, `item`, `thing`, `result`, `temp`, `val`
- Vague verbs: `process`, `handle`, `do`, `perform`, `manage`

Required:
- Numeric variables need unit suffixes: `timeout_ms`, `price_cents`, `distance_km`
- Dates need timezone indication: `created_at_utc`, `expires_at_sydney`

### Functions

- Max **50 lines** per function — split if longer
- Max **4 parameters** — use a dataclass for more
- `get_*`/`fetch_*`/`find_*` functions must NOT have side effects
- No boolean params that change behaviour — use separate functions

```python
# Bad
def allocate_deal(deal, dry_run=False):
    ...

# Good
def allocate_deal(deal):
    ...

def preview_deal_allocation(deal):
    ...
```

### Error Handling

```python
# Bad — swallowed exception
try:
    result = risky_operation()
except Exception:
    pass

# Good — logged and handled
try:
    result = risky_operation()
except OperationError as e:
    logger.exception("Failed to complete operation")
    raise
```

- Every `except` must log or re-raise — no silent swallowing
- Use specific exceptions, not bare `except:`
- Never expose internal errors to clients — use generic messages

### TODOs

```python
# Bad
# TODO: fix this later

# Good
# TODO(ALLOC-123): Handle adviser on extended leave
```

If no ticket exists, create one or don't leave the TODO.

### Logging

```python
# Bad
print(f"Allocating deal {deal_id}")

# Good
logger.info("Allocating deal", extra={"deal_id": deal_id, "adviser_id": adviser_id})
```

Never log: passwords, tokens, PII, full credit card numbers.

---

## Git Workflow

### Commit Messages

We use [Conventional Commits](https://www.conventionalcommits.org/):

```
type: short description

Optional longer description.
```

Types:
- `feat` — new feature
- `fix` — bug fix
- `docs` — documentation only
- `chore` — maintenance (deps, config)
- `refactor` — code change that neither fixes nor adds
- `style` — formatting only
- `ci` — CI/CD changes
- `perf` — performance improvement
- `test` — adding or fixing tests

Examples:
```
feat: add capacity override for holiday periods
fix: handle adviser with no upcoming availability
docs: update allocation algorithm explanation
chore: upgrade google-cloud-firestore to 2.14.0
refactor: extract eligibility logic to separate module
```

### Branch Naming

```
feature/TICKET-123-short-description
fix/TICKET-456-bug-name
chore/update-dependencies
```

### PR Description

Use the PR template. At minimum include:

1. **What** — brief description of changes
2. **Why** — context/ticket link
3. **Testing** — how you verified it works
4. **Rollback** — steps to undo if something goes wrong (required for production-impacting changes)

---

## Deployment

Currently on App Engine Standard (migrating to Cloud Run). Cloud Build auto-deploys on push to main.

```bash
# Manual deploy (if needed)
gcloud app deploy

# View logs
gcloud app logs tail -s default
```

---

## Protected Files

These files have outsized impact. Changes require extra scrutiny:

| Category | Files | Extra Requirements |
|----------|-------|-------------------|
| Infrastructure | `app.yaml`, `cloudbuild.yaml`, `.github/workflows/*` | Explain why |
| Core logic | `src/adviser_allocation/core/allocation.py` | Test coverage for changes |
| Agent config | `CLAUDE.md`, `.claude/**` | Team discussion |
| Dependencies | `requirements.txt`, `pyproject.toml` | Justify new deps |

---

## Test Coverage

**Minimum coverage:** 70%

CI will fail if coverage drops below this threshold. This repo has the highest coverage requirement due to the business-critical nature of the allocation algorithm.

---

## Getting Help

- **Repo-specific context:** Read `CLAUDE.md` in the repo root
- **Architecture overview:** See the master repo's `CLAUDE.md`
- **Stuck on something:** Ask in the team channel or open a draft PR for discussion
