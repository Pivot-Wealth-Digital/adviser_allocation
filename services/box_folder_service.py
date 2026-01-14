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
        metadata_scope: Optional[str] = None,
        metadata_template_key: Optional[str] = None,
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
        self._metadata_scope = metadata_scope
        self._metadata_template_key = metadata_template_key

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

    def share_subfolder_with_email(
        self,
        parent_folder_id: str,
        subfolder_name: str,
        email: str,
        role: str = "viewer",
    ) -> dict:
        email = (email or "").strip()
        if not email:
            raise BoxAutomationError("Cannot share folder without a recipient email")

        subfolder = self._find_child_folder(parent_folder_id, subfolder_name)
        if not subfolder:
            raise BoxAutomationError(
                f"Subfolder '{subfolder_name}' not found under folder {parent_folder_id}"
            )

        return self._add_collaborator(subfolder.get("id"), email, role)

    def list_collaborators(
        self,
        folder_id: str,
        *,
        subfolder_name: Optional[str] = None,
    ) -> tuple[list[dict], str]:
        folder_id = (folder_id or "").strip()
        if not folder_id:
            raise BoxAutomationError("Folder id is required to list collaborators")

        target_folder_id = folder_id
        if subfolder_name:
            subfolder = self._find_child_folder(folder_id, subfolder_name)
            if not subfolder:
                normalized = subfolder_name.strip().lower()
                queue = [folder_id]
                visited = set()
                while queue and not subfolder:
                    current = queue.pop(0)
                    if current in visited:
                        continue
                    visited.add(current)
                    for child in self._list_folder_children(current):
                        name = (child.get("name") or "").strip().lower()
                        if name == normalized or normalized in name:
                            subfolder = child
                            break
                        if child.get("type") == "folder":
                            queue.append(child.get("id"))
                    if subfolder:
                        break
            if not subfolder:
                raise BoxAutomationError(
                    f"Subfolder '{subfolder_name}' not found under folder {folder_id}"
                )
            target_folder_id = subfolder.get("id") or folder_id

        url = f"{self._api_base_url}/folders/{target_folder_id}/collaborations"
        try:
            resp = requests.get(url, headers=self._headers(), timeout=self._timeout)
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise BoxAutomationError(
                f"Box collaboration list failed for folder {target_folder_id}: {exc}"
            ) from exc

        entries = resp.json().get("entries", []) if resp.content else []
        collaborators: list[dict] = []
        for entry in entries:
            user = entry.get("accessible_by") or {}
            collaborators.append(
                {
                    "email": (user.get("login") or "").strip().lower(),
                    "name": (user.get("name") or "").strip(),
                    "role": entry.get("role"),
                    "status": entry.get("status"),
                }
            )
        return collaborators, target_folder_id

    def list_subfolders(self, folder_id: str) -> list[dict]:
        folder_id = (folder_id or "").strip()
        if not folder_id:
            raise BoxAutomationError("Folder id is required to list subfolders")

        children = self._list_folder_children(folder_id)
        subfolders = [
            {
                "id": child.get("id"),
                "name": child.get("name"),
                "type": child.get("type"),
            }
            for child in children
            if child.get("type") == "folder"
        ]
        subfolders.sort(key=lambda item: (item.get("name") or "").lower())
        return subfolders

    def _get_folder_details(self, folder_id: str) -> dict:
        url = f"{self._api_base_url}/folders/{folder_id}"
        try:
            resp = requests.get(url, headers=self._headers(), timeout=self._timeout)
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise BoxAutomationError(f"Failed to fetch folder {folder_id}: {exc}") from exc
        return resp.json()

    def _folder_display_path(self, folder_info: dict) -> str:
        entries = (folder_info.get("path_collection") or {}).get("entries", [])
        names = [entry.get("name") for entry in entries if entry.get("name")]
        name = folder_info.get("name")
        if name:
            names.append(name)
        return " / ".join(names)

    def get_folder_snapshot_info(self, folder_id: str) -> Optional[dict]:
        """Return friendly information for displaying a folder entry."""
        folder_id = (folder_id or "").strip()
        if not folder_id:
            return None
        try:
            info = self._get_folder_details(folder_id)
        except BoxAutomationError as exc:
            logger.warning("Unable to load details for folder %s: %s", folder_id, exc)
            return None

        return {
            "id": folder_id,
            "name": info.get("name"),
            "path": self._folder_display_path(info),
            "url": f"https://app.box.com/folder/{folder_id}",
        }

    def find_folder_missing_metadata(
        self,
        start_index: int = 0,
        page_size: int = 5,
    ) -> tuple[list[dict], list[str], Optional[int]]:
        if not self._metadata_scope or not self._metadata_template_key:
            raise BoxAutomationError("Metadata scope/template key not configured")

        try:
            start_index = int(start_index)
        except (TypeError, ValueError):
            start_index = 0
        if start_index < 0:
            start_index = 0

        try:
            page_size = int(page_size)
        except (TypeError, ValueError):
            page_size = 5
        if page_size < 1:
            page_size = 1

        root_id = self._resolve_path(self._destination_path)
        subfolders = self.list_subfolders(root_id)
        total_subfolders = len(subfolders)

        if start_index >= total_subfolders:
            return [], [], None

        candidates: list[dict] = []
        scan_errors: list[str] = []
        next_cursor: Optional[int] = None

        for idx in range(start_index, total_subfolders):
            entry = subfolders[idx]
            folder_id = entry.get("id")
            if not folder_id:
                continue
            url = (
                f"{self._api_base_url}/folders/{folder_id}/metadata/"
                f"{self._metadata_scope}/{self._metadata_template_key}"
            )
            try:
                resp = requests.get(url, headers=self._headers(), timeout=self._timeout)
            except requests.RequestException as exc:
                message = f"Metadata fetch request failed for folder {folder_id}: {exc}"
                logger.warning(message)
                scan_errors.append(message)
                continue

            if resp.status_code == 404:
                try:
                    info = self._get_folder_details(folder_id)
                except BoxAutomationError as exc:
                    message = f"Failed to load folder details for {folder_id}: {exc}"
                    logger.warning(message)
                    scan_errors.append(message)
                    info = {"name": entry.get("name"), "path": entry.get("name")}
                candidates.append(
                    {
                        "id": folder_id,
                        "name": info.get("name") or entry.get("name"),
                        "path": self._folder_display_path(info) if info else entry.get("name"),
                        "url": f"https://app.box.com/folder/{folder_id}",
                    }
                )
                if len(candidates) >= page_size:
                    next_cursor = idx + 1 if (idx + 1) < total_subfolders else None
                    break
                continue

            if resp.status_code in (401, 403):
                message = (
                    f"Skipping folder {folder_id} while checking metadata; "
                    f"received {resp.status_code} permission error."
                )
                logger.warning(message)
                scan_errors.append(message)
                continue

            try:
                resp.raise_for_status()
            except requests.HTTPError as exc:
                message = (
                    f"Metadata check failed for folder {folder_id} "
                    f"(status={resp.status_code}): {resp.text}"
                )
                logger.warning(message)
                scan_errors.append(message)
                continue

        issues = scan_errors[:5]
        return candidates[:page_size], issues, next_cursor

    def collect_metadata_tagging_status(self) -> tuple[list[dict], list[dict], list[str]]:
        """Return folder metadata grouped by tagging status.

        Returns:
            tuple containing:
                tagged folder entries,
                untagged folder entries,
                issues encountered while scanning.
        """
        if not self._metadata_scope or not self._metadata_template_key:
            raise BoxAutomationError("Metadata scope/template key not configured")

        root_id = self._resolve_path(self._destination_path)
        subfolders = self.list_subfolders(root_id)

        tagged: list[dict] = []
        untagged: list[dict] = []
        issues: list[str] = []

        for entry in subfolders:
            folder_id = entry.get("id")
            if not folder_id:
                continue

            url = (
                f"{self._api_base_url}/folders/{folder_id}/metadata/"
                f"{self._metadata_scope}/{self._metadata_template_key}"
            )
            try:
                resp = requests.get(url, headers=self._headers(), timeout=self._timeout)
            except requests.RequestException as exc:
                message = f"Metadata fetch request failed for folder {folder_id}: {exc}"
                logger.warning(message)
                issues.append(message)
                continue

            if resp.status_code == 404:
                untagged.append(
                    {
                        "id": folder_id,
                        "name": entry.get("name"),
                        "path": "",
                        "url": f"https://app.box.com/folder/{folder_id}",
                    }
                )
                continue

            if resp.status_code in (401, 403):
                message = (
                    f"Skipping folder {folder_id} while checking metadata; "
                    f"received {resp.status_code} permission error."
                )
                logger.warning(message)
                issues.append(message)
                continue

            try:
                resp.raise_for_status()
            except requests.HTTPError as exc:
                message = (
                    f"Metadata check failed for folder {folder_id} "
                    f"(status={resp.status_code}): {resp.text}"
                )
                logger.warning(message)
                issues.append(message)
                continue

            tagged.append(
                {
                    "id": folder_id,
                    "name": entry.get("name"),
                    "path": "",
                    "url": f"https://app.box.com/folder/{folder_id}",
                }
            )

        return tagged, untagged, issues

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

    def _add_collaborator(self, folder_id: Optional[str], email: str, role: str) -> dict:
        if not folder_id:
            raise BoxAutomationError("Cannot add collaborator without folder id")

        payload = {
            "item": {"type": "folder", "id": folder_id},
            "accessible_by": {"type": "user", "login": email},
            "role": role,
        }

        try:
            resp = requests.post(
                f"{self._api_base_url}/collaborations",
                headers=self._headers("application/json"),
                json=payload,
                timeout=self._timeout,
            )
        except requests.RequestException as exc:
            raise BoxAutomationError(
                f"Box collaboration create failed for folder {folder_id}: {exc}"
            ) from exc

        if resp.status_code == 409:
            logger.info(
                "Box collaborator already exists for folder %s email=%s", folder_id, email
            )
            return {"status": "exists", "email": email, "folder_id": folder_id}

        try:
            resp.raise_for_status()
        except requests.HTTPError as exc:
            raise BoxAutomationError(
                f"Box collaboration API error for folder {folder_id}: {resp.text}"
            ) from exc

        logger.info(
            "Added Box collaborator %s with role=%s to folder %s",
            email,
            role,
            folder_id,
        )
        data = resp.json()
        data.setdefault("status", "created")
        return data

    def apply_metadata_template(self, folder_id: str, metadata: dict) -> None:
        """Apply configured metadata template to folder."""
        if not self._metadata_scope or not self._metadata_template_key:
            logger.debug(
                "Box metadata template not configured; skipping metadata apply for folder %s",
                folder_id,
            )
            return

        raw_metadata = metadata or {}
        prepared: dict[str, str] = {}
        removal_candidates: list[str] = []
        for key, value in raw_metadata.items():
            mapped_key = BOX_METADATA_FIELD_MAP.get(key)
            if not mapped_key:
                continue
            formatted_value = _format_metadata_value_for_template(key, value)
            if formatted_value is None or formatted_value == "":
                removal_candidates.append(mapped_key)
                continue
            prepared[mapped_key] = formatted_value

        if not prepared and not removal_candidates:
            logger.debug("No metadata supplied for folder %s; skipping template application", folder_id)
            return

        url = f"{self._api_base_url}/folders/{folder_id}/metadata/{self._metadata_scope}/{self._metadata_template_key}"
        headers = self._headers("application/json")
        patch_headers = self._headers("application/json-patch+json")

        additions = sorted(prepared.keys())
        removals = sorted(removal_candidates)
        logger.info(
            "Applying Box metadata template %s/%s to folder %s (add=%s, remove=%s)",
            self._metadata_scope,
            self._metadata_template_key,
            folder_id,
            additions,
            removals,
        )

        try:
            resp = requests.post(url, headers=headers, json=prepared, timeout=self._timeout)
            if resp.status_code == 409:
                # Metadata already exists; fetch current values to build patch operations
                existing: dict[str, str] = {}
                try:
                    existing_resp = requests.get(url, headers=self._headers(), timeout=self._timeout)
                    if existing_resp.ok:
                        existing = {
                            key: value
                            for key, value in existing_resp.json().items()
                            if not key.startswith("$")
                        }
                except requests.RequestException as exc:
                    logger.warning(
                        "Failed to fetch existing metadata for folder %s: %s",
                        folder_id,
                        exc,
                    )

                operations: list[dict] = []
                for key in removal_candidates:
                    if key in existing:
                        operations.append({"op": "remove", "path": f"/{key}"})
                for key, value in prepared.items():
                    op = "replace" if key in existing else "add"
                    operations.append({"op": op, "path": f"/{key}", "value": value})

                if not operations:
                    logger.info(
                        "No metadata updates required for folder %s; existing metadata already matches",
                        folder_id,
                    )
                    return

                resp = requests.put(
                    url,
                    headers=patch_headers,
                    data=json.dumps(operations),
                    timeout=self._timeout,
                )
            resp.raise_for_status()
            logger.info(
                "Applied metadata template %s/%s to folder %s",
                self._metadata_scope,
                self._metadata_template_key,
                folder_id,
            )
        except requests.RequestException as exc:
            raise BoxAutomationError(f"Box metadata apply failed for folder {folder_id}: {exc}") from exc

    def rename_metadata_template(self, display_name: str) -> dict:
        """Rename the configured metadata template display name."""
        display_name = (display_name or "").strip()
        if not display_name:
            raise BoxAutomationError("Display name is required to rename metadata template")
        if not self._metadata_scope or not self._metadata_template_key:
            raise BoxAutomationError("Metadata scope/template key not configured")

        url = (
            f"{self._api_base_url}/metadata_templates/"
            f"{self._metadata_scope}/{self._metadata_template_key}/schema"
        )
        payload = [
            {
                "op": "editTemplate",
                "data": {
                    "displayName": display_name,
                },
            }
        ]
        try:
            resp = requests.put(
                url,
                headers=self._headers("application/json", as_user=False),
                json=payload,
                timeout=self._timeout,
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise BoxAutomationError(
                f"Failed to rename metadata template to '{display_name}': {exc}"
            ) from exc

        logger.info(
            "Renamed Box metadata template %s/%s to '%s'",
            self._metadata_scope,
            self._metadata_template_key,
            display_name,
        )
        return resp.json() if resp.content else {"displayName": display_name}

    def _query_metadata_entry(self, field_key: str, value: Optional[str]) -> Optional[dict]:
        value = (value or "").strip()
        if not value or not self._metadata_scope or not self._metadata_template_key:
            return None

        query_url = f"{self._api_base_url}/metadata_queries/execute_read"
        payload = {
            "from": f"{self._metadata_scope}.{self._metadata_template_key}",
            "query": f"{field_key} = :value",
            "query_params": {"value": value},
            "limit": 1,
        }
        try:
            resp = requests.post(
                query_url,
                headers=self._headers("application/json"),
                json=payload,
                timeout=self._timeout,
            )
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            entries = resp.json().get("entries", [])
            return entries[0] if entries else None
        except requests.RequestException as exc:
            logger.warning("Metadata query for field %s failed: %s", field_key, exc)
            return None

    def find_folder_by_primary_contact(
        self,
        primary_contact_link: Optional[str],
        primary_contact_id: Optional[str] = None,
    ) -> Optional[dict]:
        for candidate in (primary_contact_link, primary_contact_id):
            entry = self._query_metadata_entry("primaryContactId", candidate)
            if entry:
                return entry
        return None

    def _headers(
        self,
        content_type: Optional[str] = None,
        *,
        as_user: bool = True,
    ) -> dict:
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
        }
        if content_type:
            headers["Content-Type"] = content_type
        if as_user and self._as_user_id:
            headers["As-User"] = self._as_user_id
        return headers


