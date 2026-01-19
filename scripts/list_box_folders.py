#!/usr/bin/env python3
"""Script to list Box folder structure."""

import json
import sys
from pathlib import Path

from boxsdk import JWTAuth, Client

# Configuration
BOX_JWT_CONFIG_PATH = "/Users/noeljeffreypinton/projects/git/adviser_allocation/config/box_jwt_config.json"

# Validate JWT config file exists
if not Path(BOX_JWT_CONFIG_PATH).exists():
    print(f"Box JWT config file not found: {BOX_JWT_CONFIG_PATH}")
    sys.exit(1)

# Authenticate with Box using JWT
with open(BOX_JWT_CONFIG_PATH, 'r') as f:
    config = json.load(f)

auth = JWTAuth.from_settings_dictionary(config)
auth.authenticate_instance()
client = Client(auth)

# List root folder items
print("Root folder contents:")
print("=" * 60)

items = client.folder("0").get_items(limit=1000)
for item in items:
    print(f"  {item.type.upper()}: {item.name} (ID: {item.id})")

# If we find a Data Lake Project folder, list its contents
print("\n\nSearching for active clients folder...")
print("=" * 60)

items = client.folder("0").get_items(limit=1000)
for item in items:
    if item.type == "folder":
        print(f"\nFolder: {item.name} (ID: {item.id})")
        try:
            sub_items = client.folder(item.id).get_items(limit=100)
            for sub_item in sub_items:
                print(f"  └─ {sub_item.type.upper()}: {sub_item.name} (ID: {sub_item.id})")

                if sub_item.type == "folder":
                    try:
                        sub_sub_items = client.folder(sub_item.id).get_items(limit=100)
                        for sub_sub_item in sub_sub_items:
                            print(f"     └─ {sub_sub_item.type.upper()}: {sub_sub_item.name} (ID: {sub_sub_item.id})")

                            if sub_sub_item.type == "folder" and "active" in sub_sub_item.name.lower():
                                print(f"\n*** FOUND ACTIVE CLIENTS FOLDER: {sub_sub_item.id} ***")
                    except Exception as e:
                        print(f"     Error listing items: {e}")
        except Exception as e:
            print(f"  Error listing items: {e}")
