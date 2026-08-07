import unittest
import threading
from unittest.mock import patch

from vector_lake import cli_app


def test_cli_heavy_task_busy_returns_temporary_failure(
    isolated_memory,
    monkeypatch,
    capsys,
):
    from vector_lake.heavy_task_gate import heavy_task

    acquired = threading.Event()
    release = threading.Event()

    def hold_gate():
        with heavy_task(
            "maintenance",
            "external-holder",
            origin="pytest",
            wait_timeout_seconds=0,
        ):
            acquired.set()
            release.wait(timeout=2)

    holder = threading.Thread(target=hold_gate, name="cli-gate-holder")
    holder.start()
    assert acquired.wait(timeout=2)
    monkeypatch.setenv("VECTOR_LAKE_CLI_HEAVY_TASK_WAIT_SECONDS", "0.05")
    try:
        with (
            patch("sys.argv", ["cli.py", "doctor"]),
            patch.object(cli_app.tools, "doctor_vector_lake") as doctor,
        ):
            assert cli_app.main() == 75
        doctor.assert_not_called()
    finally:
        release.set()
        holder.join(timeout=2)

    assert not holder.is_alive()
    assert '"error": "heavy_task_busy"' in capsys.readouterr().err

class TestCLI(unittest.TestCase):
    @patch('vector_lake.tools.doctor_vector_lake')
    def test_doctor_command(self, mock_doctor):
        mock_doctor.return_value = "Healthy"
        with patch('sys.argv', ['cli.py', 'doctor']):
            result = cli_app.main()
        self.assertEqual(result, 0)
        mock_doctor.assert_called_once()

    @patch('vector_lake.tools.lint_vector_lake')
    def test_lint_command(self, mock_lint):
        mock_lint.return_value = "Lint Passed"
        with patch('sys.argv', ['cli.py', 'lint']):
            result = cli_app.main()
        self.assertEqual(result, 0)
        mock_lint.assert_called_once_with(False)

    @patch('vector_lake.tools.search_vector_lake')
    def test_search_command(self, mock_search):
        mock_search.return_value = "Search Results"
        with patch('sys.argv', ['cli.py', 'search', 'test_query', '--top_k', '3']):
            result = cli_app.main()
        self.assertEqual(result, 0)
        mock_search.assert_called_once_with('test_query', 3, domain=None, cluster=None, include_history=False, mode='page')

    @patch('vector_lake.tools.prepare_query_context')
    def test_query_command(self, mock_query):
        mock_query.return_value = "Query Completed"
        with patch('sys.argv', ['cli.py', 'query', 'test_question', '--dry-run']):
            result = cli_app.main()
        self.assertEqual(result, 0)
        mock_query.assert_called_once_with('test_question', True)

if __name__ == '__main__':
    unittest.main()