BOX_API_BASE_URL = os.environ.get("BOX_API_BASE_URL", DEFAULT_BOX_API_BASE_URL)

# Cache for Box template path (refreshed every 5 minutes or on settings update)
_BOX_TEMPLATE_PATH_CACHE: Optional[str] = None
_BOX_TEMPLATE_PATH_CACHE_TIME: float = 0
BOX_TEMPLATE_PATH_CACHE_TTL: float = 300  # 5 minutes


def get_box_template_path_from_settings() -> str:
    """Get Box template path from Firestore settings, with fallback to env var.

    First checks Firestore `system_settings/box_config` document for `template_folder_path`.
    Falls back to `BOX_TEMPLATE_PATH` environment variable if not found or unavailable.
    Uses 5-minute cache to avoid excessive Firestore reads.
    """
    global _BOX_TEMPLATE_PATH_CACHE, _BOX_TEMPLATE_PATH_CACHE_TIME

    now = time.time()
    if _BOX_TEMPLATE_PATH_CACHE and (now - _BOX_TEMPLATE_PATH_CACHE_TIME) < BOX_TEMPLATE_PATH_CACHE_TTL:
        return _BOX_TEMPLATE_PATH_CACHE

    try:
        db = get_firestore_client()
        if db:
            doc = db.collection("system_settings").document("box_config").get()
            if doc.exists:
                data = doc.to_dict() or {}
                path = (data.get("template_folder_path") or "").strip()
                if path:
                    logger.info("Using Box template path from Firestore: %s", path)
                    _BOX_TEMPLATE_PATH_CACHE = path
                    _BOX_TEMPLATE_PATH_CACHE_TIME = now
                    return path
    except Exception as exc:
        logger.warning("Failed to load Box template path from Firestore: %s", exc)

    # Fallback to environment variable
    fallback = os.environ.get(
        "BOX_TEMPLATE_PATH",
        "Team Advice/Pivot Clients/2026 Client Box Folder Template"
    )
    logger.info("Using Box template path from environment variable: %s", fallback)
    _BOX_TEMPLATE_PATH_CACHE = fallback
    _BOX_TEMPLATE_PATH_CACHE_TIME = now
    return fallback


