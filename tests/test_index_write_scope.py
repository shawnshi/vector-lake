"""VL-A03: slow index work must not serialize unrelated SQLite writers."""
import json
import sqlite3
import threading
import time

import pytest

from vector_lake import db_store, governance_store, indexer


def _seed():
    db_store.init_db()
    governance_store.upsert_entity("scope-a", {
        "entity_id": "scope-a", "page_key": "Concept_Scope", "title": "Scope",
        "canonical_name": "Scope", "type": "concept", "status": "Active",
        "aliases": [], "categories": ["Concept"], "links": [], "triples": [],
        "raw_text": "original body",
    })
    indexer.generate_index()


@pytest.mark.parametrize("phase", ["parse", "tokenize", "claim_graph", "publish", "topology"])
def test_unrelated_writer_can_commit_during_slow_index_phase(isolated_memory, monkeypatch, record_property, phase):
    _seed()
    entered, release = threading.Event(), threading.Event()
    errors = []
    target, symbol = {
        "parse": (indexer, "_load_index_unlocked"),
        "tokenize": (indexer, "_tokenize_for_fts"),
        "claim_graph": (governance_store, "build_claim_graph_projection"),
        "publish": (indexer, "_write_json_payload"),
        "topology": (indexer, "_apply_graph_topology"),
    }[phase]
    original = getattr(target, symbol)

    def pause(*args, **kwargs):
        entered.set()
        if not release.wait(5):
            raise TimeoutError("test phase release deadline")
        return original(*args, **kwargs)

    monkeypatch.setattr(target, symbol, pause)
    if phase == "topology":
        path = indexer.get_index_path()
        data = json.loads(path.read_text(encoding="utf-8"))
        data["graph_state"]["dirty"] = True
        path.write_text(json.dumps(data), encoding="utf-8")

    def run_index():
        try:
            if phase == "topology":
                indexer.refresh_graph_topology_if_dirty()
            else:
                indexer.update_index_items(["Concept_Scope.md"])
        except BaseException as exc:
            errors.append(exc)
        finally:
            db_store.close_connection()

    worker = threading.Thread(target=run_index)
    worker.start()
    contender = None
    try:
        assert entered.wait(5), "index never reached the phase"
        contender = sqlite3.connect(str(db_store.get_db_path()), timeout=0.25, isolation_level=None)
        started = time.perf_counter()
        contender.execute("BEGIN IMMEDIATE")
        contender.execute("INSERT INTO entities (entity_id, canonical_name, data_json) VALUES ('other-writer', 'Other', '{}')")
        contender.commit()
        elapsed = time.perf_counter() - started
        record_property("competing_writer_commit_ms", round(elapsed * 1000, 3))
        assert elapsed < 0.5
        assert not release.is_set(), "writer committed only after the slow phase ended"
    finally:
        if contender is not None:
            contender.close()
        release.set()
        worker.join(7)
    assert not worker.is_alive(), "index worker did not settle"
    assert not errors, errors


def test_claim_graph_keeps_one_generation_while_canonical_writer_commits(isolated_memory, monkeypatch):
    _seed()
    entered, release = threading.Event(), threading.Event()
    errors = []
    original = governance_store.load_entities

    def pause_after_entities():
        entities = original()
        entered.set()
        if not release.wait(5):
            raise TimeoutError("claim snapshot release deadline")
        return entities

    monkeypatch.setattr(governance_store, "load_entities", pause_after_entities)

    def update():
        try:
            indexer.update_index_items(["Concept_Scope.md"])
        except BaseException as exc:
            errors.append(exc)
        finally:
            db_store.close_connection()

    worker = threading.Thread(target=update)
    worker.start()
    try:
        assert entered.wait(5)
        with db_store.transaction():
            governance_store.upsert_entity("new-e", {"entity_id": "new-e", "canonical_name": "New entity", "page_key": "Concept_New", "type": "concept"})
            governance_store.save_sources({"items": {"new-s": {"source_id": "new-s", "canonical_source_page": "Source_New"}}})
            governance_store.save_claims({"items": {"new-c": {"claim_id": "new-c", "claim_text": "New claim", "subject_entity_ids": ["new-e"], "source_ids": ["new-s"], "updated_at": "2026-10-09T00:00:00+00:00"}}})
        assert not release.is_set(), "canonical commit waited for the reader"
    finally:
        release.set()
        worker.join(7)
    assert not worker.is_alive()
    assert not errors, errors
    published = json.loads(indexer.get_claim_graph_path().read_text(encoding="utf-8"))
    assert not any(node["id"] == "new-c" for node in published["nodes"]), "mixed generations: claim was newer than the entity map"
    monkeypatch.setattr(governance_store, "load_entities", original)
    latest = governance_store.build_claim_graph_projection()
    node = next(node for node in latest["nodes"] if node["id"] == "new-c")
    assert node["subject_entities"] == ["New entity"]
    assert node["source_pages"] == ["Source_New"]


