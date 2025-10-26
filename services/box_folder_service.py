from pathlib import Path
import json
import logging
import os
import re
from typing import Optional, List

import requests
from boxsdk import JWTAuth, Client
from boxsdk.exception import BoxAPIException

from utils.secrets import get_secret

DEFAULT_BOX_API_BASE_URL = "https://api.box.com/2.0"
FORBIDDEN_CHARS = set('\\/:*?"<>|')

logger = logging.getLogger(__name__)


class BoxAutomationError(RuntimeError):
    """Raised when Box folder automation fails."""


def sanitize_folder_name(name: str) -> str:
    sanitized = "".join("_" if ch in FORBIDDEN_CHARS else ch for ch in name)
    sanitized = re.sub(r"\s+", " ", sanitized).strip()
    if not sanitized:
        sanitized = "Client Folder"
    if len(sanitized) > 120:
        sanitized = sanitized[:117].rstrip() + "..."
    return sanitized


class BoxFolderService:
    """Encapsulates Box folder traversal and template copy workflow."""

    def __init__(
        self,
        *,
        token: str,
        template_path: str,
        destination_path: str,
        api_base_url: str = DEFAULT_BOX_API_BASE_URL,
        request_timeout: int = 20,
        as_user_id: Optional[str] = None,
    ):
        if not token:
            raise BoxAutomationError("Box access token is not configured")
        self._token = token
        self._template_path = template_path
        self._destination_path = destination_path
        self._api_base_url = api_base_url.rstrip("/")
        self._timeout = request_timeout
        self._as_user_id = as_user_id
        self._path_cache: dict[str, str] = {}

    def ensure_client_folder(self, folder_name: str) -> dict:
        sanitized = sanitize_folder_name(folder_name)
        parent_id = self._resolve_path(self._destination_path)

        # Check if folder already exists, and if so, create with numbered suffix
        existing = self._find_child_folder(parent_id, sanitized)
        if existing:
            logger.info(
                "Box folder '%s' already exists (id=%s), creating new folder with number suffix",
                sanitized,
                existing.get("id"),
            )
            # Find an available numbered folder name
            counter = 2
            max_attempts = 100
            while counter <= max_attempts:
                numbered_name = f"{sanitized} ({counter})"
                existing_numbered = self._find_child_folder(parent_id, numbered_name)
                if not existing_numbered:
                    sanitized = numbered_name
                    logger.info("Creating folder with suffix: %s", sanitized)
                    break
                counter += 1

        template_id = self._resolve_path(self._template_path)
        payload = {"name": sanitized, "parent": {"id": parent_id}}
        try:
            resp = requests.post(
                f"{self._api_base_url}/folders/{template_id}/copy",
                headers=self._headers("application/json"),
                json=payload,
                timeout=self._timeout,
            )
        except requests.RequestException as exc:
            raise BoxAutomationError(f"Box folder copy failed: {exc}") from exc

        if resp.status_code == 409:
            try:
                conflict = (resp.json().get("context_info", {}).get("conflicts") or [])[0]
                if conflict:
                    logger.info(
                        "Box folder conflict detected, returning existing folder (id=%s)",
                        conflict.get("id"),
                    )
                    return conflict
            except Exception:  # pragma: no cover
                pass
            raise BoxAutomationError(
                f"Box folder '{sanitized}' already exists and could not be retrieved"
            )

        try:
            resp.raise_for_status()
        except requests.HTTPError as exc:
            raise BoxAutomationError(f"Box API error during copy: {resp.text}") from exc

        logger.info(
            "Created Box folder '%s' beneath '%s'",
            sanitized,
            self._destination_path,
        )
        return resp.json()

    def _resolve_path(self, path: str) -> str:
        normalized = "/".join(segment.strip() for segment in (path or "").split("/") if segment.strip())
        if not normalized:
            return "0"
        cached = self._path_cache.get(normalized)
        if cached:
            return cached

        folder_id = "0"
        traversed: List[str] = []
        for segment in normalized.split("/"):
            traversed.append(segment)
            cache_key = "/".join(traversed)
            cached_segment = self._path_cache.get(cache_key)
            if cached_segment:
                folder_id = cached_segment
                continue

            match = next(
                (
                    item
                    for item in self._list_folder_children(folder_id)
                    if item.get("type") == "folder" and item.get("name") == segment
                ),
                None,
            )
            if not match:
                parent_path = "/" + "/".join(traversed[:-1])
                if parent_path == "/":
                    parent_path = "/"
                raise BoxAutomationError(
                    f"Box folder segment '{segment}' not found in '{parent_path}'"
                )
            folder_id = match["id"]
            self._path_cache[cache_key] = folder_id

        self._path_cache[normalized] = folder_id
        return folder_id

    def _list_folder_children(self, folder_id: str) -> List[dict]:
        items: List[dict] = []
        offset = 0
        while True:
            params = {"limit": 1000, "offset": offset}
            try:
                resp = requests.get(
                    f"{self._api_base_url}/folders/{folder_id}/items",
                    headers=self._headers(),
                    params=params,
                    timeout=self._timeout,
                )
            except requests.RequestException as exc:
                raise BoxAutomationError(
                    f"Box list children failed for folder {folder_id}: {exc}"
                ) from exc

            if resp.status_code == 404:
                raise BoxAutomationError(f"Box folder id {folder_id} not found")

            try:
                resp.raise_for_status()
            except requests.HTTPError as exc:
                raise BoxAutomationError(
                    f"Box API error listing folder {folder_id}: {resp.text}"
                ) from exc

            data = resp.json()
            entries = data.get("entries", [])
            items.extend(entries)
            total_count = data.get("total_count", len(items))
            if not entries or len(items) >= total_count:
                break
            offset += len(entries)

        return items

    def _find_child_folder(self, parent_id: str, folder_name: str) -> Optional[dict]:
        for item in self._list_folder_children(parent_id):
            if item.get("type") == "folder" and item.get("name") == folder_name:
                return item
        return None

    def _headers(self, content_type: Optional[str] = None) -> dict:
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
        }
        if content_type:
            headers["Content-Type"] = content_type
        if self._as_user_id:
            headers["As-User"] = self._as_user_id
        return headers