def refresh_box_template_path_cache() -> None:
    """Clear cached Box template path (call after updating settings in Firestore)."""
    global _BOX_TEMPLATE_PATH_CACHE, _BOX_TEMPLATE_PATH_CACHE_TIME
    _BOX_TEMPLATE_PATH_CACHE = None
    _BOX_TEMPLATE_PATH_CACHE_TIME = 0
    logger.info("Cleared Box template path cache")


BOX_TEMPLATE_PATH = get_box_template_path_from_settings()
# Note: "1. Active Clients" folder ID is 89432789614
BOX_ACTIVE_CLIENTS_PATH = os.environ.get(
    "BOX_ACTIVE_CLIENTS_PATH", "Team Advice/Pivot Clients/1. Active Clients"
)
BOX_METADATA_SCOPE = (os.environ.get("BOX_METADATA_SCOPE") or "").strip()
if not BOX_METADATA_SCOPE and os.environ.get("BOX_METADATA_TEMPLATE_SCOPE"):
    BOX_METADATA_SCOPE = os.environ["BOX_METADATA_TEMPLATE_SCOPE"].strip()
BOX_METADATA_TEMPLATE_KEY = (os.environ.get("BOX_METADATA_TEMPLATE_KEY") or "").strip()
BOX_METADATA_FIELD_MAP = {
    "deal_salutation": "deal_salutation",
    "household_type": "household_type",
    "primary_contact_link": "primary_contact_link",
    "primary_contact_id": "primary_contact_id",
    "spouse_contact_link": "spouse_contact_link",
    "hs_spouse_id": "hs_spouse_id",
    "associated_contacts": "associated_contacts",
    "associated_contact_ids": "associated_contact_ids",
}
BOX_REQUEST_TIMEOUT = int(os.environ.get("BOX_REQUEST_TIMEOUT_SECONDS", "20"))
BOX_JWT_CONFIG_PATH = os.environ.get("BOX_JWT_CONFIG_PATH") or Path(__file__).resolve().parent.parent / "config" / "box_jwt_config.json"


