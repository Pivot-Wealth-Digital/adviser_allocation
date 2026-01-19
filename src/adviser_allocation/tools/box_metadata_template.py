"""Utility script for managing Box metadata templates."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from adviser_allocation.services.box_folder_service import (  # noqa: E402
    BoxAutomationError,
    ensure_box_service,
)


def rename_template(display_name: str) -> dict:
    service = ensure_box_service()
    if not service:
        raise BoxAutomationError("Box automation is not configured")
    return service.rename_metadata_template(display_name)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Update the display name for the configured Box metadata template."
    )
    parser.add_argument(
        "--scope",
        help="Metadata scope (e.g. enterprise_123456)",
    )
    parser.add_argument(
        "--template-key",
        help="Metadata template key (e.g. clientHubspotPayload)",
    )
    parser.add_argument(
        "display_name",
        help="New display name for the metadata template (e.g. 'Client Hubspot Payload')",
    )
    args = parser.parse_args()

    if args.scope:
        os.environ["BOX_METADATA_SCOPE"] = args.scope
    if args.template_key:
        os.environ["BOX_METADATA_TEMPLATE_KEY"] = args.template_key

    try:
        result = rename_template(args.display_name)
    except BoxAutomationError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001
        print(f"Unexpected error: {exc}", file=sys.stderr)
        sys.exit(1)

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
