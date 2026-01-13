"""Cache utilities with TTL support."""

import time
import logging
from typing import Optional, Callable, Any, TypeVar, Dict
from functools import wraps
from dataclasses import dataclass

logger = logging.getLogger(__name__)

T = TypeVar('T')


@dataclass
class CacheEntry:
    """A cache entry with timestamp and TTL."""
    value: Any
    timestamp: float
    ttl: int  # seconds

    def is_expired(self) -> bool:
        """Check if this entry has expired."""
        return time.time() - self.timestamp > self.ttl

    def is_valid(self) -> bool:
        """Check if this entry is still valid."""
        return not self.is_expired()


class TTLCache:
    """Simple in-memory cache with TTL support."""

    def __init__(self, ttl: int = 300):
        """Initialize cache.

        Args:
            ttl: Time-to-live in seconds (default 5 minutes)
        """
        self.ttl = ttl
        self._cache: Dict[str, CacheEntry] = {}

    def get(self, key: str) -> Optional[Any]:
        """Get a value from cache if not expired.

        Args:
            key: Cache key

        Returns:
            Cached value or None if not found/expired
        """
        if key not in self._cache:
            return None
        entry = self._cache[key]
        if entry.is_expired():
            del self._cache[key]
            return None
        return entry.value

    def set(self, key: str, value: Any, ttl: Optional[int] = None) -> None:
        """Set a value in cache.

        Args:
            key: Cache key
            value: Value to cache
            ttl: Optional custom TTL (uses default if not provided)
        """
        self._cache[key] = CacheEntry(
            value=value,
            timestamp=time.time(),
            ttl=ttl or self.ttl
        )

    def delete(self, key: str) -> None:
        """Delete a key from cache.

        Args:
            key: Cache key
        """
        self._cache.pop(key, None)

    def clear(self) -> None:
        """Clear all entries from cache."""
        self._cache.clear()

    def cleanup_expired(self) -> int:
        """Remove all expired entries. Returns count removed."""
        expired_keys = [k for k, v in self._cache.items() if v.is_expired()]
        for key in expired_keys:
            del self._cache[key]
        if expired_keys:
            logger.debug("Cleaned up %d expired cache entries", len(expired_keys))
        return len(expired_keys)

    def stats(self) -> Dict[str, int]:
        """Get cache statistics.

        Returns:
            Dict with total, valid, and expired entry counts
        """
        self.cleanup_expired()
        total = len(self._cache)
        return {
            "total": total,
            "valid": total,
            "expired": 0,
        }


def cached_with_ttl(ttl: int = 300):
    """Decorator for functions with TTL-based caching.

    Args:
        ttl: Time-to-live in seconds

    Returns:
        Decorator function
    """
    cache = TTLCache(ttl=ttl)

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @wraps(func)
        def wrapper(*args, **kwargs) -> T:
            # Build cache key from function name and arguments
            cache_key = f"{func.__name__}:{args}:{sorted(kwargs.items())}"

            # Try to get from cache
            cached_value = cache.get(cache_key)
            if cached_value is not None:
                logger.debug("Cache hit for %s", func.__name__)
                return cached_value

            # Not in cache or expired, call function
            logger.debug("Cache miss for %s", func.__name__)
            result = func(*args, **kwargs)
            cache.set(cache_key, result)
            return result

        # Expose cache for manual clearing if needed
        wrapper.cache = cache  # type: ignore
        return wrapper

    return decorator


__all__ = [
    "CacheEntry",
    "TTLCache",
    "cached_with_ttl",
]
