"""Regression tests for the index rebuild lock (P0-4).

Without the lock, a long-running ``generate_index()`` published a stale snapshot
over every partial update that landed while it was running, and the outbox rows
for those updates were already marked complete - silent, unrecoverable loss.
"""
import json
import threading

from vector_lake import db_store, governance_store, indexer


def _entity(page_key, summary="Canonical summary", raw_text="Canonical body"):
    return {
        "entity_id": f"entity_{page_key.lower()}",
        "id": page_key.lower(),
        "page_key": page_key,
        "canonical_name": page_key,
        "title": page_key,
        "type": "concept",
        "status": "Active",
        "domain": "General",
        "categories": ["Concept"],
        "aliases": [],
        "sources": [],
        "links": [],
        "triples": [],
        "summary": summary,
        "raw_text": raw_text,
        "updated": "2026-07-13T00:00:00+00:00",
    }


def _seed(memory_dir, page_keys):
    db_store.init_db()
    for key in page_keys:
        governance_store.upsert_entity(f"entity_{key.lower()}", _entity(key))
    (memory_dir / "wiki" / "index.json").unlink(missing_ok=True)


def _index(memory_dir):
    return json.loads((memory_dir / "wiki" / "index.json").read_text(encoding="utf-8"))


def test_rebuild_holds_the_index_lock_against_partial_updates(isolated_memory, monkeypatch):
    _seed(isolated_memory, ["Concept_Alpha", "Concept_Beta"])
    indexer.generate_index()

    inside_rebuild = threading.Event()
    release_rebuild = threading.Event()
    original_sync = indexer._sync_search_index

    def blocking_sync(index_data, bodies, embeddings_map):
        inside_rebuild.set()
        release_rebuild.wait(timeout=30)
        return original_sync(index_data, bodies, embeddings_map)

    monkeypatch.setattr(indexer, "_sync_search_index", blocking_sync)

    rebuild_result = {}

    def rebuild():
        rebuild_result["path"] = indexer.generate_index()

    rebuild_thread = threading.Thread(target=rebuild)
    rebuild_thread.start()
    assert inside_rebuild.wait(timeout=30), "rebuild never reached the publish stage"

    partial_done = threading.Event()

    def partial_update():
        indexer.update_index_items(["Concept_Beta.md"])
        partial_done.set()

    partial_thread = threading.Thread(target=partial_update)
    partial_thread.start()

    # The partial update must block: the rebuild owns index.json.lock.
    assert not partial_done.wait(timeout=1.5), "partial update ran concurrently with a rebuild"

    release_rebuild.set()
    rebuild_thread.join(timeout=60)
    partial_thread.join(timeout=60)

    assert not rebuild_thread.is_alive() and not partial_thread.is_alive()
    assert rebuild_result.get("path"), "rebuild did not complete"
    assert partial_done.is_set(), "partial update never completed after the rebuild released the lock"


def test_partial_update_landing_after_rebuild_is_not_lost(isolated_memory, monkeypatch):
    _seed(isolated_memory, ["Concept_Alpha", "Concept_Beta", "Concept_Gamma"])
    indexer.generate_index()

    # A canonical change to Gamma happens while a rebuild is mid-flight.
    governance_store.upsert_entity(
        "entity_concept_gamma",
        _entity("Concept_Gamma", summary="updated during rebuild", raw_text="updated body"),
    )
    indexer.update_index_items(["Concept_Gamma.md"])

    nodes = _index(isolated_memory)["nodes"]
    assert nodes["Concept_Gamma"]["summary"] == "updated during rebuild"

    # A later rebuild must preserve it rather than restoring a stale snapshot.
    indexer.generate_index()
    nodes = _index(isolated_memory)["nodes"]
    assert {"Concept_Alpha", "Concept_Beta", "Concept_Gamma"} <= set(nodes)
    assert nodes["Concept_Gamma"]["summary"] == "updated during rebuild"


def test_rebuild_is_incremental_for_unchanged_nodes(isolated_memory):
    _seed(isolated_memory, ["Concept_Alpha", "Concept_Beta"])
    indexer.generate_index()

    state = db_store.search_index_state()
    assert len(state) == 2
    before = db_store.search_index_keys()

    calls = []
    original = db_store.upsert_search_index

    def counting_upsert(*args, **kwargs):
        calls.append(args[0])
        return original(*args, **kwargs)

    indexer.db_store.upsert_search_index = counting_upsert
    try:
        indexer.generate_index()
    finally:
        indexer.db_store.upsert_search_index = original

    assert calls == [], f"unchanged nodes were re-tokenized: {calls}"
    assert db_store.search_index_keys() == before


def test_full_rebuild_still_drops_removed_nodes(isolated_memory):
    _seed(isolated_memory, ["Concept_Alpha", "Concept_Beta"])
    indexer.generate_index()
    assert "Concept_Beta" in db_store.search_index_keys()

    conn = db_store.get_connection()
    with db_store.transaction():
        conn.execute(
            "DELETE FROM entities WHERE json_extract(data_json, '$.page_key') = 'Concept_Beta'"
        )
    indexer.generate_index()

    assert "Concept_Beta" not in db_store.search_index_keys()
    assert "Concept_Beta" not in _index(isolated_memory)["nodes"]
