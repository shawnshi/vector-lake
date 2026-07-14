import sqlite3

from vector_lake import db_store


def test_outbox_claims_pending_rows_without_signal(isolated_memory):
    db_store.init_db()
    outbox_id = db_store.enqueue_mutation(
        "Concept_Test.md",
        "update",
        payload_text="payload",
        idempotency_key="idem-test",
    )

    rows = db_store.claim_mutation_outbox(limit=10, lease_seconds=30)

    assert [row["id"] for row in rows] == [outbox_id]
    assert rows[0]["status"] == "processing"
    assert rows[0]["attempt_count"] == 1
    db_store.complete_mutation_outbox(outbox_id)
    row = db_store.get_connection().execute(
        "SELECT status, completed_at FROM mutation_outbox WHERE id = ?", (outbox_id,)
    ).fetchone()
    assert row["status"] == "completed"
    assert row["completed_at"]


def test_outbox_retries_then_dead_letters(isolated_memory):
    db_store.init_db()
    outbox_id = db_store.enqueue_mutation("Concept_Test.md", "update", payload_text="payload")
    db_store.claim_mutation_outbox(limit=1)

    retry_status = db_store.fail_mutation_outbox(outbox_id, "first failure", max_attempts=2, backoff_base=0)
    assert retry_status == "pending"
    first = db_store.get_connection().execute(
        "SELECT status, attempt_count, last_error FROM mutation_outbox WHERE id = ?", (outbox_id,)
    ).fetchone()
    assert dict(first) == {"status": "pending", "attempt_count": 1, "last_error": "first failure"}

    rows = db_store.claim_mutation_outbox(limit=1)
    assert len(rows) == 1
    terminal_status = db_store.fail_mutation_outbox(outbox_id, "second failure", max_attempts=2, backoff_base=0)
    assert terminal_status == "failed"


def test_stale_processing_lease_is_reclaimed(isolated_memory):
    db_store.init_db()
    outbox_id = db_store.enqueue_mutation("Concept_Test.md", "delete")
    conn = db_store.get_connection()
    with db_store.transaction():
        conn.execute(
            "UPDATE mutation_outbox SET status = 'processing', lease_until = '2000-01-01T00:00:00+00:00' WHERE id = ?",
            (outbox_id,),
        )

    rows = db_store.claim_mutation_outbox(limit=1)

    assert [row["id"] for row in rows] == [outbox_id]


def test_outbox_idempotency_key_deduplicates(isolated_memory):
    db_store.init_db()
    first_id = db_store.enqueue_mutation("Concept_Test.md", "update", "one", "same-event")
    second_id = db_store.enqueue_mutation("Concept_Test.md", "update", "one", "same-event")
    assert first_id == second_id
    assert db_store.get_connection().execute("SELECT COUNT(*) FROM mutation_outbox").fetchone()[0] == 1


def test_init_db_migrates_legacy_outbox(isolated_memory):
    path = db_store.get_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE mutation_outbox (id INTEGER PRIMARY KEY AUTOINCREMENT, filename TEXT, mutation_type TEXT, status TEXT DEFAULT 'pending', created_at TEXT)"
    )
    conn.execute(
        "INSERT INTO mutation_outbox (filename, mutation_type, status, created_at) VALUES ('Concept_Legacy.md', 'update', 'pending', '2026-01-01T00:00:00+00:00')"
    )
    conn.commit()
    conn.close()

    db_store.init_db()

    columns = {row["name"] for row in db_store.get_connection().execute("PRAGMA table_info(mutation_outbox)")}
    assert {"payload_text", "attempt_count", "last_error", "available_at", "lease_until", "idempotency_key"} <= columns
    legacy = db_store.get_connection().execute("SELECT status, available_at FROM mutation_outbox").fetchone()
    assert legacy["status"] == "pending"
    assert legacy["available_at"] == "2026-01-01T00:00:00+00:00"
