"""End-to-end mock test for /post/allocate — real allocation engine, mocked boundaries.

Exercises the FULL pipeline: adviser filtering, capacity computation,
earliest week algorithm, tiebreaker selection, HubSpot update, record
storage, and chat alert — with only external I/O boundaries mocked.
"""

import hashlib
import json
import os
import unittest
from datetime import date, datetime, timedelta
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

os.environ.setdefault("USE_FIRESTORE", "false")
os.environ.setdefault("PRESTART_WEEKS", "3")
os.environ.setdefault("MATRIX_CACHE_TTL", "0")

from adviser_allocation.main import app  # noqa: E402

SYDNEY_TZ = ZoneInfo("Australia/Sydney")
FROZEN_DATE = date(2026, 3, 9)  # Monday
FROZEN_NOW = datetime(2026, 3, 9, 10, 0, 0, tzinfo=SYDNEY_TZ)
TEST_HUBSPOT_SECRET = "test-hubspot-client-secret"  # pragma: allowlist secret


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _hubspot_v2_signature(client_secret, method, url, body=""):
    """Compute HubSpot v2 signature: SHA-256(client_secret + method + url + body)."""
    source = client_secret + method + url + body
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


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


def _make_allocation_payload(
    service_package="series a",
    household_type="single",
    deal_id="deal-e2e-001",
    client_email="newclient@example.com",
    agreement_start_date="",
):
    """Build a realistic HubSpot workflow webhook payload."""
    return {
        "object": {"objectType": "DEAL", "objectId": deal_id},
        "fields": {
            "service_package": service_package,
            "household_type": household_type,
            "hs_deal_record_id": deal_id,
            "client_email": client_email,
            "agreement_start_date": agreement_start_date,
        },
    }


# ---------------------------------------------------------------------------
# Mock adviser data
# ---------------------------------------------------------------------------


def _make_adviser(email, owner_id, client_types="series a;seed", household_type="single;couple"):
    """Build a HubSpot user dict for an adviser."""
    return {
        "id": f"user-{owner_id}",
        "properties": {
            "hs_email": email,
            "hubspot_owner_id": owner_id,
            "taking_on_clients": "True",
            "client_types": client_types,
            "household_type": household_type,
            "adviser_start_date": "2024-01-01",
            "pod_type": "Team",
        },
    }


ALICE_EMAIL = "alice.adviser@pivotwealth.com.au"
BOB_EMAIL = "bob.busy@pivotwealth.com.au"
CHARLIE_EMAIL = "charlie.clear@pivotwealth.com.au"

ALICE = _make_adviser(ALICE_EMAIL, "owner-A")
BOB = _make_adviser(BOB_EMAIL, "owner-B")
CHARLIE = _make_adviser(CHARLIE_EMAIL, "owner-C")


def _make_meeting(start_iso, activity_type="Clarify", owner_id="owner-X"):
    """Build a HubSpot meeting result dict."""
    return {
        "id": f"meeting-{start_iso[:10]}",
        "properties": {
            "hs_meeting_start_time": start_iso,
            "hs_activity_type": activity_type,
            "hs_meeting_title": f"{activity_type} - Test Client",
            "hs_meeting_outcome": "COMPLETED",
            "hubspot_owner_id": owner_id,
        },
    }


def _make_deal(deal_id, agreement_start_date, adviser_email):
    """Build a HubSpot deal (no clarify) result dict."""
    return {
        "id": deal_id,
        "properties": {
            "agreement_start_date": agreement_start_date,
            "dealname": f"Deal {deal_id}",
            "advisor_email": adviser_email,
        },
    }


# Alice: 2 Clarify meetings (current week + next week)
ALICE_MEETINGS = [
    _make_meeting("2026-03-10T10:00:00Z", "Clarify", "owner-A"),
    _make_meeting("2026-03-17T10:00:00Z", "Clarify", "owner-A"),
]

# Bob: 6 Clarify meetings spread over 3 weeks (at capacity)
BOB_MEETINGS = [
    _make_meeting("2026-03-09T09:00:00Z", "Clarify", "owner-B"),
    _make_meeting("2026-03-10T09:00:00Z", "Clarify", "owner-B"),
    _make_meeting("2026-03-16T09:00:00Z", "Clarify", "owner-B"),
    _make_meeting("2026-03-17T09:00:00Z", "Clarify", "owner-B"),
    _make_meeting("2026-03-23T09:00:00Z", "Clarify", "owner-B"),
    _make_meeting("2026-03-24T09:00:00Z", "Clarify", "owner-B"),
]

CHARLIE_MEETINGS = []

# Alice: 1 deal without clarify
ALICE_DEALS = [_make_deal("deal-alice-1", "2026-03-02", ALICE_EMAIL)]

# Bob: 3 deals without clarify (heavy backlog)
BOB_DEALS = [
    _make_deal("deal-bob-1", "2026-02-23", BOB_EMAIL),
    _make_deal("deal-bob-2", "2026-03-02", BOB_EMAIL),
    _make_deal("deal-bob-3", "2026-03-02", BOB_EMAIL),
]

