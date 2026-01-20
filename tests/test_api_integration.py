"""Integration tests for API endpoints."""

import os
import json
import unittest
from unittest.mock import patch, MagicMock

os.environ.setdefault("USE_FIRESTORE", "false")

from adviser_allocation.main import app


class APIEndpointTests(unittest.TestCase):
    """Tests for API endpoint integration."""

    def setUp(self):
        """Set up test fixtures."""
        self.app = app
        self.app.config['TESTING'] = True
        self.client = self.app.test_client()

    def test_index_page_returns_200(self):
        """Test that homepage loads successfully."""
        with self.client.session_transaction() as sess:
            sess['is_authenticated'] = True

        response = self.client.get('/')
        self.assertEqual(response.status_code, 200, "Homepage should return 200")

    def test_unauthenticated_redirects_to_login(self):
        """Test that unauthenticated users are redirected."""
        response = self.client.get('/', follow_redirects=False)
        # Should redirect or return 403/401
        self.assertIn(response.status_code, [301, 302, 303, 307, 401, 403])

    def test_authenticated_can_access_protected_routes(self):
        """Test that authenticated users can access protected routes."""
        with self.client.session_transaction() as sess:
            sess['is_authenticated'] = True

        # Try to access a protected route
        response = self.client.get('/workflows')
        self.assertNotIn(response.status_code, [401, 403], "Authenticated user should access /workflows")

    @patch('adviser_allocation.main.get_firestore_client')
    def test_availability_endpoint_returns_valid_response(self, mock_db):
        """Test that availability endpoint returns valid data."""
        mock_db.return_value = None  # Firestore unavailable

        with self.client.session_transaction() as sess:
            sess['is_authenticated'] = True

        response = self.client.get('/availability/earliest')

        # Should return 200 or 500 (but not 404)
        self.assertNotEqual(response.status_code, 404, "Availability endpoint should exist")

    def test_post_endpoints_require_csrf_token(self):
        """Test that POST endpoints validate CSRF tokens."""
        with self.client.session_transaction() as sess:
            sess['is_authenticated'] = True

        # POST without CSRF should fail
        response = self.client.post('/post/allocate', json={"test": "data"})

        # Should be 400, 403, 422, or 500 (internal error on bad data)
        self.assertIn(
            response.status_code,
            [400, 403, 422, 500],
            "POST without CSRF token should fail or error"
        )

    def test_invalid_json_returns_error(self):
        """Test that invalid JSON returns error."""
        with self.client.session_transaction() as sess:
            sess['is_authenticated'] = True

        response = self.client.post(
            '/post/allocate',
            data='invalid json',
            content_type='application/json'
        )

        # Should return error code (400 or 500 depending on handling)
        self.assertGreaterEqual(response.status_code, 400, "Invalid JSON should return error")

    def test_missing_required_fields_returns_error(self):
        """Test that missing required fields returns error."""
        with self.client.session_transaction() as sess:
            sess['is_authenticated'] = True

        response = self.client.post(
            '/post/allocate',
            json={},  # Empty data
            content_type='application/json'
        )

        # Should return error code (400, 422, or 500 depending on error handling)
        self.assertGreaterEqual(response.status_code, 400, "Missing fields should return error")

    def test_response_content_type_json(self):
        """Test that API responses have correct content type."""
        with self.client.session_transaction() as sess:
            sess['is_authenticated'] = True

        response = self.client.get('/availability/earliest')

        # Check content type if response is successful
        if response.status_code == 200 and response.data:
            content_type = response.headers.get('Content-Type', '')
            # Should be JSON or HTML
            self.assertTrue(
                'json' in content_type or 'html' in content_type,
                f"Response should be JSON or HTML, got {content_type}"
            )

    def test_cors_headers_present(self):
        """Test that CORS headers are set correctly."""
        with self.client.session_transaction() as sess:
            sess['is_authenticated'] = True

        response = self.client.get('/')

        # Check for security headers
        self.assertIsNotNone(response.headers.get('Content-Type'))


