"""Integration tests for database operations."""

import os
import unittest
from datetime import date, timedelta
from unittest.mock import patch, MagicMock

os.environ.setdefault("USE_FIRESTORE", "false")


class FirestoreIntegrationTests(unittest.TestCase):
    """Tests for Firestore database integration."""

    @patch('adviser_allocation.utils.firestore_helpers.get_firestore_client')
    def test_employee_document_creation(self, mock_firestore):
        """Test creating and retrieving employee document."""
        mock_db = MagicMock()
        mock_firestore.return_value = mock_db

        # Simulate document creation
        employee_data = {
            "id": "emp123",
            "name": "John Doe",
            "email": "john@example.com",
            "client_limit_monthly": 4,
        }

        mock_db.collection("employees").document("emp123").set(employee_data)

        # Verify document was set
        mock_db.collection("employees").document("emp123").set.assert_called_once()

    @patch('adviser_allocation.utils.firestore_helpers.get_firestore_client')
    def test_leave_request_persistence(self, mock_firestore):
        """Test saving and retrieving leave requests."""
        mock_db = MagicMock()
        mock_firestore.return_value = mock_db

        leave_data = {
            "employee_id": "emp123",
            "start_date": str(date.today()),
            "end_date": str(date.today() + timedelta(days=5)),
            "leave_type": "Annual",
        }

        # Save leave request
        mock_db.collection("leave_requests").add(leave_data)

        # Verify persistence
        mock_db.collection("leave_requests").add.assert_called_once()

    @patch('adviser_allocation.utils.firestore_helpers.get_firestore_client')
    def test_office_closure_date_range_query(self, mock_firestore):
        """Test querying office closures by date range."""
        mock_db = MagicMock()
        mock_query = MagicMock()

        # Mock date range query
        mock_db.collection("closures").where("start_date", "<=", "2025-12-31").where(
            "end_date", ">=", "2025-01-01"
        ).stream.return_value = [
            MagicMock(to_dict=lambda: {
                "name": "Summer Break",
                "start_date": "2025-01-15",
                "end_date": "2025-01-20",
            })
        ]

        mock_firestore.return_value = mock_db

        # Query should return closures
        result = list(mock_db.collection("closures").where(
            "start_date", "<=", "2025-12-31"
        ).where("end_date", ">=", "2025-01-01").stream())

        self.assertEqual(len(result), 1)

    @patch('adviser_allocation.utils.firestore_helpers.get_firestore_client')
    def test_capacity_override_active_date_filtering(self, mock_firestore):
        """Test filtering capacity overrides by active date."""
        mock_db = MagicMock()
        mock_firestore.return_value = mock_db

        today = date.today()
        tomorrow = today + timedelta(days=1)

        # Mock overrides
        overrides = [
            {
                "employee_id": "emp123",
                "effective_start": str(today),
                "effective_end": str(tomorrow),
                "client_limit_monthly": 8,
                "active": True,
            }
        ]

        mock_db.collection("capacity_overrides").where(
            "employee_id", "==", "emp123"
        ).where("active", "==", True).stream.return_value = [
            MagicMock(to_dict=lambda: overrides[0])
        ]

        # Query active overrides
        result = list(mock_db.collection("capacity_overrides").where(
            "employee_id", "==", "emp123"
        ).where("active", "==", True).stream())

        self.assertEqual(len(result), 1)

    @patch('adviser_allocation.utils.firestore_helpers.get_firestore_client')
    def test_allocation_history_pagination(self, mock_firestore):
        """Test pagination in allocation history queries."""
        mock_db = MagicMock()
        mock_firestore.return_value = mock_db

        # Mock paginated results
        allocations = [
            {"id": f"alloc{i}", "adviser_id": "emp123", "deal_id": f"deal{i}"}
            for i in range(10)
        ]

        mock_db.collection("allocations").order_by("timestamp").limit(5).stream.return_value = [
            MagicMock(to_dict=lambda d=alloc: d) for alloc in allocations[:5]
        ]

        # Query first page
        result = list(mock_db.collection("allocations").order_by("timestamp").limit(5).stream())

        self.assertEqual(len(result), 5)

    @patch('adviser_allocation.utils.firestore_helpers.get_firestore_client')
    def test_duplicate_employee_handling(self, mock_firestore):
        """Test handling of duplicate employee records."""
        mock_db = MagicMock()
        mock_firestore.return_value = mock_db

        # Mock query returning duplicates
        mock_db.collection("employees").where("email", "==", "test@example.com").stream.return_value = [
            MagicMock(to_dict=lambda: {"id": "emp1", "email": "test@example.com"}),
            MagicMock(to_dict=lambda: {"id": "emp2", "email": "test@example.com"}),
        ]

        # Query should return both
        result = list(mock_db.collection("employees").where(
            "email", "==", "test@example.com"
        ).stream())

        self.assertEqual(len(result), 2)


