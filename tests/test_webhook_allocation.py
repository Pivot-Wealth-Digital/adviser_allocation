"""Tests for /post/allocate webhook — full happy path and edge cases.

Verifies the Cloud Run webhook endpoint processes HubSpot allocation
requests correctly: signature validation → get_adviser → deal update →
record storage → chat alert.
"""

import hashlib
import json
import os
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("USE_FIRESTORE", "false")

from adviser_allocation.main import app

TEST_HUBSPOT_SECRET = "test-hubspot-client-secret"  # pragma: allowlist secret


def _hubspot_v2_signature(client_secret, method, url, body=""):
    """Compute HubSpot v2 signature: SHA-256(client_secret + method + url + body)."""
    source = client_secret + method + url + body
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def _make_allocation_payload(
    service_package="series a",
    household_type="seed",
    deal_id="deal-123",
    client_email="client@example.com",
    agreement_start_date="",
    renewal_service_package=None,
    renewal_household_type=None,
    omit_service_package=False,
    omit_household_type=False,
):
    """Build a realistic HubSpot workflow webhook payload."""
    fields = {
        "hs_deal_record_id": deal_id,
        "client_email": client_email,
        "agreement_start_date": agreement_start_date,
    }
    if not omit_service_package:
        fields["service_package"] = service_package
    if not omit_household_type:
        fields["household_type"] = household_type
    if renewal_service_package is not None:
        fields["renewal_service_package"] = renewal_service_package
    if renewal_household_type is not None:
        fields["renewal_household_type"] = renewal_household_type
    return {
        "object": {"objectType": "DEAL", "objectId": deal_id},
        "fields": fields,
    }


def _make_adviser_user(
    email="adviser@pivotwealth.com.au",
    hubspot_owner_id="owner-456",
    client_types="series a;seed",
    household_type="seed;series a",
):
    """Build a HubSpot user object as returned by get_adviser."""
    return {
        "id": "user-789",
        "properties": {
            "hs_email": email,
            "hubspot_owner_id": hubspot_owner_id,
            "client_types": client_types,
            "household_type": household_type,
            "taking_on_clients": "True",
            "client_limit_monthly": 6,
        },
    }


def _make_candidates_summary(email="adviser@pivotwealth.com.au"):
    """Build candidates_summary list as returned by get_adviser."""
    return [
        {
            "email": email,
            "name": "Adviser",
            "service_packages": "series a;seed",
            "household_type": "seed;series a",
            "earliest_open_week": 739600,
            "earliest_open_week_label": "W12 2026",
        }
    ]


def _post_with_sig(client, payload, query_string=""):
    """POST to /post/allocate with a valid HubSpot v2 signature."""
    body = json.dumps(payload)
    path = "/post/allocate"
    if query_string:
        path = f"{path}?{query_string}"
    url = f"http://localhost{path}"
    sig = _hubspot_v2_signature(TEST_HUBSPOT_SECRET, "POST", url, body)
    return client.post(
        path,
        data=body,
        content_type="application/json",
        headers={"X-HubSpot-Signature": sig},
    )


