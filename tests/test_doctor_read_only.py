import json
from datetime import datetime, timezone

from vector_lake import db_store
from vector_lake.runtime_health import assess_runtime_health
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
