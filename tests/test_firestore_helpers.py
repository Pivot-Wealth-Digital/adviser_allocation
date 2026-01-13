"""Tests for Firestore helpers."""

import unittest
from unittest.mock import patch, MagicMock
from typing import Dict, List, Any

from utils.firestore_helpers import (
    get_employee_leaves,
    get_employee_id,
    get_global_closures,
    get_capacity_overrides,
    save_office_closure,
    delete_office_closure,
)


class FirestoreHelpersTests(unittest.TestCase):
    """Test suite for Firestore helper functions."""

    @patch("utils.firestore_helpers._client")
    def test_get_employee_leaves_success(self, mock_client):
        """Test successful retrieval of employee leaves."""
        mock_db = MagicMock()
        mock_client.return_value = mock_db

        # Mock Firestore response
        mock_doc1 = MagicMock()
        mock_doc1.to_dict.return_value = {
            "start_date": "2025-01-01",
            "end_date": "2025-01-03",
            "type": "Full",
        }
        mock_doc2 = MagicMock()
        mock_doc2.to_dict.return_value = {
            "start_date": "2025-01-06",
            "end_date": "2025-01-06",
            "type": "Partial: 1",
        }

        mock_collection = MagicMock()
        mock_collection.stream.return_value = [mock_doc1, mock_doc2]

        mock_db.collection.return_value.document.return_value.collection.return_value = mock_collection

        leaves = get_employee_leaves("emp_123")

        self.assertEqual(len(leaves), 2)
        self.assertEqual(leaves[0]["type"], "Full")
        self.assertEqual(leaves[1]["type"], "Partial: 1")

    @patch("utils.firestore_helpers._client")
    def test_get_employee_leaves_no_db(self, mock_client):
        """Test employee leaves when database is unavailable."""
        mock_client.return_value = None

        leaves = get_employee_leaves("emp_123")

        self.assertEqual(leaves, [])

    @patch("utils.firestore_helpers._client")
    def test_get_employee_id_success(self, mock_client):
        """Test successful employee ID lookup."""
        mock_db = MagicMock()
        mock_client.return_value = mock_db

        mock_doc = MagicMock()
        mock_doc.id = "emp_123"

        mock_query = MagicMock()
        mock_query.stream.return_value = [mock_doc]

        mock_db.collection.return_value.where.return_value = mock_query

        result = get_employee_id("test@example.com")

        self.assertEqual(result, "emp_123")

    @patch("utils.firestore_helpers._client")
    def test_get_employee_id_not_found(self, mock_client):
        """Test employee ID lookup when email not found."""
        mock_db = MagicMock()
        mock_client.return_value = mock_db

        mock_query = MagicMock()
        mock_query.stream.return_value = []

        mock_db.collection.return_value.where.return_value = mock_query

        result = get_employee_id("nonexistent@example.com")

        self.assertIsNone(result)

    @patch("utils.firestore_helpers._client")
    def test_get_global_closures_success(self, mock_client):
        """Test successful retrieval of office closures."""
        mock_db = MagicMock()
        mock_client.return_value = mock_db

        mock_doc1 = MagicMock()
        mock_doc1.to_dict.return_value = {
            "start_date": "2025-12-20",
            "end_date": "2025-12-31",
        }
        mock_doc2 = MagicMock()
        mock_doc2.to_dict.return_value = {
            "start_date": "2025-01-01",
            "end_date": "2025-01-01",
        }

        mock_db.collection.return_value.stream.return_value = [mock_doc1, mock_doc2]

        closures = get_global_closures()

        self.assertEqual(len(closures), 2)
        self.assertEqual(closures[0]["start_date"], "2025-12-20")
        self.assertEqual(closures[0]["end_date"], "2025-12-31")

    @patch("utils.firestore_helpers._client")
    def test_get_capacity_overrides_success(self, mock_client):
        """Test successful retrieval of capacity overrides."""
        mock_db = MagicMock()
        mock_client.return_value = mock_db

        mock_doc = MagicMock()
        mock_doc.id = "override_1"
        mock_doc.to_dict.return_value = {
            "adviser_email": "adviser@example.com",
            "client_limit_monthly": 8,
            "effective_date": "2025-01-01",
        }

        mock_db.collection.return_value.stream.return_value = [mock_doc]

        overrides = get_capacity_overrides()

        self.assertEqual(len(overrides), 1)
        self.assertEqual(overrides[0]["adviser_email"], "adviser@example.com")
        self.assertEqual(overrides[0]["id"], "override_1")

    @patch("utils.firestore_helpers._client")
    def test_save_office_closure_success(self, mock_client):
        """Test saving an office closure."""
        mock_db = MagicMock()
        mock_client.return_value = mock_db

        mock_doc_ref = MagicMock()
        mock_doc_ref.id = "closure_123"

        mock_db.collection.return_value.document.return_value = mock_doc_ref

        result = save_office_closure("2025-12-20", "2025-12-31")

        self.assertEqual(result, "closure_123")
        mock_doc_ref.set.assert_called_once()

    @patch("utils.firestore_helpers._client")
    def test_save_office_closure_single_day(self, mock_client):
        """Test saving single-day office closure."""
        mock_db = MagicMock()
        mock_client.return_value = mock_db

        mock_doc_ref = MagicMock()
        mock_db.collection.return_value.document.return_value = mock_doc_ref

        save_office_closure("2025-01-01")

        # Verify that end_date defaults to start_date
        call_args = mock_doc_ref.set.call_args
        self.assertEqual(call_args[0][0]["start_date"], "2025-01-01")
        self.assertEqual(call_args[0][0]["end_date"], "2025-01-01")

    @patch("utils.firestore_helpers._client")
    def test_save_office_closure_no_db(self, mock_client):
        """Test saving closure when database unavailable."""
        mock_client.return_value = None

        result = save_office_closure("2025-12-20", "2025-12-31")

        self.assertIsNone(result)

    @patch("utils.firestore_helpers._client")
    def test_delete_office_closure_success(self, mock_client):
        """Test deleting an office closure."""
        mock_db = MagicMock()
        mock_client.return_value = mock_db

        mock_doc_ref = MagicMock()
        mock_db.collection.return_value.document.return_value = mock_doc_ref

        result = delete_office_closure("closure_123")

        self.assertTrue(result)
        mock_doc_ref.delete.assert_called_once()

    @patch("utils.firestore_helpers._client")
    def test_delete_office_closure_no_db(self, mock_client):
        """Test deleting closure when database unavailable."""
        mock_client.return_value = None

        result = delete_office_closure("closure_123")

        self.assertFalse(result)


if __name__ == "__main__":
    unittest.main()
