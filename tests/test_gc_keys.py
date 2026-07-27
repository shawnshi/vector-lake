import json
import os
import re
import time
from unittest.mock import patch

from vector_lake import db_store, governance_store
from vector_lake.tool_gc import gc_vector_lake


def _seed_vendor(memory_dir, edge_count: int):
    db_store.init_db()
    governance_store.save_entities({
        "items": {
            "entity-internal-id": {
                "entity_id": "entity-internal-id",
                "canonical_name": "Acme",
                "page_key": "Vendor_Acme",
                "type": "vendor",
                "status": "Active",
            }
        }
    })
    page = memory_dir / "wiki" / "Vendor_Acme.md"
    page.write_text("legacy vendor", encoding="utf-8")
    old = time.time() - 90 * 86400
    os.utime(page, (old, old))
    conn = db_store.get_connection()
    with db_store.transaction():
        for index in range(edge_count):
            conn.execute(
                "INSERT INTO claim_graph_edges (source_id, target_id, relation, weight, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                ("Vendor_Acme", f"Concept_{index}", "related", 1.0, "2026-01-01"),
            )


def test_gc_uses_page_key_for_file_and_graph_degree(isolated_memory):
    _seed_vendor(isolated_memory, edge_count=1)

    result = gc_vector_lake(days=30, dry_run=True)

    assert "Vendor_Acme.md" in result
    assert "entity-internal-id" in result


def test_gc_does_not_delete_high_degree_page_key(isolated_memory):
    _seed_vendor(isolated_memory, edge_count=2)

    result = gc_vector_lake(days=30, dry_run=True)

    assert "No orphan entities" in result


def test_gc_prunes_history_even_when_no_orphan_pages(isolated_memory):
    db_store.init_db()
    conn = db_store.get_connection()
    with db_store.transaction():
        conn.execute(
            "INSERT INTO change_sets (change_set_id, data_json, updated_at) VALUES (?, ?, ?)",
            ("changeset_old", '{"status":"applied"}', "2000-01-01T00:00:00+00:00"),
        )
        conn.execute(
            "INSERT INTO change_set_idempotency (idempotency_key, change_set_id, created_at) "
            "VALUES (?, ?, ?)",
            ("idem_old", "changeset_old", "2000-01-01T00:00:00+00:00"),
        )

    result = gc_vector_lake(days=30, dry_run=False)

    assert "Pruned 1 change set(s) and 1 idempotency key(s)" in result
    assert conn.execute("SELECT COUNT(*) FROM change_sets").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM change_set_idempotency").fetchone()[0] == 0


def test_gc_dry_run_missing_database_is_zero_write(isolated_memory):
    meta_dir = isolated_memory / "wiki" / ".meta"

    result = gc_vector_lake(days=30, dry_run=True)

    assert "[DRY-RUN] GC unavailable" in result
    assert "No changes made" in result
    assert not meta_dir.exists()


def test_gc_apply_prunes_only_unreferenced_terminal_change_sets(
    isolated_memory,
):
    db_store.init_db()
    conn = db_store.get_connection()
    old = "2000-01-01T00:00:00+00:00"
    change_sets = {
        "terminal-delete": {"status": "applied"},
        "terminal-referenced": {"status": "applied"},
        "pending-protected": {"status": "pending"},
    }
    with db_store.transaction():
        for change_set_id, body in change_sets.items():
            conn.execute(
                "INSERT INTO change_sets (change_set_id, data_json, updated_at) "
                "VALUES (?, ?, ?)",
                (change_set_id, json.dumps(body), old),
            )
            conn.execute(
                "INSERT INTO change_set_idempotency "
                "(idempotency_key, change_set_id, created_at) VALUES (?, ?, ?)",
                (f"idem-{change_set_id}", change_set_id, old),
            )
        conn.execute(
            "INSERT INTO governance_queue (item_id, data_json, updated_at) "
            "VALUES (?, ?, ?)",
            (
                "queue-active",
                json.dumps(
                    {
                        "item_id": "queue-active",
                        "status": "pending",
                        "change_set_id": "terminal-referenced",
                    }
                ),
                old,
            ),
        )

    result = gc_vector_lake(days=30, dry_run=False)

    remaining = {
        row[0]
        for row in conn.execute(
            "SELECT change_set_id FROM change_sets"
        ).fetchall()
    }
    idempotency = {
        row[0]
        for row in conn.execute(
            "SELECT change_set_id FROM change_set_idempotency"
        ).fetchall()
    }
    assert "Pruned 1 change set(s) and 1 idempotency key(s)" in result
    assert remaining == {"terminal-referenced", "pending-protected"}
    assert idempotency == remaining


def _orphan_fingerprint(report: str) -> str:
    match = re.search(
        r"Orphan candidate fingerprint: (sha256:[0-9a-f]{64})",
        report,
    )
    assert match is not None
    return match.group(1)


