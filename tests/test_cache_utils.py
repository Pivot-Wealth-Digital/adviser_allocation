"""Tests for cache utilities."""

import time
import unittest

from adviser_allocation.utils.cache_utils import CacheEntry, TTLCache, cached_with_ttl


class CacheUtilsTests(unittest.TestCase):
    """Test suite for cache utilities."""

    def test_cache_entry_not_expired(self):
        """Test cache entry that is not yet expired."""
        entry = CacheEntry(value="test_value", timestamp=time.time(), ttl=300)
        self.assertFalse(entry.is_expired())
        self.assertTrue(entry.is_valid())

    def test_cache_entry_expired(self):
        """Test cache entry that has expired."""
        entry = CacheEntry(
            value="test_value", timestamp=time.time() - 400, ttl=300  # 400 seconds ago
        )
        self.assertTrue(entry.is_expired())
        self.assertFalse(entry.is_valid())

    def test_ttl_cache_set_and_get(self):
        """Test TTL cache set and get operations."""
        cache = TTLCache(ttl=60)

        cache.set("key1", "value1")
        result = cache.get("key1")

        self.assertEqual(result, "value1")

    def test_ttl_cache_get_nonexistent(self):
        """Test getting nonexistent key from cache."""
        cache = TTLCache(ttl=60)

        result = cache.get("nonexistent")
        self.assertIsNone(result)

    def test_ttl_cache_expiration(self):
        """Test that expired entries are removed."""
        cache = TTLCache(ttl=1)

        cache.set("key1", "value1")
        self.assertIsNotNone(cache.get("key1"))

        # Wait for expiration
        time.sleep(1.1)

        result = cache.get("key1")
        self.assertIsNone(result)

    def test_ttl_cache_delete(self):
        """Test cache deletion."""
        cache = TTLCache(ttl=60)

        cache.set("key1", "value1")
        cache.delete("key1")

        result = cache.get("key1")
        self.assertIsNone(result)

    def test_ttl_cache_clear(self):
        """Test cache clearing."""
        cache = TTLCache(ttl=60)

        cache.set("key1", "value1")
        cache.set("key2", "value2")

        cache.clear()

        self.assertIsNone(cache.get("key1"))
        self.assertIsNone(cache.get("key2"))

    def test_ttl_cache_cleanup_expired(self):
        """Test cleanup of expired entries."""
        cache = TTLCache(ttl=1)

        cache.set("key1", "value1")
        cache.set("key2", "value2", ttl=60)

        time.sleep(1.1)

        count = cache.cleanup_expired()
        self.assertEqual(count, 1)
        self.assertIsNone(cache.get("key1"))
        self.assertIsNotNone(cache.get("key2"))

    def test_ttl_cache_stats(self):
        """Test cache statistics."""
        cache = TTLCache(ttl=60)

        cache.set("key1", "value1")
        cache.set("key2", "value2")

        stats = cache.stats()

        self.assertIn("total", stats)
        self.assertIn("valid", stats)
        self.assertEqual(stats["total"], 2)

    def test_ttl_cache_custom_ttl(self):
        """Test setting custom TTL on individual entries."""
        cache = TTLCache(ttl=300)

        cache.set("short", "value", ttl=1)
        cache.set("long", "value", ttl=300)

        time.sleep(1.1)

        self.assertIsNone(cache.get("short"))
        self.assertIsNotNone(cache.get("long"))

    def test_cached_with_ttl_decorator(self):
        """Test cached_with_ttl decorator."""
        call_count = 0

        @cached_with_ttl(ttl=60)
        def expensive_function(x, y):
            nonlocal call_count
            call_count += 1
            return x + y

        # First call
        result1 = expensive_function(1, 2)
        self.assertEqual(result1, 3)
        self.assertEqual(call_count, 1)

        # Second call with same args should use cache
        result2 = expensive_function(1, 2)
        self.assertEqual(result2, 3)
        self.assertEqual(call_count, 1)  # Not incremented

        # Call with different args should not use cache
        result3 = expensive_function(2, 3)
        self.assertEqual(result3, 5)
        self.assertEqual(call_count, 2)

    def test_cached_with_ttl_expiration(self):
        """Test that cached_with_ttl respects TTL."""
        call_count = 0

        @cached_with_ttl(ttl=1)
        def expensive_function(x):
            nonlocal call_count
            call_count += 1
            return x * 2

        result1 = expensive_function(5)
        self.assertEqual(result1, 10)
        self.assertEqual(call_count, 1)

        time.sleep(1.1)

        # After expiration, should call function again
        result2 = expensive_function(5)
        self.assertEqual(result2, 10)
        self.assertEqual(call_count, 2)


if __name__ == "__main__":
    unittest.main()
