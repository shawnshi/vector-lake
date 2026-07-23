import hashlib
import json
import os

import pytest

from vector_lake import (
    db_store,
    governance_service,
    governance_store,
    indexer,
    mutation_coordinator,
    wiki_utils,
)
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


def test_worker_projection_cas_does_not_overwrite_manual_edit(
    isolated_memory,
    monkeypatch,
):
    _write_purpose_contract(isolated_memory)
    target = isolated_memory / "wiki" / "Source_CAS.md"
    base = _source_content()
    manual = base.replace("Primary source content.", "Manual concurrent edit.")
    target.write_text(manual, encoding="utf-8")
    base_hash = hashlib.sha256(base.encode("utf-8")).hexdigest()
    outbox_id = db_store.enqueue_mutation(
        "Source_CAS.md",
        "update",
        payload_text=base,
        idempotency_key="projection-cas",
        validation_mode="schema",
        projection_base_hash=base_hash,
    )
    monkeypatch.setattr(
        indexer,
        "update_index_items",
        lambda filenames: (_ for _ in ()).throw(
            AssertionError("CAS-conflicted projection must not be indexed")
        ),
    )

    stats = process_mutation_outbox_batch(
        limit=1,
        max_attempts=1,
        backoff_base=0,
    )

    assert stats == {"claimed": 1, "completed": 0, "retrying": 0, "failed": 1}
    assert target.read_text(encoding="utf-8") == manual
    row = db_store.get_connection().execute(
        "SELECT status, last_error, projection_base_hash FROM mutation_outbox WHERE id = ?",
        (outbox_id,),
    ).fetchone()
    assert row["status"] == "failed"
    assert "compare-and-swap conflict" in row["last_error"]
    assert row["projection_base_hash"] == base_hash


@pytest.mark.skipif(os.name != "nt", reason="ReplaceFileW CAS is Windows-specific")
def test_worker_projection_cas_rolls_back_edit_injected_after_hash(
    isolated_memory,
    monkeypatch,
):
    _write_purpose_contract(isolated_memory)
    target = isolated_memory / "wiki" / "Source_CAS-Race.md"
    base = _named_source_content("CAS Race", "Known base content.")
    replacement = _named_source_content("CAS Race", "Recovered content.")
    manual = _named_source_content("CAS Race", "Manual edit after hash read.")
    target.write_text(base, encoding="utf-8")
    base_hash = hashlib.sha256(target.read_bytes()).hexdigest()
    outbox_id = db_store.enqueue_mutation(
        target.name,
        "update",
        payload_text=replacement,
        idempotency_key="projection-cas-after-hash",
        validation_mode="schema",
        projection_base_hash=base_hash,
    )
    journal_id = "journal_projection_cas_after_hash"
    item_id = "gov_projection_cas_after_hash"
    db_store.record_merge_journal(
        journal_id,
        item_id,
        {"outbox_ids": [outbox_id]},
        status="projection_pending",
    )
    governance_store.upsert_governance_item(
        {
            "item_id": item_id,
            "type": "merge",
            "status": "projection_pending",
            "merge_candidate": {},
            "merge_journal_id": journal_id,
            "merge_outbox_ids": [outbox_id],
        }
    )

    real_replace = wiki_utils._replace_file_with_backup
    injected = False

    def inject_edit_after_hash(replaced_path, replacement_path, backup_path):
        nonlocal injected
        if not injected:
            injected = True
            replaced_path.write_text(manual, encoding="utf-8")
        return real_replace(replaced_path, replacement_path, backup_path)

    monkeypatch.setattr(
        wiki_utils,
        "_replace_file_with_backup",
        inject_edit_after_hash,
    )
    stats = process_mutation_outbox_batch(
        limit=1,
        max_attempts=1,
        backoff_base=0,
    )

    assert injected is True
    assert stats == {"claimed": 1, "completed": 0, "retrying": 0, "failed": 1}
    assert target.read_text(encoding="utf-8") == manual
    assert not list(target.parent.glob(f"{target.name}.*.cas-*"))
    row = db_store.get_connection().execute(
        "SELECT status, last_error FROM mutation_outbox WHERE id = ?",
        (outbox_id,),
    ).fetchone()
    assert row["status"] == "failed"
    assert "compare-and-swap conflict" in row["last_error"]

    pending = governance_service.resolve_governance_item(item_id, resolution="merge")
    assert pending["status"] == "projection_pending"
    assert pending["merge_outbox_statuses"] == {outbox_id: "failed"}


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


def test_watchdog_ignores_managed_projection_event_with_mixed_newlines(
    isolated_memory,
):
    _write_purpose_contract(isolated_memory)
    while not index_queue.empty():
        index_queue.get_nowait()
        index_queue.task_done()
    mixed = _source_content().replace(
        "Primary source content.\n",
        "Primary source content.\r\n\r\nSecond line.\n",
    )
    execute_mutation_plan("Source_Mixed-Newlines.md", content=mixed)
    target = isolated_memory / "wiki" / "Source_Mixed-Newlines.md"

    WikiIndexHandler().queue_path(str(target))

    assert target.read_bytes() == mixed.encode("utf-8")
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
