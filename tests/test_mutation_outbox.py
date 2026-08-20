import sqlite3
import threading
import time

from vector_lake import db_store, mutation_coordinator
from tests.test_mutation_coordinator import (
    _source_content,
    _write_purpose_contract,
)


def test_outbox_claims_pending_rows_without_signal(isolated_memory):
    db_store.init_db()
    outbox_id = db_store.enqueue_mutation(
        "Concept_Test.md",
        "update",
        payload_text="payload",
        idempotency_key="idem-test",
    )

    rows = db_store.claim_mutation_outbox(
        limit=10,
        lease_seconds=30,
        lease_owner="worker-a",
    )

    assert [row["id"] for row in rows] == [outbox_id]
    assert rows[0]["status"] == "processing"
    assert rows[0]["attempt_count"] == 1
    assert db_store.complete_mutation_outbox(
        outbox_id,
        rows[0]["lease_owner"],
        rows[0]["lease_token"],
        rows[0]["lease_generation"],
    ) is True
    row = db_store.get_connection().execute(
        "SELECT status, completed_at FROM mutation_outbox WHERE id = ?", (outbox_id,)
    ).fetchone()
    assert row["status"] == "completed"
    assert row["completed_at"]


def test_pending_projection_event_is_managed_only_when_payload_matches(isolated_memory):
    db_store.init_db()
    db_store.enqueue_mutation(
        "Concept_Managed.md",
        "update",
        payload_text="canonical payload",
        idempotency_key="managed-payload",
        base_version="old-version",
    )

    assert db_store.is_managed_projection_state(
        "Concept_Managed.md", "update", "canonical payload"
    ) is True
    assert db_store.is_managed_projection_state(
        "Concept_Managed.md", "update", "manual conflicting payload"
    ) is False
    assert db_store.is_managed_projection_state(
        "Concept_Managed.md", "update", "canonical payload\r\n"
    ) is False

    db_store.enqueue_mutation(
        "Concept_Mixed-Newlines.md",
        "update",
        payload_text="frontmatter\nbody\r\nnext\rline\n",
        idempotency_key="managed-mixed-newlines",
        base_version="old-version",
    )
    assert db_store.is_managed_projection_state(
        "Concept_Mixed-Newlines.md",
        "update",
        "frontmatter\r\nbody\nnext\nline\r\n",
    ) is True


def test_outbox_retries_then_dead_letters(isolated_memory):
    db_store.init_db()
    outbox_id = db_store.enqueue_mutation("Concept_Test.md", "update", payload_text="payload")
    first_claim = db_store.claim_mutation_outbox(limit=1, lease_owner="worker-a")[0]

    retry_status = db_store.fail_mutation_outbox(
        outbox_id,
        "first failure",
        first_claim["lease_owner"],
        first_claim["lease_token"],
        first_claim["lease_generation"],
        max_attempts=2,
        backoff_base=0,
    )
    assert retry_status == "pending"
    first = db_store.get_connection().execute(
        "SELECT status, attempt_count, last_error, completed_at "
        "FROM mutation_outbox WHERE id = ?",
        (outbox_id,),
    ).fetchone()
    assert dict(first) == {
        "status": "pending",
        "attempt_count": 1,
        "last_error": "first failure",
        "completed_at": None,
    }

    rows = db_store.claim_mutation_outbox(limit=1, lease_owner="worker-a")
    assert len(rows) == 1
    terminal_status = db_store.fail_mutation_outbox(
        outbox_id,
        "second failure",
        rows[0]["lease_owner"],
        rows[0]["lease_token"],
        rows[0]["lease_generation"],
        max_attempts=2,
        backoff_base=0,
    )
    assert terminal_status == "failed"
    terminal = db_store.get_connection().execute(
        "SELECT status, attempt_count, last_error, completed_at "
        "FROM mutation_outbox WHERE id = ?",
        (outbox_id,),
    ).fetchone()
    assert terminal["status"] == "failed"
    assert terminal["attempt_count"] == 2
    assert terminal["last_error"] == "second failure"
    assert terminal["completed_at"]


