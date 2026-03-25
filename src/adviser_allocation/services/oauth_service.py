"""OAuth token management and Employment Hero authentication service."""

import logging
import os
import time
from typing import Dict, Optional
from urllib.parse import urlencode

from adviser_allocation.utils.common import get_cloudsql_db
from adviser_allocation.utils.http_client import LONG_TIMEOUT, post_with_retries
from adviser_allocation.utils.secrets import get_secret

logger = logging.getLogger(__name__)

# OAuth Configuration — populated lazily via _ensure_config()
EH_AUTHORIZE_URL = None
EH_TOKEN_URL = None
EH_CLIENT_ID = None
EH_CLIENT_SECRET = None
REDIRECT_URI = None
_CONFIG_LOADED = False


def _ensure_config():
    """Lazily load EH OAuth config from environment/secrets on first use."""
    global EH_AUTHORIZE_URL, EH_TOKEN_URL, EH_CLIENT_ID, EH_CLIENT_SECRET
    global REDIRECT_URI, _CONFIG_LOADED
    if _CONFIG_LOADED:
        return
    EH_AUTHORIZE_URL = os.environ.get(
        "EH_AUTHORIZE_URL", "https://oauth.employmenthero.com/oauth2/authorize"
    )
    EH_TOKEN_URL = os.environ.get("EH_TOKEN_URL", "https://oauth.employmenthero.com/oauth2/token")
    EH_CLIENT_ID = get_secret("EH_CLIENT_ID")
    EH_CLIENT_SECRET = get_secret("EH_CLIENT_SECRET")
    REDIRECT_URI = os.environ.get("REDIRECT_URI")
    _CONFIG_LOADED = True


def init_oauth_service(db=None, config: Optional[Dict] = None):
    """Initialize OAuth service with configuration.

    Args:
        db: Legacy parameter (ignored) - CloudSQL is used automatically.
        config: Dict with keys: EH_AUTHORIZE_URL, EH_TOKEN_URL, EH_CLIENT_ID,
                EH_CLIENT_SECRET, REDIRECT_URI
    """
    global EH_AUTHORIZE_URL, EH_TOKEN_URL, EH_CLIENT_ID, EH_CLIENT_SECRET
    global REDIRECT_URI, _CONFIG_LOADED

    if config:
        EH_AUTHORIZE_URL = config.get("EH_AUTHORIZE_URL")
        EH_TOKEN_URL = config.get("EH_TOKEN_URL")
        EH_CLIENT_ID = config.get("EH_CLIENT_ID")
        EH_CLIENT_SECRET = config.get("EH_CLIENT_SECRET")
        REDIRECT_URI = config.get("REDIRECT_URI")
        _CONFIG_LOADED = True


def token_key() -> str:
    """Get the token storage key.

    Returns:
        str: Token partition key (per-session or fixed dev key)
    """
    # Prefer a per-user/session key; fall back to fixed key
    return "e268304d2ad0444c"


def save_tokens(tokens: Dict) -> None:
    """Persist OAuth tokens with absolute expiry time to CloudSQL.

    Args:
        tokens: Token response dict with access_token, refresh_token, expires_in
    """
    tokens = dict(tokens)
    # Track absolute expiry (subtract 60s for clock skew)
    expires_at = time.time() + max(0, int(tokens.get("expires_in", 0)) - 60)
    tokens["_expires_at"] = expires_at

    try:
        cloudsql_db = get_cloudsql_db()
        cloudsql_db.save_tokens(
            token_key=token_key(),
            provider="employment_hero",
            tokens=tokens,
        )
        logger.info("OAuth tokens saved to CloudSQL (expires at %s)", expires_at)
    except Exception as exc:
        logger.error("Failed to save tokens to CloudSQL: %s", exc)


def load_tokens() -> Optional[Dict]:
    """Load stored OAuth tokens from CloudSQL.

    Returns:
        Dict with tokens if found, None otherwise
    """
    try:
        cloudsql_db = get_cloudsql_db()
        tokens = cloudsql_db.load_tokens(token_key=token_key())
        if tokens:
            return tokens
    except Exception as exc:
        logger.warning("Failed to load tokens from CloudSQL: %s", exc)

    return None


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
    _ensure_config()
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
    _ensure_config()
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

    Includes one retry on refresh failure and sends a Google Chat alert
    if all attempts fail.

    Returns:
        str: Valid access token

    Raises:
        RuntimeError: If no tokens found or refresh fails after retry
    """
    _ensure_config()
    tok = load_tokens()
    if not tok:
        _alert_token_failure(
            "No EH OAuth tokens found in CloudSQL. Re-auth required at /auth/start"
        )
        raise RuntimeError("No tokens found. Start at /auth/start")

    # Token still valid — return immediately
    if time.time() < tok.get("_expires_at", 0):
        return tok["access_token"]

    # Token expired — refresh with one retry
    logger.info("Token expired, refreshing...")
    last_exc = None
    for attempt in range(2):
        try:
            new_tok = refresh_access_token(tok["refresh_token"])
            if "refresh_token" not in new_tok:
                new_tok["refresh_token"] = tok["refresh_token"]
            update_tokens(new_tok)
            return new_tok["access_token"]
        except Exception as exc:
            last_exc = exc
            if attempt == 0:
                logger.warning("Token refresh attempt 1 failed: %s — retrying in 2s", exc)
                time.sleep(2)

    _alert_token_failure(f"EH OAuth token refresh failed after 2 attempts: {last_exc}")
    raise RuntimeError(f"Token refresh failed after retry: {last_exc}")


def _alert_token_failure(message: str) -> None:
    """Send a Google Chat alert for token failures."""
    try:
        from adviser_allocation.api.webhooks import send_chat_alert

        send_chat_alert({"text": f"\u26a0\ufe0f {message}"})
    except Exception as exc:
        logger.warning("Could not send chat alert: %s", exc)


def build_authorization_url(state: str) -> str:
    """Build the OAuth authorization URL.

    Args:
        state: CSRF state token

    Returns:
        str: Full authorization URL

    Raises:
        RuntimeError: If config incomplete
    """
    _ensure_config()
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
    "token_key",
    "save_tokens",
    "load_tokens",
    "update_tokens",
    "exchange_code_for_tokens",
    "refresh_access_token",
    "get_access_token",
    "build_authorization_url",
]
