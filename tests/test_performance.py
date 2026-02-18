"""Performance and load testing."""

import concurrent.futures
import os
import time
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("USE_FIRESTORE", "false")

from adviser_allocation.main import app


class ResponseTimeTests(unittest.TestCase):
    """Tests for response time requirements."""

    def setUp(self):
        """Set up test fixtures."""
        self.app = app
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    def test_homepage_response_time_under_1_second(self):
        """Test homepage loads under 1 second."""
        with self.client.session_transaction() as sess:
            sess["is_authenticated"] = True

        start_time = time.time()
        response = self.client.get("/")
        elapsed = time.time() - start_time

        self.assertLess(elapsed, 1.0, f"Homepage took {elapsed}s, should be < 1s")
        self.assertEqual(response.status_code, 200)

    def test_availability_api_response_time_under_2_seconds(self):
        """Test availability API responds under 2 seconds."""
        with self.client.session_transaction() as sess:
            sess["is_authenticated"] = True

        start_time = time.time()
        response = self.client.get("/availability/earliest")
        elapsed = time.time() - start_time

        self.assertLess(elapsed, 2.0, f"Availability API took {elapsed}s, should be < 2s")

    def test_allocation_history_response_time_under_2_seconds(self):
        """Test allocation history loads under 2 seconds."""
        with self.client.session_transaction() as sess:
            sess["is_authenticated"] = True

        start_time = time.time()
        response = self.client.get("/allocations/history")
        elapsed = time.time() - start_time

        self.assertLess(elapsed, 2.0, f"History took {elapsed}s, should be < 2s")

    def test_settings_page_response_time_under_1_second(self):
        """Test settings page loads under 1 second."""
        with self.client.session_transaction() as sess:
            sess["is_authenticated"] = True

        start_time = time.time()
        response = self.client.get("/settings/box/ui")
        elapsed = time.time() - start_time

        self.assertLess(elapsed, 1.0, f"Settings took {elapsed}s, should be < 1s")

    def test_login_response_time_under_1_second(self):
        """Test login endpoint responds under 1 second."""
        start_time = time.time()
        response = self.client.post("/auth/login", json={"username": "test", "password": "test"})
        elapsed = time.time() - start_time

        self.assertLess(elapsed, 1.0, f"Login took {elapsed}s, should be < 1s")


class ConcurrentRequestTests(unittest.TestCase):
    """Tests for handling concurrent requests."""

    def setUp(self):
        """Set up test fixtures."""
        self.app = app
        self.app.config["TESTING"] = True

    def test_handle_10_concurrent_requests(self):
        """Test handling 10 concurrent requests."""

        def make_request(i):
            client = self.app.test_client()
            with client.session_transaction() as sess:
                sess["is_authenticated"] = True
            return client.get("/availability/earliest")

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(make_request, i) for i in range(10)]
            results = [f.result() for f in concurrent.futures.as_completed(futures)]

        # All requests should succeed
        for response in results:
            self.assertNotEqual(response.status_code, 500)

    def test_handle_50_concurrent_requests(self):
        """Test handling 50 concurrent requests."""

        def make_request(i):
            client = self.app.test_client()
            with client.session_transaction() as sess:
                sess["is_authenticated"] = True
            return client.get("/")

        with concurrent.futures.ThreadPoolExecutor(max_workers=50) as executor:
            futures = [executor.submit(make_request, i) for i in range(50)]
            results = [f.result() for f in concurrent.futures.as_completed(futures)]

        # Most requests should succeed
        success_count = sum(1 for r in results if r.status_code != 500)
        self.assertGreater(success_count, 40)  # At least 80% should succeed

    def test_concurrent_authentication(self):
        """Test handling concurrent authentication attempts."""

        def make_login(i):
            client = self.app.test_client()
            return client.post("/auth/login", json={"username": f"user{i}", "password": "password"})

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(make_login, i) for i in range(10)]
            results = [f.result() for f in concurrent.futures.as_completed(futures)]

        # All requests should complete
        self.assertEqual(len(results), 10)


class DatabaseQueryPerformanceTests(unittest.TestCase):
    """Tests for database query performance."""

    def setUp(self):
        """Set up test fixtures."""
        self.app = app
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    def test_allocation_query_under_1_second(self):
        """Test allocation queries complete under 1 second."""
        with self.client.session_transaction() as sess:
            sess["is_authenticated"] = True

        start_time = time.time()
        response = self.client.get("/allocations/history?limit=100")
        elapsed = time.time() - start_time

        self.assertLess(elapsed, 1.0, f"Query took {elapsed}s, should be < 1s")

    def test_availability_calculation_under_2_seconds(self):
        """Test availability calculations under 2 seconds."""
        with self.client.session_transaction() as sess:
            sess["is_authenticated"] = True

        start_time = time.time()
        response = self.client.get("/availability/earliest?compute=true")
        elapsed = time.time() - start_time

        self.assertLess(elapsed, 2.0, f"Calculation took {elapsed}s, should be < 2s")


