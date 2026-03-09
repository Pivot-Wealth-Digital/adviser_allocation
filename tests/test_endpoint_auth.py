"""Tests for endpoint authentication (OIDC, HubSpot signature, and API key)."""

import hashlib
import os
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("USE_FIRESTORE", "false")

from adviser_allocation.main import app

TEST_HUBSPOT_SECRET = "test-hubspot-client-secret"


def hubspot_v2_signature(client_secret, method, url, body=""):
    """Compute HubSpot v2 signature: SHA-256(client_secret + method + url + body)."""
    source = client_secret + method + url + body
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


class OIDCSyncEndpointTests(unittest.TestCase):
    """Tests for OIDC-protected sync endpoints."""

    def setUp(self):
        self.app = app
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    def test_sync_employees_rejects_no_token(self):
        response = self.client.get("/sync/employees")
        self.assertEqual(response.status_code, 401)

    def test_sync_employees_rejects_missing_bearer(self):
        response = self.client.get(
            "/sync/employees",
            headers={"Authorization": "Basic abc123"},
        )
        self.assertEqual(response.status_code, 401)

    @patch("adviser_allocation.utils.auth.google_id_token")
    @patch("adviser_allocation.utils.auth.google_requests")
    @patch("adviser_allocation.utils.auth.get_secret")
    def test_sync_employees_rejects_invalid_token(self, mock_secret, mock_greq, mock_id_token):
        mock_secret.return_value = None
        mock_id_token.verify_oauth2_token.side_effect = ValueError("Invalid token")
        mock_greq.Request.return_value = MagicMock()

        response = self.client.get(
            "/sync/employees",
            headers={"Authorization": "Bearer bad-token"},
        )
        self.assertEqual(response.status_code, 401)

    @patch("adviser_allocation.utils.auth.google_id_token")
    @patch("adviser_allocation.utils.auth.google_requests")
    @patch("adviser_allocation.utils.auth.get_secret")
    @patch("adviser_allocation.main.get_employees")
    def test_sync_employees_accepts_valid_token(
        self, mock_get_emp, mock_secret, mock_greq, mock_id_token
    ):
        mock_secret.return_value = None
        mock_id_token.verify_oauth2_token.return_value = {
            "email": "scheduler@project.iam.gserviceaccount.com"
        }
        mock_greq.Request.return_value = MagicMock()
        mock_get_emp.return_value = ([], 200, {})

        response = self.client.get(
            "/sync/employees",
            headers={"Authorization": "Bearer valid-oidc-token"},
        )
        self.assertEqual(response.status_code, 200)

    @patch("adviser_allocation.utils.auth.google_id_token")
    @patch("adviser_allocation.utils.auth.google_requests")
    @patch("adviser_allocation.utils.auth.get_secret")
    def test_sync_employees_rejects_wrong_service_account(
        self, mock_secret, mock_greq, mock_id_token
    ):
        mock_secret.return_value = "expected-sa@project.iam.gserviceaccount.com"
        mock_id_token.verify_oauth2_token.return_value = {
            "email": "wrong@project.iam.gserviceaccount.com"
        }
        mock_greq.Request.return_value = MagicMock()

        response = self.client.get(
            "/sync/employees",
            headers={"Authorization": "Bearer valid-but-wrong-sa"},
        )
        self.assertEqual(response.status_code, 401)

    def test_sync_leave_requests_rejects_no_token(self):
        response = self.client.get("/sync/leave_requests")
        self.assertEqual(response.status_code, 401)

    @patch("adviser_allocation.utils.auth.google_id_token")
    @patch("adviser_allocation.utils.auth.google_requests")
    @patch("adviser_allocation.utils.auth.get_secret")
    @patch("adviser_allocation.main.get_leave_requests")
    def test_sync_leave_requests_accepts_valid_token(
        self, mock_get_leave, mock_secret, mock_greq, mock_id_token
    ):
        mock_secret.return_value = None
        mock_id_token.verify_oauth2_token.return_value = {
            "email": "scheduler@project.iam.gserviceaccount.com"
        }
        mock_greq.Request.return_value = MagicMock()
        mock_get_leave.return_value = ([], 200, {})

        response = self.client.get(
            "/sync/leave_requests",
            headers={"Authorization": "Bearer valid-oidc-token"},
        )
        self.assertEqual(response.status_code, 200)


class HubSpotSignatureTests(unittest.TestCase):
    """Tests for HubSpot signature-protected /post/allocate endpoint."""

    def setUp(self):
        self.app = app
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    @patch("adviser_allocation.utils.auth.get_secret")
    def test_allocate_rejects_no_signature(self, mock_secret):
        mock_secret.return_value = TEST_HUBSPOT_SECRET
        response = self.client.post("/post/allocate", json={"test": True})
        self.assertEqual(response.status_code, 401)

    @patch("adviser_allocation.utils.auth.get_secret")
    def test_allocate_rejects_wrong_signature(self, mock_secret):
        mock_secret.return_value = TEST_HUBSPOT_SECRET
        response = self.client.post(
            "/post/allocate",
            json={"test": True},
            headers={"X-HubSpot-Signature": "wrong-signature"},
        )
        self.assertEqual(response.status_code, 401)

    @patch("adviser_allocation.utils.auth.get_secret")
    def test_allocate_returns_500_when_secret_not_configured(self, mock_secret):
        mock_secret.return_value = None
        response = self.client.post(
            "/post/allocate",
            json={"test": True},
            headers={"X-HubSpot-Signature": "any"},
        )
        self.assertEqual(response.status_code, 500)

    @patch("adviser_allocation.utils.auth.get_secret")
    def test_allocate_get_with_valid_signature(self, mock_secret):
        mock_secret.return_value = TEST_HUBSPOT_SECRET
        url = "http://localhost/post/allocate"
        sig = hubspot_v2_signature(TEST_HUBSPOT_SECRET, "GET", url)
        response = self.client.get(
            "/post/allocate",
            headers={"X-HubSpot-Signature": sig},
        )
        self.assertEqual(response.status_code, 200)


class APIKeyWebhookTests(unittest.TestCase):
    """Tests for API key-protected /webhook/allocation endpoint."""

    def setUp(self):
        self.app = app
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    @patch("adviser_allocation.utils.auth.get_secret")
    def test_webhook_allocation_rejects_no_api_key(self, mock_secret):
        mock_secret.return_value = "correct-key"
        response = self.client.post(
            "/webhook/allocation",
            json={"test": True},
        )
        self.assertEqual(response.status_code, 401)


if __name__ == "__main__":
    unittest.main()