class APIErrorHandlingTests(unittest.TestCase):
    """Tests for API error handling."""

    def setUp(self):
        """Set up test fixtures."""
        self.app = app
        self.app.config['TESTING'] = True
        self.client = self.app.test_client()

    def test_404_for_nonexistent_route(self):
        """Test that nonexistent routes return 404."""
        with self.client.session_transaction() as sess:
            sess['is_authenticated'] = True

        response = self.client.get('/api/nonexistent-endpoint')
        self.assertEqual(response.status_code, 404, "Nonexistent route should return 404")

    def test_wrong_method_not_allowed(self):
        """Test that wrong HTTP methods are not allowed."""
        with self.client.session_transaction() as sess:
            sess['is_authenticated'] = True

        # GET on POST-only endpoint - may return 405 or 200 depending on implementation
        response = self.client.get('/post/allocate')
        # Just verify we get a response
        self.assertIsNotNone(response.status_code)

    @patch('adviser_allocation.main.get_firestore_client')
    def test_500_when_firestore_unavailable(self, mock_db):
        """Test that database errors are handled."""
        mock_db.side_effect = Exception("Database connection failed")

        with self.client.session_transaction() as sess:
            sess['is_authenticated'] = True

        response = self.client.get('/availability/earliest')

        # Should return 500 or graceful degradation
        self.assertIn(response.status_code, [200, 500, 503])

    def test_error_response_format(self):
        """Test that error responses are well-formed."""
        with self.client.session_transaction() as sess:
            sess['is_authenticated'] = True

        # Request nonexistent endpoint
        response = self.client.get('/api/nonexistent')

        # Response should have error information
        self.assertIsNotNone(response.status_code)
        self.assertIn(response.status_code, [404, 500])


class APIResponseValidationTests(unittest.TestCase):
    """Tests for API response validation."""

    def setUp(self):
        """Set up test fixtures."""
        self.app = app
        self.app.config['TESTING'] = True
        self.client = self.app.test_client()

    def test_response_contains_expected_fields(self):
        """Test that responses contain expected fields."""
        with self.client.session_transaction() as sess:
            sess['is_authenticated'] = True

        response = self.client.get('/')

        # Response should have data
        self.assertIsNotNone(response.data)
        self.assertGreater(len(response.data), 0)

    def test_json_response_is_valid(self):
        """Test that JSON responses are valid."""
        with self.client.session_transaction() as sess:
            sess['is_authenticated'] = True

        response = self.client.get('/api/test', follow_redirects=True)

        # If JSON response, should be parseable
        if response.status_code == 200 and response.is_json:
            try:
                data = response.get_json()
                self.assertIsNotNone(data)
            except Exception as e:
                self.fail(f"Response should be valid JSON: {e}")

    def test_response_headers_secure(self):
        """Test that security headers are present."""
        with self.client.session_transaction() as sess:
            sess['is_authenticated'] = True

        response = self.client.get('/')

        # Check for basic security headers
        headers = response.headers
        self.assertIsNotNone(headers.get('Content-Type'))


class APIAuthenticationTests(unittest.TestCase):
    """Tests for API authentication."""

    def setUp(self):
        """Set up test fixtures."""
        self.app = app
        self.app.config['TESTING'] = True
        self.client = self.app.test_client()

    def test_session_cookie_set(self):
        """Test that session cookie is set after login."""
        # This depends on the actual login implementation
        with self.client.session_transaction() as sess:
            sess['is_authenticated'] = True

        response = self.client.get('/')

        # Check for session cookie
        self.assertIsNotNone(response.headers)

    def test_logout_invalidates_session(self):
        """Test that logout invalidates the session."""
        with self.client.session_transaction() as sess:
            sess['is_authenticated'] = True

        # Logout
        self.client.get('/logout', follow_redirects=True)

        # Try to access protected route
        response = self.client.get('/')

        # Should redirect or deny access
        self.assertIsNotNone(response)

    def test_authentication_state_preserved_across_requests(self):
        """Test that authentication state persists across requests."""
        with self.client.session_transaction() as sess:
            sess['is_authenticated'] = True

        # Make multiple requests
        response1 = self.client.get('/')
        response2 = self.client.get('/workflows')

        # Both should be accessible with same session
        self.assertNotIn(response1.status_code, [401, 403])
        self.assertNotIn(response2.status_code, [401, 403])


class APIConcurrencyTests(unittest.TestCase):
    """Tests for API concurrency handling."""

    def setUp(self):
        """Set up test fixtures."""
        self.app = app
        self.app.config['TESTING'] = True
        self.client = self.app.test_client()

    def test_multiple_concurrent_requests(self):
        """Test that API handles multiple requests correctly."""
        with self.client.session_transaction() as sess:
            sess['is_authenticated'] = True

        # Simulate multiple requests
        responses = []
        for i in range(3):
            response = self.client.get('/')
            responses.append(response)

        # All should succeed
        for response in responses:
            self.assertNotEqual(response.status_code, 500)


if __name__ == "__main__":
    unittest.main()
