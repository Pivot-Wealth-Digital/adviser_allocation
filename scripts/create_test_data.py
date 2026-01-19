#!/usr/bin/env python3
"""
Script to create test data for the manual metadata tagging feature.
Creates a HubSpot contact and a Box folder for testing.
"""

import json
import sys
import logging
from pathlib import Path

import requests
from boxsdk import JWTAuth, Client
from boxsdk.exception import BoxAPIException

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class HubSpotAPI:
    """HubSpot API client for creating contacts."""

    def __init__(self, token: str, portal_id: str):
        self.token = token
        self.portal_id = portal_id
        self.base_url = "https://api.hubapi.com"
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    def create_contact(self, email: str, firstname: str = "Test", lastname: str = "Contact") -> dict:
        """
        Create a HubSpot contact with the given email and name.

        Args:
            email: Contact email
            firstname: Contact first name
            lastname: Contact last name

        Returns:
            Dict containing the contact ID and properties
        """
        url = f"{self.base_url}/crm/v3/objects/contacts"

        payload = {
            "properties": {
                "email": email,
                "firstname": firstname,
                "lastname": lastname,
            }
        }

        logger.info(f"Creating HubSpot contact: {email}")
        response = requests.post(
            url,
            headers=self.headers,
            json=payload,
            timeout=10
        )

        if response.status_code == 409:
            # Contact already exists, need to search for it
            logger.warning(f"Contact {email} already exists, retrieving existing contact")
            contact_id = self._find_contact_by_email(email)
            if contact_id:
                return {
                    "id": contact_id,
                    "email": email,
                    "firstname": firstname,
                    "lastname": lastname,
                    "status": "existing",
                    "portal_id": self.portal_id,
                    "url": f"https://app.hubspot.com/contacts/{self.portal_id}/record/0-1/{contact_id}"
                }
            else:
                raise ValueError(f"Contact exists but could not be retrieved: {email}")

        response.raise_for_status()
        data = response.json()

        contact_id = data.get("id")
        logger.info(f"Created HubSpot contact: {contact_id}")

        return {
            "id": contact_id,
            "email": email,
            "firstname": firstname,
            "lastname": lastname,
            "status": "created",
            "portal_id": self.portal_id,
            "url": f"https://app.hubspot.com/contacts/{self.portal_id}/record/0-1/{contact_id}"
        }

    def _find_contact_by_email(self, email: str) -> str:
        """Find a contact by email and return its ID."""
        url = f"{self.base_url}/crm/v3/objects/contacts/search"
        params = {
            "limit": 1,
            "properties": ["email", "firstname", "lastname"],
            "filterGroups": [
                {
                    "filters": [
                        {
                            "propertyName": "email",
                            "operator": "EQ",
                            "value": email
                        }
                    ]
                }
            ]
        }

        try:
            response = requests.post(
                url,
                headers={**self.headers, "Content-Type": "application/json"},
                json=params,
                timeout=10
            )
            response.raise_for_status()
            data = response.json()
            results = data.get("results", [])
            if results:
                return results[0].get("id")
        except requests.RequestException as e:
            logger.warning(f"Failed to search for contact by email {email}: {e}")

        return None