BOX_API_BASE_URL = os.environ.get("BOX_API_BASE_URL", DEFAULT_BOX_API_BASE_URL)
BOX_TEMPLATE_PATH = os.environ.get(
    "BOX_TEMPLATE_PATH", "Team Advice/Pivot Clients/2025 Client Box Folder Template"
)
# Note: "1. Active Clients" folder ID is 89432789614
BOX_ACTIVE_CLIENTS_PATH = os.environ.get(
    "BOX_ACTIVE_CLIENTS_PATH", "Team Advice/Pivot Clients/1. Active Clients"
)
BOX_REQUEST_TIMEOUT = int(os.environ.get("BOX_REQUEST_TIMEOUT_SECONDS", "20"))
BOX_JWT_CONFIG_PATH = os.environ.get("BOX_JWT_CONFIG_PATH") or Path(__file__).resolve().parent.parent / "config" / "box_jwt_config.json"

# Try multiple ways to load Box JWT config:
# 1. Environment variable "box-jwt-config" (points to Secret Manager path or direct value)
# 2. Environment variable "BOX_JWT_CONFIG_JSON" (points to Secret Manager path or direct value)
# 3. Direct fetch from Google Secret Manager secret "box-jwt-config"
# 4. Local file at BOX_JWT_CONFIG_PATH (fallback)
def _load_box_jwt_config_json() -> Optional[str]:
    """Load Box JWT config from various sources in priority order."""
    # Try environment variables first
    config = get_secret("box-jwt-config") or get_secret("BOX_JWT_CONFIG_JSON")
    if config:
        return config

    # Try direct Secret Manager fetch for 'box-jwt-config' secret
    try:
        from google.cloud import secretmanager
        import google.auth

        credentials, project_id = google.auth.default()
        if project_id and secretmanager:
            client = secretmanager.SecretManagerServiceClient(credentials=credentials)
            secret_path = f"projects/{project_id}/secrets/box-jwt-config/versions/latest"
            response = client.access_secret_version(request={"name": secret_path})
            config = response.payload.data.decode("utf-8")
            logger.info("Loaded Box JWT config from Google Secret Manager")
            return config
    except Exception as e:
        logger.debug("Could not load box-jwt-config from Secret Manager: %s", e)

    return None