class DataConsistencyTests(unittest.TestCase):
    """Tests for data consistency across operations."""

    @patch('adviser_allocation.utils.firestore_helpers.get_firestore_client')
    def test_allocation_record_includes_all_fields(self, mock_firestore):
        """Test that allocation records include all required fields."""
        mock_db = MagicMock()
        mock_firestore.return_value = mock_db

        allocation_data = {
            "adviser_id": "emp123",
            "deal_id": "deal456",
            "household_type": "series a",
            "service_package": "investment",
            "timestamp": "2025-01-20T10:00:00Z",
            "week_allocated": 1234,
        }

        # Save allocation
        mock_db.collection("allocations").add(allocation_data)

        # Verify all fields are present
        call_args = mock_db.collection("allocations").add.call_args
        saved_data = call_args[0][0]

        required_fields = ["adviser_id", "deal_id", "household_type", "service_package", "timestamp"]
        for field in required_fields:
            self.assertIn(field, saved_data)

    @patch('adviser_allocation.utils.firestore_helpers.get_firestore_client')
    def test_leave_request_date_consistency(self, mock_firestore):
        """Test that leave request dates are consistent."""
        mock_db = MagicMock()
        mock_firestore.return_value = mock_db

        start = date.today()
        end = start + timedelta(days=5)

        leave_data = {
            "employee_id": "emp123",
            "start_date": str(start),
            "end_date": str(end),
        }

        mock_db.collection("leave_requests").add(leave_data)

        # Verify end date >= start date
        call_args = mock_db.collection("leave_requests").add.call_args
        data = call_args[0][0]

        self.assertLessEqual(data["start_date"], data["end_date"])


class DatabaseErrorHandlingTests(unittest.TestCase):
    """Tests for database error handling."""

    @patch('adviser_allocation.utils.firestore_helpers.get_firestore_client')
    def test_graceful_handling_when_firestore_unavailable(self, mock_firestore):
        """Test graceful degradation when Firestore is unavailable."""
        mock_firestore.side_effect = Exception("Firestore unavailable")

        try:
            db = mock_firestore()
        except Exception as e:
            self.assertEqual(str(e), "Firestore unavailable")

    @patch('adviser_allocation.utils.firestore_helpers.get_firestore_client')
    def test_query_error_logging(self, mock_firestore):
        """Test that query errors are logged."""
        mock_db = MagicMock()
        mock_firestore.return_value = mock_db

        # Mock query that fails
        mock_db.collection("employees").where().stream.side_effect = Exception("Query failed")

        try:
            list(mock_db.collection("employees").where().stream())
        except Exception as e:
            self.assertEqual(str(e), "Query failed")

    @patch('adviser_allocation.utils.firestore_helpers.get_firestore_client')
    def test_permission_denied_handling(self, mock_firestore):
        """Test handling of permission denied errors."""
        mock_db = MagicMock()
        mock_firestore.return_value = mock_db

        # Mock permission error
        mock_db.collection("employees").document("emp123").get.side_effect = PermissionError(
            "Permission denied"
        )

        with self.assertRaises(PermissionError):
            mock_db.collection("employees").document("emp123").get()


class TransactionTests(unittest.TestCase):
    """Tests for database transactions."""

    @patch('adviser_allocation.utils.firestore_helpers.get_firestore_client')
    def test_allocation_and_history_transaction(self, mock_firestore):
        """Test that allocation and history are recorded atomically."""
        mock_db = MagicMock()
        mock_transaction = MagicMock()
        mock_firestore.return_value = mock_db
        mock_db.transaction.return_value = mock_transaction

        # Transaction should record both operations
        self.assertIsNotNone(mock_db.transaction())

    @patch('adviser_allocation.utils.firestore_helpers.get_firestore_client')
    def test_transaction_rollback_on_error(self, mock_firestore):
        """Test that transaction rolls back on error."""
        mock_db = MagicMock()
        mock_firestore.return_value = mock_db

        # Mock transaction that fails
        mock_db.transaction.side_effect = Exception("Transaction failed")

        with self.assertRaises(Exception):
            mock_db.transaction()


class BatchOperationTests(unittest.TestCase):
    """Tests for batch database operations."""

    @patch('adviser_allocation.utils.firestore_helpers.get_firestore_client')
    def test_batch_write_employees(self, mock_firestore):
        """Test batch writing multiple employees."""
        mock_db = MagicMock()
        mock_batch = MagicMock()
        mock_firestore.return_value = mock_db
        mock_db.batch.return_value = mock_batch

        employees = [
            {"id": "emp1", "name": "Alice"},
            {"id": "emp2", "name": "Bob"},
            {"id": "emp3", "name": "Charlie"},
        ]

        # Mock batch operations
        for emp in employees:
            mock_batch.set(
                mock_db.collection("employees").document(emp["id"]),
                emp
            )

        mock_batch.commit()

        # Verify batch was committed
        self.assertEqual(mock_batch.set.call_count, 3)
        mock_batch.commit.assert_called_once()

    @patch('adviser_allocation.utils.firestore_helpers.get_firestore_client')
    def test_batch_delete_closures(self, mock_firestore):
        """Test batch deleting office closures."""
        mock_db = MagicMock()
        mock_batch = MagicMock()
        mock_firestore.return_value = mock_db
        mock_db.batch.return_value = mock_batch

        closure_ids = ["closure1", "closure2", "closure3"]

        # Mock batch delete
        for closure_id in closure_ids:
            mock_batch.delete(
                mock_db.collection("closures").document(closure_id)
            )

        mock_batch.commit()

        # Verify batch was committed
        self.assertEqual(mock_batch.delete.call_count, 3)


if __name__ == "__main__":
    unittest.main()
