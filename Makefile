.PHONY: lint format check test

lint:
	black --check src/ tests/
	isort --check-only src/ tests/
	flake8 src/ tests/

format:
	black src/ tests/
	isort src/ tests/

check:
	pre-commit run --all-files

test:
	pytest tests/ -v --cov=src/adviser_allocation --cov-report=term-missing --cov-fail-under=85