def _current_portal_id() -> str:
    value = get_secret("HUBSPOT_PORTAL_ID") or os.environ.get("HUBSPOT_PORTAL_ID") or ""
    return value.strip()


CLIENT_SHARING_SUBFOLDER = os.environ.get("BOX_CLIENT_SHARE_SUBFOLDER", "Client Sharing")
CLIENT_SHARING_ROLE = os.environ.get("BOX_CLIENT_SHARE_ROLE", "viewer")


def _format_metadata_value_for_template(key: str, value) -> Optional[str]:
    if value is None or value == "":
        return None

    if isinstance(value, list):
        if key == "associated_contact_ids":
            cleaned = [str(item).strip() for item in value if str(item).strip()]
            return ", ".join(cleaned)
        if key == "associated_contacts":
            lines: list[str] = []
            for contact in value:
                if not isinstance(contact, dict):
                    if contact:
                        lines.append(str(contact))
                    continue
                display = (
                    contact.get("display_name")
                    or " ".join(filter(None, [contact.get("firstname"), contact.get("lastname")])).strip()
                    or contact.get("email")
                    or contact.get("id")
                    or "Unknown Contact"
                )
                extra_parts = []
                email = (contact.get("email") or "").strip()
                contact_id = (contact.get("id") or "").strip()
                if email:
                    extra_parts.append(email)
                if contact_id:
                    extra_parts.append(f"ID: {contact_id}")
                link = (contact.get("url") or "").strip()
                portal_id = _current_portal_id()
                if not link and contact_id and portal_id:
                    link = f"https://app.hubspot.com/contacts/{portal_id}/record/0-1/{contact_id}"
                if link:
                    extra_parts.append(f"Link: {link}")
                if extra_parts:
                    lines.append(f"{display} ({' | '.join(extra_parts)})")
                else:
                    lines.append(display)
            return "\n".join(lines)
        # Generic list formatting
        formatted_items = []
        for item in value:
            if item is None or item == "":
                continue
            if isinstance(item, dict):
                formatted_items.append(json.dumps(item, indent=2))
            else:
                formatted_items.append(str(item))
        return "\n".join(formatted_items)

    if isinstance(value, dict):
        return json.dumps(value, indent=2)

    return str(value)


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

