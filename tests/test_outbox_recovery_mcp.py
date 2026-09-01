import json

import pytest

from vector_lake import db_store, mcp_server


def test_search_projection_mutation_loads_sqlite_vec_on_cached_connection(
    isolated_memory,
    monkeypatch,
):
    db_store.init_db()
    db_store.close_connection()
    db_store.init_db()
    original_loader = db_store._load_sqlite_vec_extension
    loaded_connections = []

    def tracked_loader(connection):
        loaded_connections.append(connection)
        original_loader(connection)

    monkeypatch.setattr(db_store, "_load_sqlite_vec_extension", tracked_loader)

    with db_store.transaction() as connection:
        result = db_store.apply_search_projection_mutations(
            connection,
            embedding_deletes={"Concept_Missing"},
        )

    assert loaded_connections == [connection]
    assert result["embedding_deletes"] == 1


def test_recover_failed_mutation_outbox_tool_previews_then_requeues_exact_row(
    isolated_memory,
):
    db_store.init_db()
    outbox_id = db_store.enqueue_mutation(
        "Concept_Exact-Recovery.md",
        "delete",
        idempotency_key="exact-recovery",
    )
    connection = db_store.get_connection()
    with db_store.transaction():
        connection.execute(
            "UPDATE mutation_outbox SET status = 'failed', attempt_count = 3, "
            "last_error = 'no such module: vec0', completed_at = created_at "
            "WHERE id = ?",
            (outbox_id,),
        )

    preview = json.loads(
        mcp_server.recover_failed_mutation_outbox([outbox_id], dry_run=True)
    )

    assert preview == {
        "dry_run": True,
        "requested_ids": [outbox_id],
        "requeued": [outbox_id],
        "skipped": {},
        "superseded": {},
    }
    assert connection.execute(
        "SELECT status FROM mutation_outbox WHERE id = ?",
        (outbox_id,),
    ).fetchone()["status"] == "failed"

    applied = json.loads(
        mcp_server.recover_failed_mutation_outbox([outbox_id], dry_run=False)
    )
    row = connection.execute(
        "SELECT status, attempt_count, completed_at FROM mutation_outbox WHERE id = ?",
        (outbox_id,),
    ).fetchone()

    assert applied == {
        "dry_run": False,
        "requested_ids": [outbox_id],
        "requeued": [outbox_id],
        "skipped": {},
        "superseded": {},
    }
    assert row["status"] == "pending"
    assert row["attempt_count"] == 0
    assert row["completed_at"] is None


@pytest.mark.parametrize("outbox_ids", [[], [0], [-1], [True]])
def test_recover_failed_mutation_outbox_tool_rejects_unsafe_selection(outbox_ids):
    with pytest.raises(ValueError):
        mcp_server.recover_failed_mutation_outbox(outbox_ids, dry_run=False)
