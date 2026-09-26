"""The CLI and MCP entry points for the two bounded repair paths.

Both repairs are non-destructive by default and mutate only when explicitly told
to, so the surface contract under test is: the default reports the plan and changes
nothing, the documented flag is the only route to the mutating branch, and the text
says which of the two happened.

The CLI assertions go through a mocked ``vector_lake.tools`` (the pattern
``test_cli.py`` already uses) because ``cli_app._configure_stdout`` rebinds
``sys.stdout`` to a fresh wrapper around ``sys.stdout.buffer``.  The text itself is
asserted against ``tools`` / ``mcp_server`` directly.
"""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from vector_lake import cli_app, db_store, mcp_server, tools
from vector_lake.backup_retention import prune_backups

_OUTBOX_INDEX = "idx_mutation_outbox_idempotency"


def _seed_duplicate_history(conn: sqlite3.Connection, keys=("dup", "other")):
    conn.execute(f"DROP INDEX IF EXISTS {_OUTBOX_INDEX}")
    conn.execute(f"DROP INDEX IF EXISTS {_OUTBOX_INDEX}_active")
    now = datetime.now(timezone.utc).isoformat()
    for key in keys:
        for index in range(2):
            conn.execute(
                "INSERT INTO mutation_outbox "
                "(filename, mutation_type, status, attempt_count, created_at, available_at, "
                "idempotency_key) VALUES (?, 'update', 'completed', 0, ?, ?, ?)",
                (f"Page{index}.md", now, now, key),
            )
    conn.commit()


def _backup_root(memory_dir: Path) -> Path:
    return memory_dir / "wiki" / ".meta" / "backups"


def _seed_backups(memory_dir: Path, count: int = 4) -> Path:
    root = _backup_root(memory_dir)
    root.mkdir(parents=True, exist_ok=True)
    for index in range(count):
        path = root / f"vector_lake_{index}.db.bak"
        path.write_bytes(b"x" * 1024)
        for suffix in ("-wal", "-shm"):
            path.with_name(path.name + suffix).write_bytes(b"")
    return root


# --- CLI wiring --------------------------------------------------------------


@patch("vector_lake.tools.backup_retention_report")
def test_cli_backup_retention_defaults_to_a_dry_run(mock_report):
    mock_report.return_value = "report"
    with patch("sys.argv", ["cli.py", "backup-retention", "--keep", "2"]):
        assert cli_app.main() == 0
    mock_report.assert_called_once_with(keep=2, max_bytes=0, dry_run=True)


@patch("vector_lake.tools.backup_retention_report")
def test_cli_backup_retention_apply_switches_off_the_dry_run(mock_report):
    mock_report.return_value = "report"
    with patch("sys.argv", ["cli.py", "backup-retention", "--keep", "2", "--apply"]):
        assert cli_app.main() == 0
    mock_report.assert_called_once_with(keep=2, max_bytes=0, dry_run=False)


@patch("vector_lake.tools.backup_retention_report")
def test_cli_backup_retention_forwards_the_byte_budget(mock_report):
    mock_report.return_value = "report"
    with patch("sys.argv", ["cli.py", "backup-retention", "--max-bytes", "4096"]):
        assert cli_app.main() == 0
    mock_report.assert_called_once_with(keep=0, max_bytes=4096, dry_run=True)


@patch("vector_lake.tools.idempotency_index_report")
def test_cli_idempotency_status_invokes_the_report(mock_report):
    mock_report.return_value = "report"
    with patch("sys.argv", ["cli.py", "idempotency-status"]):
        assert cli_app.main() == 0
    mock_report.assert_called_once_with()


@patch("vector_lake.tools.repair_idempotency_keys")
def test_cli_repair_idempotency_defaults_to_a_dry_run(mock_repair):
    mock_repair.return_value = "report"
    with patch("sys.argv", ["cli.py", "repair-idempotency"]):
        assert cli_app.main() == 0
    mock_repair.assert_called_once_with(table="mutation_outbox", dry_run=True)


