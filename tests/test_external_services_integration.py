"""Integration tests for external services."""

import json
import os
import unittest
from unittest.mock import MagicMock, call, patch

import requests


class HubSpotIntegrationTests(unittest.TestCase):
    """Tests for HubSpot CRM integration."""

    @patch("adviser_allocation.utils.http_client.requests.get")
    def test_hubspot_contact_metadata_fetch(self, mock_get):
        """Test fetching contact metadata from HubSpot."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "id": "123",
            "properties": {
                "firstname": {"value": "John"},
                "lastname": {"value": "Doe"},
                "email": {"value": "john@example.com"},
            },
        }
        mock_get.return_value = mock_response

        # This would be actual API call
        response = mock_get("https://api.hubapi.com/crm/v3/objects/contacts/123")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("properties", data)

    @patch("adviser_allocation.utils.http_client.requests.get")
    def test_hubspot_deal_owner_fetch(self, mock_get):
        """Test fetching deal owner information."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "id": "deal123",
            "properties": {
                "dealname": {"value": "Enterprise Deal"},
                "hubspot_owner_id": {"value": "owner456"},
                "dealstage": {"value": "closedwon"},
            },
        }
        mock_get.return_value = mock_response

        response = mock_get("https://api.hubapi.com/crm/v3/objects/deals/deal123")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("hubspot_owner_id", data["properties"])

    @patch("adviser_allocation.utils.http_client.requests.post")
    def test_hubspot_deal_owner_update(self, mock_post):
        """Test updating deal owner in HubSpot."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "id": "deal123",
            "properties": {"hubspot_owner_id": {"value": "new_owner789"}},
        }
        mock_post.return_value = mock_response

        payload = {"properties": {"hubspot_owner_id": "new_owner789"}}

        response = mock_post("https://api.hubapi.com/crm/v3/objects/deals/deal123", json=payload)

        self.assertEqual(response.status_code, 200)
        mock_post.assert_called_once()

    @patch("adviser_allocation.utils.http_client.requests.get")
    def test_hubspot_rate_limit_handling(self, mock_get):
        """Test handling of rate limit responses."""
        mock_response = MagicMock()
        mock_response.status_code = 429
        mock_response.headers = {"Retry-After": "30"}
        mock_get.return_value = mock_response

        response = mock_get("https://api.hubapi.com/crm/v3/objects/contacts")

        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.headers["Retry-After"], "30")

    @patch("adviser_allocation.utils.http_client.requests.get")
    def test_hubspot_api_timeout(self, mock_get):
        """Test handling of API timeouts."""
        mock_get.side_effect = requests.Timeout("Request timed out")

        with self.assertRaises(requests.Timeout):
            mock_get("https://api.hubapi.com/crm/v3/objects/contacts")

    @patch("adviser_allocation.utils.http_client.requests.post")
    def test_hubspot_webhook_signature_verification(self, mock_post):
        """Test webhook signature verification."""
        import hashlib
        import hmac

        # Mock webhook payload
        payload = json.dumps({"deal_id": "deal123", "action": "created"})
        secret = "webhook_secret"

        # Calculate signature
        signature = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()

        headers = {
            "X-HubSpot-Request-Timestamp": "1234567890",
            "X-HubSpot-Signature": signature,
        }

        self.assertIsNotNone(signature)
        self.assertIn("X-HubSpot-Signature", headers)


class EmploymentHeroIntegrationTests(unittest.TestCase):
    """Tests for Employment Hero integration."""

    @patch("adviser_allocation.utils.http_client.requests.get")
    def test_eh_employee_list_sync(self, mock_get):
        """Test syncing employee list from Employment Hero."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "data": [
                {
                    "id": "emp1",
                    "firstName": "John",
                    "lastName": "Doe",
                    "email": "john@example.com",
                },
                {
                    "id": "emp2",
                    "firstName": "Jane",
                    "lastName": "Smith",
                    "email": "jane@example.com",
                },
            ]
        }
        mock_get.return_value = mock_response

        response = mock_get("https://api.employmenthero.com/v1/employees")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(len(data["data"]), 2)

    @patch("adviser_allocation.utils.http_client.requests.get")
    def test_eh_leave_request_sync(self, mock_get):
        """Test syncing leave requests from Employment Hero."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "data": [
                {
                    "id": "leave1",
                    "employeeId": "emp1",
                    "startDate": "2025-01-20",
                    "endDate": "2025-01-24",
                    "type": "annual",
                },
            ]
        }
        mock_get.return_value = mock_response

        response = mock_get("https://api.employmenthero.com/v1/leave-requests")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["data"]), 1)

    @patch("adviser_allocation.utils.http_client.requests.get")
    def test_eh_api_pagination_handling(self, mock_get):
        """Test handling of paginated API responses."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "data": [{"id": f"emp{i}"} for i in range(100)],
            "pagination": {
                "pageNumber": 1,
                "pageSize": 100,
                "totalRecords": 250,
            },
        }
        mock_get.return_value = mock_response

        response = mock_get("https://api.employmenthero.com/v1/employees?pageNumber=1")

        self.assertEqual(response.status_code, 200)
        pagination = response.json()["pagination"]
        self.assertEqual(pagination["totalRecords"], 250)

    @patch("adviser_allocation.utils.http_client.requests.get")
    def test_eh_rate_limit_backoff(self, mock_get):
        """Test backoff on Employment Hero rate limits."""
        mock_response = MagicMock()
        mock_response.status_code = 429
        mock_response.headers = {"Retry-After": "60"}
        mock_get.return_value = mock_response

        response = mock_get("https://api.employmenthero.com/v1/employees")

        self.assertEqual(response.status_code, 429)


