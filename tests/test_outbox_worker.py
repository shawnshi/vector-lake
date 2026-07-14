import json

from vector_lake import db_store, indexer, mutation_coordinator
from vector_lake.mutation_coordinator import execute_mutation_plan
from vector_lake.watchdog_app import WikiIndexHandler, index_queue, process_mutation_outbox_batch

from tests.test_mutation_coordinator import _source_content, _write_purpose_contract


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


def test_watchdog_ignores_managed_projection_event(isolated_memory):
    _write_purpose_contract(isolated_memory)
    while not index_queue.empty():
        index_queue.get_nowait()
        index_queue.task_done()
    execute_mutation_plan("Source_Test.md", content=_source_content())
    target = isolated_memory / "wiki" / "Source_Test.md"

    WikiIndexHandler().queue_path(str(target))

    assert index_queue.empty()
