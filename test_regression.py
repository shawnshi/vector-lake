import sys
from unittest import mock

sys.modules['sqlite_vec'] = mock.MagicMock()

import vector_lake.governance_store
vector_lake.governance_store.initialize_meta_store = mock.MagicMock()
vector_lake.governance_store.get_connection = mock.MagicMock()
conn_mock = mock.MagicMock()
vector_lake.governance_store.get_connection.return_value = conn_mock

# Mock the select queries to return a mocked cursor with fetchone
class MockCursor:
    def __iter__(self):
        return iter([])
    def fetchone(self):
        return [0]

conn_mock.execute.return_value = MockCursor()

from vector_lake.governance_metrics import compute_debt_metrics

if __name__ == "__main__":
    try:
        metrics = compute_debt_metrics(skip_heavy=True)
        print("Success:", metrics.keys())
    except Exception as e:
        import traceback
        traceback.print_exc()
        sys.exit(1)
