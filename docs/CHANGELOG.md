# Changelog

Documented updates and their release dates.

## v1.1.0 - 2025-10-13
- Enforced that adviser allocations start the week after a deal’s agreement date and skip any immediately following Full OOO weeks when selecting availability (`allocate.py`).
- Normalized Full OOO handling across the UI edit mode so Actual stays at zero and Difference reflects the target gap (`templates/availability_schedule.html`).
- Centralized project guides under `docs/` and added this changelog to track future revisions.
