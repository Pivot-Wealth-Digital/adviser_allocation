"""Utility helpers for Box collaboration lookups."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List

import requests

# Allow running as a script without PYTHONPATH tweaks.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from adviser_allocation.services.box_folder_service import (  # noqa: E402
    BoxAutomationError,
    ensure_box_service,
)

PIVOT_DOMAIN = "@pivotwealth.com.au"


def fetch_non_pivotwealth_emails(folder_id: str) -> List[str]:
    """Return a sorted list of collaborator emails not ending with the Pivot domain."""
    folder_id = (folder_id or "").strip()
    if not folder_id:
        raise ValueError("folder_id is required")

    service = ensure_box_service()
    if not service:
        raise BoxAutomationError("Box automation is not configured")

    url = f"{service._api_base_url}/folders/{folder_id}/collaborations"  # noqa: SLF001
    resp = requests.get(url, headers=service._headers(), timeout=service._timeout)  # noqa: SLF001
    resp.raise_for_status()
    entries = resp.json().get("entries", [])

    def _email(entry: dict) -> str:
        user = entry.get("accessible_by") or {}
        return (user.get("login") or "").strip()

    emails = {
        email.lower()
        for email in (_email(entry) for entry in entries)
        if email and not email.lower().endswith(PIVOT_DOMAIN)
    }
    return sorted(emails)


def main() -> None:
    parser = argparse.ArgumentParser(description="List non-pivotwealth collaborators for a folder")
    parser.add_argument("folder_id", help="Box folder id")
    args = parser.parse_args()
    emails = fetch_non_pivotwealth_emails(args.folder_id)
    if not emails:
        print("No external collaborators found")
    else:
        for email in emails:
            print(email)


if __name__ == "__main__":
    main()
