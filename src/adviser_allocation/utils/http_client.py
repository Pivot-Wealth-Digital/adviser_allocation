"""HTTP client utilities with retry logic, timeouts, and circuit breaking."""

import logging
from typing import Optional, Dict, Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
    retry_if_result,
)

logger = logging.getLogger(__name__)

# Default timeouts (in seconds)
DEFAULT_TIMEOUT = 10
LONG_TIMEOUT = 30


def create_session_with_retries(
    retries: int = 3,
    backoff_factor: float = 0.3,
    status_forcelist: tuple = (429, 500, 502, 503, 504),
) -> requests.Session:
    """Create a requests session with automatic retry logic.

    Args:
        retries: Number of retry attempts
        backoff_factor: Backoff factor for exponential waits
        status_forcelist: HTTP status codes to retry on

    Returns:
        A configured requests.Session with retry adapter
    """
    session = requests.Session()
    retry_strategy = Retry(
        total=retries,
        backoff_factor=backoff_factor,
        status_forcelist=status_forcelist,
        allowed_methods=["HEAD", "GET", "OPTIONS", "POST", "PUT", "DELETE"],
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def get_with_retries(
    url: str,
    headers: Optional[Dict[str, str]] = None,
    params: Optional[Dict[str, Any]] = None,
    timeout: int = DEFAULT_TIMEOUT,
    retries: int = 3,
    **kwargs,
) -> requests.Response:
    """GET request with automatic retries and timeout.

    Args:
        url: The URL to request
        headers: Optional headers dict
        params: Optional query parameters
        timeout: Request timeout in seconds
        retries: Number of retry attempts
        **kwargs: Additional arguments to pass to requests.get

    Returns:
        requests.Response object

    Raises:
        requests.RequestException: If all retries fail
    """
    session = create_session_with_retries(retries=retries)
    try:
        return session.get(url, headers=headers, params=params, timeout=timeout, **kwargs)
    finally:
        session.close()


def post_with_retries(
    url: str,
    json: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
    timeout: int = DEFAULT_TIMEOUT,
    retries: int = 3,
    **kwargs,
) -> requests.Response:
    """POST request with automatic retries and timeout.

    Args:
        url: The URL to request
        json: Optional JSON payload
        headers: Optional headers dict
        timeout: Request timeout in seconds
        retries: Number of retry attempts
        **kwargs: Additional arguments to pass to requests.post

    Returns:
        requests.Response object

    Raises:
        requests.RequestException: If all retries fail
    """
    session = create_session_with_retries(retries=retries)
    try:
        return session.post(url, json=json, headers=headers, timeout=timeout, **kwargs)
    finally:
        session.close()


def patch_with_retries(
    url: str,
    json: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
    data: Optional[str] = None,
    timeout: int = DEFAULT_TIMEOUT,
    retries: int = 3,
    **kwargs,
) -> requests.Response:
    """PATCH request with automatic retries and timeout.

    Args:
        url: The URL to request
        json: Optional JSON payload
        headers: Optional headers dict
        data: Optional raw data payload
        timeout: Request timeout in seconds
        retries: Number of retry attempts
        **kwargs: Additional arguments to pass to requests.patch

    Returns:
        requests.Response object

    Raises:
        requests.RequestException: If all retries fail
    """
    session = create_session_with_retries(retries=retries)
    try:
        if data is not None:
            return session.patch(url, data=data, headers=headers, timeout=timeout, **kwargs)
        return session.patch(url, json=json, headers=headers, timeout=timeout, **kwargs)
    finally:
        session.close()


def delete_with_retries(
    url: str,
    headers: Optional[Dict[str, str]] = None,
    timeout: int = DEFAULT_TIMEOUT,
    retries: int = 3,
    **kwargs,
) -> requests.Response:
    """DELETE request with automatic retries and timeout.

    Args:
        url: The URL to request
        headers: Optional headers dict
        timeout: Request timeout in seconds
        retries: Number of retry attempts
        **kwargs: Additional arguments to pass to requests.delete

    Returns:
        requests.Response object

    Raises:
        requests.RequestException: If all retries fail
    """
    session = create_session_with_retries(retries=retries)
    try:
        return session.delete(url, headers=headers, timeout=timeout, **kwargs)
    finally:
        session.close()


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    retry=retry_if_exception_type(requests.RequestException),
)
def get_with_tenacity(
    url: str,
    headers: Optional[Dict[str, str]] = None,
    params: Optional[Dict[str, Any]] = None,
    timeout: int = DEFAULT_TIMEOUT,
    **kwargs,
) -> requests.Response:
    """GET request with tenacity retry decorator.

    Better for inline usage where you want retry logic without session creation.

    Args:
        url: The URL to request
        headers: Optional headers dict
        params: Optional query parameters
        timeout: Request timeout in seconds
        **kwargs: Additional arguments to pass to requests.get

    Returns:
        requests.Response object

    Raises:
        requests.RequestException: If all retries fail
    """
    return requests.get(url, headers=headers, params=params, timeout=timeout, **kwargs)


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    retry=retry_if_exception_type(requests.RequestException),
)
def post_with_tenacity(
    url: str,
    json: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
    timeout: int = DEFAULT_TIMEOUT,
    **kwargs,
) -> requests.Response:
    """POST request with tenacity retry decorator.

    Better for inline usage where you want retry logic without session creation.

    Args:
        url: The URL to request
        json: Optional JSON payload
        headers: Optional headers dict
        timeout: Request timeout in seconds
        **kwargs: Additional arguments to pass to requests.post

    Returns:
        requests.Response object

    Raises:
        requests.RequestException: If all retries fail
    """
    return requests.post(url, json=json, headers=headers, timeout=timeout, **kwargs)


__all__ = [
    "create_session_with_retries",
    "get_with_retries",
    "post_with_retries",
    "patch_with_retries",
    "delete_with_retries",
    "get_with_tenacity",
    "post_with_tenacity",
    "DEFAULT_TIMEOUT",
    "LONG_TIMEOUT",
]