# Box user to impersonate for folder operations
BOX_IMPERSONATION_USER = (
    get_secret("BOX_IMPERSONATION_USER")
    or os.environ.get("BOX_IMPERSONATION_USER")
    or "noel.pinton@pivotwealth.com.au"
)
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
            metadata_scope=BOX_METADATA_SCOPE or None,
            metadata_template_key=BOX_METADATA_TEMPLATE_KEY or None,
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
    association_map: dict[str, list[dict]] = {}
    contact_ids: list[str] = []

    try:
        url = "https://api.hubapi.com/crm/v4/associations/deals/contacts/batch/read"
        payload = {"inputs": [{"id": str(deal_id)}]}
        resp = requests.post(url, headers=_hubspot_headers(), json=payload, timeout=10)
        if resp.status_code == 404:
            logger.warning("HubSpot deal %s not found when retrieving contact associations", deal_id)
        else:
            resp.raise_for_status()
            results = resp.json().get("results", [])
            for result in results:
                for to_entry in result.get("to", []):
                    contact_id = str(to_entry.get("toObjectId") or "").strip()
                    if not contact_id:
                        continue
                    if contact_id not in association_map:
                        contact_ids.append(contact_id)
                    association_map[contact_id] = to_entry.get("associationTypes") or []
    except requests.RequestException as exc:
        logger.warning("Failed to load HubSpot association metadata for deal %s: %s", deal_id, exc)

    if not contact_ids:
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
            contact["association_types"] = association_map.get(contact_id, [])
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
    """Determine the Box folder name from contact names using legacy HubSpot logic.

    Rules:
      - If primary and partner share a surname: "First & PartnerFirst Last"
      - If surnames differ: "First Last & PartnerFirst PartnerLast"
      - If no partner details: "First Last"
    Additional contacts beyond the first two are appended using their formatted
    display names separated by " & ".
    """

    def _name_parts(contact: Optional[dict]) -> tuple[str, str]:
        props = (contact or {}).get("properties") or {}
        return (
            (props.get("firstname") or "").strip(),
            (props.get("lastname") or "").strip(),
        )

    primary_first, primary_last = _name_parts(contacts[0] if contacts else None)
    partner_first, partner_last = _name_parts(contacts[1] if len(contacts) > 1 else None)

    def _join(first: str, last: str) -> str:
        return " ".join(part for part in (first.strip(), last.strip()) if part)

    candidate = ""
    has_partner = bool(partner_first or partner_last)
    if has_partner and partner_first and partner_last:
        last_matches = (
            primary_last
            and partner_last
            and primary_last.lower() == partner_last.lower()
        )
        if last_matches:
            shared_last = primary_last or partner_last
            left = primary_first.strip() or shared_last
            right = partner_first.strip() or shared_last
            candidate = f"{left} & {right} {shared_last}".strip()
        else:
            left = _join(primary_first, primary_last)
            right = _join(partner_first, partner_last)
            candidate = " & ".join(part for part in (left, right) if part)
    elif primary_first or primary_last:
        candidate = _join(primary_first, primary_last)

    if not candidate:
        unique_names: List[str] = []
        for idx, contact in enumerate(contacts):
            display = _format_contact_display(contact, position=idx)
            if display and display not in unique_names:
                unique_names.append(display)
        if not unique_names:
            unique_names.append(f"Deal {deal_id}")
        candidate = " & ".join(unique_names)

    return sanitize_folder_name(candidate)