@patch("vector_lake.tools.repair_idempotency_keys")
def test_cli_repair_idempotency_apply_and_table_are_forwarded(mock_repair):
    mock_repair.return_value = "report"
    with patch("sys.argv", ["cli.py", "repair-idempotency", "--table", "jobs", "--apply"]):
        assert cli_app.main() == 0
    mock_repair.assert_called_once_with(table="jobs", dry_run=False)


def test_cli_repair_idempotency_rejects_an_unknown_table():
    with patch("sys.argv", ["cli.py", "repair-idempotency", "--table", "claims"]):
        with pytest.raises(SystemExit):
            cli_app.main()


# --- backup retention behaviour and text ------------------------------------


def test_backup_retention_report_defaults_to_a_dry_run(isolated_memory):
    root = _seed_backups(isolated_memory)

    report = tools.backup_retention_report(keep=2)

    assert "=== Backup Retention ===" in report
    assert "[DRY RUN]" in report
    assert "REMOVE" in report
    assert len(list(root.glob("*.db.bak"))) == 4, "the dry run removed something"


def test_backup_retention_report_applies_only_past_the_bound(isolated_memory):
    root = _seed_backups(isolated_memory)

    report = tools.backup_retention_report(keep=2, dry_run=False)

    assert "[APPLIED]" in report
    assert "The newest copy is always kept." in report
    assert len(list(root.glob("*.db.bak"))) == 2


def test_backup_retention_report_says_so_when_there_is_nothing_to_do(isolated_memory):
    _seed_backups(isolated_memory, count=1)

    report = tools.backup_retention_report(keep=3)

    assert "Nothing to remove" in report
    assert "[DRY RUN]" not in report


def test_backup_retention_report_lists_unrecognised_files_as_never_pruned(isolated_memory):
    root = _seed_backups(isolated_memory, count=1)
    (root / "do-not-delete.txt").write_text("mine", encoding="utf-8")

    report = tools.backup_retention_report()

    assert "unrecognised, never pruned: do-not-delete.txt" in report
    assert (root / "do-not-delete.txt").exists()


def test_backup_retention_refuses_an_entry_outside_the_root(isolated_memory, tmp_path):
    _seed_backups(isolated_memory, count=1)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "data.bin").write_bytes(b"x")

    result = prune_backups(_backup_root(isolated_memory), keep_count=1, max_bytes=0, dry_run=False)

    # Only what the scanner produced can ever be removed.
    assert result["failures"] == []
    assert (outside / "data.bin").exists()


# --- idempotency behaviour and text -----------------------------------------


def test_idempotency_status_reports_the_degraded_level(isolated_memory):
    db_store.init_db()
    conn = db_store.get_connection()
    _seed_duplicate_history(conn)
    db_store._ensure_idempotency_index(conn, "mutation_outbox", _OUTBOX_INDEX)

    report = tools.idempotency_index_report()

    assert "mutation_outbox: uniqueness=active duplicate_groups=2" in report
    assert "jobs: uniqueness=full duplicate_groups=0" in report
    assert "cli.py repair-idempotency --table mutation_outbox" in report


def test_idempotency_status_is_clean_on_a_fresh_database(isolated_memory):
    report = tools.idempotency_index_report()

    assert "mutation_outbox: uniqueness=full duplicate_groups=0" in report
    assert "jobs: uniqueness=full duplicate_groups=0" in report
    assert "unique forever" in report


def test_repair_idempotency_defaults_to_a_dry_run(isolated_memory):
    db_store.init_db()
    conn = db_store.get_connection()
    _seed_duplicate_history(conn)
    before = conn.execute("SELECT id, idempotency_key FROM mutation_outbox ORDER BY id").fetchall()

    report = tools.repair_idempotency_keys(table="mutation_outbox")

    assert "[DRY RUN]" in report
    assert "redundant rows: 2" in report
    assert "are not deleted" in report
    after = conn.execute("SELECT id, idempotency_key FROM mutation_outbox ORDER BY id").fetchall()
    assert [tuple(row) for row in after] == [tuple(row) for row in before]


