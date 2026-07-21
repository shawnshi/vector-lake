import os
import time

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
            ("changeset_old", "{}", "2000-01-01T00:00:00+00:00"),
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
