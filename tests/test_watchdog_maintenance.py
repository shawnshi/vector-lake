from __future__ import annotations

from vector_lake import db_store
from vector_lake.watchdog_app import expire_stale_ingest_jobs_for_watchdog


def test_stale_ingest_jobs_can_be_expired_by_background_maintenance(
    isolated_memory, monkeypatch
):
    db_store.init_db()
    conn = db_store.get_connection()
    old = "2000-01-01T00:00:00+00:00"
    with db_store.transaction():
        conn.execute(
            "INSERT INTO jobs "
            "(job_id, task_type, payload, status, created_at, updated_at, available_at) "
            "VALUES (?, 'ingest', '{}', 'awaiting_subagent', ?, ?, ?)",
            ("job_background_expiry", old, old, old),
        )

    monkeypatch.setenv("VECTOR_LAKE_INGEST_TASK_MAX_AGE_SECONDS", "60")
    expired = expire_stale_ingest_jobs_for_watchdog()

    assert expired == 1
    row = conn.execute(
        "SELECT status FROM jobs WHERE job_id = 'job_background_expiry'"
    ).fetchone()
    assert row["status"] == "failed"
