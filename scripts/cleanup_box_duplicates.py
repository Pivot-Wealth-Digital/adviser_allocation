"""Delete duplicate Box folders from 1. Active Clients.

Duplicates are identified by a (N) suffix in the folder name and verified
to contain only the template structure (4 subfolders, <=1 file total).

Usage:
    # Set your Box token (developer token from Box dev console, or from App Engine service)
    export BOX_ACCESS_TOKEN="your_token_here"

    python scripts/cleanup_box_duplicates.py                    # dry-run (list only)
    python scripts/cleanup_box_duplicates.py --delete           # actually trash them
    python scripts/cleanup_box_duplicates.py --as-user 12345    # impersonate a Box user
"""

import argparse
import re
import sys
import time
from typing import Optional

import requests

BOX_API_BASE = "https://api.box.com/2.0"
ACTIVE_CLIENTS_FOLDER_ID = "89432789614"  # "1. Active Clients" folder

# Template subfolders that a clean duplicate should have
TEMPLATE_SUBFOLDERS = {
    "To send to Pivot",
    "Pivot - Client Sharing",
    "Provider sharing",
    "Pivot Internal Docs",
}

DUPLICATE_RE = re.compile(r"^(.+)\s+\((\d+)\)$")


class BoxClient:
    """Minimal Box API client using a bearer token."""

    def __init__(self, token: str, as_user_id: Optional[str] = None):
        self.token = token
        self.as_user_id = as_user_id

    def _headers(self) -> dict:
        h = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
        }
        if self.as_user_id:
            h["As-User"] = self.as_user_id
        return h

    def list_folder_items(self, folder_id: str) -> list[dict]:
        items = []
        offset = 0
        while True:
            resp = requests.get(
                f"{BOX_API_BASE}/folders/{folder_id}/items",
                headers=self._headers(),
                params={"limit": 1000, "offset": offset, "fields": "id,name,type"},
                timeout=20,
            )
            resp.raise_for_status()
            data = resp.json()
            entries = data.get("entries", [])
            items.extend(entries)
            if not entries or len(items) >= data.get("total_count", len(items)):
                break
            offset += len(entries)
        return items

    def trash_folder(self, folder_id: str) -> bool:
        resp = requests.delete(
            f"{BOX_API_BASE}/folders/{folder_id}",
            headers=self._headers(),
            params={"recursive": "true"},
            timeout=20,
        )
        if resp.status_code == 204:
            return True
        print(f"  ERROR: Delete returned {resp.status_code}: {resp.text}")
        return False


def is_template_only(client: BoxClient, folder_id: str) -> tuple[bool, str]:
    """Check if folder contains only the template structure (4 subfolders, <=1 file)."""
    items = client.list_folder_items(folder_id)
    folders = [i for i in items if i["type"] == "folder"]
    files = [i for i in items if i["type"] == "file"]
    folder_names = {f["name"] for f in folders}

    if folder_names != TEMPLATE_SUBFOLDERS:
        extra = folder_names - TEMPLATE_SUBFOLDERS
        missing = TEMPLATE_SUBFOLDERS - folder_names
        parts = []
        if extra:
            parts.append(f"extra: {extra}")
        if missing:
            parts.append(f"missing: {missing}")
        return False, f"Non-template ({', '.join(parts)})"

    for subfolder in folders:
        time.sleep(0.15)
        sub_items = client.list_folder_items(subfolder["id"])
        sub_files = [i for i in sub_items if i["type"] == "file"]
        sub_folders = [i for i in sub_items if i["type"] == "folder"]
        if sub_folders:
            return False, f"'{subfolder['name']}' has child folders"
        if len(sub_files) > 1:
            return False, f"'{subfolder['name']}' has {len(sub_files)} files"

    if files:
        return False, f"Root has {len(files)} files"

    return True, "Template-only (safe)"


def main():
    parser = argparse.ArgumentParser(description="Clean up duplicate Box folders")
    parser.add_argument("--delete", action="store_true", help="Actually trash duplicates")
    parser.add_argument("--token", help="Box access token (or set BOX_ACCESS_TOKEN env var)")
    parser.add_argument("--as-user", help="Box user ID to impersonate")
    parser.add_argument("--folder-id", default=ACTIVE_CLIENTS_FOLDER_ID,
                        help="Parent folder ID (default: Active Clients)")
    args = parser.parse_args()

    import os
    token = args.token or os.environ.get("BOX_ACCESS_TOKEN", "").strip()
    if not token:
        print("ERROR: Provide a Box token via --token or BOX_ACCESS_TOKEN env var")
        print("\nGet a developer token from: https://app.box.com/developers/console")
        print("  -> Your App -> Configuration -> Developer Token -> Generate")
        sys.exit(1)

    client = BoxClient(token, as_user_id=args.as_user)

    # Verify token works
    print(f"Listing folders in Active Clients (id={args.folder_id})...")
    try:
        children = client.list_folder_items(args.folder_id)
    except requests.HTTPError as exc:
        print(f"ERROR: Box API call failed: {exc}")
        if "401" in str(exc):
            print("Token may be expired. Generate a new developer token.")
        sys.exit(1)

    folders = [c for c in children if c["type"] == "folder"]
    print(f"Total folders: {len(folders)}\n")

    # Find duplicates by (N) suffix
    duplicates = []
    for folder in folders:
        name = folder.get("name", "")
        match = DUPLICATE_RE.match(name)
        if match:
            duplicates.append({
                "id": folder["id"],
                "name": name,
                "base_name": match.group(1),
                "number": int(match.group(2)),
            })

    duplicates.sort(key=lambda d: (d["base_name"], d["number"]))
    print(f"Found {len(duplicates)} folders with (N) suffix:")
    print("-" * 70)

    safe_to_delete = []
    unsafe = []

    for dup in duplicates:
        print(f"\n  {dup['name']}  (id={dup['id']})")
        time.sleep(0.2)
        is_safe, reason = is_template_only(client, dup["id"])
        if is_safe:
            print(f"    -> SAFE: {reason}")
            safe_to_delete.append(dup)
        else:
            print(f"    -> SKIP: {reason}")
            unsafe.append(dup)

    print()
    print("=" * 70)
    print(f"SAFE to delete: {len(safe_to_delete)}")
    print(f"SKIPPED (has content): {len(unsafe)}")

    if unsafe:
        print("\nSkipped (need manual review):")
        for u in unsafe:
            print(f"  - {u['name']}")

    if not safe_to_delete:
        print("\nNothing to delete.")
        return

    if not args.delete:
        print(f"\nDRY RUN — add --delete to trash {len(safe_to_delete)} folders")
        return

    print(f"\nTrashing {len(safe_to_delete)} folders...")
    deleted = 0
    for dup in safe_to_delete:
        print(f"  Trashing: {dup['name']}...", end=" ")
        time.sleep(0.3)
        try:
            if client.trash_folder(dup["id"]):
                print("OK")
                deleted += 1
            else:
                print("FAILED")
        except requests.RequestException as exc:
            print(f"FAILED ({exc})")

    print(f"\nDone. Trashed {deleted}/{len(safe_to_delete)} duplicate folders.")


if __name__ == "__main__":
    main()