BOX_JWT_CONFIG_JSON = _load_box_jwt_config_json()

# Box user to impersonate for folder operations (hardcoded)
BOX_IMPERSONATION_USER = "noel.pinton@pivotwealth.com.au"
HUBSPOT_TOKEN = get_secret("HUBSPOT_TOKEN") or os.environ.get("HUBSPOT_TOKEN")
HUBSPOT_HEADERS = {
    "Authorization": f"Bearer {HUBSPOT_TOKEN}" if HUBSPOT_TOKEN else None,
    "Content-Type": "application/json",
}

_BOX_SERVICE_INITIALISED = False
_BOX_FOLDER_SERVICE: Optional[BoxFolderService] = None


def _load_box_jwt_settings() -> Optional[dict]:
    if BOX_JWT_CONFIG_JSON:
        try:
            config = json.loads(BOX_JWT_CONFIG_JSON)
            logger.info("Loaded Box JWT config from BOX_JWT_CONFIG_JSON")
            return config
        except json.JSONDecodeError as exc:
            logger.error("Invalid BOX_JWT_CONFIG_JSON: %s", exc)
            return None
    if BOX_JWT_CONFIG_PATH and os.path.exists(BOX_JWT_CONFIG_PATH):
        try:
            with open(BOX_JWT_CONFIG_PATH, "r", encoding="utf-8") as fh:
                config = json.load(fh)
            logger.info("Loaded Box JWT config from %s", BOX_JWT_CONFIG_PATH)
            return config
        except (OSError, json.JSONDecodeError) as exc:
            logger.error("Failed to read Box JWT config at %s: %s", BOX_JWT_CONFIG_PATH, exc)
            return None
    logger.info("No Box JWT configuration found; Box automation disabled")
    return None


def _find_box_user_id(client: Client, identifier: str) -> Optional[str]:
    term = (identifier or "").strip()
    if not term:
        return None
    try:
        if term.isdigit():
            try:
                user = client.user(term).get()
                return user.id
            except BoxAPIException:
                pass
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
        logger.error("Failed to search Box users for '%s': %s", term, exc)
        return None


def ensure_box_service() -> Optional[BoxFolderService]:
    global _BOX_SERVICE_INITIALISED, _BOX_FOLDER_SERVICE
    if _BOX_SERVICE_INITIALISED:
        return _BOX_FOLDER_SERVICE

    config_data = _load_box_jwt_settings()
    if not config_data:
        _BOX_SERVICE_INITIALISED = True
        _BOX_FOLDER_SERVICE = None
        logger.error("Box JWT config not found; Box automation disabled")
        return None

    try:
        auth = JWTAuth.from_settings_dictionary(config_data)
        access_token = auth.authenticate_instance()
        logger.info("Fetched Box access token via JWT service account")
        impersonated_user_id = None

        if BOX_IMPERSONATION_USER:
            logger.info("Attempting to find Box user: %s", BOX_IMPERSONATION_USER)
            client = Client(auth)
            impersonated_user_id = _find_box_user_id(client, BOX_IMPERSONATION_USER)
            if impersonated_user_id:
                logger.info(
                    "✅ Impersonating Box user '%s' (id=%s)",
                    BOX_IMPERSONATION_USER,
                    impersonated_user_id,
                )
            else:
                logger.error(
                    "❌ Box impersonation user '%s' was not found; proceeding without impersonation",
                    BOX_IMPERSONATION_USER,
                )
        else:
            logger.warning(
                "⚠️  BOX_IMPERSONATION_USER not configured; using service account (limited permissions)"
            )
        _BOX_FOLDER_SERVICE = BoxFolderService(
            token=access_token,
            template_path=BOX_TEMPLATE_PATH,
            destination_path=BOX_ACTIVE_CLIENTS_PATH,
            api_base_url=BOX_API_BASE_URL,
            request_timeout=BOX_REQUEST_TIMEOUT,
            as_user_id=impersonated_user_id,
        )
    except Exception as exc:  # pragma: no cover
        logger.error("Failed to initialise BoxFolderService: %s", exc)
        _BOX_FOLDER_SERVICE = None
    finally:
        _BOX_SERVICE_INITIALISED = True
    return _BOX_FOLDER_SERVICE