def provision_box_folder(
    deal_id: str,
    contacts_override: Optional[List[dict]] = None,
    folder_name_override: Optional[str] = None,
) -> dict:
    """Create the Box client folder without applying metadata or sharing.

    Args:
        deal_id: HubSpot deal ID
        contacts_override: Optional contacts list to use when building the folder name
        folder_name_override: Optional explicit folder name

    Returns:
        Dict describing the folder result.
    """
    service = ensure_box_service()
    if not service:
        logger.info(
            "Box automation not configured; skipping bare folder creation for deal %s",
            deal_id,
        )
        return {
            "status": "skipped",
            "reason": "box_not_configured",
        }

    contacts = contacts_override or get_hubspot_deal_contacts(deal_id)
    folder_name = (folder_name_override or "").strip()
    if not folder_name:
        folder_name = build_client_folder_name(deal_id, contacts)

    folder = service.ensure_client_folder(folder_name)
    folder_id = folder.get("id") if folder else None

    parent_path = BOX_ACTIVE_CLIENTS_PATH
    resolved_folder_name = folder.get("name") if folder else folder_name
    folder_url = f"https://app.box.com/folder/{folder_id}" if folder_id else None
    full_path = None
    if parent_path and resolved_folder_name:
        full_path = f"{parent_path.rstrip('/')}/{resolved_folder_name}"

    formatted_contacts = [
        name
        for name in (
            _format_contact_display(contact, position=idx)
            for idx, contact in enumerate(contacts)
        )
        if name
    ]

    return {
        "status": "created" if folder_id else "unknown",
        "folder": {
            "id": folder_id,
            "name": resolved_folder_name,
            "parent_path": parent_path,
            "path": full_path,
            "url": folder_url,
        },
        "contacts": formatted_contacts,
    }


