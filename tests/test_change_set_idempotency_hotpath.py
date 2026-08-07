import json

import pytest

from vector_lake import db_store, governance_store


def _change_set(change_set_id: str, idempotency_key: str) -> dict:
    return {
        "change_set_id": change_set_id,
        "idempotency_key": idempotency_key,
        "status": "pending",
    }


def test_indexed_change_set_lookup_does_not_scan_json_history(isolated_memory):
    db_store.init_db()
    stored = _change_set("changeset_indexed", "indexed-key")
    assert governance_store.record_prepared_change_sets([stored]) == 1

    conn = db_store.get_connection()
    statements = []
    conn.set_trace_callback(statements.append)
    try:
        found = governance_store._load_change_set_by_idempotency_key("indexed-key")
    finally:
        conn.set_trace_callback(None)

    assert found["change_set_id"] == stored["change_set_id"]
    assert found["idempotency_key"] == stored["idempotency_key"]
    assert found["status"] == stored["status"]
    assert found["manifest_version"] == 2
    assert found["payload"]["available"] is True
    assert not any("json_extract" in statement.lower() for statement in statements)


def test_change_set_lookup_preserves_unmapped_legacy_history(isolated_memory):
    db_store.init_db()
    stored = _change_set("changeset_legacy", "legacy-key")
    db_store.get_connection().execute(
        "INSERT INTO change_sets (change_set_id, data_json, updated_at) VALUES (?, ?, ?)",
        (stored["change_set_id"], json.dumps(stored), governance_store._utc_now()),
    )

    found = governance_store._load_change_set_by_idempotency_key("legacy-key")

    assert found == stored


def test_change_set_lookup_rejects_ambiguous_unmapped_legacy_key(
    isolated_memory,
):
    db_store.init_db()
    first = _change_set("changeset_legacy_a", "legacy-conflict")
    second = _change_set("changeset_legacy_b", "legacy-conflict")
    now = governance_store._utc_now()
    db_store.get_connection().executemany(
        "INSERT INTO change_sets (change_set_id, data_json, updated_at) "
        "VALUES (?, ?, ?)",
        [
            (first["change_set_id"], json.dumps(first), now),
            (second["change_set_id"], json.dumps(second), now),
        ],
    )

    with pytest.raises(
        governance_store.ChangeSetIdempotencyConflict,
        match="multiple unmapped owners",
    ):
        governance_store._load_change_set_by_idempotency_key("legacy-conflict")
