"""OAuth token management and Employment Hero authentication service."""

import time
import logging
from typing import Optional, Dict
from urllib.parse import urlencode

from flask import session
import requests

from utils.common import USE_FIRESTORE
from utils.secrets import get_secret
from utils.http_client import post_with_retries, LONG_TIMEOUT

logger = logging.getLogger(__name__)

# OAuth Configuration
EH_AUTHORIZE_URL = None  # Set via init_oauth_service
EH_TOKEN_URL = None
EH_CLIENT_ID = None
EH_CLIENT_SECRET = None
REDIRECT_URI = None

_db = None


def init_oauth_service(db=None, config: Optional[Dict] = None):
    """Initialize OAuth service with configuration and database.

    Args:
        db: Firestore database client (optional)
        config: Dict with keys: EH_AUTHORIZE_URL, EH_TOKEN_URL, EH_CLIENT_ID,
                EH_CLIENT_SECRET, REDIRECT_URI
    """
    global _db, EH_AUTHORIZE_URL, EH_TOKEN_URL, EH_CLIENT_ID, EH_CLIENT_SECRET, REDIRECT_URI

    _db = db
    if config:
        EH_AUTHORIZE_URL = config.get("EH_AUTHORIZE_URL")
        EH_TOKEN_URL = config.get("EH_TOKEN_URL")
        EH_CLIENT_ID = config.get("EH_CLIENT_ID")
        EH_CLIENT_SECRET = config.get("EH_CLIENT_SECRET")
        REDIRECT_URI = config.get("REDIRECT_URI")


def get_oauth_config() -> Dict[str, str]:
    """Get current OAuth configuration.

    Returns:
        Dict with oauth configuration
    """
    return {
        "EH_AUTHORIZE_URL": EH_AUTHORIZE_URL,
        "EH_TOKEN_URL": EH_TOKEN_URL,
        "EH_CLIENT_ID": EH_CLIENT_ID,
        "EH_CLIENT_SECRET": EH_CLIENT_SECRET,
        "REDIRECT_URI": REDIRECT_URI,
    }


def token_key() -> str:
    """Get the token storage key.

    Returns:
        str: Token partition key (per-session or fixed dev key)
    """
    # Prefer a per-user/session key; fall back to fixed key
    return "e268304d2ad0444c"


def save_tokens(tokens: Dict) -> None:
    """Persist OAuth tokens with absolute expiry time.

    Args:
        tokens: Token response dict with access_token, refresh_token, expires_in
    """
    tokens = dict(tokens)
    # Track absolute expiry (subtract 60s for clock skew)
    tokens["_expires_at"] = time.time() + max(0, int(tokens.get("expires_in", 0)) - 60)

    if USE_FIRESTORE and _db:
        _db.collection("eh_tokens").document(token_key()).set(tokens)
    else:
        session["eh_tokens"] = tokens

    logger.info("OAuth tokens saved (expires at %s)", tokens.get("_expires_at"))


def load_tokens() -> Optional[Dict]:
    """Load stored OAuth tokens.

    Returns:
        Dict with tokens if found, None otherwise
    """
    if USE_FIRESTORE and _db:
        try:
            doc = _db.collection("eh_tokens").document(token_key()).get()
            return doc.to_dict() if doc.exists else None
        except Exception as exc:
            logger.warning("Failed to load tokens from Firestore: %s", exc)
            return None
    return session.get("eh_tokens")


def update_tokens(tokens: Dict) -> None:
    """Update stored tokens.

    Args:
        tokens: New token dict
    """
    save_tokens(tokens)


def exchange_code_for_tokens(code: str) -> Dict:
    """Exchange authorization code for access and refresh tokens.

    Args:
        code: Authorization code from OAuth provider

    Returns:
        Dict with access_token, refresh_token, expires_in, etc.

    Raises:
        RuntimeError: If token exchange fails
    """
    if not all([EH_CLIENT_ID, EH_CLIENT_SECRET, EH_TOKEN_URL, REDIRECT_URI]):
        raise RuntimeError("OAuth configuration incomplete")

    data = {
        "client_id": EH_CLIENT_ID,
        "client_secret": EH_CLIENT_SECRET,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
    }

    try:
        resp = post_with_retries(EH_TOKEN_URL, data=data, timeout=LONG_TIMEOUT, retries=2)
        if resp.status_code != 200:
            raise RuntimeError(f"Token exchange failed: {resp.status_code} {resp.text}")
        return resp.json()
    except Exception as exc:
        logger.error("Failed to exchange code for tokens: %s", exc)
        raise


def refresh_access_token(refresh_token: str) -> Dict:
    """Refresh an access token using the refresh token.

    Args:
        refresh_token: Refresh token from previous auth

    Returns:
        Dict with new access_token, possibly new refresh_token

    Raises:
        RuntimeError: If refresh fails
    """
    if not all([EH_CLIENT_ID, EH_CLIENT_SECRET, EH_TOKEN_URL]):
        raise RuntimeError("OAuth configuration incomplete")

    data = {
        "client_id": EH_CLIENT_ID,
        "client_secret": EH_CLIENT_SECRET,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }

    try:
        resp = post_with_retries(EH_TOKEN_URL, data=data, timeout=LONG_TIMEOUT, retries=2)
        if resp.status_code != 200:
            raise RuntimeError(f"Refresh failed: {resp.status_code} {resp.text}")
        return resp.json()
    except Exception as exc:
        logger.error("Failed to refresh access token: %s", exc)
        raise


def get_access_token() -> str:
    """Get a valid access token, refreshing if necessary.

    Returns:
        str: Valid access token

    Raises:
        RuntimeError: If no tokens found or refresh fails
    """
    tok = load_tokens()
    if not tok:
        raise RuntimeError("No tokens found. Start at /auth/start")

    # Check if token is expired (with 60s buffer)
    if time.time() >= tok.get("_expires_at", 0):
        logger.info("Token expired, refreshing...")
        try:
            new_tok = refresh_access_token(tok["refresh_token"])
            # Keep old refresh_token if provider doesn't return new one
            if "refresh_token" not in new_tok:
                new_tok["refresh_token"] = tok["refresh_token"]
            update_tokens(new_tok)
            return new_tok["access_token"]
        except Exception as exc:
            logger.error("Token refresh failed: %s", exc)
            raise

    return tok["access_token"]


def build_authorization_url(state: str) -> str:
    """Build the OAuth authorization URL.

    Args:
        state: CSRF state token

    Returns:
        str: Full authorization URL

    Raises:
        RuntimeError: If config incomplete
    """
    if not all([EH_AUTHORIZE_URL, EH_CLIENT_ID, REDIRECT_URI]):
        raise RuntimeError("OAuth configuration incomplete")

    params = {
        "client_id": EH_CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "state": state,
    }
    return f"{EH_AUTHORIZE_URL}?{urlencode(params)}"


__all__ = [
    "init_oauth_service",
    "get_oauth_config",
    "token_key",
    "save_tokens",
    "load_tokens",
    "update_tokens",
    "exchange_code_for_tokens",
    "refresh_access_token",
    "get_access_token",
    "build_authorization_url",
]
