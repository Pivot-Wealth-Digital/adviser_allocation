#!/usr/bin/env python3
"""Pre-commit hook to catch generic variable names.

Coding Law: No generic names like data, info, item, thing, result, temp, val.
Use descriptive names instead.

This hook WARNS but does not block (existing code has violations).
"""
import ast
import sys
from pathlib import Path

# Forbidden standalone variable names
FORBIDDEN_NAMES = {"data", "info", "item", "thing", "result", "temp", "val", "obj"}

# Allowed contexts (these are often fine)
ALLOWED_PATTERNS = {
    "response_data",
    "request_data",
    "json_data",
    "form_data",
    "post_data",
    "user_info",
    "file_info",
    "error_info",
    "list_item",
    "menu_item",
    "queue_item",
    "search_result",
    "query_result",
    "temp_file",
    "temp_dir",
}


def check_file(filepath: Path) -> list[str]:
    """Check a file for generic variable names."""
    warnings = []
    try:
        content = filepath.read_text(encoding="utf-8")
        tree = ast.parse(content)
    except (SyntaxError, UnicodeDecodeError):
        return warnings

    for node in ast.walk(tree):
        # Check simple assignments: x = ...
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    name = target.id
                    if name in FORBIDDEN_NAMES:
                        warnings.append(
                            f"{filepath}:{node.lineno}: Generic variable name '{name}'. "
                            f"Consider: '{name}_response', '{name}_payload', '{name}_record'"
                        )

        # Check named expressions (walrus): (x := ...)
        elif isinstance(node, ast.NamedExpr):
            if isinstance(node.target, ast.Name):
                name = node.target.id
                if name in FORBIDDEN_NAMES:
                    warnings.append(
                        f"{filepath}:{node.lineno}: Generic variable name '{name}'. "
                        f"Consider a more descriptive name."
                    )

    return warnings


def main() -> int:
    """Main entry point."""
    warnings = []
    for arg in sys.argv[1:]:
        path = Path(arg)
        if path.suffix == ".py" and path.exists():
            warnings.extend(check_file(path))

    if warnings:
        print("Generic Variable Name Warnings (Coding Law):")
        print("-" * 50)
        for warning in warnings:
            print(f"  WARNING: {warning}")
        print()
        print("Tip: Use descriptive names like 'contact_data', 'sync_result', etc.")
        print("(This is a warning - commit will proceed)")
        print()

    # Return 0 (success) - this is a warning, not a blocker
    return 0


if __name__ == "__main__":
    sys.exit(main())
