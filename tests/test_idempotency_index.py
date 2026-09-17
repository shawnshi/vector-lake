"""The uniqueness guarantee each idempotency table actually ends up with.

``enqueue_mutation`` / ``enqueue_job`` SELECT by ``idempotency_key`` and then
INSERT inside one ``BEGIN IMMEDIATE``, so the write lock is what really serialises
them.  The unique index is the layer that survives a caller forgetting the lock,
and it must not disappear just because an older database already holds duplicate
history.
"""

import sqlite3
from datetime import datetime, timezone

import pytest

from vector_lake import db_store

_OUTBOX_INDEX = "idx_mutation_outbox_idempotency"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _seed_duplicate_history(conn: sqlite3.Connection, keys=("dup",), statuses=("completed", "completed")):
    """Write duplicate keys the way an older release could have left them."""
    conn.execute(f"DROP INDEX IF EXISTS {_OUTBOX_INDEX}")
    conn.execute(f"DROP INDEX IF EXISTS {_OUTBOX_INDEX}_active")
    now = _now()
    for key in keys:
        for index, status in enumerate(statuses):
            conn.execute(
                "INSERT INTO mutation_outbox "
                "(filename, mutation_type, status, attempt_count, created_at, available_at, idempotency_key) "
                "VALUES (?, 'update', ?, 0, ?, ?, ?)",
                (f"Page{index}.md", status, now, now, key),
            )
    conn.commit()


def test_clean_database_gets_the_full_unique_index(isolated_memory):
    db_store.init_db()
    conn = db_store.get_connection()

    assert db_store.idempotency_index_state()["mutation_outbox"] == {
        "uniqueness": "full",
        "duplicate_groups": 0,
    }

    now = _now()
    conn.execute(
        "INSERT INTO mutation_outbox (filename, mutation_type, status, created_at, idempotency_key) "
        "VALUES ('A.md', 'update', 'pending', ?, 'k1')",
        (now,),
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO mutation_outbox (filename, mutation_type, status, created_at, idempotency_key) "
            "VALUES ('B.md', 'update', 'pending', ?, 'k1')",
            (now,),
        )
    conn.rollback()


def test_duplicate_history_degrades_to_the_non_terminal_index(isolated_memory):
    db_store.init_db()
    conn = db_store.get_connection()
    _seed_duplicate_history(conn)

    level = db_store._ensure_idempotency_index(conn, "mutation_outbox", _OUTBOX_INDEX)

    assert level == "active"
    assert db_store.idempotency_index_state()["mutation_outbox"]["uniqueness"] == "active"

    # A concurrent enqueue is the case that has to stay impossible: at most one
    # non-terminal row may carry a key.  The first one is allowed because the
    # terminal history above is deliberately outside the partial index.
    conn.execute(
        "INSERT INTO mutation_outbox (filename, mutation_type, status, created_at, idempotency_key) "
        "VALUES ('C.md', 'update', 'pending', ?, 'dup')",
        (_now(),),
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO mutation_outbox (filename, mutation_type, status, created_at, idempotency_key) "
            "VALUES ('C2.md', 'update', 'processing', ?, 'dup')",
            (_now(),),
        )
    conn.rollback()

    # Terminal history is still allowed to repeat, which is what makes the
    # degradation non-destructive.
    conn.execute(
        "INSERT INTO mutation_outbox (filename, mutation_type, status, created_at, idempotency_key) "
        "VALUES ('D.md', 'update', 'completed', ?, 'dup')",
        (_now(),),
    )
    conn.commit()


def test_in_flight_duplicates_leave_no_index_at_all(isolated_memory):
    db_store.init_db()
    conn = db_store.get_connection()
    _seed_duplicate_history(conn, statuses=("pending", "pending"))

    assert db_store._ensure_idempotency_index(conn, "mutation_outbox", _OUTBOX_INDEX) == "absent"
    assert db_store.idempotency_index_state()["mutation_outbox"]["uniqueness"] == "absent"


def test_repair_dry_run_reports_without_touching_rows(isolated_memory):
    db_store.init_db()
    conn = db_store.get_connection()
    _seed_duplicate_history(conn, keys=("dup", "other"))
    before = conn.execute("SELECT id, idempotency_key FROM mutation_outbox ORDER BY id").fetchall()

    result = db_store.repair_idempotency_keys("mutation_outbox", dry_run=True)

    assert result["dry_run"] is True
    assert result["duplicate_groups_before"] == 2
    assert result["redundant_rows"] == 2
    assert result["uniqueness_before"] == "absent"
    assert result["uniqueness_after"] == "absent"
    after = conn.execute("SELECT id, idempotency_key FROM mutation_outbox ORDER BY id").fetchall()
    assert [tuple(row) for row in after] == [tuple(row) for row in before]


