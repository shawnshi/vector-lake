import hashlib
import os
import threading
import time

import pytest

from vector_lake import (
    db_store,
    governance_service,
    governance_store,
    indexer,
    mutation_coordinator,
    wiki_utils,
    watchdog_app,
)
from vector_lake.mutation_coordinator import execute_mutation_plan
from vector_lake.watchdog_app import (
    WikiIndexHandler,
    index_queue,
    process_legacy_projection_batch,
    process_mutation_outbox_batch,
)

from tests.test_mutation_coordinator import _named_source_content, _source_content, _write_purpose_contract


def _projection_journal_snapshot(
    outbox_ids,
    *,
    target_filename,
    source_filename,
    merged_content,
    target_projection_hash="",
    source_projection_hash="",
    target_version="",
    source_version="",
):
    return {
        "outbox_ids": list(outbox_ids),
        "target_filename": target_filename,
        "source_filename": source_filename,
        "target_version": target_version,
        "source_version": source_version,
        "target_projection_hash": target_projection_hash,
        "source_projection_hash": source_projection_hash,
        "merged_projection_hash": wiki_utils.semantic_text_hash(merged_content),
    }


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
    index_data = indexer.read_committed_index_snapshot()
    assert "Source_Test" in index_data["nodes"]


def test_watchdog_busy_gate_preserves_signal_and_does_not_claim(
    isolated_memory,
    monkeypatch,
):
    from vector_lake.heavy_task_gate import heavy_task

    db_store.init_db()
    while not index_queue.empty():
        index_queue.get_nowait()
        index_queue.task_done()
    signal_path = wiki_utils.get_outbox_signal_path()
    signal_path.write_text("1", encoding="utf-8")
    holder_acquired = threading.Event()
    holder_release = threading.Event()
    deferred = threading.Event()
    stop_event = threading.Event()
    claimed = []

    def hold_gate():
        with heavy_task(
            "projection",
            "external-holder",
            origin="pytest",
            wait_timeout_seconds=0,
        ):
            holder_acquired.set()
            holder_release.wait(timeout=3)

    def capture_status(_state, _processed, _queue, message, _error, **_kwargs):
        if "deferred by heavy-task gate" in message:
            deferred.set()

    monkeypatch.setattr(watchdog_app, "write_status", capture_status)
    monkeypatch.setattr(
        watchdog_app,
        "process_mutation_outbox_batch",
        lambda **_kwargs: claimed.append("claimed"),
    )
    holder = threading.Thread(target=hold_gate, name="watchdog-gate-holder")
    worker = threading.Thread(
        target=watchdog_app.index_worker_loop,
        args=(stop_event,),
        name="watchdog-gate-contender",
    )
    holder.start()
    assert holder_acquired.wait(timeout=2)
    worker.start()
    try:
        assert deferred.wait(timeout=2)
        assert signal_path.read_text(encoding="utf-8") == "1"
        assert claimed == []
    finally:
        stop_event.set()
        holder_release.set()
        worker.join(timeout=3)
        holder.join(timeout=3)

    assert not worker.is_alive()
    assert not holder.is_alive()


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
    monkeypatch.setattr(
        indexer,
        "projection_pair_matches_current_generation",
        lambda: True,
    )

    stats = process_mutation_outbox_batch(limit=10)

    assert stats == {"claimed": 2, "completed": 2, "retrying": 0, "failed": 0}
    assert calls == [["Concept_First.md", "Concept_Second.md"]]


def test_large_worker_batch_uses_extended_lease(isolated_memory, monkeypatch):
    captured = {}

    def claim(*, limit, lease_seconds, lease_owner):
        captured.update(
            limit=limit,
            lease_seconds=lease_seconds,
            lease_owner=lease_owner,
        )
        return []

    monkeypatch.setattr(db_store, "claim_mutation_outbox", claim)

    stats = process_mutation_outbox_batch(limit=10000)

    assert stats["claimed"] == 0
    assert captured["limit"] == 10000
    assert captured["lease_seconds"] == 3600
    assert captured["lease_owner"].startswith("watchdog:")