class BoxAPI:
    """Box API client for creating folders."""

    def __init__(self, jwt_config_path: str, impersonation_user: str):
        self.jwt_config_path = jwt_config_path
        self.impersonation_user = impersonation_user
        self.client = None
        self._authenticate()

    def _authenticate(self):
        """Authenticate with Box using JWT."""
        logger.info("Authenticating with Box API using JWT")

        with open(self.jwt_config_path, 'r') as f:
            config = json.load(f)

        auth = JWTAuth.from_settings_dictionary(config)
        access_token = auth.authenticate_instance()
        self.client = Client(auth)

        logger.info("Successfully authenticated with Box API")

    def _find_user_id(self, email: str) -> str:
        """Find a Box user ID by email."""
        logger.info(f"Finding Box user ID for: {email}")

        try:
            users = self.client.users(filter_term=email, limit=10)
            for user in users:
                if user.login.lower() == email.lower():
                    logger.info(f"Found user {email} with ID: {user.id}")
                    return user.id

            logger.warning(f"Could not find user {email}, returning None")
            return None
        except BoxAPIException as e:
            logger.error(f"Failed to find Box user {email}: {e}")
            return None

    def get_folder_by_path(self, path: str) -> str:
        """
        Get folder ID by path.

        Args:
            path: Folder path like "Team Advice/Pivot Clients/1. Active Clients"

        Returns:
            Folder ID
        """
        logger.info(f"Resolving folder path: {path}")

        folder_id = "0"  # Root folder
        segments = [s.strip() for s in path.split("/") if s.strip()]

        for segment in segments:
            items = self.client.folder(folder_id).get_items(limit=1000)
            found = False

            for item in items:
                if item.name == segment and item.type == "folder":
                    folder_id = item.id
                    logger.info(f"Found folder '{segment}' with ID: {folder_id}")
                    found = True
                    break

            if not found:
                raise ValueError(f"Folder segment '{segment}' not found in path: {path}")

        return folder_id

    def create_folder(self, parent_folder_id: str, folder_name: str) -> dict:
        """
        Create a folder in Box.

        Args:
            parent_folder_id: ID of the parent folder
            folder_name: Name for the new folder

        Returns:
            Dict containing folder ID and details
        """
        logger.info(f"Creating Box folder: {folder_name} in parent {parent_folder_id}")

        parent_folder = self.client.folder(parent_folder_id)
        subfolder = parent_folder.create_subfolder(folder_name)

        logger.info(f"Created Box folder: {subfolder.id}")

        return {
            "id": subfolder.id,
            "name": subfolder.name,
            "parent_id": parent_folder_id,
            "status": "created",
            "url": f"https://app.box.com/folder/{subfolder.id}"
        }

    def create_folder_in_root(self, folder_name: str) -> dict:
        """
        Create a folder in the Box root directory.

        Args:
            folder_name: Name for the new folder

        Returns:
            Dict containing folder ID and details
        """
        logger.info(f"Creating Box folder in root: {folder_name}")

        root_folder = self.client.folder("0")

        try:
            subfolder = root_folder.create_subfolder(folder_name)
            logger.info(f"Created Box folder in root: {subfolder.id}")
            return {
                "id": subfolder.id,
                "name": subfolder.name,
                "parent_id": "0",
                "status": "created",
                "url": f"https://app.box.com/folder/{subfolder.id}"
            }
        except BoxAPIException as e:
            if e.status == 409:
                # Folder already exists, find it
                logger.warning(f"Folder '{folder_name}' already exists, retrieving existing folder")
                try:
                    items = root_folder.get_items(limit=1000)
                    for item in items:
                        if item.type == "folder" and item.name == folder_name:
                            logger.info(f"Found existing folder: {item.id}")
                            return {
                                "id": item.id,
                                "name": item.name,
                                "parent_id": "0",
                                "status": "existing",
                                "url": f"https://app.box.com/folder/{item.id}"
                            }
                except BoxAPIException as search_error:
                    logger.error(f"Failed to find existing folder: {search_error}")
                raise ValueError(f"Folder exists but could not be retrieved: {folder_name}")
            raise


