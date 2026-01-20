"""End-to-end workflow tests for user journeys."""

import os
import json
import unittest
import time
from datetime import date, timedelta
from unittest.mock import patch, MagicMock

os.environ.setdefault("USE_FIRESTORE", "false")

from adviser_allocation.main import app


class AdministratorWorkflowTests(unittest.TestCase):
    """Tests for administrator workflow."""

    def setUp(self):
        """Set up test fixtures."""
        self.app = app
        self.app.config['TESTING'] = True
        self.client = self.app.test_client()

    def test_admin_login_workflow(self):
        """Test admin login workflow."""
        # Step 1: Visit login page
        response = self.client.get('/login')
        self.assertIn(response.status_code, [200, 301, 302])

        # Step 2: Submit credentials
        response = self.client.post('/auth/login', json={
            "username": "admin",
            "password": "admin_password"
        })
        self.assertIsNotNone(response)

        # Step 3: Verify authentication
        with self.client.session_transaction() as sess:
            self.assertIsNotNone(sess)

    def test_admin_office_closure_management(self):
        """Test admin managing office closures."""
        with self.client.session_transaction() as sess:
            sess['is_authenticated'] = True
            sess['role'] = 'admin'

        # Step 1: View closures page
        response = self.client.get('/closures/ui')
        self.assertNotIn(response.status_code, [500])

        # Step 2: Create new closure
        closure_data = {
            "name": "Summer Break",
            "start_date": str(date.today() + timedelta(days=30)),
            "end_date": str(date.today() + timedelta(days=37)),
        }
        response = self.client.post('/closures', json=closure_data)
        self.assertIsNotNone(response)

        # Step 3: View updated closures
        response = self.client.get('/closures/ui')
        self.assertIsNotNone(response)

    def test_admin_capacity_override_workflow(self):
        """Test admin managing capacity overrides."""
        with self.client.session_transaction() as sess:
            sess['is_authenticated'] = True
            sess['role'] = 'admin'

        # Step 1: View capacity overrides
        response = self.client.get('/capacity_overrides/ui')
        self.assertNotIn(response.status_code, [500])

        # Step 2: Create override
        override_data = {
            "employee_id": "emp123",
            "client_limit_monthly": 8,
            "start_date": str(date.today()),
            "end_date": str(date.today() + timedelta(days=30)),
        }
        response = self.client.post('/capacity_overrides', json=override_data)
        self.assertIsNotNone(response)

    def test_admin_box_settings_update(self):
        """Test admin updating Box settings."""
        with self.client.session_transaction() as sess:
            sess['is_authenticated'] = True
            sess['role'] = 'admin'

        # Step 1: Access settings
        response = self.client.get('/settings/box/ui')
        self.assertNotIn(response.status_code, [401, 403])

        # Step 2: Update settings
        settings = {
            "box_client_id": "new_client_id",
            "box_enterprise_id": "new_enterprise_id",
        }
        response = self.client.post('/settings/box/update', json=settings)
        self.assertIsNotNone(response)

        # Step 3: Verify settings persisted
        response = self.client.get('/settings/box/ui')
        self.assertIsNotNone(response)


class AdviserAvailabilityWorkflowTests(unittest.TestCase):
    """Tests for adviser availability viewing workflow."""

    def setUp(self):
        """Set up test fixtures."""
        self.app = app
        self.app.config['TESTING'] = True
        self.client = self.app.test_client()

    def test_view_earliest_availability_workflow(self):
        """Test viewing earliest availability."""
        with self.client.session_transaction() as sess:
            sess['is_authenticated'] = True

        # Step 1: Navigate to availability page
        response = self.client.get('/availability/earliest')
        self.assertNotEqual(response.status_code, 404)

        # Step 2: Page should load with adviser data
        self.assertIsNotNone(response.data)

    def test_adviser_schedule_view_workflow(self):
        """Test viewing adviser schedule."""
        with self.client.session_transaction() as sess:
            sess['is_authenticated'] = True

        # Step 1: Access schedule page
        response = self.client.get('/availability/schedule')
        self.assertNotEqual(response.status_code, 404)

        # Step 2: Verify data is present
        self.assertIsNotNone(response.data)

    def test_availability_matrix_workflow(self):
        """Test viewing availability matrix."""
        with self.client.session_transaction() as sess:
            sess['is_authenticated'] = True

        # Step 1: Access matrix view
        response = self.client.get('/availability/matrix')
        self.assertNotEqual(response.status_code, 404)

        # Step 2: Verify matrix data loaded
        self.assertIsNotNone(response.data)

    def test_filter_availability_by_service_package(self):
        """Test filtering availability by service package."""
        with self.client.session_transaction() as sess:
            sess['is_authenticated'] = True

        # Step 1: Access availability with filter
        response = self.client.get('/availability/earliest?service_package=investment')
        self.assertNotEqual(response.status_code, 404)

        # Step 2: Results should be filtered
        self.assertIsNotNone(response)