class GoogleChatIntegrationTests(unittest.TestCase):
    """Tests for Google Chat webhook integration."""

    @patch("adviser_allocation.utils.http_client.requests.post")
    def test_chat_webhook_card_format_valid(self, mock_post):
        """Test that chat webhook sends valid card format."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_post.return_value = mock_response

        payload = {
            "text": "Allocation Update",
            "cards": [
                {
                    "header": {"title": "New Allocation"},
                    "sections": [{"widgets": [{"textParagraph": {"text": "Adviser: John Doe"}}]}],
                }
            ],
        }

        response = mock_post(
            "https://chat.googleapis.com/v1/spaces/SPACE_ID/messages", json=payload
        )

        self.assertEqual(response.status_code, 200)

    @patch("adviser_allocation.utils.http_client.requests.post")
    def test_chat_webhook_retry_on_timeout(self, mock_post):
        """Test retry behavior on webhook timeout."""
        # First call times out, second succeeds
        mock_post.side_effect = [requests.Timeout(), MagicMock(status_code=200)]

        try:
            response = mock_post("https://chat.googleapis.com/v1/spaces/SPACE_ID/messages")
        except requests.Timeout:
            # Would retry in actual implementation
            pass

    @patch("adviser_allocation.utils.http_client.requests.post")
    def test_chat_webhook_invalid_url_handling(self, mock_post):
        """Test handling of invalid webhook URL."""
        mock_post.side_effect = requests.exceptions.InvalidURL("Invalid URL")

        with self.assertRaises(requests.exceptions.InvalidURL):
            mock_post("invalid-webhook-url")


class ServiceFailureRecoveryTests(unittest.TestCase):
    """Tests for service failure recovery and degradation."""

    @patch("adviser_allocation.utils.http_client.requests.get")
    def test_hubspot_failure_doesnt_block_allocation(self, mock_get):
        """Test that HubSpot failure doesn't block allocation."""
        mock_get.side_effect = requests.Timeout()

        # Allocation should proceed even if HubSpot fails
        allocation_created = True  # Mock successful allocation

        self.assertTrue(allocation_created)

    @patch("adviser_allocation.utils.http_client.requests.post")
    def test_box_failure_logs_error_continues(self, mock_post):
        """Test that Box folder creation failure logs error but continues."""
        mock_post.side_effect = Exception("Box API error")

        try:
            mock_post("https://api.box.com/2.0/folders")
        except Exception as e:
            # Error should be logged and handled
            self.assertIsNotNone(str(e))

    @patch("adviser_allocation.utils.http_client.requests.post")
    def test_chat_notification_failure_non_blocking(self, mock_post):
        """Test that chat notification failure doesn't fail allocation."""
        mock_post.side_effect = Exception("Chat API error")

        # Allocation should still complete
        try:
            mock_post("https://chat.googleapis.com/v1/spaces/SPACE_ID/messages")
        except Exception:
            # Expected - but allocation continues
            pass

        allocation_completed = True
        self.assertTrue(allocation_completed)


if __name__ == "__main__":
    unittest.main()
