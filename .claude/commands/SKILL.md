# Build Validator - adviser_allocation

Validates code changes for the adviser allocation engine.

## When to Use

Run after modifying allocation logic, API endpoints, or Firestore interactions.

## Commands

```bash
# Run tests with coverage
pytest tests/ -v --cov=src/adviser_allocation

# Lint check
uv run ruff check src/ tests/

# Type check
mypy src/
```

## Key Test Areas

- **Allocation algorithm**: `tests/test_allocation.py`
- **API endpoints**: `tests/test_api.py`
- **Firestore operations**: `tests/test_firestore.py`
- **Employment Hero sync**: `tests/test_eh_sync.py`

## Deployment Note

This repo uses App Engine. After passing tests:

```bash
gcloud app deploy --project=pivot-digital-466902
```

CI/CD auto-deploys on push to main if tests pass.
