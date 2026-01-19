#!/usr/bin/env python3
"""
Box SDK Script to:
1. Search for "Active Clients" folder
2. Create a new folder called "Test Client - Manual Tag" inside it
3. Return the folder ID
"""

import json
import sys
from pathlib import Path

import requests
from boxsdk import JWTAuth, Client
from boxsdk.exception import BoxAPIException

# Configuration
JWT_CONFIG_PATH = "/Users/noeljeffreypinton/projects/git/adviser_allocation/config/box_jwt_config.json"
BOX_IMPERSONATION_USER = "noel.pinton@pivotwealth.com.au"
BOX_API_BASE_URL = "https://api.box.com/2.0"

# Search parameters
SEARCH_FOLDER_NAME = "Active Clients"
NEW_FOLDER_NAME = "Test Client - Manual Tag"


def load_jwt_config(config_path: str) -> dict:
    """Load JWT configuration from file."""
    config_path = Path(config_path)
    if not config_path.exists():
        raise FileNotFoundError(f"JWT config file not found: {config_path}")

    with open(config_path, 'r', encoding='utf-8') as f:
        return json.load(f)


def find_box_user_id(client: Client, identifier: str) -> str:
    """Find a Box user by email or username."""
    term = (identifier or "").strip()
    if not term:
        raise ValueError("Identifier is required")

    try:
        # Try exact email match first
        users = client.users(limit=1000, filter_term=term)
        exact_match = None
        partial_match = None

        for user in users:
            login = (user.login or "").lower()
            name = (user.name or "").lower()
            if login == term.lower():
                exact_match = user.id
                break
            if term.lower() in login or term.lower() in name:
                partial_match = partial_match or user.id

        return exact_match or partial_match
    except BoxAPIException as exc:
        print(f"Error searching for user '{term}': {exc}", file=sys.stderr)
        return None


def resolve_folder_path(access_token: str, path: str, as_user_id: str = None) -> str:
    """Resolve a folder path (e.g., 'Team Advice/Pivot Clients/1. Active Clients') to folder ID."""
    try:
        # Normalize path
        normalized = "/".join(segment.strip() for segment in (path or "").split("/") if segment.strip())
        if not normalized:
            return "0"

        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        }
        if as_user_id:
            headers["As-User"] = as_user_id

        folder_id = "0"
        traversed = []
        for segment in normalized.split("/"):
            traversed.append(segment)
            print(f"   Resolving: {'/'.join(traversed)}")

            # List children of current folder
            params = {"limit": 1000, "offset": 0}
            resp = requests.get(
                f"{BOX_API_BASE_URL}/folders/{folder_id}/items",
                headers=headers,
                params=params,
                timeout=20,
            )
            resp.raise_for_status()

            entries = resp.json().get("entries", [])
            match = None
            for entry in entries:
                if entry.get("type") == "folder" and entry.get("name") == segment:
                    match = entry
                    break

            if not match:
                raise ValueError(f"Folder segment '{segment}' not found in '{'/'.join(traversed[:-1]) or '/'}'")

            folder_id = match.get("id")
            print(f"      Found: {match.get('name')} (ID: {folder_id})")

        print(f"   Successfully resolved path to folder ID: {folder_id}")
        return folder_id
    except requests.RequestException as exc:
        print(f"Error resolving folder path '{path}': {exc}", file=sys.stderr)
        return None
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return None


def list_folder_children(access_token: str, folder_id: str, as_user_id: str = None) -> list:
    """List all children in a folder."""
    try:
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        }
        if as_user_id:
            headers["As-User"] = as_user_id

        items = []
        offset = 0
        while True:
            params = {"limit": 1000, "offset": offset}
            resp = requests.get(
                f"{BOX_API_BASE_URL}/folders/{folder_id}/items",
                headers=headers,
                params=params,
                timeout=20,
            )
            resp.raise_for_status()

            data = resp.json()
            entries = data.get("entries", [])
            items.extend(entries)
            total_count = data.get("total_count", len(items))
            if not entries or len(items) >= total_count:
                break
            offset += len(entries)

        return items
    except requests.RequestException as exc:
        print(f"Error listing folder contents: {exc}", file=sys.stderr)
        return []