def test_repair_keeps_the_canonical_row_and_reclaims_the_full_index(isolated_memory):
    db_store.init_db()
    conn = db_store.get_connection()
    _seed_duplicate_history(conn, keys=("dup", "other"))
    canonical = {
        str(row["idempotency_key"]): int(row["id"])
        for row in conn.execute(
            "SELECT MIN(id) AS id, idempotency_key FROM mutation_outbox "
            "WHERE idempotency_key IS NOT NULL GROUP BY idempotency_key"
        )
    }

    result = db_store.repair_idempotency_keys("mutation_outbox", dry_run=False)

    assert result["uniqueness_after"] == "full"
    assert result["duplicate_groups_after"] == 0
    rows = {
        int(row["id"]): (row["idempotency_key"], row["status"])
        for row in conn.execute("SELECT id, idempotency_key, status FROM mutation_outbox")
    }
    # No row was deleted, and the canonical row for each key keeps the key.
    assert len(rows) == 4
    for key, row_id in canonical.items():
        assert rows[row_id][0] == key
    # Every redundant row survives, with its status intact and only the key cleared.
    assert sorted(status for _key, status in rows.values()) == [
        "completed",
        "completed",
        "completed",
        "completed",
    ]
    assert db_store.idempotency_index_state()["mutation_outbox"]["duplicate_groups"] == 0


def test_enqueue_mutation_still_returns_the_row_the_repair_keeps(isolated_memory):
    db_store.init_db()
    conn = db_store.get_connection()
    _seed_duplicate_history(conn)
    canonical_id = conn.execute(
        "SELECT MIN(id) FROM mutation_outbox WHERE idempotency_key = 'dup'"
    ).fetchone()[0]

    db_store.repair_idempotency_keys("mutation_outbox", dry_run=False)

    assert db_store.enqueue_mutation("X.md", "update", idempotency_key="dup") == canonical_id


def test_repair_rejects_an_unknown_table(isolated_memory):
    db_store.init_db()
    with pytest.raises(ValueError, match="Unsupported idempotency table"):
        db_store.repair_idempotency_keys("claims", dry_run=True)


def test_doctor_reports_the_degraded_uniqueness_level(isolated_memory):
    from vector_lake import tool_doctor

    db_store.init_db()
    conn = db_store.get_connection()
    _seed_duplicate_history(conn)
    db_store._ensure_idempotency_index(conn, "mutation_outbox", _OUTBOX_INDEX)

    report = tool_doctor.doctor_vector_lake()

    assert "Idempotency Index" in report
    assert "mutation_outbox=active(dups=1)" in report
    assert "idempotency_index_degraded:mutation_outbox=active" in report


def test_repair_handles_the_jobs_table_whose_primary_key_is_job_id(isolated_memory):
    """Regression: the repair hardcoded ``id`` and raised ``no such column: id``.

    ``jobs`` is keyed by ``job_id``, so "the canonical row is the lowest one" has to
    be expressed against that column.  The bug only showed up on a clean ``jobs``
    table, because the failure happened before the "nothing to repair" early return.
    """
    db_store.init_db()
    conn = db_store.get_connection()
    conn.execute("DROP INDEX IF EXISTS idx_jobs_idempotency")
    conn.execute("DROP INDEX IF EXISTS idx_jobs_idempotency_active")
    now = datetime.now(timezone.utc).isoformat()
    for index in range(2):
        conn.execute(
            "INSERT INTO jobs (job_id, task_type, payload, status, retries, created_at, "
            "updated_at, available_at, idempotency_key) "
            "VALUES (?, 'ingest', '{}', 'completed', 0, ?, ?, ?, 'jobkey')",
            (f"job{index}", now, now, now),
        )
    conn.commit()
    canonical_job_id = conn.execute("SELECT MIN(job_id) AS key FROM jobs").fetchone()["key"]

    assert db_store._ensure_idempotency_index(conn, "jobs", "idx_jobs_idempotency") == "active"
    assert db_store.idempotency_index_state()["jobs"] == {
        "uniqueness": "active",
        "duplicate_groups": 1,
    }

    result = db_store.repair_idempotency_keys("jobs", dry_run=False)

    assert result["redundant_rows"] == 1
    assert result["uniqueness_after"] == "full"
    assert result["duplicate_groups_after"] == 0
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 2
    assert conn.execute(
        "SELECT job_id FROM jobs WHERE idempotency_key = 'jobkey'"
    ).fetchone()["job_id"] == canonical_job_id


def test_repair_on_a_clean_jobs_table_is_a_no_op(isolated_memory):
    db_store.init_db()

    result = db_store.repair_idempotency_keys("jobs", dry_run=False)

    assert result["redundant_rows"] == 0
    assert result["uniqueness_after"] == "full"
