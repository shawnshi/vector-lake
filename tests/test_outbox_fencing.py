import sqlite3
from datetime import datetime, timezone

import pytest

from vector_lake import db_store


def _expire(claim):
    with db_store.transaction() as conn:
        conn.execute("UPDATE mutation_outbox SET lease_until = '2000-01-01T00:00:00+00:00' WHERE id = ?", (claim["id"],))


@pytest.mark.parametrize("operation", ["complete", "fail"])
def test_stale_worker_cannot_rewrite_success_of_reclaimed_lease(isolated_memory, operation):
    item = db_store.enqueue_mutation("Concept_Lease.md", "delete")
    old = db_store.claim_mutation_outbox(limit=1)[0]
    _expire(old)
    current = db_store.claim_mutation_outbox(limit=1)[0]
    db_store.complete_mutation_outbox(item, claim=current)
    with pytest.raises(db_store.MutationClaimLost):
        if operation == "complete":
            db_store.complete_mutation_outbox(item, claim=old)
        else:
            db_store.fail_mutation_outbox(item, "stale failure", claim=old)
    assert db_store.get_connection().execute("SELECT status FROM mutation_outbox WHERE id=?", (item,)).fetchone()[0] == "completed"


@pytest.mark.parametrize("operation", ["complete", "fail"])
def test_expired_claim_cannot_write_before_any_reclaim(isolated_memory, operation):
    item = db_store.enqueue_mutation("Concept_Lease.md", "delete")
    claim = db_store.claim_mutation_outbox()[0]
    _expire(claim)
    with pytest.raises(db_store.MutationClaimLost):
        if operation == "complete":
            db_store.complete_mutation_outbox(item, claim=claim)
        else:
            db_store.fail_mutation_outbox(item, "expired failure", claim=claim)
    assert db_store.get_connection().execute("SELECT status FROM mutation_outbox WHERE id=?", (item,)).fetchone()[0] == "processing"


@pytest.mark.parametrize("field,value", [("lease_owner", "forged"), ("lease_token", "forged"), ("lease_generation", -1), ("projection_generation", -1), ("id", -1), ("lease_generation", 1.5), ("lease_generation", True)])
def test_forged_claim_fails_closed(isolated_memory, field, value):
    item = db_store.enqueue_mutation("Concept_Lease.md", "delete")
    claim = db_store.claim_mutation_outbox()[0]
    claim[field] = value
    with pytest.raises(db_store.MutationClaimLost):
        db_store.complete_mutation_outbox(item, claim=claim)
    assert db_store.get_connection().execute("SELECT status FROM mutation_outbox WHERE id=?", (item,)).fetchone()[0] == "processing"


def test_id_only_completion_is_not_an_unfenced_compatibility_path(isolated_memory):
    item = db_store.enqueue_mutation("Concept_Lease.md", "delete")
    db_store.claim_mutation_outbox()
    with pytest.raises(TypeError):
        db_store.complete_mutation_outbox(item)


def test_newer_intent_supersedes_old_retry_even_when_old_becomes_due(isolated_memory):
    old_id = db_store.enqueue_mutation("Concept_Order.md", "update", "A", "A")
    old = db_store.claim_mutation_outbox()[0]
    db_store.fail_mutation_outbox(old_id, "temporary failure", backoff_base=100, claim=old)
    new_id = db_store.enqueue_mutation("Concept_Order.md", "update", "B", "B")
    current = db_store.claim_mutation_outbox()[0]
    assert current["id"] == new_id
    db_store.complete_mutation_outbox(new_id, claim=current)
    with db_store.transaction() as conn:
        conn.execute("UPDATE mutation_outbox SET available_at='2000-01-01T00:00:00+00:00' WHERE id=?", (old_id,))
    assert db_store.claim_mutation_outbox() == []
    assert db_store.get_connection().execute("SELECT status FROM mutation_outbox WHERE id=?", (old_id,)).fetchone()[0] == "superseded"


def test_a_b_a_revives_same_id_with_newer_projection_generation(isolated_memory):
    first = db_store.enqueue_mutation("Concept_Order.md", "update", "A", "A")
    first_claim = db_store.claim_mutation_outbox()[0]
    db_store.complete_mutation_outbox(first, claim=first_claim)
    second = db_store.enqueue_mutation("Concept_Order.md", "update", "B", "B")
    second_claim = db_store.claim_mutation_outbox()[0]
    db_store.complete_mutation_outbox(second, claim=second_claim)
    revived = db_store.enqueue_mutation("Concept_Order.md", "update", "A", "A")
    assert revived == first and revived < second
    latest = db_store.claim_mutation_outbox()[0]
    assert latest["projection_generation"] > second_claim["projection_generation"]
    assert latest["lease_generation"] > first_claim["lease_generation"]
    assert db_store.is_managed_projection_state("Concept_Order.md", "update", "A")
    assert not db_store.is_managed_projection_state("Concept_Order.md", "update", "B")
    with pytest.raises(db_store.MutationClaimLost):
        db_store.complete_mutation_outbox(revived, claim=first_claim)
    db_store.complete_mutation_outbox(revived, claim=latest)


