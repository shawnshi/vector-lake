"""The handoff-expiry sweep must not create an unrecoverable write block.

``expire_stale_subagent_jobs`` increments ``retries`` on every hourly pass and
used to write no ``result_json``.  Runtime Health blocks every ordinary write when
``status = 'failed' AND retries >= 3`` unless the row carries an exact quarantine
marker, so after three sweeps a job that had merely been waiting for a subagent
that never ran became a permanent block that no entry point could clear:
``retry_terminal_ingest_job`` rejects it (not a legacy runner error),
``reopen_provenance_only`` rejects it (no seed-marked page), and
``reconcile_ingest_job_debt``'s ``supersede_duplicate`` rejects it (no duplicate).
"""

from __future__ import annotations

import json

from vector_lake import db_store


def _enqueue_stale_awaiting(
    isolated_memory, job_id: str, *, retries: int = 0
) -> None:
    conn = db_store.get_connection()
    conn.execute(
        "INSERT INTO jobs(job_id, task_type, payload, status, retries, "
        "created_at, updated_at) VALUES (?, 'ingest', '{}', 'awaiting_subagent', "
        "?, '2000-01-01T00:00:00+00:00', '2000-01-01T00:00:00+00:00')",
        (job_id, retries),
    )
    conn.commit()


def _terminal_row(job_id: str):
    return db_store.get_connection().execute(
        "SELECT status, retries, error_msg, result_json FROM jobs WHERE job_id = ?",
        (job_id,),
    ).fetchone()


def test_expiry_writes_the_quarantine_marker(isolated_memory):
    db_store.init_db()
    _enqueue_stale_awaiting(isolated_memory, "expire-a")

    assert db_store.expire_stale_subagent_jobs(max_age_seconds=1) == 1

    row = _terminal_row("expire-a")
    assert row["status"] == "failed"
    assert row["error_msg"] == db_store._INGEST_HANDOFF_EXPIRY_ERROR
    marker = json.loads(row["result_json"])
    assert marker["maintenance"] == db_store._INGEST_HANDOFF_EXPIRY_MAINTENANCE
    assert marker["state"] == "quarantined"
    assert db_store._ingest_result_is_quarantined(row["result_json"]) is True


def test_expired_handoff_is_reviewable_debt_not_a_write_block(isolated_memory):
    db_store.init_db()
    # The live incident reached ``retries = 3`` exactly this way: two failed
    # runner attempts, then the expiry sweep's own increment.
    _enqueue_stale_awaiting(isolated_memory, "expire-b", retries=2)

    assert db_store.expire_stale_subagent_jobs(max_age_seconds=1) == 1
    assert _terminal_row("expire-b")["retries"] == 3

    from vector_lake.runtime_health import assess_runtime_health

    health = assess_runtime_health()
    detail = health["detail"]

    # Still counted and still visible, but no longer an unrecoverable block.
    assert detail["terminal_failed_jobs"] == 1
    assert detail["blocking_terminal_failed_jobs"] == 0
    assert detail["auto_ingest_quarantined_jobs"] == 1
    assert "auto_ingest_quarantined_jobs:1" in health["warnings"]
    assert not any(
        issue.startswith("terminal_failed_jobs:") for issue in health["issues"]
    )
    assert detail["auto_ingest_quarantine_recovery"]["preview"]


def test_arbitrary_terminal_failures_still_block(isolated_memory):
    """The marker must stay exact, or the gate would stop protecting anything."""
    from vector_lake.runtime_health import assess_runtime_health

    db_store.init_db()
    conn = db_store.get_connection()
    conn.execute(
        "INSERT INTO jobs(job_id, task_type, payload, status, retries, "
        "created_at, updated_at, error_msg) VALUES "
        "('arbitrary', 'ingest', '{}', 'failed', 3, '2000-01-01T00:00:00+00:00', "
        "'2000-01-01T00:00:00+00:00', 'some real failure')"
    )
    conn.commit()

    health = assess_runtime_health()

    assert health["detail"]["blocking_terminal_failed_jobs"] == 1
    assert "terminal_failed_jobs:1" in health["issues"]
    assert health["ok"] is False