def _hubspot_headers() -> dict:
    if not HUBSPOT_HEADERS.get("Authorization"):
        raise RuntimeError("HUBSPOT_TOKEN is not configured")
    return HUBSPOT_HEADERS


def _fetch_hubspot_contact(contact_id: str) -> Optional[dict]:
    try:
        url = f"https://api.hubapi.com/crm/v3/objects/contacts/{contact_id}"
        params = {"properties": "firstname,lastname,email"}
        resp = requests.get(url, headers=_hubspot_headers(), params=params, timeout=10)
        if resp.status_code == 404:
            logger.warning("HubSpot contact %s not found", contact_id)
            return None
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as exc:
        logger.error("Failed to fetch HubSpot contact %s: %s", contact_id, exc)
        return None


def get_hubspot_deal_contacts(deal_id: str) -> List[dict]:
    url = f"https://api.hubapi.com/crm/v4/objects/deals/{deal_id}/associations/contacts"
    resp = requests.get(url, headers=_hubspot_headers(), timeout=10)
    if resp.status_code == 404:
        logger.warning("HubSpot deal %s not found when retrieving contacts", deal_id)
        return []
    resp.raise_for_status()
    contact_ids = [
        str(item.get("toObjectId"))
        for item in resp.json().get("results", [])
        if item.get("toObjectId")
    ]
    contacts: List[dict] = []
    for contact_id in contact_ids:
        contact = _fetch_hubspot_contact(contact_id)
        if contact:
            contacts.append(contact)
    return contacts


def _format_contact_display(contact: dict, position: int = 0) -> str:
    """Format contact name for folder display.

    Args:
        contact: Contact dict with properties
        position: Position in contact list (0 for first, 1+ for subsequent)

    Returns:
        Formatted name: "Last, First" for first contact, "First" for others
    """
    props = contact.get("properties") or {}
    first = (props.get("firstname") or "").strip()
    last = (props.get("lastname") or "").strip()

    if position == 0:
        # First contact: "Last, First"
        if first and last:
            return f"{last}, {first}"
        return (first or last).strip()
    else:
        # Subsequent contacts: just "First"
        return first if first else (last or "").strip()


def build_client_folder_name(deal_id: str, contacts: List[dict]) -> str:
    """Build Box folder name from deal contacts.

    Naming convention: "Last, First & First & First"
    - First contact: Last, First
    - Additional contacts: First name only

    Example: "Smith, John & Jane & Bob"
    """
    unique_names: List[str] = []
    for idx, contact in enumerate(contacts):
        display = _format_contact_display(contact, position=idx)
        if display and display not in unique_names:
            unique_names.append(display)

    if not unique_names:
        unique_names.append(f"Deal {deal_id}")

    raw_name = " & ".join(unique_names)
    return sanitize_folder_name(raw_name)