def main():
    """Main function to create test data."""

    # Configuration
    HUBSPOT_TOKEN = os.getenv("HUBSPOT_TOKEN", "your-token-here")
    HUBSPOT_PORTAL_ID = "47011873"
    PROJECT_ROOT = Path(__file__).resolve().parent.parent
    BOX_JWT_CONFIG_PATH = PROJECT_ROOT / "config" / "box_jwt_config.json"
    BOX_IMPERSONATION_USER = "noel.pinton@pivotwealth.com.au"
    BOX_ACTIVE_CLIENTS_PATH = "Team Advice/Pivot Clients/1. Active Clients"

    # Test data
    TEST_CONTACT_EMAIL = "test.contact@example.com"
    TEST_CONTACT_FIRSTNAME = "Test"
    TEST_CONTACT_LASTNAME = "Contact"
    TEST_FOLDER_NAME = "Test Client Folder"

    # Validate JWT config file exists
    if not Path(BOX_JWT_CONFIG_PATH).exists():
        logger.error(f"Box JWT config file not found: {BOX_JWT_CONFIG_PATH}")
        sys.exit(1)

    result = {
        "hubspot_contact": None,
        "box_folder": None,
        "errors": []
    }

    try:
        # Create HubSpot contact
        logger.info("=" * 60)
        logger.info("Creating HubSpot Contact")
        logger.info("=" * 60)

        hubspot_api = HubSpotAPI(HUBSPOT_TOKEN, HUBSPOT_PORTAL_ID)
        contact = hubspot_api.create_contact(
            email=TEST_CONTACT_EMAIL,
            firstname=TEST_CONTACT_FIRSTNAME,
            lastname=TEST_CONTACT_LASTNAME
        )
        result["hubspot_contact"] = contact
        logger.info(f"✓ HubSpot Contact created: {contact['id']}")

    except Exception as e:
        error_msg = f"Failed to create HubSpot contact: {str(e)}"
        logger.error(error_msg)
        result["errors"].append(error_msg)

    try:
        # Create Box folder
        logger.info("=" * 60)
        logger.info("Creating Box Folder")
        logger.info("=" * 60)

        box_api = BoxAPI(BOX_JWT_CONFIG_PATH, BOX_IMPERSONATION_USER)

        # Try to get the parent folder ID from the configured path
        parent_folder_id = None
        try:
            logger.info(f"Attempting to resolve parent folder path: {BOX_ACTIVE_CLIENTS_PATH}")
            parent_folder_id = box_api.get_folder_by_path(BOX_ACTIVE_CLIENTS_PATH)
            logger.info(f"Parent folder ID: {parent_folder_id}")
        except ValueError as path_error:
            logger.warning(f"Could not resolve path {BOX_ACTIVE_CLIENTS_PATH}: {path_error}")
            logger.info("Will create folder in Box root instead")
            parent_folder_id = None

        # If path resolution failed, create in root
        if parent_folder_id is None:
            logger.info("Creating folder in Box root (ID: 0)")
            folder = box_api.create_folder_in_root(TEST_FOLDER_NAME)
        else:
            # Create the test folder under the resolved parent
            folder = box_api.create_folder(parent_folder_id, TEST_FOLDER_NAME)

        result["box_folder"] = folder
        logger.info(f"✓ Box Folder created: {folder['id']}")

    except Exception as e:
        error_msg = f"Failed to create Box folder: {str(e)}"
        logger.error(error_msg)
        result["errors"].append(error_msg)

    # Print results
    logger.info("=" * 60)
    logger.info("Test Data Creation Summary")
    logger.info("=" * 60)

    if result["hubspot_contact"]:
        print("\n✓ HubSpot Contact Created:")
        print(f"  - Contact ID: {result['hubspot_contact']['id']}")
        print(f"  - Email: {result['hubspot_contact']['email']}")
        print(f"  - Name: {result['hubspot_contact']['firstname']} {result['hubspot_contact']['lastname']}")
        print(f"  - Status: {result['hubspot_contact'].get('status', 'unknown')}")
        if result['hubspot_contact'].get('url'):
            print(f"  - URL: {result['hubspot_contact']['url']}")
    else:
        print("\n✗ HubSpot Contact: FAILED")

    if result["box_folder"]:
        print("\n✓ Box Folder Created:")
        print(f"  - Folder ID: {result['box_folder']['id']}")
        print(f"  - Folder Name: {result['box_folder']['name']}")
        print(f"  - Parent ID: {result['box_folder']['parent_id']}")
        print(f"  - Status: {result['box_folder'].get('status', 'unknown')}")
        print(f"  - URL: {result['box_folder']['url']}")
    else:
        print("\n✗ Box Folder: FAILED")

    if result["errors"]:
        print("\n⚠ Errors encountered:")
        for error in result["errors"]:
            print(f"  - {error}")

    # Print final result JSON
    print("\n" + "=" * 60)
    print("Result JSON:")
    print("=" * 60)
    print(json.dumps(result, indent=2))

    # Return exit code
    if result["errors"]:
        sys.exit(1)

    return result


if __name__ == "__main__":
    main()