def test_repair_idempotency_apply_reclaims_the_full_index_without_deleting_rows(isolated_memory):
    db_store.init_db()
    conn = db_store.get_connection()
    _seed_duplicate_history(conn)

    report = tools.repair_idempotency_keys(table="mutation_outbox", dry_run=False)

    assert "[APPLIED] No rows deleted." in report
    assert "uniqueness: absent -> full" in report
    assert conn.execute("SELECT COUNT(*) FROM mutation_outbox").fetchone()[0] == 4
    assert db_store.idempotency_index_state()["mutation_outbox"]["uniqueness"] == "full"


def test_repair_idempotency_says_so_when_there_is_nothing_to_repair(isolated_memory):
    db_store.init_db()

    report = tools.repair_idempotency_keys(table="jobs", dry_run=False)

    assert "Nothing to repair" in report or "redundant rows: 0" in report


def test_repair_idempotency_reports_an_unknown_table_without_raising(isolated_memory):
    report = tools.repair_idempotency_keys(table="not_a_table")

    assert report.startswith("Error: unknown idempotency table")
    assert "mutation_outbox, jobs" in report


# --- MCP surface -------------------------------------------------------------


def test_mcp_backup_retention_is_a_dry_run_by_default(isolated_memory):
    root = _seed_backups(isolated_memory)

    report = mcp_server.backup_retention_report(keep=1)

    assert "[DRY RUN]" in report
    assert len(list(root.glob("*.db.bak"))) == 4

    applied = mcp_server.backup_retention_report(keep=1, dry_run=False)
    assert "[APPLIED]" in applied
    assert len(list(root.glob("*.db.bak"))) == 1


def test_mcp_repair_idempotency_is_a_dry_run_by_default(isolated_memory):
    db_store.init_db()
    conn = db_store.get_connection()
    _seed_duplicate_history(conn, keys=("dup",))

    dry = mcp_server.repair_idempotency_keys(table="mutation_outbox")
    assert "[DRY RUN]" in dry
    assert conn.execute("SELECT COUNT(*) FROM mutation_outbox").fetchone()[0] == 2

    applied = mcp_server.repair_idempotency_keys(table="mutation_outbox", dry_run=False)
    assert "[APPLIED] No rows deleted." in applied
    assert conn.execute("SELECT COUNT(*) FROM mutation_outbox").fetchone()[0] == 2
    assert db_store.idempotency_index_state()["mutation_outbox"]["uniqueness"] == "full"


def test_mcp_idempotency_status_matches_the_tools_facade(isolated_memory):
    assert mcp_server.idempotency_index_status() == tools.idempotency_index_report()


# --- surface registration ----------------------------------------------------


def test_both_repairs_are_registered_on_every_surface():
    for tool in (
        "backup_retention_report",
        "idempotency_index_status",
        "repair_idempotency_keys",
    ):
        assert hasattr(mcp_server, tool), f"{tool} is missing from mcp_server module"
        assert callable(getattr(mcp_server, tool))

    for exported in (
        "backup_retention_report",
        "idempotency_index_report",
        "repair_idempotency_keys",
        "IDEMPOTENCY_TABLES",
    ):
        assert exported in tools.__all__, f"{exported} is missing from the tools facade"
        assert hasattr(tools, exported)

    commands = cli_app.build_parser()._subparsers._group_actions[0].choices
    for command in ("backup-retention", "idempotency-status", "repair-idempotency"):
        assert command in commands, f"{command} is missing from the CLI surface"


def test_the_underlying_functions_stay_non_destructive_by_default():
    """No surface may reach a mutating branch by accident."""
    import inspect

    from vector_lake import backup_retention

    assert inspect.signature(db_store.repair_idempotency_keys).parameters["dry_run"].default is True
    assert inspect.signature(backup_retention.prune_backups).parameters["dry_run"].default is True
    assert inspect.signature(tools.repair_idempotency_keys).parameters["dry_run"].default is True
    assert inspect.signature(tools.backup_retention_report).parameters["dry_run"].default is True
    assert mcp_server.backup_retention_report.__defaults__ == (0, 0, True)
    assert mcp_server.repair_idempotency_keys.__defaults__ == ("mutation_outbox", True)
