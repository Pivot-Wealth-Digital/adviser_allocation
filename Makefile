.PHONY: lint format check test

lint:
	uv run ruff format --check src/ tests/
	uv run ruff check src/ tests/

format:
	uv run ruff format src/ tests/

check:
	pre-commit run --all-files

test:
	pytest tests/ -v --cov=src/adviser_allocation --cov-report=term-missing --cov-fail-under=85
