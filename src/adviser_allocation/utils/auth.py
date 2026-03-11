"""Authentication decorators for securing endpoints."""

import hashlib
import hmac
import logging
import os
from functools import wraps

from flask import jsonify, request

from adviser_allocation.utils.secrets import get_secret

logger = logging.getLogger(__name__)

try:
    from google.auth.transport import requests as google_requests
    from google.oauth2 import id_token as google_id_token
except ImportError:  # pragma: no cover
    google_requests = None  # type: ignore[assignment]
    google_id_token = None  # type: ignore[assignment]


def require_oidc_token(func):
    """Verify Google OIDC token from Authorization header.

    Validates that the request contains a valid Google-issued OIDC token.
    Optionally checks the token email matches SCHEDULER_SERVICE_ACCOUNT.
    """

    @wraps(func)
    def wrapper(*args, **kwargs):
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            logger.warning("OIDC auth failed: missing Bearer token on %s", request.path)
            return jsonify({"error": "Unauthorized"}), 401

        token = auth_header.removeprefix("Bearer ")

        if not google_id_token or not google_requests:
            logger.error("google-auth library not available for OIDC verification")
            return jsonify({"error": "Internal server error"}), 500

        try:
            claims = google_id_token.verify_oauth2_token(
                token,
                google_requests.Request(),
            )
        except ValueError as exc:
            logger.warning("OIDC token verification failed on %s: %s", request.path, exc)
            return jsonify({"error": "Unauthorized"}), 401

        expected_sa = get_secret("SCHEDULER_SERVICE_ACCOUNT")
        if not expected_sa:
            if os.environ.get("K_SERVICE"):
                logger.error(
                    "SCHEDULER_SERVICE_ACCOUNT not configured — rejecting OIDC request on %s",
                    request.path,
                )
                return jsonify({"error": "Internal server error"}), 500
        else:
            token_email = claims.get("email", "")
            if token_email != expected_sa:
                logger.warning(
                    "OIDC email mismatch on %s: got %s, expected %s",
                    request.path,
                    token_email,
                    expected_sa,
                )
                return jsonify({"error": "Unauthorized"}), 401

        return func(*args, **kwargs)

    return wrapper


def require_hubspot_signature(func):
    """Verify HubSpot Private App webhook using X-HubSpot-Signature (v2).

    Per https://developers.hubspot.com/docs/apps/legacy-apps/authentication/validating-requests

    Private apps use v2 signatures (workflow webhook actions):
    1. Build source string = client_secret + HTTP method + URI + body
    2. SHA-256 hash (plain, not HMAC)
    3. Hex-encode and constant-time compare against X-HubSpot-Signature header
    """

    @wraps(func)
    def wrapper(*args, **kwargs):
        client_secret = get_secret("HUBSPOT_CLIENT_SECRET")
        if not client_secret:
            logger.error("HUBSPOT_CLIENT_SECRET not configured for signature verification")
            return jsonify({"error": "Internal server error"}), 500

        signature = request.headers.get("X-HubSpot-Signature", "")
        if not signature:
            logger.warning(
                "HubSpot signature missing on %s from %s",
                request.path,
                request.remote_addr,
            )
            return jsonify({"error": "Unauthorized"}), 401

        request_body = request.get_data(as_text=True)
        source_string = client_secret + request.method + request.url + request_body

        computed_hex = hashlib.sha256(source_string.encode("utf-8")).hexdigest()

        if not hmac.compare_digest(computed_hex, signature):
            logger.warning(
                "HubSpot signature mismatch on %s from %s",
                request.path,
                request.remote_addr,
            )
            return jsonify({"error": "Unauthorized"}), 401

        return func(*args, **kwargs)

    return wrapper


def require_api_key(func):
    """Verify API key from query parameter or X-API-Key header.

    Compares the provided key against WEBHOOK_API_KEY secret
    using constant-time comparison.
    """

    @wraps(func)
    def wrapper(*args, **kwargs):
        expected_key = get_secret("ADVISER_ALLOCATION_WEBHOOK_API_KEY")
        if not expected_key:
            logger.error("ADVISER_ALLOCATION_WEBHOOK_API_KEY not configured")
            return jsonify({"error": "Internal server error"}), 500

        provided_key = request.headers.get("X-API-Key", "") or request.args.get("api_key", "")
        if not provided_key or not hmac.compare_digest(provided_key, expected_key):
            logger.warning("API key auth failed on %s from %s", request.path, request.remote_addr)
            return jsonify({"error": "Unauthorized"}), 401

        return func(*args, **kwargs)

    return wrapper
