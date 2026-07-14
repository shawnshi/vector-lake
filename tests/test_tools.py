import unittest
from unittest.mock import patch, MagicMock
from pathlib import Path

from vector_lake.tool_doctor import doctor_vector_lake
from vector_lake.tool_debt import debt_vector_lake

class TestTools(unittest.TestCase):
    @patch('vector_lake.tool_doctor.get_wiki_dir')
    @patch('vector_lake.tool_doctor.get_raw_dir')
    @patch('vector_lake.tool_doctor.get_memory_dir')
    @patch('vector_lake.tool_doctor.get_index_path')
    @patch('vector_lake.tool_doctor.get_meta_dir')
    @patch('vector_lake.tool_doctor.get_db_path')
    def test_doctor_vector_lake(self, mock_db, mock_meta, mock_idx, mock_mem, mock_raw, mock_wiki):
        for mock_path in [mock_wiki, mock_raw, mock_mem, mock_idx, mock_meta, mock_db]:
            m = MagicMock()
            m.exists.return_value = True
            m.__str__.return_value = "/mock/path"
            mock_path.return_value = m

        output = doctor_vector_lake()
        self.assertIn("=== Vector Lake Doctor ===", output)
        self.assertIn("[OK] Wiki:", output)
        self.assertIn("MCP Server: Import OK", output)

    @patch('vector_lake.governance_metrics.compute_debt_metrics')
    @patch('vector_lake.governance_metrics.find_merge_candidates')
    def test_debt_vector_lake(self, mock_find, mock_calc):
        mock_calc.return_value = {
            "stale_claim_count": 0,
            "expired_claim_count": 5,
            "pending_governance_item_count": 10,
            "validity_state_counts": {"active": 100}
        }
        mock_find.return_value = []

        output = debt_vector_lake(top=5)
        self.assertIn("=== Vector Lake Debt Dashboard ===", output)
        self.assertIn("expired_claim_count: 5", output)
        self.assertIn("pending_governance_item_count: 10", output)

if __name__ == '__main__':
    unittest.main()