CHARLIE_DEALS = []

# Office closures: Easter 2026
MOCK_CLOSURES = [
    {
        "id": "closure-easter",
        "start_date": "2026-04-03",
        "end_date": "2026-04-06",
        "description": "Easter",
        "tags": ["HOLIDAY"],
    }
]

# Alice leave: full week 2026-03-23 to 2026-03-27
ALICE_LEAVE = [
    {
        "leave_request_id": "lr-alice-1",
        "employee_id": "emp-alice",
        "start_date": "2026-03-23",
        "end_date": "2026-03-27",
        "leave_type": "ANNUAL",
        "status": "approved",
    }
]


# ---------------------------------------------------------------------------
# Side-effect functions for mocks
# ---------------------------------------------------------------------------


def _meeting_side_effect(user, timestamp_milliseconds):
    """Inject meetings per adviser and return user."""
    email = user["properties"]["hs_email"]
    meetings_map = {
        ALICE_EMAIL: ALICE_MEETINGS,
        BOB_EMAIL: BOB_MEETINGS,
        CHARLIE_EMAIL: CHARLIE_MEETINGS,
    }
    user["meetings"] = {"results": meetings_map.get(email, [])}
    return user


def _deals_side_effect(user_email):
    """Return deals per adviser email."""
    deals_map = {
        ALICE_EMAIL: ALICE_DEALS,
        BOB_EMAIL: BOB_DEALS,
        CHARLIE_EMAIL: CHARLIE_DEALS,
    }
    return deals_map.get(user_email, [])


def _employee_id_side_effect(email):
    """Return employee ID by email."""
    employee_map = {
        ALICE_EMAIL: "emp-alice",
        BOB_EMAIL: "emp-bob",
        CHARLIE_EMAIL: None,  # Charlie not in Employment Hero
    }
    return employee_map.get(email)


def _employee_leaves_side_effect(employee_id):
    """Return leave requests by employee ID."""
    leave_map = {
        "emp-alice": ALICE_LEAVE,
        "emp-bob": [],
    }
    return leave_map.get(employee_id, [])


# ---------------------------------------------------------------------------
# Test class
# ---------------------------------------------------------------------------


