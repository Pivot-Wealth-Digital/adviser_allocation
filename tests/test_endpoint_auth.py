"""Tests for endpoint authentication (OIDC and API key)."""

import os
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("USE_FIRESTORE", "false")

from adviser_allocation.main import app


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


class APIKeyWebhookTests(unittest.TestCase):
    """Tests for API key-protected webhook endpoints."""

    def setUp(self):
        self.app = app
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    @patch("adviser_allocation.utils.auth.get_secret")
    def test_allocate_rejects_no_api_key(self, mock_secret):
        mock_secret.return_value = "correct-key"
        response = self.client.post(
            "/post/allocate",
            json={"test": True},
        )
        self.assertEqual(response.status_code, 401)

    @patch("adviser_allocation.utils.auth.get_secret")
    def test_allocate_rejects_wrong_api_key(self, mock_secret):
        mock_secret.return_value = "correct-key"
        response = self.client.post(
            "/post/allocate?api_key=wrong-key",
            json={"test": True},
        )
        self.assertEqual(response.status_code, 401)

    @patch("adviser_allocation.utils.auth.get_secret")
    def test_allocate_returns_500_when_key_not_configured(self, mock_secret):
        mock_secret.return_value = None
        response = self.client.post(
            "/post/allocate?api_key=any-key",
            json={"test": True},
        )
        self.assertEqual(response.status_code, 500)

    @patch("adviser_allocation.utils.auth.get_secret")
    def test_allocate_get_with_valid_key(self, mock_secret):
        mock_secret.return_value = "correct-key"
        response = self.client.get("/post/allocate?api_key=correct-key")
        self.assertEqual(response.status_code, 200)

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