def test_watchdog_private_outbox_lease_helpers_are_strictly_fenced(
    isolated_memory,
):
    db_store.init_db()
    outbox_id = db_store.enqueue_mutation("Concept_Fenced.md", "delete")
    row = db_store.claim_mutation_outbox(
        limit=1,
        lease_seconds=30,
        lease_owner="watchdog:test-run:100",
    )[0]
    before = db_store.get_connection().execute(
        "SELECT lease_until, attempt_count FROM mutation_outbox WHERE id = ?",
        (outbox_id,),
    ).fetchone()

    assert watchdog_app._renew_mutation_outbox_lease(db_store, row, 120) is True
    renewed = db_store.get_connection().execute(
        "SELECT lease_until, attempt_count FROM mutation_outbox WHERE id = ?",
        (outbox_id,),
    ).fetchone()
    assert renewed["lease_until"] > before["lease_until"]
    assert renewed["attempt_count"] == 1

    for field, bad_value in (
        ("lease_owner", "foreign-owner"),
        ("lease_token", "foreign-token"),
        ("lease_generation", int(row["lease_generation"]) + 1),
    ):
        forged = dict(row)
        forged[field] = bad_value
        assert watchdog_app._renew_mutation_outbox_lease(
            db_store,
            forged,
            120,
        ) is False
        assert watchdog_app._release_mutation_outbox_lease(
            db_store,
            forged,
            "forged release",
        ) is False

    assert watchdog_app._release_mutation_outbox_lease(
        db_store,
        row,
        "bounded scheduler deferral",
    ) is True
    released = db_store.get_connection().execute(
        "SELECT status, attempt_count, lease_owner, lease_token, last_error "
        "FROM mutation_outbox WHERE id = ?",
        (outbox_id,),
    ).fetchone()
    assert dict(released) == {
        "status": "pending",
        "attempt_count": 0,
        "lease_owner": None,
        "lease_token": None,
        "last_error": "bounded scheduler deferral",
    }


def test_slow_outbox_stage_renews_lease_until_operation_returns(monkeypatch):
    renewals = []
    row = {
        "id": 7,
        "lease_owner": "owner",
        "lease_token": "token",
        "lease_generation": 3,
    }

    monkeypatch.setenv("VECTOR_LAKE_OUTBOX_LEASE_RENEW_INTERVAL_SECONDS", "0.05")
    monkeypatch.setattr(
        watchdog_app,
        "_renew_mutation_outbox_lease",
        lambda _store, current, _seconds: renewals.append(current["id"]) or True,
    )
    monkeypatch.setattr(db_store, "close_connection", lambda: None)

    active = watchdog_app._run_with_outbox_lease_renewal(
        db_store,
        [row],
        3,
        lambda _rows: time.sleep(0.13),
    )

    assert active == [row]
    assert len(renewals) >= 3


def test_outbox_materialization_does_not_hold_sqlite_write_transaction(
    isolated_memory,
    monkeypatch,
):
    db_store.init_db()
    db_store.enqueue_mutation(
        "Concept_No-IO-Lock.md",
        "update",
        payload_text="projection",
        validation_mode="schema",
    )
    observed = []

    def materialize(*_args, **_kwargs):
        observed.append(db_store.get_connection().in_transaction)

    monkeypatch.setattr(
        mutation_coordinator,
        "materialize_markdown_projection",
        materialize,
    )
    monkeypatch.setattr(indexer, "index_projection_matches_canonical", lambda _items: False)
    monkeypatch.setattr(indexer, "update_index_items", lambda _items: None)
    monkeypatch.setattr(
        indexer,
        "projection_pair_matches_current_generation",
        lambda: True,
    )

    stats = process_mutation_outbox_batch(limit=1)

    assert observed == [False]
    assert stats == {"claimed": 1, "completed": 1, "retrying": 0, "failed": 0}


def test_outbox_index_failure_isolates_poison_row_and_completes_peer(
    isolated_memory,
    monkeypatch,
):
    db_store.init_db()
    good_id = db_store.enqueue_mutation("Concept_Good.md", "delete")
    poison_id = db_store.enqueue_mutation("Concept_Poison.md", "delete")
    calls = []

    monkeypatch.setattr(indexer, "index_projection_matches_canonical", lambda _items: False)

    def index_batch(filenames):
        calls.append(list(filenames))
        if "Concept_Poison.md" in filenames:
            raise ValueError("poison projection")

    monkeypatch.setattr(indexer, "update_index_items", index_batch)
    monkeypatch.setattr(
        indexer,
        "projection_pair_matches_current_generation",
        lambda: True,
    )

    stats = process_mutation_outbox_batch(
        limit=2,
        max_attempts=3,
        backoff_base=0,
    )

    assert calls == [
        ["Concept_Good.md", "Concept_Poison.md"],
        ["Concept_Good.md"],
        ["Concept_Poison.md"],
    ]
    assert stats == {"claimed": 2, "completed": 1, "retrying": 1, "failed": 0}
    statuses = db_store.mutation_outbox_statuses([good_id, poison_id])
    assert statuses == {good_id: "completed", poison_id: "pending"}