class TestWebhookAllocationHappyPath(unittest.TestCase):
    """Full happy path: valid signature → adviser found → deal updated → record stored."""

    def setUp(self):
        self.app = app
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

        # Patch all external dependencies for happy path
        self.p_secret = patch(
            "adviser_allocation.utils.auth.get_secret",
            return_value=TEST_HUBSPOT_SECRET,
        )
        self.p_get_adviser = patch("adviser_allocation.api.webhooks.get_adviser")
        self.p_patch = patch("adviser_allocation.api.webhooks.patch_with_retries")
        self.p_store = patch("adviser_allocation.api.webhooks.store_allocation_record")
        self.p_chat = patch("adviser_allocation.api.webhooks.send_chat_alert")
        self.p_headers = patch(
            "adviser_allocation.api.webhooks._hubspot_headers",
            return_value={
                "Authorization": "Bearer test-token",
                "Content-Type": "application/json",
            },
        )

        self.mock_secret = self.p_secret.start()
        self.mock_get_adviser = self.p_get_adviser.start()
        self.mock_patch_hs = self.p_patch.start()
        self.mock_store = self.p_store.start()
        self.mock_chat = self.p_chat.start()
        self.mock_headers = self.p_headers.start()

        # Default: one adviser found, HubSpot update succeeds
        self.mock_get_adviser.return_value = (
            _make_adviser_user(),
            _make_candidates_summary(),
        )
        mock_resp = MagicMock(status_code=200)
        mock_resp.raise_for_status = MagicMock()
        self.mock_patch_hs.return_value = mock_resp

    def tearDown(self):
        self.p_secret.stop()
        self.p_get_adviser.stop()
        self.p_patch.stop()
        self.p_store.stop()
        self.p_chat.stop()
        self.p_headers.stop()

    def test_successful_allocation_returns_200(self):
        response = _post_with_sig(self.client, _make_allocation_payload())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["message"], "Webhook received successfully")

    def test_calls_get_adviser_with_correct_args(self):
        _post_with_sig(
            self.client,
            _make_allocation_payload(service_package="series a", household_type="seed"),
        )
        self.mock_get_adviser.assert_called_once_with("series a", "", "seed")

    def test_passes_agreement_start_date_to_get_adviser(self):
        _post_with_sig(
            self.client,
            _make_allocation_payload(agreement_start_date="1741234567000"),
        )
        self.mock_get_adviser.assert_called_once_with("series a", "1741234567000", "seed")

    def test_updates_hubspot_deal_with_owner_id(self):
        self.mock_get_adviser.return_value = (
            _make_adviser_user(hubspot_owner_id="owner-999"),
            _make_candidates_summary(),
        )
        _post_with_sig(self.client, _make_allocation_payload(deal_id="deal-ABC"))

        self.mock_patch_hs.assert_called_once()
        patch_url = self.mock_patch_hs.call_args[0][0]
        self.assertIn("deal-ABC", patch_url)
        self.assertIn("hubapi.com/crm/v3/objects/deals/", patch_url)

    def test_stores_completed_allocation_record(self):
        self.mock_get_adviser.return_value = (
            _make_adviser_user(email="jane@pivotwealth.com.au", hubspot_owner_id="owner-111"),
            _make_candidates_summary(email="jane@pivotwealth.com.au"),
        )
        _post_with_sig(self.client, _make_allocation_payload(deal_id="deal-STORE"))

        self.mock_store.assert_called_once()
        record = self.mock_store.call_args[0][1]
        self.assertEqual(record["deal_id"], "deal-STORE")
        self.assertEqual(record["adviser_email"], "jane@pivotwealth.com.au")
        self.assertEqual(record["status"], "completed")
        self.assertEqual(record["allocation_result"], "completed")
        self.assertEqual(self.mock_store.call_args[1]["source"], "hubspot_webhook")

    def test_chat_alert_disabled(self):
        """Chat alerts are currently disabled (send_chat_alert_flag = False)."""
        _post_with_sig(self.client, _make_allocation_payload())
        self.mock_chat.assert_not_called()

    def test_chat_alert_disabled_even_with_flag(self):
        """Even with send_chat_alert=1, alerts stay off while hardcoded False."""
        _post_with_sig(
            self.client,
            _make_allocation_payload(),
            query_string="send_chat_alert=1",
        )
        self.mock_chat.assert_not_called()


class TestWebhookAllocationNoAdviser(unittest.TestCase):
    """When get_adviser returns no match."""

    def setUp(self):
        self.app = app
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

        self.p_secret = patch(
            "adviser_allocation.utils.auth.get_secret",
            return_value=TEST_HUBSPOT_SECRET,
        )
        self.p_get_adviser = patch("adviser_allocation.api.webhooks.get_adviser")
        self.p_store = patch("adviser_allocation.api.webhooks.store_allocation_record")

        self.mock_secret = self.p_secret.start()
        self.mock_get_adviser = self.p_get_adviser.start()
        self.mock_store = self.p_store.start()

        self.mock_get_adviser.return_value = (None, [])

    def tearDown(self):
        self.p_secret.stop()
        self.p_get_adviser.stop()
        self.p_store.stop()

    def test_no_adviser_returns_200_with_message(self):
        response = _post_with_sig(self.client, _make_allocation_payload())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["message"], "No eligible adviser found")

    def test_no_adviser_stores_failed_record(self):
        _post_with_sig(self.client, _make_allocation_payload(deal_id="deal-FAIL"))

        self.mock_store.assert_called_once()
        record = self.mock_store.call_args[0][1]
        self.assertEqual(record["deal_id"], "deal-FAIL")
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["error_message"], "No eligible adviser found")