def _upload_metadata_file(service: BoxFolderService, folder_id: str, metadata: dict) -> None:
    """Upload metadata JSON file to Box folder using Box API REST endpoint.

    Uses POST /files/content endpoint with multipart form data.
    Equivalent to: client.folder(folder_id).upload(file_stream)

    Args:
        service: BoxFolderService instance
        folder_id: Box folder ID to upload to
        metadata: Metadata dictionary to write as JSON
    """
    import tempfile
    import os

    json_content = json.dumps(metadata, indent=2, default=str)

    try:
        # Create temporary file to upload
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as tmp:
            tmp.write(json_content)
            tmp_path = tmp.name

        try:
            # Box upload endpoint (different from API endpoint)
            url = "https://upload.box.com/api/2.0/files/content"

            # Attributes as JSON string
            attributes_json = json.dumps({
                'name': 'metadata.json',
                'parent': {'id': folder_id}
            })

            # Open file in binary mode for upload
            with open(tmp_path, 'rb') as file_stream:
                files = {
                    'attributes': (None, attributes_json),
                    'file': ('metadata.json', file_stream, 'application/json'),
                }

                headers = service._headers()
                # Remove Content-Type - let requests set multipart boundary
                if 'Content-Type' in headers:
                    del headers['Content-Type']

                logger.info('Uploading metadata.json to Box folder %s via %s', folder_id, url)
                logger.debug('Authorization header present: %s', 'Authorization' in headers)
                resp = requests.post(
                    url,
                    headers=headers,
                    files=files,
                    timeout=service._timeout,
                )

            logger.debug('Box API response status: %d', resp.status_code)

            if resp.status_code >= 400:
                logger.error('Box API error: %s', resp.text)
                resp.raise_for_status()

            logger.info('Successfully uploaded metadata.json to Box folder %s', folder_id)

        finally:
            # Clean up temp file
            try:
                os.unlink(tmp_path)
            except Exception:
                pass

    except requests.exceptions.HTTPError as exc:
        logger.error('Failed to upload metadata.json to folder %s: %s', folder_id, exc)
        if hasattr(exc, 'response'):
            logger.error('Box response: %s', exc.response.text)
    except Exception as exc:
        logger.error('Unexpected error uploading metadata.json to folder %s: %s', folder_id, exc)


def create_box_folder_for_deal(deal_id: str, deal_metadata: Optional[dict] = None) -> dict:
    """Create Box folder for a deal and upload metadata.

    Args:
        deal_id: HubSpot deal ID
        deal_metadata: Optional metadata dict containing:
            - hs_deal_record_id
            - service_package
            - agreement_start_date
            - household_type
            - hs_spouse_id
            - hs_contact_id
            - deal_salutation

    Returns:
        Dict with folder creation result
    """
    service = ensure_box_service()
    if not service:
        logger.info(
            "Box automation not configured; skipping folder creation for deal %s",
            deal_id,
        )
        return {"status": "skipped", "reason": "box_not_configured"}

    logger.info('Starting Box folder creation for deal %s', deal_id)
    contacts = get_hubspot_deal_contacts(deal_id)
    folder_name = build_client_folder_name(deal_id, contacts)
    logger.info('Using folder name %s under %s', folder_name, BOX_ACTIVE_CLIENTS_PATH)
    folder = service.ensure_client_folder(folder_name)
    formatted_contacts = [name for name in (_format_contact_display(c) for c in contacts) if name]
    logger.info('Created Box folder for deal %s (id=%s)', deal_id, folder.get('id'))

    # Upload metadata file if provided
    if deal_metadata:
        logger.info('Uploading metadata to Box folder %s: %s', folder.get('id'), deal_metadata)
        _upload_metadata_file(service, folder.get('id'), deal_metadata)
    else:
        logger.warning('No metadata provided for deal %s', deal_id)

    return {
        "status": "created",
        "folder": {
            "id": folder.get("id"),
            "name": folder.get("name", folder_name),
            "parent_path": BOX_ACTIVE_CLIENTS_PATH,
        },
        "contacts": formatted_contacts,
    }


__all__ = [
    "BoxAutomationError",
    "BoxFolderService",
    "sanitize_folder_name",
    "ensure_box_service",
    "create_box_folder_for_deal",
]
