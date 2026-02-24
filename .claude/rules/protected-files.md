# Protected Files — Extra Caution Required

Before modifying these files, WARN the user and confirm intent:

## Infrastructure
- `Dockerfile`, `cloudbuild.yaml`, `.github/workflows/*`
- Ask: "This is infrastructure config. Are you sure you want to modify it?"

## Schema
- `**/migrations/*.py`, `schema.sql`
- Ask: "This is a schema migration. What's the rollback plan?"

## Core Business Logic
- pivotapp: `src/handler.py`
- gcs-data-lake: `src/pipelines/*/sync.py`
- adviser_allocation: `src/adviser_allocation/core/allocation.py`
- zoom-bot: `src/processor.py`
- Ask: "This is core business logic. Should I add tests for the changes?"

## Agent Config
- `CLAUDE.md`, `.claude/**`
- Ask: "This affects AI agent behaviour for the whole team. Confirm?"

## Dependencies
- `requirements.txt`, `pyproject.toml`
- For new deps: Check if already installed, justify need, check for vulnerabilities