def test_worker_completes_when_selected_index_projection_is_already_current(isolated_memory, monkeypatch):
    db_store.init_db()
    db_store.enqueue_mutation("Concept_Already-Deleted.md", "delete")
    monkeypatch.setattr(indexer, "index_projection_matches_canonical", lambda filenames: True)
    pair_checks = iter((False, True))
    monkeypatch.setattr(
        indexer,
        "projection_pair_matches_current_generation",
        lambda: next(pair_checks),
    )
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


def test_worker_skips_projection_writers_when_pair_is_already_current(
    isolated_memory,
    monkeypatch,
):
    db_store.init_db()
    db_store.enqueue_mutation("Concept_Already-Deleted.md", "delete")
    monkeypatch.setattr(
        indexer,
        "index_projection_matches_canonical",
        lambda filenames: True,
    )
    monkeypatch.setattr(
        indexer,
        "projection_pair_matches_current_generation",
        lambda: True,
    )
    monkeypatch.setattr(
        indexer,
        "update_index_items",
        lambda filenames: (_ for _ in ()).throw(
            AssertionError("current projection pair must be reused")
        ),
    )
    monkeypatch.setattr(
        indexer,
        "refresh_claim_graph_projection",
        lambda: (_ for _ in ()).throw(
            AssertionError("current claim graph must be reused")
        ),
    )

    stats = process_mutation_outbox_batch(limit=10)

    assert stats == {"claimed": 1, "completed": 1, "retrying": 0, "failed": 0}


def test_worker_skips_duplicate_markdown_projection(isolated_memory, monkeypatch):
    _write_purpose_contract(isolated_memory)
    execute_mutation_plan("Source_Test.md", content=_source_content())
    monkeypatch.setattr(
        mutation_coordinator,
        "materialize_markdown_projection",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("projection should be reused")),
    )
    monkeypatch.setattr(indexer, "update_index_items", lambda filenames: None)
    monkeypatch.setattr(
        indexer,
        "projection_pair_matches_current_generation",
        lambda: True,
    )

    stats = process_mutation_outbox_batch(limit=10)

    assert stats["completed"] == 1


def test_worker_rebuilds_damaged_stale_pair_before_marking_completed(
    isolated_memory,
):
    _write_purpose_contract(isolated_memory)
    db_store.init_db()
    indexer.generate_index()
    execute_mutation_plan("Source_Test.md", content=_source_content())
    wiki_utils.get_projection_manifest_path().write_bytes(b"{")

    stats = process_mutation_outbox_batch(limit=10)

    assert stats == {"claimed": 1, "completed": 1, "retrying": 0, "failed": 0}
    row = db_store.get_connection().execute(
        "SELECT status FROM mutation_outbox"
    ).fetchone()
    assert row["status"] == "completed"
    committed = indexer.read_committed_index_snapshot()
    assert "Source_Test" in committed["nodes"]
    assert indexer.projection_pair_matches_current_generation() is True