def test_stale_processing_lease_is_reclaimed(isolated_memory):
    db_store.init_db()
    outbox_id = db_store.enqueue_mutation("Concept_Test.md", "delete")
    conn = db_store.get_connection()
    with db_store.transaction():
        conn.execute(
            "UPDATE mutation_outbox SET status = 'processing', lease_until = '2000-01-01T00:00:00+00:00' WHERE id = ?",
            (outbox_id,),
        )

    rows = db_store.claim_mutation_outbox(limit=1, lease_owner="worker-b")

    assert [row["id"] for row in rows] == [outbox_id]


def test_newer_same_page_intent_supersedes_processing_worker(isolated_memory):
    db_store.init_db()
    first_id = db_store.enqueue_mutation(
        "Concept_Test.md",
        "update",
        payload_text="old",
        idempotency_key="old-event",
    )
    first_claim = db_store.claim_mutation_outbox(
        limit=1,
        lease_seconds=30,
        lease_owner="worker-a",
    )[0]

    second_id = db_store.enqueue_mutation(
        "Concept_Test.md",
        "update",
        payload_text="new",
        idempotency_key="new-event",
    )

    rows = db_store.get_connection().execute(
        "SELECT id, status, superseded_by FROM mutation_outbox ORDER BY id"
    ).fetchall()
    assert [dict(row) for row in rows] == [
        {"id": first_id, "status": "superseded", "superseded_by": second_id},
        {"id": second_id, "status": "pending", "superseded_by": None},
    ]
    assert db_store.complete_mutation_outbox(
        first_id,
        first_claim["lease_owner"],
        first_claim["lease_token"],
        first_claim["lease_generation"],
    ) is False
    assert db_store.fail_mutation_outbox(
        first_id,
        "stale worker failure",
        first_claim["lease_owner"],
        first_claim["lease_token"],
        first_claim["lease_generation"],
        max_attempts=1,
        backoff_base=0,
    ) == "stale"

    second_claim = db_store.claim_mutation_outbox(
        limit=10,
        lease_seconds=30,
        lease_owner="worker-b",
    )
    assert [row["id"] for row in second_claim] == [second_id]


def test_expired_worker_cannot_finish_reclaimed_lease(isolated_memory):
    db_store.init_db()
    outbox_id = db_store.enqueue_mutation("Concept_Test.md", "delete")
    first = db_store.claim_mutation_outbox(
        limit=1,
        lease_seconds=30,
        lease_owner="worker-a",
    )[0]
    with db_store.transaction():
        db_store.get_connection().execute(
            "UPDATE mutation_outbox SET lease_until = '2000-01-01T00:00:00+00:00' WHERE id = ?",
            (outbox_id,),
        )
    second = db_store.claim_mutation_outbox(
        limit=1,
        lease_seconds=30,
        lease_owner="worker-b",
    )[0]

    assert second["lease_generation"] == first["lease_generation"] + 1
    assert db_store.complete_mutation_outbox(
        outbox_id,
        first["lease_owner"],
        first["lease_token"],
        first["lease_generation"],
    ) is False
    assert db_store.complete_mutation_outbox(
        outbox_id,
        second["lease_owner"],
        second["lease_token"],
        second["lease_generation"],
    ) is True


def test_outbox_idempotency_key_deduplicates(isolated_memory):
    db_store.init_db()
    first_id = db_store.enqueue_mutation("Concept_Test.md", "update", "one", "same-event")
    second_id = db_store.enqueue_mutation("Concept_Test.md", "update", "one", "same-event")
    assert first_id == second_id
    assert db_store.get_connection().execute("SELECT COUNT(*) FROM mutation_outbox").fetchone()[0] == 1


