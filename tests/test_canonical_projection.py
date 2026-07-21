import json

from vector_lake import db_store, governance_store, indexer

from tests.test_mutation_coordinator import _source_content


def _entity(raw_text="Canonical body", title="Acme Inc"):
    return {
        "entity_id": "entity_acme",
        "id": "vendor_acme",
        "page_key": "Vendor_Acme",
        "canonical_name": title,
        "title": title,
        "type": "vendor",
        "status": "Active",
        "domain": "General",
        "categories": ["Company"],
        "aliases": ["Acme"],
        "sources": ["raw/acme.pdf"],
        "links": ["Product_Widget"],
        "triples": [{"predicate": "created", "target": "Product_Widget"}],
        "summary": "Canonical summary",
        "raw_text": raw_text,
        "updated": "2026-07-13T00:00:00+00:00",
    }


def test_incremental_projection_reads_sqlite_not_markdown(isolated_memory, monkeypatch):
    db_store.init_db()
    governance_store.upsert_entity("entity_acme", _entity())
    index_path = isolated_memory / "wiki" / "index.json"
    index_path.write_text(
        json.dumps({
            "nodes": {"Vendor_Acme": {"title": "Old", "aliases": ["Old Alias"]}},
            "aliases": {"Old Alias": "Vendor_Acme"},
            "categories": [],
            "weighted_edges": [],
            "error_log": [],
            "graph_state": {"dirty": False},
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(indexer, "_parse_wiki_node", lambda *_: (_ for _ in ()).throw(AssertionError("Markdown parsed")))

    indexer.update_index_items(["Vendor_Acme.md"])

    projected = json.loads(index_path.read_text(encoding="utf-8"))
    node = projected["nodes"]["Vendor_Acme"]
    assert node["title"] == "Acme Inc"
    assert node["type"] == "vendor"
    assert node["raw_text"] == "Canonical body"
    assert projected["aliases"]["Acme"] == "Vendor_Acme"
    assert "Old Alias" not in projected["aliases"]
    fts_row = db_store.get_connection().execute(
        "SELECT node_key, text FROM wiki_search_index WHERE node_key = ?",
        ("Vendor_Acme",),
    ).fetchone()
    assert fts_row is not None
    assert "Canonical" in fts_row["text"]


def test_incremental_projection_updates_existing_node(isolated_memory):
    db_store.init_db()
    governance_store.upsert_entity("entity_acme", _entity())
    index_path = isolated_memory / "wiki" / "index.json"
    index_path.write_text(
        json.dumps({
            "nodes": {},
            "aliases": {},
            "categories": [],
            "weighted_edges": [],
            "error_log": [],
            "graph_state": {"dirty": False},
        }),
        encoding="utf-8",
    )
    indexer.update_index_items(["Vendor_Acme.md"])

    governance_store.upsert_entity("entity_acme", _entity(raw_text="Updated canonical body", title="Acme Updated"))
    indexer.update_index_items(["Vendor_Acme.md"])

    projected = json.loads(index_path.read_text(encoding="utf-8"))
    assert projected["nodes"]["Vendor_Acme"]["title"] == "Acme Updated"
    assert projected["nodes"]["Vendor_Acme"]["raw_text"] == "Updated canonical body"


def test_delete_cascade_locates_entity_by_page_key(isolated_memory):
    db_store.init_db()
    governance_store.upsert_entity("entity_acme", _entity())
    conn = db_store.get_connection()
    with db_store.transaction():
        conn.execute("INSERT INTO alias_registry (key, value, updated_at) VALUES (?, ?, ?)", ("Acme", "entity_acme", "now"))
        conn.execute("INSERT INTO wiki_search_index (node_key, title, summary, text) VALUES (?, ?, ?, ?)", ("Vendor_Acme", "Acme", "", "body"))
        claim = {
            "claim_id": "claim_acme",
            "claim_text": "Acme is active.",
            "status": "Active",
            "locator": {"page_key": "Vendor_Acme", "heading": "Facts", "block_index": 1},
            "source_page": "Vendor_Acme.md",
            "evidence_ids": ["evidence_acme"],
            "source_ids": [],
            "subject_entity_ids": ["entity_acme"],
        }
        conn.execute(
            "INSERT INTO claims (claim_id, claim_text, status, data_json, updated_at) VALUES (?, ?, ?, ?, ?)",
            ("claim_acme", claim["claim_text"], "Active", json.dumps(claim), "now"),
        )
        conn.execute(
            "INSERT INTO evidence (evidence_id, data_json, updated_at) VALUES (?, ?, ?)",
            (
                "evidence_acme",
                json.dumps({
                    "evidence_id": "evidence_acme",
                    "source_id": "source_acme",
                    "locator": {"page_key": "Vendor_Acme", "heading": "Facts", "block_index": 1},
                    "evidence_text": "Acme evidence.",
                }),
                "now",
            ),
        )
        memory = {
            "memory_id": "memory_acme",
            "memory_type": "fact",
            "text": "Acme is active.",
            "source_claim_id": "claim_acme",
            "source_page": "Vendor_Acme.md",
            "validity_state": "active",
            "validity_reasons": [],
            "memory_score": 0.9,
        }
        conn.execute(
            "INSERT INTO operational_memory (memory_id, memory_type, score, status, data_json, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("memory_acme", "fact", 0.9, "Active", json.dumps(memory), "now"),
        )
        conn.execute(
            "INSERT INTO entity_identities "
            "(entity_id, page_key, canonical_name, identity_origin, data_json, recorded_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "entity_acme",
                "Vendor_Acme",
                "Acme Inc",
                "explicit",
                json.dumps({"entity_id": "entity_acme", "page_key": "Vendor_Acme"}),
                "now",
                "now",
            ),
        )

    result = db_store.delete_node_cascade("Vendor_Acme")

    assert result["entity_ids"] == ["entity_acme"]
    assert conn.execute("SELECT 1 FROM entities WHERE entity_id = 'entity_acme'").fetchone() is None
    assert conn.execute("SELECT 1 FROM alias_registry WHERE value = 'entity_acme'").fetchone() is None
    assert conn.execute("SELECT 1 FROM wiki_search_index WHERE node_key = 'Vendor_Acme'").fetchone() is None
    assert conn.execute("SELECT 1 FROM evidence WHERE evidence_id = 'evidence_acme'").fetchone() is None
    archived_memory = json.loads(
        conn.execute(
            "SELECT data_json FROM operational_memory WHERE memory_id = 'memory_acme'"
        ).fetchone()["data_json"]
    )
    assert archived_memory["validity_state"] == "archived"
    assert "source_claim_deleted" in archived_memory["validity_reasons"]
    assert governance_store.search_operational_memory("Acme") == []
    claim_version = json.loads(
        conn.execute(
            "SELECT data_json FROM claim_versions WHERE claim_id = 'claim_acme' ORDER BY version_no DESC"
        ).fetchone()["data_json"]
    )
    evidence_version = json.loads(
        conn.execute(
            "SELECT data_json FROM evidence_versions WHERE evidence_id = 'evidence_acme' ORDER BY version_no DESC"
        ).fetchone()["data_json"]
    )
    identity = json.loads(
        conn.execute(
            "SELECT data_json FROM entity_identities WHERE entity_id = 'entity_acme'"
        ).fetchone()["data_json"]
    )
    assert claim_version["lifecycle_state"] == "deleted"
    assert evidence_version["lifecycle_state"] == "deleted"
    assert identity["lifecycle_state"] == "deleted"


def test_full_rebuild_keeps_same_name_entities_and_clears_stale_fts(isolated_memory, monkeypatch):
    db_store.init_db()
    first = _entity(title="Shared Display Name")
    second = _entity(title="Shared Display Name")
    second["entity_id"] = "entity_beta"
    second["id"] = "vendor_beta"
    second["page_key"] = "Vendor_Beta"
    governance_store.upsert_entity(first["entity_id"], first)
    governance_store.upsert_entity(second["entity_id"], second)
    conn = db_store.get_connection()
    with db_store.transaction():
        conn.execute(
            "INSERT INTO wiki_search_index (node_key, title, summary, text) VALUES ('Vendor_Stale', 'Stale', '', 'stale')"
        )

    indexer.generate_index()

    projected = json.loads((isolated_memory / "wiki" / "index.json").read_text(encoding="utf-8"))
    assert {"Vendor_Acme", "Vendor_Beta"} <= set(projected["nodes"])
    assert len(projected["nodes"]) == 2
    fts_keys = {row["node_key"] for row in conn.execute("SELECT node_key FROM wiki_search_index")}
    assert fts_keys == {"Vendor_Acme", "Vendor_Beta"}


def test_migration_dry_run_does_not_create_or_modify_sqlite(isolated_memory):
    (isolated_memory / "wiki" / "Source_Test.md").write_text(_source_content(), encoding="utf-8")
    db_path = isolated_memory / "wiki" / ".meta" / "vector_lake.db"

    result = governance_store.migrate_existing_wiki(dry_run=True)

    assert result["dry_run"] is True
    assert result["pages_scanned"] == 1
    assert result["entities"] == 1
    assert not db_path.exists()


def test_identical_claim_upsert_preserves_storage_updated_at(isolated_memory, monkeypatch):
    db_store.init_db()
    claim = {
        "claim_id": "claim_stable_storage_time",
        "claim_text": "Stable claim",
        "status": "Active",
        "source_ids": [],
        "evidence_ids": [],
    }
    times = iter(("2026-07-19T00:00:00+00:00", "2026-07-19T01:00:00+00:00", "2026-07-19T02:00:00+00:00"))
    monkeypatch.setattr(governance_store, "_utc_now", lambda: next(times))

    with db_store.transaction():
        governance_store._upsert_canonical_records("claims", "claim_id", [claim])
    with db_store.transaction():
        governance_store._upsert_canonical_records("claims", "claim_id", [dict(claim)])
    unchanged = db_store.get_connection().execute(
        "SELECT updated_at FROM claims WHERE claim_id = ?", (claim["claim_id"],)
    ).fetchone()["updated_at"]

    changed = dict(claim, claim_text="Changed claim")
    with db_store.transaction():
        governance_store._upsert_canonical_records("claims", "claim_id", [changed])
    changed_at = db_store.get_connection().execute(
        "SELECT updated_at FROM claims WHERE claim_id = ?", (claim["claim_id"],)
    ).fetchone()["updated_at"]

    assert unchanged == "2026-07-19T00:00:00+00:00"
    assert changed_at == "2026-07-19T02:00:00+00:00"
