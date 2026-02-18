"""Tests for OAuth service module."""

import unittest
from unittest.mock import patch, MagicMock
from datetime import datetime, timedelta
import time

from flask import Flask

from adviser_allocation.services.oauth_service import (
    init_oauth_service,
    token_key,
    save_tokens,
    load_tokens,
    exchange_code_for_tokens,
    refresh_access_token,
    get_access_token,
    build_authorization_url,
)


class OAuthServiceTests(unittest.TestCase):
    """Test suite for OAuth service."""

    def setUp(self):
        """Set up test fixtures."""
        self.app = Flask(__name__)
        self.app.config["SECRET_KEY"] = "test_secret"
        self.app_context = self.app.app_context()
        self.app_context.push()

        self.oauth_config = {
            "EH_AUTHORIZE_URL": "https://oauth.example.com/authorize",
            "EH_TOKEN_URL": "https://oauth.example.com/token",
            "EH_CLIENT_ID": "test_client_id",
            "EH_CLIENT_SECRET": "test_client_secret",
            "REDIRECT_URI": "https://app.example.com/callback",
        }

    def tearDown(self):
        """Clean up after tests."""
        self.app_context.pop()

    def test_init_oauth_service(self):
        """Test OAuth service initialization."""
        init_oauth_service(db=None, config=self.oauth_config)
        # Service initialized without error
        self.assertTrue(True)

    def test_token_key(self):
        """Test that token key is consistent."""
        key1 = token_key()
        key2 = token_key()
        self.assertEqual(key1, key2)
        self.assertIsInstance(key1, str)

    @patch("services.oauth_service.USE_FIRESTORE", False)
    def test_save_and_load_tokens(self):
        """Test saving and loading tokens with Flask session."""
        init_oauth_service(db=None, config=self.oauth_config)

        test_tokens = {
            "access_token": "test_access_token",
            "refresh_token": "test_refresh_token",
            "expires_in": 3600,
        }

        # Use Flask test client to provide request context
        with self.app.test_request_context():
            save_tokens(test_tokens)
            loaded = load_tokens()

            self.assertIsNotNone(loaded)
            self.assertEqual(loaded["access_token"], "test_access_token")
            self.assertEqual(loaded["refresh_token"], "test_refresh_token")
            self.assertIn("_expires_at", loaded)

    @patch("services.oauth_service.post_with_retries")
    def test_exchange_code_for_tokens(self, mock_post):
        """Test OAuth code exchange."""
        init_oauth_service(db=None, config=self.oauth_config)

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "access_token": "new_access_token",
            "refresh_token": "new_refresh_token",
            "expires_in": 3600,
        }
        mock_post.return_value = mock_response

        result = exchange_code_for_tokens("auth_code_123")

        self.assertEqual(result["access_token"], "new_access_token")
        self.assertEqual(result["refresh_token"], "new_refresh_token")

    @patch("services.oauth_service.post_with_retries")
    def test_exchange_code_failure(self, mock_post):
        """Test OAuth code exchange failure."""
        init_oauth_service(db=None, config=self.oauth_config)

        mock_response = MagicMock()
        mock_response.status_code = 400
        mock_response.text = "Invalid code"
        mock_post.return_value = mock_response

        with self.assertRaises(RuntimeError):
            exchange_code_for_tokens("invalid_code")

    @patch("services.oauth_service.post_with_retries")
    def test_refresh_access_token(self, mock_post):
        """Test token refresh."""
        init_oauth_service(db=None, config=self.oauth_config)

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "access_token": "refreshed_access_token",
            "expires_in": 3600,
        }
        mock_post.return_value = mock_response

        result = refresh_access_token("old_refresh_token")

        self.assertEqual(result["access_token"], "refreshed_access_token")

    @patch("services.oauth_service.USE_FIRESTORE", False)
    def test_get_access_token_uses_cached_when_valid(self):
        """Test that get_access_token returns cached token if valid."""
        init_oauth_service(db=None, config=self.oauth_config)

        with self.app.test_request_context():
            # Save a valid token that won't expire for 1 hour
            test_tokens = {
                "access_token": "cached_token",
                "refresh_token": "refresh_token",
                "expires_in": 3600,
            }
            save_tokens(test_tokens)

            # Should return cached token
            result = get_access_token()
            self.assertEqual(result, "cached_token")

    @patch("services.oauth_service.USE_FIRESTORE", False)
    @patch("services.oauth_service.post_with_retries")
    def test_get_access_token_refreshes_when_expired(self, mock_post):
        """Test that get_access_token refreshes expired token."""
        init_oauth_service(db=None, config=self.oauth_config)

        with self.app.test_request_context():
            # Save an expired token
            test_tokens = {
                "access_token": "expired_token",
                "refresh_token": "refresh_token",
                "_expires_at": time.time() - 100,  # Expired 100 seconds ago
            }
            save_tokens(test_tokens)

            # Mock refresh response
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {
                "access_token": "new_token",
                "refresh_token": "new_refresh_token",
                "expires_in": 3600,
            }
            mock_post.return_value = mock_response

            result = get_access_token()
            self.assertEqual(result, "new_token")
            mock_post.assert_called()

    def test_build_authorization_url(self):
        """Test authorization URL building."""
        init_oauth_service(db=None, config=self.oauth_config)

        url = build_authorization_url("test_state_123")

        self.assertIn("https://oauth.example.com/authorize", url)
        self.assertIn("client_id=test_client_id", url)
        self.assertIn("state=test_state_123", url)
        self.assertIn("redirect_uri=", url)

    def test_build_authorization_url_with_partial_config(self):
        """Test that authorization URL fails with incomplete configuration."""
        # Initialize with incomplete config
        init_oauth_service(
            db=None,
            config={
                "EH_AUTHORIZE_URL": "https://oauth.example.com/authorize",
                # Missing other required fields
            },
        )

        with self.assertRaises(RuntimeError):
            build_authorization_url("state")


if __name__ == "__main__":
    unittest.main()