def test_writer_migration_preserves_old_rows_and_recovers_unfenced_processing(isolated_memory):
    path = db_store.get_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE mutation_outbox (id INTEGER PRIMARY KEY AUTOINCREMENT, filename TEXT, mutation_type TEXT, status TEXT, created_at TEXT)")
        conn.execute("INSERT INTO mutation_outbox VALUES (1, 'Concept_Legacy.md', 'delete', 'processing', ?)", (datetime.now(timezone.utc).isoformat(),))
    claims = db_store.claim_mutation_outbox()
    assert len(claims) == 1
    assert claims[0]["id"] == 1
    assert claims[0]["lease_generation"] > 0
    assert claims[0]["projection_generation"] > 0
    assert bool(claims[0]["lease_owner"]) and bool(claims[0]["lease_token"])
    db_store.complete_mutation_outbox(1, claim=claims[0])


def test_idempotency_key_cannot_address_a_different_page(isolated_memory):
    db_store.enqueue_mutation("Concept_First.md", "delete", idempotency_key="shared")
    with pytest.raises(ValueError, match="Idempotency"):
        db_store.enqueue_mutation("Concept_Second.md", "delete", idempotency_key="shared")


def test_archived_reader_does_not_upgrade_outbox_protocol(isolated_memory, monkeypatch):
    item = db_store.enqueue_mutation("Concept_Archive.md", "delete")
    claim = db_store.claim_mutation_outbox()[0]
    db_store.complete_mutation_outbox(item, claim=claim)
    path = db_store.get_db_path()
    db_store.close_connection()
    with sqlite3.connect(path) as conn:
        conn.execute("DROP INDEX idx_mutation_outbox_filename_generation")
        for name in ["lease_owner", "lease_token", "lease_generation", "projection_generation", "base_version", "superseded_by"]:
            conn.execute(f"ALTER TABLE mutation_outbox DROP COLUMN {name}")
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as readonly:
        readonly.row_factory = sqlite3.Row
        monkeypatch.setattr(db_store, "get_connection", lambda: readonly)
        assert db_store.is_managed_projection_state("Concept_Archive.md", "delete")
        assert "projection_generation" not in {row[1] for row in readonly.execute("PRAGMA table_info(mutation_outbox)")}
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            db_store.enqueue_mutation("Concept_New.md", "delete")


def test_two_processes_cannot_claim_the_same_live_lease(isolated_memory):
    import json
    import os
    import subprocess
    import sys

    item = db_store.enqueue_mutation("Concept_Process.md", "delete")
    env = os.environ.copy()
    env.update({"PYTHONDONTWRITEBYTECODE": "1", "VECTOR_LAKE_MEMORY_DIR": str(isolated_memory), "VECTOR_LAKE_DB_PATH": str(db_store.get_db_path())})
    for name in ["GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"]:
        env.pop(name, None)
    code = "from vector_lake import db_store; import sys,json; db_store.init_db(); print('ready',flush=True); sys.stdin.readline(); rows=db_store.claim_mutation_outbox(limit=1); print(json.dumps([r['id'] for r in rows]),flush=True); db_store.close_connection()"
    processes = [subprocess.Popen([sys.executable, "-B", "-c", code], env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(2)]
    try:
        for process in processes:
            assert process.stdout.readline().strip() == "ready"
        for process in processes:
            process.stdin.write("go\n")
            process.stdin.flush()
        results = []
        for process in processes:
            output, error = process.communicate(timeout=10)
            assert process.returncode == 0, error
            results.append(json.loads(output.strip()))
        assert sorted(results) == [[], [item]]
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)


def test_payload_retention_keeps_the_page_revision_floor(isolated_memory):
    from vector_lake.outbox_retention import prune_outbox_payloads

    first = db_store.enqueue_mutation("Concept_Retention.md", "update", "A")
    a = db_store.claim_mutation_outbox()[0]
    db_store.complete_mutation_outbox(first, claim=a)
    second = db_store.enqueue_mutation("Concept_Retention.md", "update", "B")
    b = db_store.claim_mutation_outbox()[0]
    db_store.complete_mutation_outbox(second, claim=b)
    with db_store.transaction() as conn:
        conn.execute("UPDATE mutation_outbox SET completed_at='2000-01-01T00:00:00+00:00'")
    result = prune_outbox_payloads(days=1)
    assert result["stripped"] == 2
    assert db_store.get_connection().execute("SELECT COUNT(*) FROM mutation_outbox").fetchone()[0] == 2
    third = db_store.enqueue_mutation("Concept_Retention.md", "update", "C")
    c = db_store.claim_mutation_outbox()[0]
    assert c["id"] == third and c["projection_generation"] > b["projection_generation"]