def test_index_sql_group_rolls_back_on_vector_invalidation_failure(isolated_memory, monkeypatch):
    _seed()
    conn = db_store.get_connection()
    before = tuple(conn.execute("SELECT * FROM wiki_search_index WHERE node_key = 'Concept_Scope'").fetchone())
    entity = governance_store.load_entities()["items"]["scope-a"]
    entity["raw_text"] = "updated body"
    governance_store.upsert_entity("scope-a", entity)

    def fail(_key):
        raise RuntimeError("invalidation failed")

    monkeypatch.setattr(db_store, "delete_embedding", fail)
    with pytest.raises(RuntimeError, match="invalidation failed"):
        indexer.update_index_items(["Concept_Scope.md"])
    assert tuple(conn.execute("SELECT * FROM wiki_search_index WHERE node_key = 'Concept_Scope'").fetchone()) == before
    assert not conn.in_transaction


def test_publication_failure_is_visible_and_retry_converges(isolated_memory, monkeypatch):
    _seed()
    original = indexer._write_json_payload

    def fail(*_args, **_kwargs):
        raise OSError("publication failed")

    monkeypatch.setattr(indexer, "_write_json_payload", fail)
    with pytest.raises(OSError, match="publication failed"):
        indexer.update_index_items(["Concept_Scope.md"])
    assert not db_store.get_connection().in_transaction
    monkeypatch.setattr(indexer, "_write_json_payload", original)
    indexer.update_index_items(["Concept_Scope.md"])
    assert "Concept_Scope" in json.loads(indexer.get_index_path().read_text(encoding="utf-8"))["nodes"]


def test_worker_retries_actual_index_publication_failure(isolated_memory, monkeypatch):
    from tests.test_mutation_coordinator import _source_content, _write_purpose_contract
    from vector_lake.mutation_coordinator import execute_mutation_plan
    from vector_lake.watchdog_app import process_mutation_outbox_batch

    _write_purpose_contract(isolated_memory)
    execute_mutation_plan("Source_Test.md", content=_source_content())
    indexer.generate_index()
    original = indexer._write_json_payload

    def fail(*_args, **_kwargs):
        raise OSError("worker index publication failed")

    monkeypatch.setattr(indexer, "_write_json_payload", fail)
    stats = process_mutation_outbox_batch(limit=10, backoff_base=0)
    assert stats["completed"] == 0 and stats["retrying"] == 1
    row = db_store.get_connection().execute("SELECT * FROM mutation_outbox WHERE filename = 'Source_Test.md'").fetchone()
    assert row["status"] == "pending"
    old_generation = row["lease_generation"]
    monkeypatch.setattr(indexer, "_write_json_payload", original)
    stats = process_mutation_outbox_batch(limit=10, backoff_base=0)
    assert stats["completed"] == 1 and stats["retrying"] == 0
    row = db_store.get_connection().execute("SELECT * FROM mutation_outbox WHERE filename = 'Source_Test.md'").fetchone()
    assert row["status"] == "completed" and row["lease_generation"] > old_generation
    assert "Source_Test" in json.loads(indexer.get_index_path().read_text(encoding="utf-8"))["nodes"]


@pytest.mark.parametrize("operation", [indexer.generate_index, indexer.refresh_graph_topology_if_dirty, lambda: indexer.update_index_items(["Concept_Scope.md"])])
def test_index_lock_cannot_be_acquired_under_an_outer_sqlite_transaction(isolated_memory, operation):
    db_store.init_db()
    with db_store.transaction():
        with pytest.raises(RuntimeError, match="outside a SQLite transaction"):
            operation()