def create_box_folder_for_deal(
    deal_id: str,
    deal_metadata: Optional[dict] = None,
    folder_name_override: Optional[str] = None,
    *,
    contacts_override: Optional[List[dict]] = None,
    share_email_override: Optional[str] = None,
) -> dict:
    """Create Box folder for a deal and apply metadata template.

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
        folder_name_override: Optional folder name to use instead of the default

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
    contacts: List[dict]
    if contacts_override is not None:
        contacts = contacts_override
    else:
        contacts = get_hubspot_deal_contacts(deal_id)
    formatted_contacts = [
        name
        for name in (
            _format_contact_display(contact, position=idx)
            for idx, contact in enumerate(contacts)
        )
        if name
    ]

    folder = None
    folder_name = None

    metadata_payload = dict(deal_metadata or {})
    metadata_response = dict(metadata_payload)
    logger.debug(
        "Prepared metadata payload for deal %s with keys=%s",
        deal_id,
        sorted(metadata_payload.keys()),
    )

    template_metadata = dict(metadata_payload)
    primary_contact_value = template_metadata.get("primary_contact_link") or template_metadata.get("primary_contact_id")
    if "primary_contact_link" not in template_metadata and primary_contact_value:
        template_metadata["primary_contact_link"] = primary_contact_value
    if "spouse_contact_link" not in template_metadata and template_metadata.get("hs_spouse_id"):
        template_metadata["spouse_contact_link"] = template_metadata.get("hs_spouse_id")

    existing_entry = service.find_folder_by_primary_contact(
        template_metadata.get("primary_contact_link"),
        metadata_payload.get("primary_contact_id"),
    )
    if existing_entry:
        folder = existing_entry.get("item") or existing_entry
        folder_name = folder.get("name")
        logger.info(
            "Found existing Box folder for deal %s (id=%s); skipping creation",
            deal_id,
            folder.get("id"),
        )
    else:
        if folder_name_override and folder_name_override.strip():
            folder_name = folder_name_override.strip()
            logger.info(
                "Using custom folder name '%s' for deal %s under %s",
                folder_name,
                deal_id,
                BOX_ACTIVE_CLIENTS_PATH,
            )
        else:
            folder_name = build_client_folder_name(deal_id, contacts)
            logger.info('Using folder name %s under %s', folder_name, BOX_ACTIVE_CLIENTS_PATH)
        folder = service.ensure_client_folder(folder_name)
        logger.info('Created Box folder for deal %s (id=%s)', deal_id, folder.get('id'))

    folder_id = folder.get('id') if folder else None
    if template_metadata and folder_id:
        try:
            service.apply_metadata_template(folder_id, template_metadata)
        except BoxAutomationError as exc:
            logger.error(
                "Failed to apply Box metadata template for deal %s (folder %s): %s",
                deal_id,
                folder_id,
                exc,
            )
    elif template_metadata and not folder_id:
        logger.error(
            "Cannot apply metadata template for deal %s because folder id is missing in response",
            deal_id,
        )
    elif not template_metadata:
        logger.warning('No metadata provided for deal %s', deal_id)

    if isinstance(metadata_response.get("associated_contacts"), list):
        formatted_contacts_meta = []
        for entry in metadata_response["associated_contacts"]:
            if not isinstance(entry, dict):
                formatted_contacts_meta.append(entry)
                continue
            display = entry.get("display_name") or entry.get("firstname") or entry.get("lastname") or entry.get("email") or entry.get("id") or "Unknown Contact"
            parts = []
            email = (entry.get("email") or "").strip()
            contact_id = (entry.get("id") or "").strip()
            link = (entry.get("url") or "").strip()
            if email:
                parts.append(email)
            if contact_id:
                parts.append(f"ID: {contact_id}")
            if link:
                parts.append(f"Link: {link}")
            if parts:
                formatted_contacts_meta.append(f"{display} ({' | '.join(parts)})")
            else:
                formatted_contacts_meta.append(display)
        metadata_response["associated_contacts"] = formatted_contacts_meta

    if isinstance(metadata_response.get("associated_contact_ids"), list):
        cleaned_ids = [str(item).strip() for item in metadata_response["associated_contact_ids"] if str(item).strip()]
        metadata_response["associated_contact_ids"] = ", ".join(cleaned_ids)

    # Determine primary contact email for sharing
    primary_contact_id = metadata_payload.get("primary_contact_id") or None
    share_emails: List[str] = []

    def _maybe_add_email(contact: dict) -> None:
        props = contact.get("properties") or {}
        email = (props.get("email") or "").strip()
        if email and email not in share_emails:
            share_emails.append(email)

    if share_email_override:
        share_emails.append(share_email_override)

    if primary_contact_id:
        for contact in contacts:
            if str(contact.get("id")) == str(primary_contact_id):
                _maybe_add_email(contact)
                break
    if not share_emails:
        for contact in contacts:
            _maybe_add_email(contact)

    share_results: List[dict] = []
    logger.debug(
        "Client sharing skipped for deal %s (folder %s); feature temporarily disabled.",
        deal_id,
        folder_id,
    )

    preferred_order = [
        "deal_salutation",
        "household_type",
        "primary_contact_link",
        "primary_contact_id",
        "spouse_contact_link",
        "hs_spouse_id",
        "associated_contacts",
        "associated_contact_ids",
    ]
    ordered_metadata = {}
    for key in preferred_order:
        if key in metadata_response:
            ordered_metadata[key] = metadata_response.pop(key)
    for key, value in list(metadata_response.items()):
        ordered_metadata[key] = value
    metadata_response = ordered_metadata

    status = "existing" if existing_entry else "created"
    resolved_folder_name = (
        (folder.get("name") if folder else None)
        or folder_name
        or build_client_folder_name(deal_id, contacts)
    )
    parent_path = BOX_ACTIVE_CLIENTS_PATH
    full_path = None
    folder_url = None
    if folder_id:
        folder_url = f"https://app.box.com/folder/{folder_id}"
    if parent_path and resolved_folder_name:
        full_path = f"{parent_path.rstrip('/')}/{resolved_folder_name}"

    logger.info(
        "Finished Box folder processing for deal %s with status=%s (folder_id=%s)",
        deal_id,
        status,
        folder.get("id") if folder else None,
    )
    return {
        "status": status,
        "folder": {
            "id": folder.get("id") if folder else None,
            "name": resolved_folder_name,
            "parent_path": parent_path,
            "path": full_path,
            "url": folder_url,
        },
        "contacts": formatted_contacts,
        "metadata": metadata_response,
        "sharing": {
            "subfolder": CLIENT_SHARING_SUBFOLDER,
            "role": CLIENT_SHARING_ROLE,
            "recipients": share_results,
        },
    }


__all__ = [
    "BoxAutomationError",
    "BoxFolderService",
    "sanitize_folder_name",
    "ensure_box_service",
    "provision_box_folder",
    "create_box_folder_for_deal",
]
