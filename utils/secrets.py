import os
import logging
from typing import Optional

try:
    import google.auth
    from google.cloud import secretmanager
except Exception:  # pragma: no cover
    google = None  # type: ignore
    secretmanager = None  # type: ignore


def _make_sm_client():
    try:
        if not google or not secretmanager:
            return None
        credentials, _ = google.auth.default()  # Uses ADC in GCP; local may fail
        return secretmanager.SecretManagerServiceClient(credentials=credentials)
    except Exception as e:  # pragma: no cover
        logging.debug(f"Secret Manager client unavailable: {e}")
        return None


_SM_CLIENT = _make_sm_client()


def get_secret(name: str) -> Optional[str]:
    """Return a secret value by name.

    Behavior:
    - Reads environment variable `name` first. If missing, returns None.
    - If the env var value looks like a Secret Manager resource path
      (starts with 'projects/'), fetch the secret payload using ADC.
    - Otherwise, returns the env var value directly.
    - On any error, falls back to returning the env var value (which may be None).
    """
    hint = os.environ.get(name)
    if not hint:
        return None

    try:
        if isinstance(hint, str) and hint.startswith("projects/") and _SM_CLIENT:
            resp = _SM_CLIENT.access_secret_version(request={"name": hint})
            return resp.payload.data.decode("utf-8")
        return hint
    except Exception as e:  # pragma: no cover
        logging.warning(f"get_secret fallback for {name}: {e}")
        return hint

