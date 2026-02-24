---
globs:
  - "**/*.py"
---
# Code Quality Rules

## Naming
- NO generic names: data, info, item, thing, result, temp, val, obj
- NO vague verbs: process, handle, do, perform, manage
- Numeric vars need units: timeout_ms, price_cents, distance_km
- Dates need timezone: created_at_utc, expires_at_sydney

## Functions
- Max 50 lines per function — split if longer
- Max 4 parameters — use dataclass/dict for more
- `get_*`/`fetch_*`/`find_*` = NO side effects (read-only)
- NO boolean params that change behaviour — use separate functions

## Error Handling
- Every `except` must log or re-raise — no silent swallowing
- Use specific exceptions, not bare `except:`
- Validate at boundaries, trust internal code

## TODOs
- Format: `# TODO(TICKET-123): description`
- NO bare TODOs: `# TODO: fix later` ← FORBIDDEN
- If no ticket exists, don't leave the TODO

## Formatting
- black with 100 char line length
- isort for imports
- flake8 for linting
- Run `pre-commit run --all-files` to check
