import json

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
    indexer.generate_index()
    path = isolated_memory / "wiki" / "index.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["nodes"]["System_Legacy"] = {
        "id": "system-legacy",
        "title": "Legacy System",
        "type": "system",
        "aliases": [],
        "categories": [],
    }
    data["aliases"]["system-legacy"] = "System_Legacy"
    data["weighted_edges"].append({"source": "System_Legacy", "target": "Concept_User", "weight": 2.0})
    path.write_text(json.dumps(data), encoding="utf-8")
    db_store.upsert_search_index("System_Legacy", "legacy", "system", "legacy system")

    indexer.update_index_items(["Concept_User.md"])

    updated = json.loads(path.read_text(encoding="utf-8"))
    assert "System_Legacy" not in updated["nodes"]
    assert "system-legacy" not in updated["aliases"]
    assert all("System_Legacy" not in (edge["source"], edge["target"]) for edge in updated["weighted_edges"])
    assert db_store.get_connection().execute(
        "SELECT COUNT(*) FROM wiki_search_index WHERE node_key = 'System_Legacy'"
    ).fetchone()[0] == 0
