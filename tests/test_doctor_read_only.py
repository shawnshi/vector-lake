import json
from pathlib import Path
from datetime import datetime, timezone

import pytest

from vector_lake import db_store
from vector_lake.runtime_health import assess_runtime_health
from vector_lake import tool_doctor
from vector_lake.tool_doctor import doctor_vector_lake
from vector_lake.wiki_utils import peek_meta_dir
from vector_lake.tool_projection import projection_diff_report


def test_read_only_diagnostics_do_not_create_meta_state(isolated_memory):
    expected_meta = isolated_memory / "wiki" / ".meta"
    expected_db = expected_meta / "vector_lake.db"
    assert expected_meta.exists() is False

    assert peek_meta_dir() == expected_meta
    assert db_store.peek_db_path() == expected_db
    schema = db_store.inspect_schema_migration_state()
    health = assess_runtime_health()
    doctor = doctor_vector_lake()
    projection_report = projection_diff_report()

    assert schema["status"] == "missing"
    assert schema["issues"] == ["database_missing"]
    assert health["status"] == "blocked"
    assert "database_blocked:database_missing:" in health["issues"][0]
    assert "[WARN] Gemini Embedding: Unavailable" in doctor
    assert "[FAIL] Schema Migrations:" in doctor
    assert "database_missing" in doctor
    assert "SQLite canonical entities: 0" in projection_report
    assert expected_meta.exists() is False
    assert expected_db.exists() is False


def test_checkpointed_read_only_snapshot_rejects_pending_wal_without_touching(
    isolated_memory,
):
    db_store.init_db()
    db_path = db_store.get_db_path()
    db_store.close_all_connections()
    wal_path = Path(str(db_path) + "-wal")
    wal_path.write_bytes(b"pending-frame-sentinel")
    before = wal_path.read_bytes()

    with pytest.raises(
        db_store.ReadOnlySnapshotUnavailable,
        match="uncheckpointed_wal",
    ):
        with db_store.checkpointed_read_only_snapshot(db_path):
            pass

    assert wal_path.read_bytes() == before


def test_doctor_uses_immutable_snapshot_for_closed_database(
    isolated_memory,
):
    db_store.init_db()
    db_path = db_store.get_db_path()
    db_store.close_all_connections()
    paths = [
        db_path,
        Path(str(db_path) + "-wal"),
        Path(str(db_path) + "-shm"),
    ]

    def identity(path):
        if not path.exists():
            return (False, 0, 0)
        stat = path.stat()
        return (True, stat.st_size, stat.st_mtime_ns)

    before = {path: identity(path) for path in paths}
    report = doctor_vector_lake()
    after = {path: identity(path) for path in paths}

    assert "[OK] Schema Migrations:" in report
    assert before == after


def test_dependency_probe_does_not_execute_installed_module(monkeypatch):
    observed = []

    def find_spec(module_name):
        observed.append(module_name)
        return object()

    monkeypatch.setattr(tool_doctor.importlib.util, "find_spec", find_spec)
    monkeypatch.setattr(
        tool_doctor.importlib,
        "import_module",
        lambda module_name: (_ for _ in ()).throw(
            AssertionError(f"unexpected import: {module_name}")
        ),
    )

    assert tool_doctor._dependency_available("sqlite_vec") is True
    assert observed == ["sqlite_vec"]


def test_schema_readiness_reasons_explain_migratable_older_schema():
    reasons = tool_doctor._schema_readiness_reasons(
        {
            "ready": False,
            "status": "invalid",
            "user_version": 4,
            "supported_version": 5,
            "issues": [],
        }
    )

    assert reasons == ["database_schema_upgrade_required:4->5"]


def test_doctor_rejects_stale_watchdog_component(
    isolated_memory,
    monkeypatch,
):
    db_store.init_db()
    index_path = isolated_memory / "wiki" / "index.json"
    index_path.write_text(
        json.dumps({"nodes": {}, "graph_state": {"dirty": False}}),
        encoding="utf-8",
    )
    now = datetime.now(timezone.utc).isoformat()
    status_path = isolated_memory / "wiki" / ".meta" / ".watchdog_status.json"
    status_path.write_text(
        json.dumps(
            {
                "status": "idle",
                "current_action": "waiting",
                "updated_at": now,
                "components": {
                    "scheduler": {
                        "status": "idle",
                        "heartbeat_at": "2000-01-01T00:00:00+00:00",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(
        "VECTOR_LAKE_WATCHDOG_COMPONENT_MAX_AGE_SECONDS",
        "30",
    )

    doctor = doctor_vector_lake()

    watchdog_line = next(
        line for line in doctor.splitlines() if "Watchdog Status:" in line
    )
    assert watchdog_line.startswith("[FAIL]")
    assert "stale=scheduler" in watchdog_line


def test_doctor_rejects_stopped_watchdog_component(isolated_memory):
    db_store.init_db()
    now = datetime.now(timezone.utc).isoformat()
    status_path = isolated_memory / "wiki" / ".meta" / ".watchdog_status.json"
    status_path.write_text(
        json.dumps(
            {
                "status": "idle",
                "updated_at": now,
                "components": {
                    "ingest": {
                        "status": "stopped",
                        "heartbeat_at": now,
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    doctor = doctor_vector_lake()
    watchdog_line = next(
        line for line in doctor.splitlines() if "Watchdog Status:" in line
    )

    assert watchdog_line.startswith("[FAIL]")
    assert "unhealthy=ingest" in watchdog_line