def test_marker_predicate_rejects_near_misses():
    for payload in (
        None,
        "",
        "not json",
        {"maintenance": "auto_ingest_controller"},
        {"maintenance": "auto_ingest_controller", "state": "failed"},
        {"maintenance": "something_else", "state": "quarantined"},
        {"state": "quarantined"},
    ):
        assert db_store._ingest_result_is_quarantined(payload) is False
    for maintenance in db_store._INGEST_QUARANTINE_MAINTENANCE_CLASSES:
        assert db_store._ingest_result_is_quarantined(
            json.dumps({"maintenance": maintenance, "state": "quarantined"})
        )


def test_backfill_is_idempotent_and_narrowly_scoped(isolated_memory):
    from datetime import datetime, timezone

    db_store.init_db()
    conn = db_store.get_connection()
    rows = [
        # (job_id, task_type, status, retries, error_msg, result_json)
        ("legacy-expiry", "ingest", "failed", 3, db_store._INGEST_HANDOFF_EXPIRY_ERROR, None),
        ("already-marked", "ingest", "failed", 3, db_store._INGEST_HANDOFF_EXPIRY_ERROR,
         json.dumps({"maintenance": "auto_ingest_controller", "state": "quarantined"})),
        ("other-error", "ingest", "failed", 3, "different failure", None),
        ("below-threshold", "ingest", "failed", 1, db_store._INGEST_HANDOFF_EXPIRY_ERROR, None),
        ("not-terminal", "ingest", "awaiting_subagent", 3, db_store._INGEST_HANDOFF_EXPIRY_ERROR, None),
        ("other-task", "cleanup", "failed", 3, db_store._INGEST_HANDOFF_EXPIRY_ERROR, None),
    ]
    conn.executemany(
        "INSERT INTO jobs(job_id, task_type, payload, status, retries, created_at, "
        "updated_at, error_msg, result_json) VALUES (?, ?, '{}', ?, ?, "
        "'2000-01-01T00:00:00+00:00', '2000-01-01T00:00:00+00:00', ?, ?)",
        rows,
    )
    conn.commit()

    now_str = datetime.now(timezone.utc).isoformat()
    assert db_store.mark_unquarantined_expired_handoffs(conn, now_str=now_str) == 1
    # Nothing left to do on a second pass.
    assert db_store.mark_unquarantined_expired_handoffs(conn, now_str=now_str) == 0

    assert db_store._ingest_result_is_quarantined(
        _terminal_row("legacy-expiry")["result_json"]
    )
    assert db_store._ingest_result_is_quarantined(
        _terminal_row("already-marked")["result_json"]
    )
    assert _terminal_row("other-error")["result_json"] is None
    assert _terminal_row("below-threshold")["result_json"] is None
    assert _terminal_row("not-terminal")["result_json"] is None
    assert _terminal_row("other-task")["result_json"] is None


def test_backfill_respects_its_limit(isolated_memory):
    from datetime import datetime, timezone

    db_store.init_db()
    conn = db_store.get_connection()
    conn.executemany(
        "INSERT INTO jobs(job_id, task_type, payload, status, retries, created_at, "
        "updated_at, error_msg) VALUES (?, 'ingest', '{}', 'failed', 3, "
        "'2000-01-01T00:00:00+00:00', '2000-01-01T00:00:00+00:00', ?)",
        [(f"bulk-{index}", db_store._INGEST_HANDOFF_EXPIRY_ERROR) for index in range(5)],
    )
    conn.commit()

    assert (
        db_store.mark_unquarantined_expired_handoffs(
            conn, now_str=datetime.now(timezone.utc).isoformat(), limit=2
        )
        == 2
    )
    remaining = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE result_json IS NULL"
    ).fetchone()[0]
    assert remaining == 3