class TestWebhookAllocationAuth(unittest.TestCase):
    """Signature validation edge cases."""

    def setUp(self):
        self.app = app
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    @patch("adviser_allocation.utils.auth.get_secret")
    def test_missing_signature_returns_401(self, mock_secret):
        mock_secret.return_value = TEST_HUBSPOT_SECRET
        response = self.client.post("/post/allocate", json=_make_allocation_payload())
        self.assertEqual(response.status_code, 401)

    @patch("adviser_allocation.utils.auth.get_secret")
    def test_invalid_signature_returns_401(self, mock_secret):
        mock_secret.return_value = TEST_HUBSPOT_SECRET
        response = self.client.post(
            "/post/allocate",
            json=_make_allocation_payload(),
            headers={"X-HubSpot-Signature": "deadbeef" * 8},
        )
        self.assertEqual(response.status_code, 401)

    @patch("adviser_allocation.utils.auth.get_secret")
    def test_no_secret_configured_returns_500(self, mock_secret):
        mock_secret.return_value = None
        response = self.client.post(
            "/post/allocate",
            json=_make_allocation_payload(),
            headers={"X-HubSpot-Signature": "any"},
        )
        self.assertEqual(response.status_code, 500)

    @patch("adviser_allocation.utils.auth.get_secret")
    def test_get_request_with_valid_sig_returns_200(self, mock_secret):
        mock_secret.return_value = TEST_HUBSPOT_SECRET
        url = "http://localhost/post/allocate"
        sig = _hubspot_v2_signature(TEST_HUBSPOT_SECRET, "GET", url)
        response = self.client.get(
            "/post/allocate",
            headers={"X-HubSpot-Signature": sig},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("please use POST", response.get_json()["message"])


class TestWebhookAllocationInputValidation(unittest.TestCase):
    """Input edge cases."""

    def setUp(self):
        self.app = app
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    @patch("adviser_allocation.utils.auth.get_secret")
    def test_non_json_content_type_returns_415(self, mock_secret):
        mock_secret.return_value = TEST_HUBSPOT_SECRET
        body = "not json"
        url = "http://localhost/post/allocate"
        sig = _hubspot_v2_signature(TEST_HUBSPOT_SECRET, "POST", url, body)
        response = self.client.post(
            "/post/allocate",
            data=body,
            content_type="text/plain",
            headers={"X-HubSpot-Signature": sig},
        )
        self.assertEqual(response.status_code, 415)

    @patch("adviser_allocation.api.webhooks.get_adviser")
    @patch("adviser_allocation.utils.auth.get_secret")
    def test_empty_object_type_skips_allocation(self, mock_secret, mock_get_adviser):
        mock_secret.return_value = TEST_HUBSPOT_SECRET
        payload = {"object": {}, "fields": {"service_package": "series a"}}
        response = _post_with_sig(self.client, payload)
        self.assertEqual(response.status_code, 200)
        mock_get_adviser.assert_not_called()


class TestWebhookAllocationHubSpotFailure(unittest.TestCase):
    """HubSpot deal update failure — allocation record still stored as failed."""

    def setUp(self):
        self.app = app
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

        self.p_secret = patch(
            "adviser_allocation.utils.auth.get_secret",
            return_value=TEST_HUBSPOT_SECRET,
        )
        self.p_get_adviser = patch("adviser_allocation.api.webhooks.get_adviser")
        self.p_patch = patch("adviser_allocation.api.webhooks.patch_with_retries")
        self.p_store = patch("adviser_allocation.api.webhooks.store_allocation_record")
        self.p_chat = patch("adviser_allocation.api.webhooks.send_chat_alert")
        self.p_headers = patch(
            "adviser_allocation.api.webhooks._hubspot_headers",
            return_value={
                "Authorization": "Bearer test-token",
                "Content-Type": "application/json",
            },
        )

        self.mock_secret = self.p_secret.start()
        self.mock_get_adviser = self.p_get_adviser.start()
        self.mock_patch_hs = self.p_patch.start()
        self.mock_store = self.p_store.start()
        self.mock_chat = self.p_chat.start()
        self.mock_headers = self.p_headers.start()

        self.mock_get_adviser.return_value = (
            _make_adviser_user(),
            _make_candidates_summary(),
        )

    def tearDown(self):
        self.p_secret.stop()
        self.p_get_adviser.stop()
        self.p_patch.stop()
        self.p_store.stop()
        self.p_chat.stop()
        self.p_headers.stop()

    def test_hubspot_http_error_stores_failed_record(self):
        import requests as req

        mock_resp = MagicMock(status_code=500, text="Internal Server Error")
        http_error = req.exceptions.HTTPError(response=mock_resp)
        mock_resp.raise_for_status.side_effect = http_error
        self.mock_patch_hs.return_value = mock_resp

        response = _post_with_sig(self.client, _make_allocation_payload(deal_id="deal-HS-FAIL"))

        self.assertEqual(response.status_code, 200)

        self.mock_store.assert_called_once()
        record = self.mock_store.call_args[0][1]
        self.assertEqual(record["deal_id"], "deal-HS-FAIL")
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["allocation_result"], "failed")

        self.mock_chat.assert_not_called()

    def test_unexpected_error_stores_failed_record(self):
        self.mock_patch_hs.side_effect = RuntimeError("Network timeout")

        response = _post_with_sig(self.client, _make_allocation_payload(deal_id="deal-ERR"))

        self.assertEqual(response.status_code, 200)

        self.mock_store.assert_called_once()
        record = self.mock_store.call_args[0][1]
        self.assertEqual(record["deal_id"], "deal-ERR")
        self.assertEqual(record["status"], "failed")
        self.assertIn("Network timeout", record["error_message"])


