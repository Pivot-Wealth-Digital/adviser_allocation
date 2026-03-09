"""
Database connection management for Adviser Allocation.
Uses Cloud SQL PostgreSQL (shared client_pipeline database).
"""

import logging
import os
from typing import Optional

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

# Try to import Cloud SQL Connector (for cloud deployments)
# Falls back to direct pg8000 connection via Cloud SQL Proxy for local dev
try:
    from google.cloud.sql.connector import Connector

    CLOUD_SQL_CONNECTOR_AVAILABLE = True
except ImportError:
    CLOUD_SQL_CONNECTOR_AVAILABLE = False
    Connector = None

logger = logging.getLogger(__name__)

# Module-level engine cache
_engine: Optional[Engine] = None


class CloudSQLConnector:
    """Manages Cloud SQL connections using Cloud SQL Python Connector"""

    def __init__(
        self,
        instance_connection_string: str,
        user: str,
        password: str,
        db: str,
        pool_size: int = 5,
        max_overflow: int = 2,
        enable_iam_auth: bool = False,
    ):
        self.instance_connection_string = instance_connection_string
        self.user = user
        self.password = password
        self.db = db
        self.pool_size = pool_size
        self.max_overflow = max_overflow
        self.enable_iam_auth = enable_iam_auth
        self.connector = Connector()
        self.engine: Optional[Engine] = None

    def connect(self) -> Engine:
        """Create SQLAlchemy engine with Cloud SQL connector"""

        def getconn():
            kwargs = {
                "instance_connection_string": self.instance_connection_string,
                "driver": "pg8000",
                "user": self.user,
                "db": self.db,
            }
            if self.enable_iam_auth:
                kwargs["enable_iam_auth"] = True
            else:
                kwargs["password"] = self.password
            return self.connector.connect(**kwargs)

        self.engine = create_engine(
            "postgresql+pg8000://",
            creator=getconn,
            pool_size=self.pool_size,
            max_overflow=self.max_overflow,
            pool_timeout=30,
            pool_recycle=1800,  # Recycle connections every 30 min
            pool_pre_ping=True,  # Verify connections before use
        )

        auth_mode = "IAM" if self.enable_iam_auth else "password"
        logger.info("Connected to Cloud SQL (auth=%s)", auth_mode)
        return self.engine

    def close(self):
        """Close connection pool and connector"""
        if self.engine:
            self.engine.dispose()
        self.connector.close()
        logger.info("Closed Cloud SQL connections")


def get_db_engine(force_new: bool = False) -> Engine:
    """Get or create Cloud SQL engine from environment variables.

    Supports three connection modes (in order of preference):
    1. Cloud SQL Proxy (local dev): Set CLOUD_SQL_USE_PROXY=true, connects to localhost:5432
    2. IAM auth (cloud): Set CLOUD_SQL_IAM_USER to the service account email
    3. Password auth (fallback): Set CLOUD_SQL_USER + CLOUD_SQL_PASSWORD

    Args:
        force_new: If True, create a new engine even if one exists.

    Returns:
        SQLAlchemy Engine instance.
    """
    global _engine

    if _engine is not None and not force_new:
        return _engine

    db_name = os.getenv("CLOUD_SQL_DATABASE", "client_pipeline")
    db_user = os.getenv("CLOUD_SQL_USER", "postgres")
    db_password = os.getenv("CLOUD_SQL_PASSWORD")

    # Mode 1: Cloud SQL Proxy (local development)
    use_proxy = os.getenv("CLOUD_SQL_USE_PROXY", "").lower() in ("true", "1", "yes")

    # Auto-detect proxy mode if connector not available
    if not CLOUD_SQL_CONNECTOR_AVAILABLE:
        use_proxy = True
        logger.info("Cloud SQL Connector not available, using proxy mode")

    if use_proxy:
        proxy_host = os.getenv("CLOUD_SQL_PROXY_HOST", "127.0.0.1")
        proxy_port = int(os.getenv("CLOUD_SQL_PROXY_PORT", "5432"))

        if not db_password:
            raise ValueError(
                "Missing CLOUD_SQL_PASSWORD for proxy connection. Set in environment or .env file"
            )

        # Direct pg8000 connection via Cloud SQL Proxy
        from urllib.parse import quote_plus

        encoded_password = quote_plus(db_password)

        connection_url = (
            f"postgresql+pg8000://{db_user}:{encoded_password}@{proxy_host}:{proxy_port}/{db_name}"
        )
        _engine = create_engine(
            connection_url,
            pool_size=5,
            max_overflow=2,
            pool_timeout=30,
            pool_recycle=1800,
            pool_pre_ping=True,
        )
        logger.info("Connected to Cloud SQL via proxy (%s:%s)", proxy_host, proxy_port)
        return _engine

    # Mode 2 & 3: Cloud SQL Connector (for cloud deployments)
    instance_conn_str = os.getenv("CLOUD_SQL_CONNECTION_STRING")
    if not instance_conn_str:
        raise ValueError("Missing required env var: CLOUD_SQL_CONNECTION_STRING")

    # Prefer IAM auth (service account), fall back to password auth
    iam_user = os.getenv("CLOUD_SQL_IAM_USER")
    if iam_user:
        # IAM auth: user is the SA email without .gserviceaccount.com
        db_user = iam_user.replace(".gserviceaccount.com", "")
        connector = CloudSQLConnector(
            instance_connection_string=instance_conn_str,
            user=db_user,
            password="",
            db=db_name,
            enable_iam_auth=True,
        )
    else:
        if not db_password:
            raise ValueError(
                "Missing required env var: set CLOUD_SQL_IAM_USER for IAM auth "
                "or CLOUD_SQL_PASSWORD for password auth"
            )
        connector = CloudSQLConnector(
            instance_connection_string=instance_conn_str,
            user=db_user,
            password=db_password,
            db=db_name,
        )

    _engine = connector.connect()
    return _engine