def test_gc_preview_fingerprint_is_stable_for_unchanged_candidates(
    isolated_memory,
):
    _seed_vendor(isolated_memory, edge_count=1)

    first = gc_vector_lake(days=30, dry_run=True)
    second = gc_vector_lake(days=30, dry_run=True)

    assert _orphan_fingerprint(first) == _orphan_fingerprint(second)
    assert "No changes made" in first


def test_gc_plain_apply_retains_orphans_and_only_runs_safe_phase(
    isolated_memory,
):
    _seed_vendor(isolated_memory, edge_count=1)
    page = isolated_memory / "wiki" / "Vendor_Acme.md"

    with patch(
        "vector_lake.mutation_coordinator.execute_mutation_batch"
    ) as execute:
        result = gc_vector_lake(days=30, dry_run=False)

    assert "Orphan deletion was not confirmed" in result
    assert "retained 1 candidate page(s)" in result
    assert page.exists()
    execute.assert_not_called()


def test_gc_matching_fingerprint_explicitly_enables_orphan_deletion(
    isolated_memory,
):
    _seed_vendor(isolated_memory, edge_count=1)
    preview = gc_vector_lake(days=30, dry_run=True)
    fingerprint = _orphan_fingerprint(preview)

    with patch(
        "vector_lake.mutation_coordinator.execute_mutation_batch",
        return_value={"ok": True, "deferred": []},
    ) as execute:
        result = gc_vector_lake(
            days=30,
            dry_run=False,
            orphan_confirmation=fingerprint,
        )

    assert "Deleted 1 orphan pages" in result
    mutations = execute.call_args.args[0]
    assert mutations[0]["filename"] == "Vendor_Acme.md"
    assert mutations[0]["is_delete"] is True
    assert len(mutations[0]["expected_projection_hash"]) == 64
    assert execute.call_args.kwargs["return_details"] is True
    execute.call_args.kwargs["precondition_callback"]()


def test_gc_stale_fingerprint_blocks_orphan_and_history_mutation(
    isolated_memory,
):
    _seed_vendor(isolated_memory, edge_count=1)
    preview = gc_vector_lake(days=30, dry_run=True)
    fingerprint = _orphan_fingerprint(preview)
    page = isolated_memory / "wiki" / "Vendor_Acme.md"
    page.write_text("changed vendor", encoding="utf-8")
    old = time.time() - 90 * 86400
    os.utime(page, (old, old))
    conn = db_store.get_connection()
    with db_store.transaction():
        conn.execute(
            "INSERT INTO change_sets (change_set_id, data_json, updated_at) "
            "VALUES (?, ?, ?)",
            ("stale-token-history", '{"status":"applied"}', "2000-01-01"),
        )

    result = gc_vector_lake(
        days=30,
        dry_run=False,
        orphan_confirmation=fingerprint,
    )

    assert "[BLOCKED]" in result
    assert "No changes made" in result
    assert conn.execute(
        "SELECT COUNT(*) FROM change_sets WHERE change_set_id = ?",
        ("stale-token-history",),
    ).fetchone()[0] == 1


def test_gc_rechecks_canonical_edges_inside_mutation_precondition(
    isolated_memory,
):
    from vector_lake import mutation_coordinator, runtime_health

    _seed_vendor(isolated_memory, edge_count=1)
    preview = gc_vector_lake(days=30, dry_run=True)
    fingerprint = _orphan_fingerprint(preview)
    real_execute = mutation_coordinator.execute_mutation_batch

    def inject_relation_before_transaction(mutations, **kwargs):
        conn = db_store.get_connection()
        with db_store.transaction():
            conn.execute(
                "INSERT INTO claim_graph_edges "
                "(source_id, target_id, relation, weight, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    "Vendor_Acme",
                    "Concept_concurrent",
                    "related",
                    1.0,
                    "2026-07-27T00:00:00+00:00",
                ),
            )
        return real_execute(mutations, **kwargs)

    with (
        patch.object(
            mutation_coordinator,
            "execute_mutation_batch",
            side_effect=inject_relation_before_transaction,
        ),
        patch.object(runtime_health, "enforce_runtime_write_health"),
    ):
        result = gc_vector_lake(
            days=30,
            dry_run=False,
            orphan_confirmation=fingerprint,
        )

    assert "confirmed orphan deletion was blocked" in result
    assert "candidate set changed after confirmation" in result
    assert (isolated_memory / "wiki" / "Vendor_Acme.md").exists()
    assert db_store.get_connection().execute(
        "SELECT 1 FROM entities "
        "WHERE json_extract(data_json, '$.page_key') = ?",
        ("Vendor_Acme",),
    ).fetchone() is not None
    assert db_store.get_connection().execute(
        "SELECT 1 FROM mutation_outbox"
    ).fetchone() is None