def test_historical_failed_idempotency_replay_becomes_latest_intent(isolated_memory):
    db_store.init_db()
    first_id = db_store.enqueue_mutation(
        "Concept_Replay.md",
        "update",
        "old",
        "event-a",
    )
    first_claim = db_store.claim_mutation_outbox(limit=1, lease_owner="worker-a")[0]
    assert db_store.fail_mutation_outbox(
        first_id,
        "terminal",
        first_claim["lease_owner"],
        first_claim["lease_token"],
        first_claim["lease_generation"],
        max_attempts=1,
        backoff_base=0,
    ) == "failed"
    second_id = db_store.enqueue_mutation(
        "Concept_Replay.md",
        "update",
        "new",
        "event-b",
    )

    replay_id = db_store.enqueue_mutation(
        "Concept_Replay.md",
        "update",
        "old",
        "event-a",
    )

    assert replay_id > second_id > first_id
    rows = db_store.get_connection().execute(
        "SELECT id, idempotency_key, status, superseded_by FROM mutation_outbox ORDER BY id"
    ).fetchall()
    assert [dict(row) for row in rows] == [
        {"id": first_id, "idempotency_key": "event-a", "status": "failed", "superseded_by": None},
        {"id": second_id, "idempotency_key": "event-b", "status": "superseded", "superseded_by": replay_id},
        {"id": replay_id, "idempotency_key": "event-a", "status": "pending", "superseded_by": None},
    ]


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
    assert {
        "payload_text",
        "attempt_count",
        "last_error",
        "available_at",
        "lease_until",
        "lease_owner",
        "lease_token",
        "lease_generation",
        "superseded_by",
        "idempotency_key",
        "projection_base_hash",
    } <= columns
    legacy = db_store.get_connection().execute("SELECT status, available_at FROM mutation_outbox").fetchone()
    assert legacy["status"] == "pending"
    assert legacy["available_at"] == "2026-01-01T00:00:00+00:00"


def test_init_db_preserves_old_rows_and_supersedes_duplicate_active_intents(isolated_memory):
    path = db_store.get_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE mutation_outbox (id INTEGER PRIMARY KEY AUTOINCREMENT, filename TEXT, mutation_type TEXT, status TEXT DEFAULT 'pending', created_at TEXT)"
    )
    conn.executemany(
        "INSERT INTO mutation_outbox (filename, mutation_type, status, created_at) VALUES (?, 'update', ?, ?)",
        [
            ("Concept_Legacy.md", "pending", "2026-01-01T00:00:00+00:00"),
            ("Concept_Legacy.md", "processing", "2026-01-02T00:00:00+00:00"),
        ],
    )
    conn.commit()
    conn.close()

    db_store.init_db()

    rows = db_store.get_connection().execute(
        "SELECT id, status, superseded_by FROM mutation_outbox ORDER BY id"
    ).fetchall()
    assert [dict(row) for row in rows] == [
        {"id": 1, "status": "superseded", "superseded_by": 2},
        {"id": 2, "status": "processing", "superseded_by": None},
    ]


def test_projection_validation_and_staging_do_not_hold_sqlite_write_lock(
    isolated_memory,
    monkeypatch,
):
    _write_purpose_contract(isolated_memory)
    materialize_entered = threading.Event()
    release_materialize = threading.Event()
    real_materialize = mutation_coordinator.materialize_markdown_projection
    result = {}
    errors = []

    def blocked_materialize(*args, **kwargs):
        materialize_entered.set()
        assert release_materialize.wait(timeout=5)
        return real_materialize(*args, **kwargs)

    monkeypatch.setattr(
        mutation_coordinator,
        "materialize_markdown_projection",
        blocked_materialize,
    )

    def commit_mutation():
        try:
            result.update(
                mutation_coordinator.execute_mutation_batch(
                    [
                        {
                            "filename": "Source_No-Long-Lock.md",
                            "content": _source_content(),
                        }
                    ],
                    return_details=True,
                )
            )
        except BaseException as exc:
            errors.append(exc)
        finally:
            db_store.close_connection()

    mutation_thread = threading.Thread(target=commit_mutation)
    mutation_thread.start()
    assert materialize_entered.wait(timeout=5)

    writer = sqlite3.connect(db_store.get_db_path(), timeout=0.5)
    started = time.monotonic()
    try:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("CREATE TABLE barrier_writer_probe(value TEXT)")
        writer.execute("INSERT INTO barrier_writer_probe VALUES ('committed')")
        writer.commit()
    finally:
        writer.close()
    elapsed = time.monotonic() - started

    release_materialize.set()
    mutation_thread.join(timeout=5)
    assert not mutation_thread.is_alive()
    assert errors == []
    assert elapsed < 0.5
    assert result["committed"] is True
    assert result["deferred"] == []
    assert (
        isolated_memory / "wiki" / "Source_No-Long-Lock.md"
    ).read_text(encoding="utf-8") == _source_content()


