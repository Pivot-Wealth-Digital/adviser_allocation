"""
CloudSQL integration for adviser_allocation.
Syncs Box folder creation to the client_pipeline database.
"""

import logging
import os
from datetime import datetime
from typing import List, Optional

from adviser_allocation.utils.secrets import get_secret

logger = logging.getLogger(__name__)

# Lazy imports to avoid startup errors if deps not installed
_engine = None


def _get_engine():
    """Get or create CloudSQL engine (lazy initialization)."""
    global _engine
    if _engine is not None:
        return _engine

    # Check if CloudSQL is configured (using get_secret for Secret Manager support)
    instance_conn_str = get_secret("CLOUD_SQL_CONNECTION_STRING")
    db_password = get_secret("CLOUD_SQL_PASSWORD")

    if not instance_conn_str and not db_password:
        logger.debug("CloudSQL not configured, skipping sync")
        return None

    try:
        import sqlalchemy as sa
        from sqlalchemy import create_engine
    except ImportError:
        logger.warning("sqlalchemy not installed, CloudSQL sync disabled")
        return None

    db_name = get_secret("CLOUD_SQL_DATABASE") or "client_pipeline"
    db_user = get_secret("CLOUD_SQL_USER") or "postgres"

    # Check for proxy mode (local dev)
    use_proxy = os.getenv("CLOUD_SQL_USE_PROXY", "").lower() in ("true", "1", "yes")

    # Try Cloud SQL Connector first
    try:
        from google.cloud.sql.connector import Connector

        connector_available = True
    except ImportError:
        connector_available = False
        use_proxy = True  # Fall back to proxy if connector not available

    if use_proxy:
        if not db_password:
            logger.warning("CloudSQL proxy mode requires CLOUD_SQL_PASSWORD")
            return None

        from urllib.parse import quote_plus

        proxy_host = os.getenv("CLOUD_SQL_PROXY_HOST", "127.0.0.1")
        proxy_port = int(os.getenv("CLOUD_SQL_PROXY_PORT", "5432"))
        encoded_password = quote_plus(db_password)

        connection_url = (
            f"postgresql+pg8000://{db_user}:{encoded_password}"
            f"@{proxy_host}:{proxy_port}/{db_name}"
        )
        _engine = create_engine(
            connection_url,
            pool_size=2,
            max_overflow=1,
            pool_timeout=10,
            pool_pre_ping=True,
        )
        logger.info("CloudSQL connected via proxy (%s:%s)", proxy_host, proxy_port)
        return _engine

    # Cloud SQL Connector mode
    if not instance_conn_str:
        logger.warning("Missing CLOUD_SQL_CONNECTION_STRING")
        return None

    if not connector_available:
        logger.warning("Cloud SQL Connector not available")
        return None

    connector = Connector()

    def getconn():
        return connector.connect(
            instance_connection_string=instance_conn_str,
            driver="pg8000",
            user=db_user,
            password=db_password,
            db=db_name,
        )

    _engine = create_engine(
        "postgresql+pg8000://",
        creator=getconn,
        pool_size=2,
        max_overflow=1,
        pool_timeout=10,
        pool_pre_ping=True,
    )
    logger.info("CloudSQL connected via Cloud SQL Connector")
    return _engine


def sync_box_folder_to_cloudsql(
    folder_id: str,
    folder_name: str,
    contact_ids: Optional[List[str]] = None,
    deal_id: Optional[str] = None,
) -> bool:
    """
    Sync a newly created Box folder to CloudSQL tables.

    Inserts into:
    - box_folders (folder_id, folder_name, sync_status, last_synced)
    - contact_box_associations (contact_id, folder_id) for each contact

    Args:
        folder_id: Box folder ID
        folder_name: Display name of the folder
        contact_ids: List of HubSpot contact IDs to associate
        deal_id: Optional HubSpot deal ID for logging

    Returns:
        True if sync succeeded, False otherwise
    """
    engine = _get_engine()
    if engine is None:
        logger.debug("CloudSQL not configured, skipping folder sync")
        return False

    try:
        from sqlalchemy import text

        with engine.connect() as conn:
            # Insert or update box_folders
            conn.execute(
                text(
                    """
                    INSERT INTO box_folders (folder_id, folder_name, sync_status, last_synced)
                    VALUES (:folder_id, :folder_name, 'synced', CURRENT_TIMESTAMP)
                    ON CONFLICT (folder_id) DO UPDATE SET
                        folder_name = EXCLUDED.folder_name,
                        sync_status = 'synced',
                        last_synced = CURRENT_TIMESTAMP
                """
                ),
                {"folder_id": folder_id, "folder_name": folder_name},
            )
            logger.info("Synced box_folders: folder_id=%s, name=%s", folder_id, folder_name)

            # Insert contact associations
            if contact_ids:
                for contact_id in contact_ids:
                    if not contact_id:
                        continue
                    conn.execute(
                        text(
                            """
                            INSERT INTO contact_box_associations (contact_id, folder_id, created_at)
                            VALUES (:contact_id, :folder_id, CURRENT_TIMESTAMP)
                            ON CONFLICT (contact_id, folder_id) DO NOTHING
                        """
                        ),
                        {"contact_id": str(contact_id), "folder_id": folder_id},
                    )
                logger.info(
                    "Synced contact_box_associations: folder_id=%s, contacts=%s",
                    folder_id,
                    contact_ids,
                )

            conn.commit()
            return True

    except Exception as e:
        logger.error(
            "Failed to sync folder %s to CloudSQL: %s",
            folder_id,
            str(e),
            exc_info=True,
        )
        return False