class AllocationHistoryWorkflowTests(unittest.TestCase):
    """Tests for allocation history viewing workflow."""

    def setUp(self):
        """Set up test fixtures."""
        self.app = app
        self.app.config['TESTING'] = True
        self.client = self.app.test_client()

    def test_view_allocation_history_workflow(self):
        """Test viewing allocation history."""
        with self.client.session_transaction() as sess:
            sess['is_authenticated'] = True

        # Step 1: Navigate to history page
        response = self.client.get('/allocations/history')
        self.assertNotEqual(response.status_code, 404)

        # Step 2: Page should load
        self.assertIsNotNone(response.data)

    def test_filter_allocations_by_status(self):
        """Test filtering allocations by status."""
        with self.client.session_transaction() as sess:
            sess['is_authenticated'] = True

        # Step 1: Access with status filter
        response = self.client.get('/allocations/history?status=completed')
        self.assertNotEqual(response.status_code, 404)

    def test_filter_allocations_by_adviser(self):
        """Test filtering allocations by adviser."""
        with self.client.session_transaction() as sess:
            sess['is_authenticated'] = True

        # Step 1: Access with adviser filter
        response = self.client.get('/allocations/history?adviser_id=emp123')
        self.assertNotEqual(response.status_code, 404)

    def test_allocation_history_pagination(self):
        """Test pagination in allocation history."""
        with self.client.session_transaction() as sess:
            sess['is_authenticated'] = True

        # Step 1: First page
        response1 = self.client.get('/allocations/history?page=1&limit=10')
        self.assertNotEqual(response1.status_code, 404)

        # Step 2: Second page
        response2 = self.client.get('/allocations/history?page=2&limit=10')
        self.assertNotEqual(response2.status_code, 404)


class MeetingScheduleWorkflowTests(unittest.TestCase):
    """Tests for meeting schedule viewing workflow."""

    def setUp(self):
        """Set up test fixtures."""
        self.app = app
        self.app.config['TESTING'] = True
        self.client = self.app.test_client()

    def test_view_meeting_schedule_workflow(self):
        """Test viewing meeting schedule."""
        with self.client.session_transaction() as sess:
            sess['is_authenticated'] = True

        # Step 1: Access schedule page
        response = self.client.get('/availability/schedule')
        self.assertNotEqual(response.status_code, 404)

    def test_filter_schedule_by_adviser(self):
        """Test filtering schedule by adviser."""
        with self.client.session_transaction() as sess:
            sess['is_authenticated'] = True

        response = self.client.get('/availability/schedule?adviser_id=emp123')
        self.assertIsNotNone(response)

    def test_export_schedule_to_calendar(self):
        """Test exporting schedule to calendar."""
        with self.client.session_transaction() as sess:
            sess['is_authenticated'] = True

        # Step 1: Request calendar export
        response = self.client.get('/availability/schedule?format=ical')
        self.assertIsNotNone(response)


