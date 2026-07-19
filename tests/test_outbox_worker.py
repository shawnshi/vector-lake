import json

from vector_lake import db_store, governance_store, indexer, mutation_coordinator
from vector_lake.mutation_coordinator import execute_mutation_plan
from vector_lake.watchdog_app import (
    WikiIndexHandler,
    index_queue,
    process_legacy_projection_batch,
    process_mutation_outbox_batch,
)

from tests.test_mutation_coordinator import _named_source_content, _source_content, _write_purpose_contract


def test_worker_recovers_projection_without_signal(isolated_memory):
    _write_purpose_contract(isolated_memory)
    execute_mutation_plan("Source_Test.md", content=_source_content())
    target = isolated_memory / "wiki" / "Source_Test.md"
    target.unlink()

    stats = process_mutation_outbox_batch(limit=10)

    assert stats == {"claimed": 1, "completed": 1, "retrying": 0, "failed": 0}
    assert target.read_text(encoding="utf-8") == _source_content()
    row = db_store.get_connection().execute("SELECT status FROM mutation_outbox").fetchone()
    assert row["status"] == "completed"
    index_data = json.loads((isolated_memory / "wiki" / "index.json").read_text(encoding="utf-8"))
    assert "Source_Test" in index_data["nodes"]


def test_worker_continues_after_one_row_fails(isolated_memory):
    db_store.init_db()
    first_id = db_store.enqueue_mutation("Concept_Missing.md", "update", payload_text=None)
    second_id = db_store.enqueue_mutation("Concept_Delete.md", "delete")

    stats = process_mutation_outbox_batch(limit=10, max_attempts=3, backoff_base=0)

    assert stats == {"claimed": 2, "completed": 1, "retrying": 1, "failed": 0}
    rows = {
        row["id"]: row["status"]
        for row in db_store.get_connection().execute("SELECT id, status FROM mutation_outbox")
    }
    assert rows[first_id] == "pending"
    assert rows[second_id] == "completed"


def test_worker_batches_index_update_once_for_all_ready_rows(isolated_memory, monkeypatch):
    db_store.init_db()
    db_store.enqueue_mutation("Concept_First.md", "delete")
    db_store.enqueue_mutation("Concept_Second.md", "delete")
    calls = []
    monkeypatch.setattr(indexer, "update_index_items", lambda filenames: calls.append(list(filenames)))

    stats = process_mutation_outbox_batch(limit=10)

    assert stats == {"claimed": 2, "completed": 2, "retrying": 0, "failed": 0}
    assert calls == [["Concept_First.md", "Concept_Second.md"]]


def test_large_worker_batch_uses_extended_lease(isolated_memory, monkeypatch):
    captured = {}

    def claim(*, limit, lease_seconds):
        captured.update(limit=limit, lease_seconds=lease_seconds)
        return []

    monkeypatch.setattr(db_store, "claim_mutation_outbox", claim)

    stats = process_mutation_outbox_batch(limit=10000)

    assert stats["claimed"] == 0
    assert captured == {"limit": 10000, "lease_seconds": 3600}


def test_worker_completes_when_selected_index_projection_is_already_current(isolated_memory, monkeypatch):
    db_store.init_db()
    db_store.enqueue_mutation("Concept_Already-Deleted.md", "delete")
    monkeypatch.setattr(indexer, "index_projection_matches_canonical", lambda filenames: True)
    monkeypatch.setattr(
        indexer,
        "update_index_items",
        lambda filenames: (_ for _ in ()).throw(AssertionError("current index must be reused")),
    )
    refreshed = []
    monkeypatch.setattr(
        indexer,
        "refresh_claim_graph_projection",
        lambda: refreshed.append("claim_graph"),
    )

    stats = process_mutation_outbox_batch(limit=10)

    assert stats == {"claimed": 1, "completed": 1, "retrying": 0, "failed": 0}
    assert refreshed == ["claim_graph"]


def test_worker_skips_duplicate_markdown_projection(isolated_memory, monkeypatch):
    _write_purpose_contract(isolated_memory)
    execute_mutation_plan("Source_Test.md", content=_source_content())
    monkeypatch.setattr(
        mutation_coordinator,
        "materialize_markdown_projection",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("projection should be reused")),
    )
    monkeypatch.setattr(indexer, "update_index_items", lambda filenames: None)

    stats = process_mutation_outbox_batch(limit=10)

    assert stats["completed"] == 1


