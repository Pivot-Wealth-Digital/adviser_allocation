"""Tests for the authoritative Primary/Spouse resolution in the Box tagger.

The tagger must source the Box folder's primary_contact_id from the deal's "Client"
association label and the spouse (hs_spouse_id) from "Client's Spouse" — NOT from the
unreliable hs_contact_id/hs_spouse_id deal properties (which an upstream HubSpot
workflow can set wrong, producing the Primary/Spouse swap).
"""

from unittest.mock import patch

from adviser_allocation.api import box_routes


def _contacts(*pairs):
    """Build deal-contact dicts shaped like get_hubspot_deal_contacts() output."""
    return [
        {"id": cid, "properties": {}, "association_types": [{"label": label}]}
        for cid, label in pairs
    ]


# --- _contact_by_label ---------------------------------------------------------


def test_contact_by_label_is_case_insensitive():
    contacts = _contacts(("1", "Client"), ("2", "Client's Spouse"))
    assert box_routes._contact_by_label(contacts, "client")["id"] == "1"
    assert box_routes._contact_by_label(contacts, "CLIENT'S SPOUSE")["id"] == "2"


def test_contact_by_label_no_match_returns_none():
    contacts = _contacts(("1", "Deal Referred By"))
    assert box_routes._contact_by_label(contacts, "Client") is None
    assert box_routes._contact_by_label(None, "Client") is None


# --- _authoritative_household_from_deal ----------------------------------------


@patch.object(box_routes.box_service, "get_hubspot_deal_contacts")
def test_authoritative_resolves_client_and_spouse(mock_get):
    mock_get.return_value = _contacts(("111", "Client"), ("222", "Client's Spouse"))
    assert box_routes._authoritative_household_from_deal("D1") == ("111", "222")


def test_authoritative_no_deal_id_returns_none():
    assert box_routes._authoritative_household_from_deal("") == (None, None)
    assert box_routes._authoritative_household_from_deal(None) == (None, None)


@patch.object(box_routes.box_service, "get_hubspot_deal_contacts")
def test_authoritative_lookup_error_is_swallowed(mock_get):
    mock_get.side_effect = RuntimeError("hubspot down")
    assert box_routes._authoritative_household_from_deal("D1") == (None, None)


@patch.object(box_routes.box_service, "get_hubspot_deal_contacts")
def test_authoritative_no_client_label_does_not_invent_primary(mock_get):
    mock_get.return_value = _contacts(("222", "Client's Spouse"))
    primary, spouse = box_routes._authoritative_household_from_deal("D1")
    assert primary is None and spouse == "222"


@patch.object(box_routes.box_service, "get_hubspot_deal_contacts")
def test_authoritative_self_spouse_is_dropped(mock_get):
    # Same contact carries both labels -> never tag one person as both.
    mock_get.return_value = [
        {
            "id": "111",
            "properties": {},
            "association_types": [{"label": "Client"}, {"label": "Client's Spouse"}],
        }
    ]
    assert box_routes._authoritative_household_from_deal("D1") == ("111", None)


# --- _apply_authoritative_household (the override) ------------------------------


@patch.object(box_routes, "_hubspot_contact_url", side_effect=lambda cid: f"url/{cid}")
@patch.object(box_routes.box_service, "get_hubspot_deal_contacts")
def test_apply_corrects_swapped_primary_and_spouse(mock_get, _url):
    """Payload had Primary = the spouse and spouse = the Client (the swap); the
    authoritative Client/Spouse labels correct both."""
    mock_get.return_value = _contacts(("111", "Client"), ("222", "Client's Spouse"))
    metadata = {"primary_contact_id": "222", "hs_spouse_id": "111"}  # swapped
    out = box_routes._apply_authoritative_household(metadata, "D1")
    assert out["primary_contact_id"] == "111"
    assert out["primary_contact_link"] == "url/111"
    assert out["hs_spouse_id"] == "222"
    assert out["spouse_contact_link"] == "url/222"


@patch.object(box_routes, "_hubspot_contact_url", side_effect=lambda cid: f"url/{cid}")
@patch.object(box_routes.box_service, "get_hubspot_deal_contacts")
def test_apply_no_client_label_leaves_payload_untouched(mock_get, _url):
    mock_get.return_value = _contacts(("999", "Deal Referred By"))
    metadata = {"primary_contact_id": "555", "hs_spouse_id": "556"}
    out = box_routes._apply_authoritative_household(dict(metadata), "D1")
    assert out["primary_contact_id"] == "555"  # not overridden
    assert out["hs_spouse_id"] == "556"  # not cleared


@patch.object(box_routes, "_hubspot_contact_url", side_effect=lambda cid: f"url/{cid}")
@patch.object(box_routes.box_service, "get_hubspot_deal_contacts")
def test_apply_no_spouse_label_never_clears_existing_spouse(mock_get, _url):
    mock_get.return_value = _contacts(("111", "Client"))  # no spouse label
    metadata = {"primary_contact_id": "111", "hs_spouse_id": "888"}
    out = box_routes._apply_authoritative_household(dict(metadata), "D1")
    assert out["primary_contact_id"] == "111"
    assert out["hs_spouse_id"] == "888"  # conservative: left untouched