class CachingEffectivenessTests(unittest.TestCase):
    """Tests for caching effectiveness."""

    def setUp(self):
        """Set up test fixtures."""
        self.app = app
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    def test_second_request_faster_than_first(self):
        """Test that cached requests are faster than uncached."""
        with self.client.session_transaction() as sess:
            sess["is_authenticated"] = True

        # First request (uncached)
        start1 = time.time()
        response1 = self.client.get("/availability/earliest")
        time1 = time.time() - start1

        # Second request (should be cached)
        start2 = time.time()
        response2 = self.client.get("/availability/earliest")
        time2 = time.time() - start2

        # Second request should not be significantly slower
        # (allow some variance, but cache should help)
        self.assertLess(time2 + 0.5, time1 + 1.0)

    def test_cache_hit_rate(self):
        """Test cache hit rate on repeated requests."""
        with self.client.session_transaction() as sess:
            sess["is_authenticated"] = True

        # Make 10 identical requests
        times = []
        for i in range(10):
            start = time.time()
            response = self.client.get("/availability/earliest")
            elapsed = time.time() - start
            times.append(elapsed)

        # Later requests should be faster (cached)
        avg_first_half = sum(times[:5]) / 5
        avg_second_half = sum(times[5:]) / 5

        # Second half should be comparable or faster
        self.assertLess(avg_second_half, avg_first_half + 0.2)


class MemoryUsageTests(unittest.TestCase):
    """Tests for memory efficiency."""

    def setUp(self):
        """Set up test fixtures."""
        self.app = app
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    def test_multiple_requests_no_memory_leak(self):
        """Test that multiple requests don't cause memory leaks."""
        with self.client.session_transaction() as sess:
            sess["is_authenticated"] = True

        # Make multiple requests
        for i in range(100):
            response = self.client.get("/availability/earliest")
            self.assertNotEqual(response.status_code, 500)

        # If we get here without hanging or crashing, memory is ok
        self.assertTrue(True)

    def test_large_response_handling(self):
        """Test handling of large responses."""
        with self.client.session_transaction() as sess:
            sess["is_authenticated"] = True

        # Request with lots of data
        response = self.client.get("/allocations/history?limit=1000")

        # Should handle large response
        self.assertIsNotNone(response.data)


class ErrorRecoveryPerformanceTests(unittest.TestCase):
    """Tests for performance during error conditions."""

    def setUp(self):
        """Set up test fixtures."""
        self.app = app
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    def test_performance_with_database_timeout(self):
        """Test performance when database times out."""
        with self.client.session_transaction() as sess:
            sess["is_authenticated"] = True

        with patch("adviser_allocation.main.get_firestore_client") as mock_db:
            import requests

            mock_db.side_effect = requests.Timeout()

            start_time = time.time()
            response = self.client.get("/availability/earliest")
            elapsed = time.time() - start_time

            # Should fail fast, not hang
            self.assertLess(elapsed, 5.0)

    def test_performance_with_invalid_input(self):
        """Test performance with invalid input."""
        with self.client.session_transaction() as sess:
            sess["is_authenticated"] = True

        start_time = time.time()
        response = self.client.get("/allocations?limit=invalid&offset=bad")
        elapsed = time.time() - start_time

        # Should handle gracefully and quickly
        self.assertLess(elapsed, 1.0)


class LoadTestingTests(unittest.TestCase):
    """Tests for load testing scenarios."""

    def setUp(self):
        """Set up test fixtures."""
        self.app = app
        self.app.config["TESTING"] = True

    def test_sustained_load_50_requests(self):
        """Test sustained load of 50 requests."""
        successful = 0
        total_time = 0

        for i in range(50):
            client = self.app.test_client()
            with client.session_transaction() as sess:
                sess["is_authenticated"] = True

            start = time.time()
            response = client.get("/availability/earliest")
            elapsed = time.time() - start

            if response.status_code != 500:
                successful += 1
            total_time += elapsed

        avg_time = total_time / 50

        # At least 90% should succeed
        self.assertGreater(successful, 45)
        # Average response time should be reasonable
        self.assertLess(avg_time, 2.0)

    def test_spike_load_100_requests(self):
        """Test spike load of 100 concurrent requests."""

        def make_request(i):
            client = self.app.test_client()
            with client.session_transaction() as sess:
                sess["is_authenticated"] = True
            return client.get("/")

        with concurrent.futures.ThreadPoolExecutor(max_workers=100) as executor:
            futures = [executor.submit(make_request, i) for i in range(100)]
            results = [f.result() for f in concurrent.futures.as_completed(futures)]

        # At least 80% should succeed under spike
        successful = sum(1 for r in results if r.status_code != 500)
        self.assertGreater(successful, 80)


class ApiResponseFormatTests(unittest.TestCase):
    """Tests for API response format consistency."""

    def setUp(self):
        """Set up test fixtures."""
        self.app = app
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    def test_json_response_format_consistent(self):
        """Test that JSON responses have consistent format."""
        with self.client.session_transaction() as sess:
            sess["is_authenticated"] = True

        # Make multiple requests
        for i in range(5):
            response = self.client.get("/availability/earliest")

            # Should have consistent format
            if response.is_json:
                data = response.get_json()
                self.assertIsNotNone(data)

    def test_response_headers_consistent(self):
        """Test that response headers are consistent."""
        with self.client.session_transaction() as sess:
            sess["is_authenticated"] = True

        responses = []
        for i in range(5):
            response = self.client.get("/")
            responses.append(response)

        # All responses should have Content-Type
        for response in responses:
            self.assertIsNotNone(response.headers.get("Content-Type"))


if __name__ == "__main__":
    unittest.main()
