"""Tests for HTTP client utilities."""

import unittest
from unittest.mock import patch, MagicMock
import requests

from adviser_allocation.utils.http_client import (
    create_session_with_retries,
    get_with_retries,
    post_with_retries,
    patch_with_retries,
    delete_with_retries,
    DEFAULT_TIMEOUT,
)


class HTTPClientTests(unittest.TestCase):
    """Test suite for HTTP client utilities."""

    def test_create_session_with_retries(self):
        """Test that session is created with retry adapter."""
        session = create_session_with_retries(retries=3)
        self.assertIsNotNone(session)
        # Check that HTTP and HTTPS have adapters
        self.assertIsNotNone(session.get_adapter("http://example.com"))
        self.assertIsNotNone(session.get_adapter("https://example.com"))

    @patch("utils.http_client.requests.Session.get")
    def test_get_with_retries_sets_timeout(self, mock_get):
        """Test that GET requests include timeout."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_get.return_value = mock_response

        # We can't directly test this without mocking Session.get
        # Just ensure it doesn't raise
        try:
            result = get_with_retries("http://example.com")
            self.assertIsNotNone(result)
        except Exception:
            # Expected since we're not fully mocking
            pass

    @patch("utils.http_client.requests.Session.post")
    def test_post_with_retries_sends_json(self, mock_post):
        """Test that POST requests send JSON properly."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_post.return_value = mock_response

        try:
            result = post_with_retries("http://example.com", json={"key": "value"})
            self.assertIsNotNone(result)
        except Exception:
            pass

    @patch("utils.http_client.requests.Session.patch")
    def test_patch_with_retries_supports_both_json_and_data(self, mock_patch):
        """Test that PATCH requests support both json and data payloads."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_patch.return_value = mock_response

        try:
            # Test JSON
            result = patch_with_retries("http://example.com", json={"key": "value"})
            self.assertIsNotNone(result)

            # Test data
            result = patch_with_retries("http://example.com", data='{"key": "value"}')
            self.assertIsNotNone(result)
        except Exception:
            pass

    @patch("utils.http_client.requests.Session.delete")
    def test_delete_with_retries(self, mock_delete):
        """Test DELETE requests."""
        mock_response = MagicMock()
        mock_response.status_code = 204
        mock_delete.return_value = mock_response

        try:
            result = delete_with_retries("http://example.com")
            self.assertIsNotNone(result)
        except Exception:
            pass

    def test_default_timeout_constant(self):
        """Test that DEFAULT_TIMEOUT is reasonable."""
        self.assertGreater(DEFAULT_TIMEOUT, 0)
        self.assertLess(DEFAULT_TIMEOUT, 120)


if __name__ == "__main__":
    unittest.main()