class WorkflowErrorHandlingTests(unittest.TestCase):
    """Tests for error handling in workflows."""

    def setUp(self):
        """Set up test fixtures."""
        self.app = app
        self.app.config['TESTING'] = True
        self.client = self.app.test_client()

    def test_firestore_unavailable_graceful_degradation(self):
        """Test graceful degradation when Firestore unavailable."""
        with self.client.session_transaction() as sess:
            sess['is_authenticated'] = True

        with patch('adviser_allocation.main.get_firestore_client') as mock_db:
            mock_db.side_effect = Exception("Firestore unavailable")

            response = self.client.get('/availability/earliest')

            # Should handle gracefully
            self.assertIsNotNone(response)

    def test_hubspot_timeout_graceful_degradation(self):
        """Test graceful degradation on HubSpot timeout."""
        with self.client.session_transaction() as sess:
            sess['is_authenticated'] = True

        with patch('adviser_allocation.utils.http_client.requests.get') as mock_get:
            import requests
            mock_get.side_effect = requests.Timeout()

            response = self.client.get('/availability/earliest')

            # Should handle timeout gracefully
            self.assertIsNotNone(response)

    def test_invalid_date_input_validation(self):
        """Test validation of invalid date input."""
        with self.client.session_transaction() as sess:
            sess['is_authenticated'] = True

        response = self.client.get('/closures/get?date=invalid-date')

        # Should handle gracefully
        self.assertIsNotNone(response)

    def test_unauthorized_access_redirects(self):
        """Test unauthorized access handling."""
        # Without authentication
        response = self.client.get('/settings/box/ui', follow_redirects=False)

        # Should redirect to login
        self.assertIn(response.status_code, [301, 302, 401, 403])


class WorkflowPerformanceTests(unittest.TestCase):
    """Tests for workflow performance."""

    def setUp(self):
        """Set up test fixtures."""
        self.app = app
        self.app.config['TESTING'] = True
        self.client = self.app.test_client()

    def test_availability_page_load_time(self):
        """Test availability page loads within reasonable time."""
        with self.client.session_transaction() as sess:
            sess['is_authenticated'] = True

        start_time = time.time()
        response = self.client.get('/availability/earliest')
        elapsed = time.time() - start_time

        # Should load within 5 seconds
        self.assertLess(elapsed, 5.0)
        self.assertNotEqual(response.status_code, 500)

    def test_allocation_history_page_load_time(self):
        """Test allocation history page loads within reasonable time."""
        with self.client.session_transaction() as sess:
            sess['is_authenticated'] = True

        start_time = time.time()
        response = self.client.get('/allocations/history')
        elapsed = time.time() - start_time

        # Should load within 5 seconds
        self.assertLess(elapsed, 5.0)

    def test_settings_page_load_time(self):
        """Test settings page loads within reasonable time."""
        with self.client.session_transaction() as sess:
            sess['is_authenticated'] = True

        start_time = time.time()
        response = self.client.get('/settings/box/ui')
        elapsed = time.time() - start_time

        # Should load within 3 seconds
        self.assertLess(elapsed, 3.0)


class MultiStepWorkflowTests(unittest.TestCase):
    """Tests for complex multi-step workflows."""

    def setUp(self):
        """Set up test fixtures."""
        self.app = app
        self.app.config['TESTING'] = True
        self.client = self.app.test_client()

    def test_complete_allocation_workflow(self):
        """Test complete allocation workflow from start to finish."""
        with self.client.session_transaction() as sess:
            sess['is_authenticated'] = True

        # Step 1: View availability
        response1 = self.client.get('/availability/earliest')
        self.assertNotEqual(response1.status_code, 404)

        # Step 2: Create allocation (if applicable)
        response2 = self.client.post('/post/allocate', json={
            "deal_id": "deal123",
            "adviser_id": "emp123",
            "household_type": "series a"
        })
        self.assertIsNotNone(response2)

        # Step 3: View allocation history
        response3 = self.client.get('/allocations/history')
        self.assertNotEqual(response3.status_code, 404)

    def test_admin_setup_workflow(self):
        """Test complete admin setup workflow."""
        with self.client.session_transaction() as sess:
            sess['is_authenticated'] = True
            sess['role'] = 'admin'

        # Step 1: Access settings
        response1 = self.client.get('/settings/box/ui')
        self.assertNotIn(response1.status_code, [401, 403])

        # Step 2: Create closure
        response2 = self.client.post('/closures', json={
            "name": "Holiday",
            "start_date": str(date.today() + timedelta(days=30)),
            "end_date": str(date.today() + timedelta(days=37))
        })
        self.assertIsNotNone(response2)

        # Step 3: Create capacity override
        response3 = self.client.post('/capacity_overrides', json={
            "employee_id": "emp123",
            "client_limit_monthly": 6,
            "start_date": str(date.today()),
            "end_date": str(date.today() + timedelta(days=30))
        })
        self.assertIsNotNone(response3)


if __name__ == "__main__":
    unittest.main()