def test_worker_does_not_materialize_or_index_after_lease_is_superseded(
    isolated_memory,
    monkeypatch,
):
    db_store.init_db()
    old_id = db_store.enqueue_mutation(
        "Concept_Race.md",
        "update",
        payload_text="old payload",
        idempotency_key="old-race",
        validation_mode="schema",
    )
    original_check = db_store.mutation_outbox_lease_is_current
    checks = {"count": 0}

    def supersede_before_materialize(*args):
        checks["count"] += 1
        if checks["count"] == 2:
            db_store.enqueue_mutation(
                "Concept_Race.md",
                "update",
                payload_text="new payload",
                idempotency_key="new-race",
                validation_mode="schema",
            )
        return original_check(*args)

    indexed = []
    monkeypatch.setattr(db_store, "mutation_outbox_lease_is_current", supersede_before_materialize)
    monkeypatch.setattr(indexer, "update_index_items", lambda filenames: indexed.append(list(filenames)))

    stats = process_mutation_outbox_batch(limit=1)

    assert stats == {"claimed": 1, "completed": 0, "retrying": 0, "failed": 0}
    assert not (isolated_memory / "wiki" / "Concept_Race.md").exists()
    assert indexed == []
    rows = db_store.get_connection().execute(
        "SELECT id, status, superseded_by FROM mutation_outbox ORDER BY id"
    ).fetchall()
    assert dict(rows[0]) == {"id": old_id, "status": "superseded", "superseded_by": rows[1]["id"]}
    assert rows[1]["status"] == "pending"


def test_watchdog_ignores_managed_projection_event(isolated_memory):
    _write_purpose_contract(isolated_memory)
    while not index_queue.empty():
        index_queue.get_nowait()
        index_queue.task_done()
    execute_mutation_plan("Source_Test.md", content=_source_content())
    target = isolated_memory / "wiki" / "Source_Test.md"

    WikiIndexHandler().queue_path(str(target))

    assert index_queue.empty()


def test_watchdog_promotes_manual_projection_edit_to_canonical_and_outbox(
    isolated_memory,
    monkeypatch,
):
    _write_purpose_contract(isolated_memory)
    execute_mutation_plan("Source_Test.md", content=_source_content())
    execute_mutation_plan(
        "Source_Unrelated.md",
        content=_named_source_content("source_unrelated", "Unrelated Source"),
    )
    target = isolated_memory / "wiki" / "Source_Test.md"
    edited = target.read_text(encoding="utf-8").replace(
        "Primary source content.",
        "Manually revised source content.",
    )
    target.write_text(edited, encoding="utf-8")
    for loader_name in (
        "load_entities",
        "load_claims",
        "load_evidence",
        "load_sources",
        "load_change_sets",
    ):
        monkeypatch.setattr(
            governance_store,
            loader_name,
            lambda: (_ for _ in ()).throw(AssertionError("legacy full history load")),
        )
    before_change_sets = db_store.get_connection().execute("SELECT COUNT(*) FROM change_sets").fetchone()[0]
    unrelated_before = db_store.get_connection().execute(
        "SELECT data_json FROM entities WHERE json_extract(data_json, '$.id') = 'source_unrelated'"
    ).fetchone()[0]

    stats = process_legacy_projection_batch(["Source_Test.md"])

    assert stats == {"completed": 1, "failed": 0}
    assert db_store.get_connection().execute("SELECT COUNT(*) FROM change_sets").fetchone()[0] == before_change_sets + 1
    assert db_store.get_connection().execute(
        "SELECT data_json FROM entities WHERE json_extract(data_json, '$.id') = 'source_unrelated'"
    ).fetchone()[0] == unrelated_before
    assert governance_store.canonical_page_versions({"Source_Test"})["Source_Test"] == (
        governance_store.canonical_page_version_from_content("Source_Test.md", edited)
    )
    outbox = db_store.get_connection().execute(
        "SELECT mutation_type, payload_text FROM mutation_outbox ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert outbox["mutation_type"] == "update"
    assert outbox["payload_text"] == edited
