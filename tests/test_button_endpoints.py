"""Test that all homepage button endpoints actually work (return 200)."""

import os
import sys

# Load .env before importing app
from dotenv import load_dotenv

project_root = os.path.dirname(os.path.dirname(__file__))
load_dotenv(os.path.join(project_root, ".env"))

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../src"))

from datetime import datetime

from adviser_allocation.main import app


class ButtonEndpointTester:
    """Test button target endpoints."""

    BUTTON_ENDPOINTS = {
        # Availability & Scheduling
        "View Earliest Availability": "/availability/earliest",
        "View Adviser Schedule": "/availability/schedule",
        "Availability Matrix": "/availability/matrix",
        # Allocation Management
        "Client Allocation": "/post/allocate",
        "Allocation History": "/allocations/history",
        "Automation Workflows": "/workflows",
        # Box Automation
        "Manual Run UI": "/box/create",
        "Collaborator Audit": "/box/collaborators",
        "Metadata Status": "/box/folder/metadata/status",
        # System Configuration
        "Closures Management": "/closures/ui",
        "Capacity Overrides": "/capacity_overrides/ui",
        "Box Settings": "/settings/box/ui",
        # Guides & Diagnostics
        "Custom-Code Guide": "/workflows/box-details",
    }

    def __init__(self):
        self.app = app
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()
        self.results = []

    def test_endpoints(self):
        """Test each button endpoint."""
        print("\n" + "=" * 80)
        print("BUTTON ENDPOINT FUNCTIONALITY TEST")
        print("=" * 80)
        print("\nTesting all button target endpoints:\n")

        with self.client:
            # Mock authentication
            with self.client.session_transaction() as sess:
                sess["is_authenticated"] = True

            for label, endpoint in self.BUTTON_ENDPOINTS.items():
                try:
                    response = self.client.get(endpoint)
                    status_ok = response.status_code == 200
                    status_symbol = "✓" if status_ok else "✗"

                    self.results.append(
                        {
                            "label": label,
                            "endpoint": endpoint,
                            "status_code": response.status_code,
                            "ok": status_ok,
                        }
                    )

                    print(f"{status_symbol} {label:40} {endpoint:40} → {response.status_code}")

                    # If 500, try to extract error
                    if response.status_code == 500:
                        try:
                            error_text = response.get_data(as_text=True)
                            if "Traceback" in error_text:
                                # Extract the last line of traceback
                                lines = error_text.split("\n")
                                for i, line in enumerate(lines):
                                    if "Error" in line or "Exception" in line:
                                        print(f"    Error: {line.strip()}")
                                        break
                        except Exception:
                            pass

                except Exception as e:
                    self.results.append(
                        {
                            "label": label,
                            "endpoint": endpoint,
                            "status_code": None,
                            "error": str(e),
                            "ok": False,
                        }
                    )
                    print(f"✗ {label:40} {endpoint:40} → ERROR: {e}")

        # Summary
        passed = sum(1 for r in self.results if r["ok"])
        total = len(self.results)

        print("\n" + "=" * 80)
        print("SUMMARY")
        print("=" * 80)
        print(f"Endpoints tested: {total}")
        print(f"Passing: {passed}")
        print(f"Failing: {total - passed}")

        if passed == total:
            print("\n✓ ALL ENDPOINTS WORKING")
        else:
            print(f"\n✗ {total - passed} ENDPOINT(S) BROKEN")
            print("\nFailing endpoints:")
            for result in self.results:
                if not result["ok"]:
                    print(f"  - {result['label']} ({result['endpoint']})")
                    print(f"    Status: {result.get('status_code', 'ERROR')}")

        print("=" * 80 + "\n")

        return passed == total


if __name__ == "__main__":
    tester = ButtonEndpointTester()
    all_working = tester.test_endpoints()

    sys.exit(0 if all_working else 1)
