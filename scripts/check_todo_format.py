#!/usr/bin/env python3
"""Pre-commit hook to enforce ticket-linked format for action items.

Coding Law: Action items must follow format: # ACTION(TICKET-123): description
Never bare action items without ticket references.
"""
import re
import sys
from pathlib import Path

# Valid format: # TODO(ABC-123): description
VALID_TODO = re.compile(r"#\s*TODO\([A-Z]+-\d+\):\s*.+")
# Invalid: # TODO without proper ticket format (catches TODO:, TODO, todo, etc.)
INVALID_TODO = re.compile(r"#\s*TODO(?!\([A-Z]+-\d+\):)", re.IGNORECASE)


def check_file(filepath: Path) -> list[str]:
    """Check a file for invalid TODO comments."""
    errors = []
    try:
        content = filepath.read_text(encoding="utf-8")
    except Exception:
        return errors

    for i, line in enumerate(content.splitlines(), 1):
        if INVALID_TODO.search(line):
            errors.append(
                f"{filepath}:{i}: Invalid TODO format. "
                f"Use: # TODO(TICKET-123): description"
            )
    return errors


def main() -> int:
    """Main entry point."""
    errors = []
    for arg in sys.argv[1:]:
        path = Path(arg)
        if path.suffix == ".py" and path.exists():
            errors.extend(check_file(path))

    if errors:
        print("TODO Format Violations (Coding Law):")
        print("-" * 40)
        for error in errors:
            print(error)
        print()
        print("Fix: Use format # TODO(TICKET-123): description")
        print("If no ticket exists, create one first.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