class TestWebhookAllocationRenewals(unittest.TestCase):
    """Renewal deals send renewal_service_package instead of service_package."""

    def setUp(self):
        self.app = app
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

        self.p_secret = patch(
            "adviser_allocation.utils.auth.get_secret",
            return_value=TEST_HUBSPOT_SECRET,
        )
        self.p_get_adviser = patch("adviser_allocation.api.webhooks.get_adviser")
        self.p_patch = patch("adviser_allocation.api.webhooks.patch_with_retries")
        self.p_store = patch("adviser_allocation.api.webhooks.store_allocation_record")
        self.p_chat = patch("adviser_allocation.api.webhooks.send_chat_alert")
        self.p_headers = patch(
            "adviser_allocation.api.webhooks._hubspot_headers",
            return_value={
                "Authorization": "Bearer test-token",
                "Content-Type": "application/json",
            },
        )

        self.mock_secret = self.p_secret.start()
        self.mock_get_adviser = self.p_get_adviser.start()
        self.mock_patch_hs = self.p_patch.start()
        self.mock_store = self.p_store.start()
        self.mock_chat = self.p_chat.start()
        self.mock_headers = self.p_headers.start()

        self.mock_get_adviser.return_value = (
            _make_adviser_user(),
            _make_candidates_summary(),
        )
        mock_resp = MagicMock(status_code=200)
        mock_resp.raise_for_status = MagicMock()
        self.mock_patch_hs.return_value = mock_resp

    def tearDown(self):
        self.p_secret.stop()
        self.p_get_adviser.stop()
        self.p_patch.stop()
        self.p_store.stop()
        self.p_chat.stop()
        self.p_headers.stop()

    def test_renewal_service_package_fallback(self):
        """When service_package missing, falls back to renewal_service_package."""
        payload = _make_allocation_payload(
            omit_service_package=True,
            renewal_service_package="series a",
        )
        response = _post_with_sig(self.client, payload)
        self.assertEqual(response.status_code, 200)
        self.mock_get_adviser.assert_called_once()
        call_args = self.mock_get_adviser.call_args[0]
        self.assertEqual(call_args[0], "series a")

    def test_renewal_household_type_fallback(self):
        """When household_type blank, falls back to renewal_household_type."""
        payload = _make_allocation_payload(
            household_type="",
            renewal_household_type="couple",
        )
        response = _post_with_sig(self.client, payload)
        self.assertEqual(response.status_code, 200)
        self.mock_get_adviser.assert_called_once()
        call_args = self.mock_get_adviser.call_args[0]
        self.assertEqual(call_args[2], "couple")

    def test_primary_field_takes_precedence(self):
        """When both fields present, primary service_package wins."""
        payload = _make_allocation_payload(
            service_package="series a",
            renewal_service_package="ipo",
        )
        response = _post_with_sig(self.client, payload)
        self.assertEqual(response.status_code, 200)
        call_args = self.mock_get_adviser.call_args[0]
        self.assertEqual(call_args[0], "series a")

    def test_missing_both_service_package_fields_stores_failed_record(self):
        """Neither service_package nor renewal_service_package -> failed record."""
        payload = _make_allocation_payload(omit_service_package=True)
        response = _post_with_sig(self.client, payload)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["message"], "Missing service package field")
        self.mock_get_adviser.assert_not_called()
        self.mock_store.assert_called_once()
        record = self.mock_store.call_args[0][1]
        self.assertEqual(record["status"], "failed")
        self.assertIn("Missing service_package", record["error_message"])


if __name__ == "__main__":
    unittest.main()
