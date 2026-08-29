from vector_lake import db_store, indexer, governance_store


def test_warm_incremental_index_removes_legacy_system_nodes(isolated_memory):
    db_store.init_db()
    governance_store.save_entities({
        "items": {
            "entity-user": {
                "entity_id": "entity-user",
                "canonical_name": "User Node",
                "page_key": "Concept_User",
                "type": "concept",
                "status": "Active",
                "aliases": [],
                "categories": ["Concept"],
            }
        }
    })
    governance_store.upsert_entity(
        "system-legacy",
        {
            "entity_id": "system-legacy",
            "canonical_name": "Legacy System",
            "page_key": "System_Legacy",
            "type": "system",
            "status": "Active",
        },
    )
    db_store.upsert_search_index("System_Legacy", "legacy", "system", "legacy system")

    indexer.generate_index()

    updated = indexer.read_committed_index_snapshot(
        isolated_memory / "wiki" / "index.json"
    )
    assert "System_Legacy" not in updated["nodes"]
    assert "system-legacy" not in updated["aliases"]
    assert all("System_Legacy" not in (edge["source"], edge["target"]) for edge in updated["weighted_edges"])
    assert db_store.get_connection().execute(
        "SELECT COUNT(*) FROM wiki_search_index WHERE node_key = 'System_Legacy'"
    ).fetchone()[0] == 0