def create_folder_in_parent(access_token: str, parent_folder_id: str, folder_name: str, as_user_id: str = None) -> str:
    """Create a new folder inside a parent folder."""
    try:
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if as_user_id:
            headers["As-User"] = as_user_id

        payload = {
            "name": folder_name,
            "parent": {"id": parent_folder_id},
        }

        resp = requests.post(
            f"{BOX_API_BASE_URL}/folders",
            headers=headers,
            json=payload,
            timeout=20,
        )
        resp.raise_for_status()

        folder_data = resp.json()
        folder_id = folder_data.get("id")
        print(f"Created folder: {folder_data.get('name')} (ID: {folder_id})")
        return folder_id
    except requests.RequestException as exc:
        print(f"Error creating folder '{folder_name}': {exc}", file=sys.stderr)
        return None


def main():
    """Main execution function."""
    print("=" * 60)
    print("Box SDK Folder Creation Script")
    print("=" * 60)

    # Step 1: Load JWT config
    print("\n1. Loading JWT configuration...")
    try:
        jwt_config = load_jwt_config(JWT_CONFIG_PATH)
        print(f"   Successfully loaded JWT config from: {JWT_CONFIG_PATH}")
    except Exception as exc:
        print(f"   Error: {exc}", file=sys.stderr)
        return 1

    # Step 2: Authenticate with Box using JWT
    print("\n2. Authenticating with Box API...")
    try:
        auth = JWTAuth.from_settings_dictionary(jwt_config)
        access_token = auth.authenticate_instance()
        client = Client(auth)
        print(f"   Successfully authenticated with Box API")
    except Exception as exc:
        print(f"   Error: {exc}", file=sys.stderr)
        return 1

    # Step 3: Find Box user for impersonation
    impersonated_user_id = None
    print(f"\n3. Finding Box user for impersonation: {BOX_IMPERSONATION_USER}...")
    try:
        impersonated_user_id = find_box_user_id(client, BOX_IMPERSONATION_USER)
        if impersonated_user_id:
            print(f"   Found user (ID: {impersonated_user_id})")
        else:
            print(f"   Warning: Could not find user, proceeding without impersonation")
    except Exception as exc:
        print(f"   Warning: {exc}", file=sys.stderr)

    # Step 4: Resolve path to "Active Clients" folder
    active_clients_path = "Team Advice/Pivot Clients/1. Active Clients"
    print(f"\n4. Resolving path to '{active_clients_path}'...")
    active_clients_id = resolve_folder_path(access_token, active_clients_path, impersonated_user_id)
    if not active_clients_id:
        print(f"   Error: Could not resolve path to '{active_clients_path}'", file=sys.stderr)
        return 1

    # Step 5: Create new folder inside "Active Clients"
    print(f"\n5. Creating new folder '{NEW_FOLDER_NAME}' inside '{SEARCH_FOLDER_NAME}'...")
    new_folder_id = create_folder_in_parent(access_token, active_clients_id, NEW_FOLDER_NAME, impersonated_user_id)
    if not new_folder_id:
        print(f"   Error: Could not create folder", file=sys.stderr)
        return 1

    # Step 6: Return the folder ID
    print("\n" + "=" * 60)
    print("SUCCESS")
    print("=" * 60)
    print(f"\nNew Folder ID: {new_folder_id}")
    print(f"Folder Name: {NEW_FOLDER_NAME}")
    print(f"Parent Path: {active_clients_path}")
    print(f"Box URL: https://app.box.com/folder/{new_folder_id}")
    print("=" * 60 + "\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
