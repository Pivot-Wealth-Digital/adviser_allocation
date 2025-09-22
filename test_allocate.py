#!/usr/bin/env python3
"""
Test script for the /post/allocate endpoint with the provided event payload.
"""

import requests
import json

# The event payload you provided
test_event = {
    'callbackId': '64d81666-7afb-446f-b4cf-eb45e496673e',
    'origin': {
        'portalId': 47011873,
        'userId': None,
        'actionDefinitionId': 223563888,
        'actionDefinitionVersion': 0,
        'actionExecutionIndexIdentifier': None,
        'extensionDefinitionId': 223563888,
        'extensionDefinitionVersionId': 0
    },
    'object': {
        'objectId': 42970036094,
        'objectType': 'DEAL'
    },
    'fields': {
        'agreement_start_date': '1761264000000',
        'hs_deal_record_id': '42970036094',
        'service_package': 'Series A'
    },
    'inputFields': {
        'agreement_start_date': '1761264000000',
        'hs_deal_record_id': '42970036094',
        'service_package': 'Series A'
    }
}

def test_allocate_endpoint():
    """Test the /post/allocate endpoint with the event payload."""

    # Endpoint URL (adjust port if needed)
    url = "http://localhost:8080/post/allocate"

    headers = {
        "Content-Type": "application/json"
    }

    print("Testing /post/allocate endpoint...")
    print(f"URL: {url}")
    print(f"Payload: {json.dumps(test_event, indent=2)}")
    print("-" * 50)

    try:
        response = requests.post(url, json=test_event, headers=headers, timeout=30)

        print(f"Status Code: {response.status_code}")
        print(f"Response Headers: {dict(response.headers)}")
        print(f"Response Body: {response.text}")

        if response.status_code == 200:
            print("✅ Request successful!")
        else:
            print("❌ Request failed!")

    except requests.exceptions.ConnectionError:
        print("❌ Connection failed! Make sure the Flask app is running on localhost:8080")
    except requests.exceptions.Timeout:
        print("❌ Request timed out!")
    except Exception as e:
        print(f"❌ Error: {e}")

if __name__ == "__main__":
    test_allocate_endpoint()