class TestWebhookE2EMockAllocation(unittest.TestCase):
    """Full pipeline test: real get_adviser(), mocked external boundaries."""

    def setUp(self):
        self.app = app
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

        # Clear module-level caches before each test
        from adviser_allocation.core.allocation import (
            _USER_IDS_CACHE,
            _capacity_override_ttl_cache,
        )

        _USER_IDS_CACHE.clear()
        _capacity_override_ttl_cache.clear()

        # Build mock DB
        self.mock_db = MagicMock()
        self.mock_db.get_global_closures.return_value = MOCK_CLOSURES
        self.mock_db.get_capacity_overrides.return_value = []
        self.mock_db.get_employee_id_by_email.side_effect = _employee_id_side_effect
        self.mock_db.get_employee_leaves_as_dicts.side_effect = _employee_leaves_side_effect
        self.mock_db.store_allocation_record.return_value = "req-e2e-001"

        # --- Patches ---

        # 1. Auth: HubSpot signature secret
        self.p_secret = patch(
            "adviser_allocation.utils.auth.get_secret",
            return_value=TEST_HUBSPOT_SECRET,
        )

        # 2. Allocation engine: HubSpot adviser list
        self.p_users = patch(
            "adviser_allocation.core.allocation.get_user_ids_adviser",
            return_value=[
                _make_adviser(ALICE_EMAIL, "owner-A"),
                _make_adviser(BOB_EMAIL, "owner-B"),
                _make_adviser(CHARLIE_EMAIL, "owner-C"),
            ],
        )

        # 3. Allocation engine: meetings per adviser
        self.p_meetings = patch(
            "adviser_allocation.core.allocation.get_user_meeting_details",
            side_effect=_meeting_side_effect,
        )

        # 4. Allocation engine: deals without clarify
        self.p_deals = patch(
            "adviser_allocation.core.allocation.get_deals_no_clarify",
            side_effect=_deals_side_effect,
        )

        # 5. Allocation engine: CloudSQL DB
        self.p_db_alloc = patch(
            "adviser_allocation.core.allocation.get_cloudsql_db",
            return_value=self.mock_db,
        )

        # 6. Allocation service: CloudSQL DB (for store_allocation_record)
        self.p_db_svc = patch(
            "adviser_allocation.services.allocation_service.get_cloudsql_db",
            return_value=self.mock_db,
        )

        # 7. Time freeze: sydney_today in allocation
        self.p_today = patch(
            "adviser_allocation.core.allocation.sydney_today",
            return_value=FROZEN_DATE,
        )

        # 8. Time freeze: sydney_now in allocation
        self.p_now_alloc = patch(
            "adviser_allocation.core.allocation.sydney_now",
            return_value=FROZEN_NOW,
        )

        # 9. Time freeze: sydney_now in allocation_service
        self.p_now_svc = patch(
            "adviser_allocation.services.allocation_service.sydney_now",
            return_value=FROZEN_NOW,
        )

        # 10. Webhook: HubSpot deal update
        mock_hs_resp = MagicMock(status_code=200)
        mock_hs_resp.raise_for_status = MagicMock()
        self.p_patch_hs = patch(
            "adviser_allocation.api.webhooks.patch_with_retries",
            return_value=mock_hs_resp,
        )

        # 11. Webhook: HubSpot headers
        self.p_headers = patch(
            "adviser_allocation.api.webhooks._hubspot_headers",
            return_value={
                "Authorization": "Bearer test-token",
                "Content-Type": "application/json",
            },
        )

        # 12. Webhook: Google Chat alert
        self.p_chat = patch("adviser_allocation.api.webhooks.send_chat_alert")

        # Start all patches
        self.mock_secret = self.p_secret.start()
        self.mock_users = self.p_users.start()
        self.mock_meetings = self.p_meetings.start()
        self.mock_deals = self.p_deals.start()
        self.mock_db_alloc = self.p_db_alloc.start()
        self.mock_db_svc = self.p_db_svc.start()
        self.mock_today = self.p_today.start()
        self.mock_now_alloc = self.p_now_alloc.start()
        self.mock_now_svc = self.p_now_svc.start()
        self.mock_patch_hs = self.p_patch_hs.start()
        self.mock_headers = self.p_headers.start()
        self.mock_chat = self.p_chat.start()

        # Run the allocation once — all test methods assert against this result
        self._response = _post_with_sig(
            self.client,
            _make_allocation_payload(),
            query_string="send_chat_alert=1",
        )

    def tearDown(self):
        self.p_secret.stop()
        self.p_users.stop()
        self.p_meetings.stop()
        self.p_deals.stop()
        self.p_db_alloc.stop()
        self.p_db_svc.stop()
        self.p_today.stop()
        self.p_now_alloc.stop()
        self.p_now_svc.stop()
        self.p_patch_hs.stop()
        self.p_headers.stop()
        self.p_chat.stop()

        from adviser_allocation.core.allocation import (
            _USER_IDS_CACHE,
            _capacity_override_ttl_cache,
        )

        _USER_IDS_CACHE.clear()
        _capacity_override_ttl_cache.clear()

    # --- Tests ---

    def test_selects_charlie_as_winner(self):
        """Charlie (no load, no leave) should be the selected adviser."""
        self.assertEqual(self._response.status_code, 200)
        self.assertEqual(
            self._response.get_json()["message"],
            "Webhook received successfully",
        )

        # Verify HubSpot deal was updated with Charlie's owner ID
        self.mock_patch_hs.assert_called_once()
        patch_url = self.mock_patch_hs.call_args[0][0]
        self.assertIn("deal-e2e-001", patch_url)

        patch_data = json.loads(self.mock_patch_hs.call_args[1]["data"])
        self.assertEqual(patch_data["properties"]["advisor"], "owner-C")

    def test_stores_completed_allocation_record(self):
        """Allocation record should be stored with completed status and Charlie's details."""
        self.mock_db.store_allocation_record.assert_called_once()
        record = self.mock_db.store_allocation_record.call_args[0][0]

        self.assertEqual(record["status"], "completed")
        self.assertEqual(record["allocation_result"], "completed")
        self.assertEqual(record["adviser_email"], CHARLIE_EMAIL)
        self.assertEqual(record["adviser_hubspot_id"], "owner-C")
        self.assertEqual(record["deal_id"], "deal-e2e-001")
        self.assertIn("Series A", record["service_package"])

    def test_chat_alert_disabled(self):
        """Chat alerts are currently disabled (send_chat_alert_flag = False)."""
        self.mock_chat.assert_not_called()

    def test_alice_not_selected_due_to_leave(self):
        """Alice is eligible but not the winner (leave + backlog push her later)."""
        patch_data = json.loads(self.mock_patch_hs.call_args[1]["data"])
        self.assertNotEqual(patch_data["properties"]["advisor"], "owner-A")

    def test_bob_not_selected_due_to_capacity(self):
        """Bob is eligible but not the winner (6 meetings + 3 deals = at capacity)."""
        patch_data = json.loads(self.mock_patch_hs.call_args[1]["data"])
        self.assertNotEqual(patch_data["properties"]["advisor"], "owner-B")

    def test_household_mismatch_returns_500(self):
        """When no adviser supports the household type, get_adviser raises → 500."""
        # Stop patches that were started in setUp — we need a fresh call
        # with different payload (household_type="family")
        response = _post_with_sig(
            self.client,
            _make_allocation_payload(household_type="family"),
        )
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.get_json()["message"], "Internal Server Error")

        # Chat alerts disabled — should still be zero after the failed request
        self.assertEqual(self.mock_chat.call_count, 0)


if __name__ == "__main__":
    unittest.main()