def test_worker_does_not_complete_when_writer_returns_without_committed_pair(
    isolated_memory,
    monkeypatch,
):
    db_store.init_db()
    outbox_id = db_store.enqueue_mutation("Concept_Not-Ready.md", "delete")
    monkeypatch.setattr(
        indexer,
        "index_projection_matches_canonical",
        lambda _filenames: False,
    )
    monkeypatch.setattr(indexer, "update_index_items", lambda _filenames: None)
    monkeypatch.setattr(
        indexer,
        "projection_pair_matches_current_generation",
        lambda: False,
    )

    stats = process_mutation_outbox_batch(
        limit=10,
        max_attempts=3,
        backoff_base=0,
    )

    assert stats == {"claimed": 1, "completed": 0, "retrying": 1, "failed": 0}
    row = db_store.get_connection().execute(
        "SELECT status, last_error FROM mutation_outbox WHERE id = ?",
        (outbox_id,),
    ).fetchone()
    assert row["status"] == "pending"
    assert "not committed" in row["last_error"]


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
    source_outbox_id = db_store.enqueue_mutation(
        "Source_CAS-Race-Duplicate.md",
        "delete",
        validation_mode="schema",
        base_version="",
        projection_base_hash="",
    )
    outbox_ids = [outbox_id, source_outbox_id]
    journal_id = "journal_projection_cas_after_hash"
    item_id = "gov_projection_cas_after_hash"
    db_store.record_merge_journal(
        journal_id,
        item_id,
        _projection_journal_snapshot(
            outbox_ids,
            target_filename=target.name,
            source_filename="Source_CAS-Race-Duplicate.md",
            merged_content=replacement,
            target_projection_hash=base_hash,
        ),
        status="projection_pending",
    )
    governance_store.upsert_governance_item(
        {
            "item_id": item_id,
            "type": "merge",
            "status": "projection_pending",
            "merge_candidate": {},
            "merge_journal_id": journal_id,
            "merge_outbox_ids": outbox_ids,
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
    assert pending["merge_outbox_statuses"] == {
        outbox_id: "pending",
        source_outbox_id: "completed",
    }
    assert pending["failed_outbox_recovery"]["requeued"] == [outbox_id]


def test_explicit_failed_outbox_recovery_requeues_latest_intent(
    isolated_memory,
):
    db_store.init_db()
    outbox_id = db_store.enqueue_mutation(
        "Concept_Recover-Latest.md",
        "delete",
        idempotency_key="recover-latest",
    )
    conn = db_store.get_connection()
    with db_store.transaction():
        conn.execute(
            "UPDATE mutation_outbox SET status = 'failed', attempt_count = 3, "
            "last_error = 'transient failure', completed_at = created_at "
            "WHERE id = ?",
            (outbox_id,),
        )

    recovery = db_store.recover_failed_mutation_outbox([outbox_id])
    row = conn.execute(
        "SELECT status, attempt_count, last_error, completed_at, lease_owner "
        "FROM mutation_outbox WHERE id = ?",
        (outbox_id,),
    ).fetchone()

    assert recovery == {"requeued": [outbox_id], "superseded": {}, "skipped": {}}
    assert row["status"] == "pending"
    assert row["attempt_count"] == 0
    assert row["last_error"] == "transient failure"
    assert row["completed_at"] is None
    assert row["lease_owner"] is None
    claimed = db_store.claim_mutation_outbox(outbox_ids=[outbox_id])
    assert [item["id"] for item in claimed] == [outbox_id]
    assert claimed[0]["attempt_count"] == 1


def test_failed_outbox_recovery_fences_older_intent_behind_newer_work(
    isolated_memory,
):
    db_store.init_db()
    old_id = db_store.enqueue_mutation(
        "Concept_Recover-Ordering.md",
        "update",
        payload_text="old",
        idempotency_key="recover-ordering-old",
        validation_mode="schema",
    )
    conn = db_store.get_connection()
    with db_store.transaction():
        conn.execute(
            "UPDATE mutation_outbox SET status = 'failed', attempt_count = 3 "
            "WHERE id = ?",
            (old_id,),
        )
    newer_id = db_store.enqueue_mutation(
        "Concept_Recover-Ordering.md",
        "update",
        payload_text="new",
        idempotency_key="recover-ordering-new",
        validation_mode="schema",
    )

    recovery = db_store.recover_failed_mutation_outbox([old_id])
    old = conn.execute(
        "SELECT status, superseded_by FROM mutation_outbox WHERE id = ?",
        (old_id,),
    ).fetchone()

    assert recovery == {
        "requeued": [],
        "superseded": {old_id: newer_id},
        "skipped": {},
    }
    assert old["status"] == "superseded"
    assert old["superseded_by"] == newer_id


def test_failed_outbox_recovery_does_not_overtake_newer_failed_intent(
    isolated_memory,
):
    db_store.init_db()
    conn = db_store.get_connection()
    old_id = db_store.enqueue_mutation(
        "Concept_Recover-Failed-Ordering.md",
        "update",
        payload_text="old",
        idempotency_key="recover-failed-old",
        validation_mode="schema",
    )
    with db_store.transaction():
        conn.execute(
            "UPDATE mutation_outbox SET status = 'failed', attempt_count = 3 "
            "WHERE id = ?",
            (old_id,),
        )
    newer_id = db_store.enqueue_mutation(
        "Concept_Recover-Failed-Ordering.md",
        "update",
        payload_text="new",
        idempotency_key="recover-failed-new",
        validation_mode="schema",
    )
    with db_store.transaction():
        conn.execute(
            "UPDATE mutation_outbox SET status = 'failed', attempt_count = 3 "
            "WHERE id = ?",
            (newer_id,),
        )

    recovery = db_store.recover_failed_mutation_outbox([old_id])

    assert recovery == {
        "requeued": [],
        "superseded": {},
        "skipped": {old_id: f"newer_failed_intent:{newer_id}"},
    }
    assert db_store.mutation_outbox_statuses([old_id, newer_id]) == {
        old_id: "failed",
        newer_id: "failed",
    }


def test_restricted_outbox_claim_does_not_supersede_unrelated_rows(
    isolated_memory,
):
    db_store.init_db()
    unrelated_old_id = db_store.enqueue_mutation(
        "Concept_Unrelated-Ordering.md",
        "update",
        payload_text="old",
        idempotency_key="unrelated-ordering-old",
        validation_mode="schema",
    )
    unrelated_new_id = db_store.enqueue_mutation(
        "Concept_Unrelated-Ordering.md",
        "update",
        payload_text="new",
        idempotency_key="unrelated-ordering-new",
        validation_mode="schema",
    )
    requested_id = db_store.enqueue_mutation(
        "Concept_Requested.md",
        "delete",
        idempotency_key="requested-only",
        validation_mode="schema",
    )
    conn = db_store.get_connection()
    with db_store.transaction():
        conn.execute(
            "UPDATE mutation_outbox SET status = 'pending', superseded_by = NULL, "
            "completed_at = NULL WHERE id = ?",
            (unrelated_old_id,),
        )

    claimed = db_store.claim_mutation_outbox(outbox_ids=[requested_id])

    assert [row["id"] for row in claimed] == [requested_id]
    assert db_store.mutation_outbox_statuses(
        [unrelated_old_id, unrelated_new_id]
    ) == {
        unrelated_old_id: "pending",
        unrelated_new_id: "pending",
    }


def test_projection_pending_review_recovers_terminal_failed_outbox(
    isolated_memory,
    monkeypatch,
):
    db_store.init_db()
    merged_content = "---\ntype: concept\ntitle: Recover\n---\n\nRecovered.\n"
    target_outbox_id = db_store.enqueue_mutation(
        "Concept_Recover-Target.md",
        "update",
        payload_text=merged_content,
        validation_mode="schema",
    )
    source_outbox_id = db_store.enqueue_mutation(
        "Concept_Recover-Projection.md",
        "delete",
        idempotency_key="recover-projection",
        validation_mode="schema",
    )
    outbox_ids = [target_outbox_id, source_outbox_id]
    conn = db_store.get_connection()
    with db_store.transaction():
        conn.execute(
            "UPDATE mutation_outbox SET status = 'completed', "
            "completed_at = created_at WHERE id = ?",
            (target_outbox_id,),
        )
        conn.execute(
            "UPDATE mutation_outbox SET status = 'failed', attempt_count = 3, "
            "last_error = 'transient projection failure', completed_at = created_at "
            "WHERE id = ?",
            (source_outbox_id,),
        )
    journal_id = "journal_recover_projection"
    item_id = "gov_recover_projection"
    db_store.record_merge_journal(
        journal_id,
        item_id,
        _projection_journal_snapshot(
            outbox_ids,
            target_filename="Concept_Recover-Target.md",
            source_filename="Concept_Recover-Projection.md",
            merged_content=merged_content,
        ),
        status="projection_pending",
    )
    governance_store.upsert_governance_item(
        {
            "item_id": item_id,
            "type": "merge",
            "title": "Recover projection",
            "status": "projection_pending",
            "merge_candidate": {},
            "merge_journal_id": journal_id,
            "merge_outbox_ids": outbox_ids,
        }
    )
    monkeypatch.setattr(
        indexer,
        "index_projection_matches_canonical",
        lambda _filenames: True,
    )
    monkeypatch.setattr(
        indexer,
        "projection_pair_matches_current_generation",
        lambda: True,
    )
    monkeypatch.setattr(
        governance_service,
        "_post_merge_errors",
        lambda _candidate, _journal: [],
    )

    resolved = governance_service.resolve_governance_item(
        item_id,
        resolution="merge",
    )

    assert resolved["status"] == "resolved"
    assert resolved["merge_outbox_statuses"] == {
        target_outbox_id: "completed",
        source_outbox_id: "completed",
    }
    assert resolved["failed_outbox_recovery"]["requeued"] == [source_outbox_id]
    assert db_store.mutation_outbox_statuses(outbox_ids) == {
        target_outbox_id: "completed",
        source_outbox_id: "completed",
    }
    assert db_store.get_merge_journal(journal_id)["status"] == "completed"


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