def test_newer_intent_wins_while_older_projection_is_staging(
    isolated_memory,
    monkeypatch,
):
    _write_purpose_contract(isolated_memory)
    original = _source_content()
    old_content = original.replace("Primary source content.", "Old intent.")
    new_content = original.replace("Primary source content.", "New intent.")
    old_stage_entered = threading.Event()
    release_old_stage = threading.Event()
    real_materialize = mutation_coordinator.materialize_markdown_projection
    old_result = {}
    old_errors = []

    def block_old_materialize(
        filename,
        mutation_type,
        payload_text=None,
        validation_mode="full",
        projection_base_hash=None,
    ):
        if payload_text and "Old intent." in payload_text:
            old_stage_entered.set()
            assert release_old_stage.wait(timeout=5)
        return real_materialize(
            filename,
            mutation_type,
            payload_text,
            validation_mode=validation_mode,
            projection_base_hash=projection_base_hash,
        )

    monkeypatch.setattr(
        mutation_coordinator,
        "materialize_markdown_projection",
        block_old_materialize,
    )

    def commit_old_intent():
        try:
            old_result.update(
                mutation_coordinator.execute_mutation_batch(
                    [
                        {
                            "filename": "Source_Latest-Intent.md",
                            "content": old_content,
                        }
                    ],
                    validation_mode="schema",
                    return_details=True,
                )
            )
        except BaseException as exc:
            old_errors.append(exc)
        finally:
            db_store.close_connection()

    old_thread = threading.Thread(target=commit_old_intent)
    old_thread.start()
    assert old_stage_entered.wait(timeout=5)

    new_result = mutation_coordinator.execute_mutation_batch(
        [
            {
                "filename": "Source_Latest-Intent.md",
                "content": new_content,
            }
        ],
        validation_mode="schema",
        return_details=True,
    )
    release_old_stage.set()
    old_thread.join(timeout=5)

    assert not old_thread.is_alive()
    assert old_errors == []
    assert old_result["committed"] is True
    assert old_result["deferred"] == ["Source_Latest-Intent.md"]
    assert old_result["post_commit_warnings"] == []
    assert new_result["committed"] is True
    assert new_result["deferred"] == []
    assert (
        isolated_memory / "wiki" / "Source_Latest-Intent.md"
    ).read_text(encoding="utf-8") == new_content
    rows = db_store.get_connection().execute(
        "SELECT id, status, superseded_by FROM mutation_outbox "
        "WHERE filename = 'Source_Latest-Intent.md' ORDER BY id"
    ).fetchall()
    assert [dict(row) for row in rows] == [
        {
            "id": old_result["outbox_ids"][0],
            "status": "superseded",
            "superseded_by": new_result["outbox_ids"][0],
        },
        {
            "id": new_result["outbox_ids"][0],
            "status": "pending",
            "superseded_by": None,
        },
    ]
    assert not tuple(
        (isolated_memory / "wiki").glob("Source_Latest-Intent.md.*.stage")
    )
