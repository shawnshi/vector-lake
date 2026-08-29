from __future__ import annotations

import errno
import os
import queue
import sqlite3
import threading
import time
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from vector_lake import db_store, governance_store
from vector_lake.watchdog_app import (
    DiaryWatchdogHandler,
    RawWatchdogHandler,
    WikiIndexEventBuffer,
    _scan_wiki_projection_drift,
    _scan_wiki_reconcile_plan,
    _wiki_reconcile_marker_path,
    expire_stale_ingest_jobs_for_watchdog,
    maintain_operational_memory_search_for_watchdog,
    process_legacy_projection_queue_batch,
    reconcile_wiki_overflow_once,
)


def test_operational_memory_auto_maintenance_is_bounded(monkeypatch):
    from vector_lake import heavy_task_gate

    observed = {}
    monkeypatch.setattr(
        governance_store,
        "operational_memory_search_index_status",
        lambda **_kwargs: {
            "configured": True,
            "available": True,
            "ready": False,
            "auto_maintenance_configured": True,
        },
    )

    def maintain(**kwargs):
        observed.update(kwargs)
        return {
            "configured": True,
            "available": True,
            "ready": True,
            "batches": 3,
        }

    monkeypatch.setattr(
        governance_store,
        "maintain_operational_memory_search_index_budget",
        maintain,
    )
    monkeypatch.setattr(
        heavy_task_gate,
        "heavy_task",
        lambda *_args, **_kwargs: nullcontext(),
    )
    monkeypatch.setattr(db_store, "close_connection", lambda: None)
    monkeypatch.setenv("VECTOR_LAKE_OPERATIONAL_MEMORY_AUTO_BATCH", "512")
    monkeypatch.setenv("VECTOR_LAKE_OPERATIONAL_MEMORY_AUTO_MAX_BATCHES", "4")
    monkeypatch.setenv("VECTOR_LAKE_OPERATIONAL_MEMORY_AUTO_WALL_SECONDS", "2")

    result = maintain_operational_memory_search_for_watchdog()

    assert result["ready"] is True
    assert result["deferred"] is False
    assert observed == {
        "batch_size": 512,
        "max_batches": 4,
        "wall_seconds": 2.0,
    }


def test_operational_memory_gate_busy_defers_before_full_attestation(monkeypatch):
    from vector_lake import heavy_task_gate

    status_calls = []

    def quick_status(*, allow_integrity_scan=True):
        status_calls.append(allow_integrity_scan)
        if allow_integrity_scan:
            raise AssertionError("gate-busy path must not run a full attestation")
        return {
            "configured": True,
            "available": True,
            "ready": False,
            "auto_maintenance_configured": True,
            "status": "backfilling",
            "warnings": ["operational_memory_search_attestation_required"],
        }

    class BusyLease:
        def __enter__(self):
            raise heavy_task_gate.HeavyTaskBusy(
                task_class="scan",
                operation="watchdog-operational-memory-index",
                origin="watchdog",
                wait_timeout_seconds=0,
                gate_status={"current": {"operation": "external-holder"}},
            )

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(
        governance_store,
        "operational_memory_search_index_status",
        quick_status,
    )
    monkeypatch.setattr(heavy_task_gate, "heavy_task", lambda *_a, **_k: BusyLease())

    started = time.perf_counter()
    result = maintain_operational_memory_search_for_watchdog()

    assert time.perf_counter() - started < 0.1
    assert result["deferred"] is True
    assert result["batches"] == 0
    assert status_calls == [False]


def test_ready_watchdog_cycles_reuse_revision_cache_and_detect_external_tamper(
    isolated_memory, monkeypatch
):
    monkeypatch.setenv("VECTOR_LAKE_OPERATIONAL_MEMORY_FTS", "1")
    monkeypatch.setenv("VECTOR_LAKE_OPERATIONAL_MEMORY_ATTESTATION_SECONDS", "60")
    monkeypatch.setenv("VECTOR_LAKE_OPERATIONAL_MEMORY_AUTO_MAINTAIN", "1")
    monkeypatch.setenv("VECTOR_LAKE_OPERATIONAL_MEMORY_AUTO_BATCH", "2")
    monkeypatch.setenv("VECTOR_LAKE_OPERATIONAL_MEMORY_AUTO_MAX_BATCHES", "1")
    records = [
        {
            "memory_id": f"memory_{index}",
            "memory_type": "fact",
            "memory_key": f"watchdog_key_{index}",
            "text": "watchdog proof needle" if index == 3 else "unrelated",
            "source_page": "Concept_Watchdog-Proof.md",
            "validity_state": "active",
            "memory_score": 0.8,
        }
        for index in range(4)
    ]
    governance_store.initialize_meta_store()
    governance_store.save_memory_objects({
        "items": {record["memory_id"]: record for record in records}
    })
    assert governance_store.maintain_operational_memory_search_index(100)["ready"]
    conn = db_store.get_connection()
    state_before = tuple(conn.execute(
        "SELECT proof_generation, updated_at "
        "FROM operational_memory_search_state WHERE singleton = 1"
    ).fetchone())
    db_store.close_all_connections()

    real_inspect = db_store.inspect_operational_memory_search_integrity
    inspections = []

    def counted_inspect(*args, **kwargs):
        inspections.append(1)
        return real_inspect(*args, **kwargs)

    monkeypatch.setattr(
        db_store,
        "inspect_operational_memory_search_integrity",
        counted_inspect,
    )
    cold_started = time.perf_counter()
    first = maintain_operational_memory_search_for_watchdog()
    cold_seconds = time.perf_counter() - cold_started
    watchdog_conn = db_store.get_connection()
    hot_started = time.perf_counter()
    second = maintain_operational_memory_search_for_watchdog()
    hot_seconds = time.perf_counter() - hot_started

    assert first["ready"] is second["ready"] is True
    assert first["batches"] == second["batches"] == 0
    assert db_store.get_connection() is watchdog_conn
    assert inspections == [1]
    assert tuple(watchdog_conn.execute(
        "SELECT proof_generation, updated_at "
        "FROM operational_memory_search_state WHERE singleton = 1"
    ).fetchone()) == state_before

    doc_id = int(watchdog_conn.execute(
        "SELECT doc_id FROM operational_memory_search_docs WHERE memory_id = ?",
        ("memory_3",),
    ).fetchone()[0])
    external = sqlite3.connect(str(db_store.get_db_path()))
    try:
        external.execute(
            "DELETE FROM operational_memory_search_fts WHERE rowid = ?",
            (doc_id,),
        )
        external.execute(
            "INSERT INTO operational_memory_search_fts "
            "(rowid, key_text, memory_text, page_text, type_text) "
            "VALUES (?, 'stale', 'equal count tamper', 'stale', 'fact')",
            (doc_id,),
        )
        external.execute(
            "DELETE FROM operational_memory_search_short_fts WHERE rowid = ?",
            (doc_id,),
        )
        external.execute(
            "INSERT INTO operational_memory_search_short_fts "
            "(rowid, short_text) VALUES (?, 'equal count tamper')",
            (doc_id,),
        )
        external.commit()
    finally:
        external.close()

    watchdog_conn._operational_memory_search_integrity_cache[
        "attested_at_monotonic"
    ] -= 61

    repairing = maintain_operational_memory_search_for_watchdog()
    assert repairing["ready"] is False
    assert repairing["batches"] == 1
    assert inspections == [1, 1]
    repaired = maintain_operational_memory_search_for_watchdog()
    assert repaired["ready"] is True
    assert repaired["batches"] == 1
    assert inspections == [1, 1, 1]
    assert [
        item["memory_id"]
        for item in governance_store.search_operational_memory("needle", top_k=10)
    ] == ["memory_3"]
    print(
        "watchdog operational-memory cache benchmark "
        f"cold={cold_seconds:.6f}s hot={hot_seconds:.6f}s"
    )


def test_watchdog_memory_maintenance_failure_discards_connection(
    isolated_memory, monkeypatch
):
    governance_store.initialize_meta_store()
    failed_conn = db_store.get_connection()

    def fail_status(**_kwargs):
        assert db_store.get_connection() is failed_conn
        raise RuntimeError("injected watchdog maintenance failure")

    monkeypatch.setattr(
        governance_store,
        "operational_memory_search_index_status",
        fail_status,
    )
    with pytest.raises(RuntimeError, match="injected watchdog maintenance failure"):
        maintain_operational_memory_search_for_watchdog()

    assert db_store.get_connection() is not failed_conn
    with pytest.raises(sqlite3.ProgrammingError):
        failed_conn.execute("SELECT 1")


class _EmptyWatchdogIndexQueue:
    full_reconcile_required = False
    max_pending = 1

    @staticmethod
    def qsize():
        return 0

    @staticmethod
    def empty():
        return True

    @staticmethod
    def get_nowait():
        raise queue.Empty

    @staticmethod
    def task_done():
        return None

    @staticmethod
    def claim_full_reconcile_marker(**_kwargs):
        return None


def test_stale_ingest_jobs_can_be_expired_by_background_maintenance(
    isolated_memory, monkeypatch
):
    db_store.init_db()
    conn = db_store.get_connection()
    old = "2000-01-01T00:00:00+00:00"
    with db_store.transaction():
        conn.execute(
            "INSERT INTO jobs "
            "(job_id, task_type, payload, status, created_at, updated_at, available_at) "
            "VALUES (?, 'ingest', '{}', 'awaiting_subagent', ?, ?, ?)",
            ("job_background_expiry", old, old, old),
        )

    monkeypatch.setenv("VECTOR_LAKE_INGEST_TASK_MAX_AGE_SECONDS", "60")
    expired = expire_stale_ingest_jobs_for_watchdog()

    assert expired == 1
    row = conn.execute(
        "SELECT status FROM jobs WHERE job_id = 'job_background_expiry'"
    ).fetchone()
    assert row["status"] == "failed"


def test_wiki_index_event_buffer_is_bounded_generation_tracked_and_cas_cleared():
    events = WikiIndexEventBuffer(max_pending=2)

    assert events.put("Concept_Alpha.md") is True
    assert events.put(os.path.join(".", "Concept_Alpha.md")) is False
    assert events.put("Concept_Beta.md") is True

    assert events.put("Concept_Gamma.md") is False
    assert events.qsize() == 2
    assert events.full_reconcile_required is True
    first_generation = events.claim_full_reconcile_marker(
        retry_interval_seconds=30,
        now=100,
    )
    assert first_generation == 1
    assert (
        events.claim_full_reconcile_marker(
            retry_interval_seconds=30,
            now=101,
        )
        is None
    )

    # Coalesced events stay bounded but advance the generation, so an old scan
    # cannot acknowledge changes that arrived while it was running.
    assert events.put("Concept_Delta.md") is False
    assert events.reconcile_generation == 2
    assert events.clear_full_reconcile(first_generation) is False
    assert events.allow_immediate_full_reconcile_retry() is True
    current_generation = events.claim_full_reconcile_marker(
        retry_interval_seconds=30,
        now=101,
    )
    assert current_generation == 2

    assert events.get_nowait() == "Concept_Alpha.md"
    events.task_done()
    assert events.get_nowait() == "Concept_Beta.md"
    events.task_done()

    assert events.clear_full_reconcile(current_generation) is True
    assert events.full_reconcile_required is False
    assert events.put("Concept_Gamma.md") is True


def test_overflow_scan_finds_only_canonical_page_add_drift_and_delete(
    isolated_memory,
    monkeypatch,
):
    wiki_dir = isolated_memory / "wiki"
    (wiki_dir / "Concept_Added.MD").write_text("added", encoding="utf-8")
    (wiki_dir / "Concept_Drift.md").write_text("new", encoding="utf-8")
    (wiki_dir / "Concept_Stable.md").write_text("same", encoding="utf-8")
    for filename in (
        "index.md",
        "log.md",
        "overview.md",
        "orphan_pages.md",
        "wiki_link_stats.md",
        "Synthesis_log.md",
        "System_Runtime.md",
    ):
        (wiki_dir / filename).write_text("ignored", encoding="utf-8")

    canonical_versions = {
        "Concept_Deleted": "gone",
        "Concept_Drift": "old",
        "Concept_Stable": "same",
        "Synthesis_log": "ignored",
        "System_Canonical": "ignored",
    }
    monkeypatch.setattr(
        governance_store,
        "canonical_page_versions",
        lambda page_keys=None: canonical_versions,
    )
    monkeypatch.setattr(
        governance_store,
        "canonical_page_version_from_content",
        lambda _filename, content: content.strip(),
    )

    scan = _scan_wiki_projection_drift(limit=25)

    assert scan == {
        "candidates": [
            "Concept_Added.MD",
            "Concept_Deleted.md",
            "Concept_Drift.md",
        ],
        "errors": [],
        "total_drift": 3,
    }


def test_overflow_scan_caps_each_projection_batch_at_25(
    isolated_memory,
    monkeypatch,
):
    wiki_dir = isolated_memory / "wiki"
    for index in range(30):
        (wiki_dir / f"Concept_{index:02d}.md").write_text(
            "added",
            encoding="utf-8",
        )
    monkeypatch.setattr(
        governance_store,
        "canonical_page_versions",
        lambda page_keys=None: {},
    )

    scan = _scan_wiki_projection_drift(limit=100)

    assert len(scan["candidates"]) == 25
    assert scan["total_drift"] == 30
    assert scan["errors"] == []


def test_overflow_reconcile_cas_retains_concurrent_generation_and_retries_now(
    monkeypatch,
):
    events = WikiIndexEventBuffer(max_pending=1)
    assert events.put("Concept_Queued.md") is True
    assert events.put("Concept_Overflow.md") is False
    assert events.get_nowait() == "Concept_Queued.md"
    events.task_done()
    first_generation = events.claim_full_reconcile_marker(
        retry_interval_seconds=30,
        now=100,
    )
    assert first_generation == 1

    scans = iter(
        [
            {
                "candidates": ["Concept_Drift.md"],
                "errors": [],
                "total_drift": 1,
            },
            {"candidates": [], "errors": [], "total_drift": 0},
        ]
    )
    batches = []
    monkeypatch.setattr(
        "vector_lake.watchdog_app._scan_wiki_reconcile_plan",
        lambda limit: next(scans),
    )

    def process_batch(filenames):
        batches.append(list(filenames))
        events.put("Concept_Changed-During-Reconcile.md")
        return {"completed": len(filenames), "failed": 0}

    monkeypatch.setattr(
        "vector_lake.watchdog_app.process_legacy_projection_batch",
        process_batch,
    )

    first = reconcile_wiki_overflow_once(events, first_generation, batch_size=25)

    assert batches == [["Concept_Drift.md"]]
    assert first["cleared"] is False
    assert first["generation_changed"] is True
    assert events.full_reconcile_required is True
    current_generation = events.claim_full_reconcile_marker(
        retry_interval_seconds=30,
        now=101,
    )
    assert current_generation == 2

    monkeypatch.setattr(
        "vector_lake.watchdog_app._scan_wiki_reconcile_plan",
        lambda limit: {"candidates": [], "errors": [], "total_drift": 0},
    )
    second = reconcile_wiki_overflow_once(events, current_generation, batch_size=25)

    assert second["cleared"] is True
    assert events.full_reconcile_required is False


def test_successful_partial_overflow_batch_is_immediately_claimable(monkeypatch):
    events = WikiIndexEventBuffer(max_pending=1)
    events.put("Concept_Queued.md")
    events.put("Concept_Overflow.md")
    events.get_nowait()
    events.task_done()
    generation = events.claim_full_reconcile_marker(
        retry_interval_seconds=30,
        now=100,
    )
    scan_calls = []
    scans = iter(
        [
            {
                "candidates": ["Concept_First.md", "Concept_Remaining.md"],
                "errors": [],
                "total_drift": 2,
            },
            {
                "candidates": ["Concept_Remaining.md"],
                "errors": [],
                "total_drift": 1,
            },
        ]
    )

    def scan(limit):
        scan_calls.append(limit)
        return next(scans)

    monkeypatch.setattr(
        "vector_lake.watchdog_app._scan_wiki_reconcile_plan",
        scan,
    )
    monkeypatch.setattr(
        "vector_lake.watchdog_app.process_legacy_projection_batch",
        lambda filenames: {"completed": len(filenames), "failed": 0},
    )

    result = reconcile_wiki_overflow_once(events, generation, batch_size=1)

    assert result["completed"] == 1
    assert result["remaining"] == 1
    assert result["generation_changed"] is False
    assert result["cleared"] is False
    assert len(scan_calls) == 1
    assert (
        events.claim_full_reconcile_marker(
            retry_interval_seconds=30,
            now=101,
        )
        == generation
    )


def test_quiet_reconcile_plan_scans_only_initial_and_final(monkeypatch):
    events = WikiIndexEventBuffer(max_pending=1)
    events.require_full_reconcile()
    generation = events.reconcile_generation
    scan_calls = []
    scans = iter(
        [
            {
                "candidates": [f"Concept_{index}.md" for index in range(6)],
                "errors": [],
                "total_drift": 6,
            },
            {"candidates": [], "errors": [], "total_drift": 0},
        ]
    )
    batches = []

    def scan(limit):
        scan_calls.append(limit)
        return next(scans)

    monkeypatch.setattr(
        "vector_lake.watchdog_app._scan_wiki_reconcile_plan",
        scan,
    )
    monkeypatch.setattr(
        "vector_lake.watchdog_app.process_legacy_projection_batch",
        lambda filenames: (
            batches.append(list(filenames))
            or {"completed": len(filenames), "failed": 0}
        ),
    )

    results = [
        reconcile_wiki_overflow_once(events, generation, batch_size=2) for _ in range(3)
    ]

    assert batches == [
        ["Concept_0.md", "Concept_1.md"],
        ["Concept_2.md", "Concept_3.md"],
        ["Concept_4.md", "Concept_5.md"],
    ]
    assert len(scan_calls) == 2
    assert [result["remaining"] for result in results] == [4, 2, 0]
    assert results[-1]["cleared"] is True


def test_3045_item_quiet_backlog_uses_two_scans(monkeypatch):
    events = WikiIndexEventBuffer(max_pending=1)
    events.require_full_reconcile()
    generation = events.reconcile_generation
    candidates = [f"Concept_{index:04d}.md" for index in range(3_045)]
    scans = iter(
        [
            {
                "candidates": candidates,
                "errors": [],
                "total_drift": len(candidates),
            },
            {"candidates": [], "errors": [], "total_drift": 0},
        ]
    )
    scan_count = 0
    batches = []

    def scan(limit):
        nonlocal scan_count
        scan_count += 1
        return next(scans)

    monkeypatch.setattr(
        "vector_lake.watchdog_app._scan_wiki_reconcile_plan",
        scan,
    )
    monkeypatch.setattr(
        "vector_lake.watchdog_app.process_legacy_projection_batch",
        lambda filenames: (
            batches.append(list(filenames))
            or {"completed": len(filenames), "failed": 0}
        ),
    )

    result = {"cleared": False}
    for _ in range(200):
        result = reconcile_wiki_overflow_once(
            events,
            generation,
            batch_size=25,
        )
        if result["cleared"]:
            break

    assert result["cleared"] is True
    assert scan_count == 2
    assert len(batches) == 122
    assert max(map(len, batches)) == 25
    assert sum(map(len, batches)) == 3_045


def test_reconcile_plan_keeps_failed_batch_for_retry(monkeypatch):
    events = WikiIndexEventBuffer(max_pending=1)
    events.require_full_reconcile()
    generation = events.reconcile_generation
    scans = iter(
        [
            {
                "candidates": ["Concept_A.md", "Concept_B.md"],
                "errors": [],
                "total_drift": 2,
            },
            {"candidates": [], "errors": [], "total_drift": 0},
        ]
    )
    batches = []

    monkeypatch.setattr(
        "vector_lake.watchdog_app._scan_wiki_reconcile_plan",
        lambda limit: next(scans),
    )

    def process(filenames):
        batch = list(filenames)
        batches.append(batch)
        if len(batches) == 1:
            return {"completed": 0, "failed": len(batch)}
        return {"completed": len(batch), "failed": 0}

    monkeypatch.setattr(
        "vector_lake.watchdog_app.process_legacy_projection_batch",
        process,
    )

    failed = reconcile_wiki_overflow_once(events, generation, batch_size=1)
    retried = reconcile_wiki_overflow_once(events, generation, batch_size=1)

    assert failed["failed"] == 1
    assert failed["remaining"] == 2
    assert retried["failed"] == 0
    assert retried["remaining"] == 1
    assert batches == [["Concept_A.md"], ["Concept_A.md"]]


def test_reconcile_plan_candidate_limit_is_hard_capped(monkeypatch):
    observed = []
    monkeypatch.setattr(
        "vector_lake.watchdog_app._collect_wiki_projection_drift",
        lambda selected_limit: (
            observed.append(selected_limit)
            or {"candidates": [], "errors": [], "total_drift": 0}
        ),
    )

    _scan_wiki_reconcile_plan(limit=1_000_000)

    assert observed == [50_000]


def test_reconcile_plan_install_race_fails_closed(monkeypatch):
    events = WikiIndexEventBuffer(max_pending=1)
    events.require_full_reconcile()
    generation = events.reconcile_generation
    processed = []
    original_install = events.install_reconcile_plan

    monkeypatch.setattr(
        "vector_lake.watchdog_app._scan_wiki_reconcile_plan",
        lambda limit: {
            "candidates": ["Concept_A.md"],
            "errors": [],
            "total_drift": 1,
        },
    )

    def install_then_invalidate(expected_generation, candidates, total_drift):
        installed = original_install(
            expected_generation,
            candidates,
            total_drift,
        )
        events.put("Concept_Concurrent.md")
        return installed

    monkeypatch.setattr(events, "install_reconcile_plan", install_then_invalidate)
    monkeypatch.setattr(
        "vector_lake.watchdog_app.process_legacy_projection_batch",
        lambda filenames: (
            processed.append(list(filenames))
            or {"completed": len(filenames), "failed": 0}
        ),
    )

    result = reconcile_wiki_overflow_once(events, generation, batch_size=1)

    assert result["generation_changed"] is True
    assert result["cleared"] is False
    assert processed == []


def test_reconcile_plan_is_invalidated_by_concurrent_generation(monkeypatch):
    events = WikiIndexEventBuffer(max_pending=1)
    events.require_full_reconcile()
    generation = events.reconcile_generation
    scans = []
    batches = []

    def scan(limit):
        scans.append(limit)
        return {
            "candidates": ["Concept_A.md", "Concept_B.md"],
            "errors": [],
            "total_drift": 2,
        }

    def process(filenames):
        batches.append(list(filenames))
        events.put("Concept_Concurrent.md")
        return {"completed": len(filenames), "failed": 0}

    monkeypatch.setattr(
        "vector_lake.watchdog_app._scan_wiki_reconcile_plan",
        scan,
    )
    monkeypatch.setattr(
        "vector_lake.watchdog_app.process_legacy_projection_batch",
        process,
    )

    stale = reconcile_wiki_overflow_once(events, generation, batch_size=1)
    current_generation = events.reconcile_generation
    current = reconcile_wiki_overflow_once(
        events,
        current_generation,
        batch_size=1,
    )

    assert stale["generation_changed"] is True
    assert current["generation"] == current_generation
    assert len(scans) == 2
    assert batches == [["Concept_A.md"], ["Concept_A.md"]]


def test_restored_marker_rebuilds_plan_instead_of_reusing_process_state(
    isolated_memory,
    monkeypatch,
):
    original = WikiIndexEventBuffer(
        max_pending=1,
        persist_reconcile_marker=True,
    )
    original.require_full_reconcile()
    first_generation = original.reconcile_generation
    scans = []

    monkeypatch.setattr(
        "vector_lake.watchdog_app._scan_wiki_reconcile_plan",
        lambda limit: (
            scans.append(limit)
            or {
                "candidates": ["Concept_A.md"],
                "errors": [],
                "total_drift": 1,
            }
        ),
    )
    monkeypatch.setattr(
        "vector_lake.watchdog_app.process_legacy_projection_batch",
        lambda filenames: {"completed": 0, "failed": len(filenames)},
    )
    reconcile_wiki_overflow_once(original, first_generation, batch_size=1)

    restored = WikiIndexEventBuffer(
        max_pending=1,
        persist_reconcile_marker=True,
    )
    assert restored.restore_full_reconcile_marker() is True
    reconcile_wiki_overflow_once(
        restored,
        restored.reconcile_generation,
        batch_size=1,
    )

    assert len(scans) == 2


def test_overflow_reconcile_failures_and_scan_errors_keep_backoff(monkeypatch):
    failed_events = WikiIndexEventBuffer(max_pending=1)
    failed_events.put("Concept_Queued.md")
    failed_events.put("Concept_Overflow.md")
    failed_events.get_nowait()
    failed_events.task_done()
    failed_generation = failed_events.claim_full_reconcile_marker(
        retry_interval_seconds=30,
        now=100,
    )
    monkeypatch.setattr(
        "vector_lake.watchdog_app._scan_wiki_reconcile_plan",
        lambda limit: {
            "candidates": ["Concept_Failed.md"],
            "errors": [],
            "total_drift": 1,
        },
    )
    monkeypatch.setattr(
        "vector_lake.watchdog_app.process_legacy_projection_batch",
        lambda filenames: {"completed": 0, "failed": len(filenames)},
    )

    failed = reconcile_wiki_overflow_once(failed_events, failed_generation)

    assert failed["failed"] == 1
    assert (
        failed_events.claim_full_reconcile_marker(
            retry_interval_seconds=30,
            now=101,
        )
        is None
    )
    assert (
        failed_events.claim_full_reconcile_marker(
            retry_interval_seconds=30,
            now=130,
        )
        == failed_generation
    )

    error_events = WikiIndexEventBuffer(max_pending=1)
    error_events.put("Concept_Queued.md")
    error_events.put("Concept_Overflow.md")
    error_events.get_nowait()
    error_events.task_done()
    error_generation = error_events.claim_full_reconcile_marker(
        retry_interval_seconds=30,
        now=200,
    )
    monkeypatch.setattr(
        "vector_lake.watchdog_app._scan_wiki_reconcile_plan",
        lambda limit: {
            "candidates": [],
            "errors": ["Concept_Bad.md: parse error"],
            "total_drift": 0,
        },
    )

    scan_error = reconcile_wiki_overflow_once(error_events, error_generation)

    assert scan_error["scan_errors"]
    assert (
        error_events.claim_full_reconcile_marker(
            retry_interval_seconds=30,
            now=201,
        )
        is None
    )


def test_reconcile_marker_persist_failure_is_visible_and_retried(tmp_path, monkeypatch):
    from vector_lake import watchdog_app

    monkeypatch.setattr(
        watchdog_app,
        "_wiki_reconcile_marker_path",
        lambda: tmp_path / "wiki_reconcile_required.json",
    )
    events = WikiIndexEventBuffer(persist_reconcile_marker=True)
    monkeypatch.setattr(events, "_persist_full_reconcile_locked", lambda: False)

    with pytest.raises(RuntimeError, match="could not be persisted"):
        events.require_full_reconcile()

    assert events.full_reconcile_required is True
    with pytest.raises(RuntimeError, match="refusing the claim"):
        events.claim_full_reconcile_marker(now=0.0)


def test_reconcile_marker_disk_full_during_fsync_fails_closed_and_cleans_temp(
    tmp_path,
    monkeypatch,
):
    from vector_lake import watchdog_app

    marker = tmp_path / "wiki_reconcile_required.json"
    monkeypatch.setattr(
        watchdog_app,
        "_wiki_reconcile_marker_path",
        lambda: marker,
    )
    monkeypatch.setattr(
        watchdog_app.os,
        "fsync",
        lambda _fd: (_ for _ in ()).throw(OSError(errno.ENOSPC, "injected disk full")),
    )
    events = WikiIndexEventBuffer(persist_reconcile_marker=True)

    with pytest.raises(RuntimeError, match="could not be persisted"):
        events.require_full_reconcile()

    assert events.full_reconcile_required is True
    assert not marker.exists()
    assert list(tmp_path.glob(".wiki_reconcile_required.json.*.tmp")) == []


def test_reconcile_marker_replace_permission_failure_cleans_temp(
    tmp_path,
    monkeypatch,
):
    from vector_lake import watchdog_app

    marker = tmp_path / "wiki_reconcile_required.json"
    monkeypatch.setattr(
        watchdog_app,
        "_wiki_reconcile_marker_path",
        lambda: marker,
    )

    def deny_replace(_source, _destination):
        raise PermissionError(errno.EACCES, "injected replace denial")

    monkeypatch.setattr(watchdog_app.os, "replace", deny_replace)
    events = WikiIndexEventBuffer(persist_reconcile_marker=True)

    with pytest.raises(RuntimeError, match="could not be persisted"):
        events.require_full_reconcile()

    assert events.full_reconcile_required is True
    assert not marker.exists()
    assert list(tmp_path.glob(".wiki_reconcile_required.json.*.tmp")) == []


def test_unreadable_reconcile_marker_restores_required_state(
    tmp_path,
    monkeypatch,
):
    from vector_lake import watchdog_app

    marker = tmp_path / "wiki_reconcile_required.json"
    marker.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        watchdog_app,
        "_wiki_reconcile_marker_path",
        lambda: marker,
    )
    original_read_text = watchdog_app.Path.read_text

    def deny_marker_read(path, *args, **kwargs):
        if path == marker:
            raise PermissionError(errno.EACCES, "injected read denial")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(watchdog_app.Path, "read_text", deny_marker_read)
    events = WikiIndexEventBuffer(persist_reconcile_marker=True)

    assert events.restore_full_reconcile_marker() is True
    assert events.full_reconcile_required is True
    assert events.reconcile_generation == 1


def test_reconcile_marker_unlink_permission_failure_keeps_durable_state(
    tmp_path,
    monkeypatch,
):
    from vector_lake import watchdog_app

    marker = tmp_path / "wiki_reconcile_required.json"
    monkeypatch.setattr(
        watchdog_app,
        "_wiki_reconcile_marker_path",
        lambda: marker,
    )
    events = WikiIndexEventBuffer(persist_reconcile_marker=True)
    generation = events.require_full_reconcile()
    original_unlink = watchdog_app.Path.unlink

    def deny_marker_unlink(path, *args, **kwargs):
        if path == marker:
            raise PermissionError(errno.EACCES, "injected unlink denial")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(watchdog_app.Path, "unlink", deny_marker_unlink)

    assert events.clear_full_reconcile(generation) is False
    assert events.full_reconcile_required is True
    assert marker.exists()


def test_reconcile_marker_remove_failure_keeps_required_state(monkeypatch):
    events = WikiIndexEventBuffer(persist_reconcile_marker=True)
    monkeypatch.setattr(events, "_persist_full_reconcile_locked", lambda: True)
    generation = events.require_full_reconcile()
    monkeypatch.setattr(events, "_remove_full_reconcile_marker_locked", lambda: False)

    assert events.clear_full_reconcile(generation) is False
    assert events.full_reconcile_required is True
    assert events.reconcile_generation == generation


def test_overflow_marker_persist_failure_is_not_silent(monkeypatch):
    events = WikiIndexEventBuffer(
        max_pending=1,
        persist_reconcile_marker=True,
    )
    assert events.put("first.md") is True
    monkeypatch.setattr(events, "_persist_full_reconcile_locked", lambda: False)

    with pytest.raises(RuntimeError, match="overflowed"):
        events.put("second.md")

    assert events.full_reconcile_required is True


def test_persistent_wiki_reconcile_marker_survives_restart(isolated_memory):
    events = WikiIndexEventBuffer(
        max_pending=1,
        persist_reconcile_marker=True,
    )
    generation = events.require_full_reconcile()
    marker = _wiki_reconcile_marker_path()

    assert marker.exists()

    restored = WikiIndexEventBuffer(
        max_pending=1,
        persist_reconcile_marker=True,
    )
    assert restored.restore_full_reconcile_marker() is True
    assert restored.full_reconcile_required is True
    assert restored.reconcile_generation == generation
    assert restored.clear_full_reconcile(generation) is True
    assert not marker.exists()


def test_failed_manual_wiki_batch_sets_durable_reconcile_marker(
    isolated_memory,
    monkeypatch,
):
    events = WikiIndexEventBuffer(
        max_pending=1,
        persist_reconcile_marker=True,
    )
    monkeypatch.setattr(
        "vector_lake.watchdog_app.process_legacy_projection_batch",
        lambda _filenames: {"completed": 0, "failed": 1},
    )

    stats = process_legacy_projection_queue_batch(
        events,
        ["Concept_Failed.md"],
    )

    assert stats == {"completed": 0, "failed": 1}
    assert events.full_reconcile_required is True
    assert _wiki_reconcile_marker_path().exists()


def test_raw_watchdog_requeues_failed_batch_with_backoff(
    isolated_memory,
    monkeypatch,
):
    from vector_lake import tool_ingest

    source = isolated_memory / "raw" / "retry.txt"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("retry", encoding="utf-8")
    completed = threading.Event()
    calls = []

    def prepare(*, batch_size, candidate_paths):
        calls.append((batch_size, candidate_paths))
        if len(calls) == 1:
            raise RuntimeError("temporary failure")
        completed.set()
        return "ok"

    monkeypatch.setattr(tool_ingest, "prepare_ingest_batch", prepare)
    handler = RawWatchdogHandler(retry_base_seconds=0.01)
    try:
        handler.handle_event(SimpleNamespace(is_directory=False, src_path=str(source)))
        assert completed.wait(timeout=2)
    finally:
        handler.shutdown()

    expected = (1, [str(source.resolve())])
    assert calls == [expected, expected]


def test_raw_watchdog_gate_busy_defers_full_scan_without_failure_count(
    isolated_memory,
    monkeypatch,
):
    from vector_lake import tool_ingest
    from vector_lake.heavy_task_gate import heavy_task

    holder_acquired = threading.Event()
    holder_release = threading.Event()
    completed = threading.Event()
    calls = []

    def hold_gate():
        with heavy_task(
            "maintenance",
            "external-holder",
            origin="pytest",
            wait_timeout_seconds=0,
        ):
            holder_acquired.set()
            holder_release.wait(timeout=3)

    def prepare(*, batch_size, candidate_paths, _enqueue_all=False):
        calls.append((batch_size, candidate_paths, _enqueue_all))
        completed.set()
        return f"{tool_ingest.FULL_SCAN_COMPLETE_TOKEN}\ncomplete"

    monkeypatch.setattr(tool_ingest, "prepare_ingest_batch", prepare)
    holder = threading.Thread(target=hold_gate, name="raw-gate-holder")
    holder.start()
    assert holder_acquired.wait(timeout=2)
    handler = RawWatchdogHandler(retry_base_seconds=0.05)
    try:
        handler.request_full_scan()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            with handler.lock:
                deferred = (
                    handler.pending_overflow
                    and handler.retry_attempt == 0
                    and handler.retry_timer is not None
                )
            if deferred:
                break
            time.sleep(0.01)
        assert deferred is True
        assert calls == []

        holder_release.set()
        assert completed.wait(timeout=2)
    finally:
        holder_release.set()
        handler.shutdown()
        holder.join(timeout=3)

    assert not holder.is_alive()
    assert calls == [(50, None, True)]


def test_scheduled_lint_retries_due_slot_after_gate_busy_past_minute(
    monkeypatch,
):
    from vector_lake import db_store, heavy_task_gate, indexer, tool_lint, watchdog_app

    moments = iter(
        [
            time.struct_time((2026, 7, 30, 10, 0, 0, 3, 211, -1)),
            time.struct_time((2026, 7, 30, 10, 1, 0, 3, 211, -1)),
        ]
    )
    gate_attempts = []
    lint_calls = []
    close_calls = []
    messages = []

    class SuccessfulLease:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def fake_heavy_task(task_class, operation, **_kwargs):
        gate_attempts.append((task_class, operation))
        if len(gate_attempts) == 1:
            raise heavy_task_gate.HeavyTaskBusy(
                task_class=task_class,
                operation=operation,
                origin="watchdog",
                wait_timeout_seconds=0,
                gate_status={"current": {"operation": "external-holder"}},
            )
        return SuccessfulLease()

    class StopAfterRetry:
        def __init__(self):
            self.waits = []

        @staticmethod
        def is_set():
            return False

        def wait(self, seconds):
            self.waits.append(seconds)
            return len(self.waits) == 2

    stop_event = StopAfterRetry()
    # Keep this scheduled-lint retry test independent from the separate
    # five-second operational-memory maintenance cadence.
    monkeypatch.setenv("VECTOR_LAKE_OPERATIONAL_MEMORY_AUTO_IDLE_SECONDS", "300")
    monkeypatch.setattr(watchdog_app.time, "localtime", lambda: next(moments))
    monkeypatch.setattr(
        watchdog_app,
        "expire_stale_ingest_jobs_for_watchdog",
        lambda: 0,
    )
    monkeypatch.setattr(heavy_task_gate, "heavy_task", fake_heavy_task)
    monkeypatch.setattr(
        indexer,
        "refresh_graph_topology_if_dirty",
        lambda: False,
    )
    monkeypatch.setattr(
        tool_lint,
        "lint_vector_lake",
        lambda *, auto_fix: lint_calls.append(auto_fix),
    )
    monkeypatch.setattr(
        db_store,
        "get_connection",
        lambda: (_ for _ in ()).throw(
            AssertionError("scheduled lint must not checkpoint")
        ),
    )
    monkeypatch.setattr(
        db_store,
        "close_connection",
        lambda: close_calls.append(True),
    )
    monkeypatch.setattr(
        watchdog_app,
        "write_status",
        lambda _state, _processed, _queue, message, _error, **_kwargs: (
            messages.append(message)
        ),
    )

    watchdog_app.scheduled_lint_loop(stop_event)

    assert gate_attempts == [
        ("scan", "watchdog-scheduled-lint"),
        ("scan", "watchdog-scheduled-lint"),
    ]
    assert lint_calls == [False]
    assert close_calls == [True, True, True]
    assert stop_event.waits == [30, 30]
    assert "Scheduled Lint deferred" in messages
    assert "Scheduled Lint finished" in messages


def test_outbox_worker_yields_after_successful_batch(
    isolated_memory,
    monkeypatch,
):
    from vector_lake import watchdog_app

    waits = []

    class StopOnYield:
        @staticmethod
        def is_set():
            return False

        @staticmethod
        def wait(seconds):
            waits.append(seconds)
            return True

    monkeypatch.setenv("VECTOR_LAKE_OUTBOX_BATCH_YIELD_SECONDS", "0.025")
    monkeypatch.setattr(watchdog_app, "index_queue", _EmptyWatchdogIndexQueue())
    monkeypatch.setattr(db_store, "mutation_outbox_has_claimable", lambda: True)
    monkeypatch.setattr(db_store, "close_connection", lambda: None)
    monkeypatch.setattr(
        watchdog_app,
        "process_mutation_outbox_batch",
        lambda **_kwargs: {
            "claimed": 50,
            "completed": 50,
            "retrying": 0,
            "failed": 0,
        },
    )
    monkeypatch.setattr(watchdog_app, "write_status", lambda *_args, **_kwargs: None)

    watchdog_app.index_worker_loop(StopOnYield())

    assert waits == [0.025]


def test_topology_refresh_deadline_debounces_but_caps_staleness(monkeypatch):
    from vector_lake import watchdog_app

    monkeypatch.setenv("VECTOR_LAKE_TOPOLOGY_REFRESH_DEBOUNCE_SECONDS", "10")
    monkeypatch.setenv("VECTOR_LAKE_TOPOLOGY_MAX_STALENESS_SECONDS", "30")

    dirty_since, due = watchdog_app._topology_refresh_deadline(
        now=100.0,
        dirty_since=None,
    )
    assert (dirty_since, due) == (100.0, 110.0)

    dirty_since, due = watchdog_app._topology_refresh_deadline(
        now=125.0,
        dirty_since=dirty_since,
    )
    assert (dirty_since, due) == (100.0, 130.0)


@pytest.mark.parametrize("interrupt_type", [KeyboardInterrupt, SystemExit])
def test_outbox_worker_records_base_interrupt_as_failed_gate_task(
    isolated_memory,
    monkeypatch,
    interrupt_type,
):
    from vector_lake import watchdog_app
    from vector_lake.heavy_task_gate import heavy_task_gate_status

    class MustNotWait:
        @staticmethod
        def is_set():
            return False

        @staticmethod
        def wait(_seconds):
            raise AssertionError("an interrupt must propagate without retrying")

    def interrupt(**_kwargs):
        raise interrupt_type("requested stop")

    monkeypatch.setattr(watchdog_app, "index_queue", _EmptyWatchdogIndexQueue())
    monkeypatch.setattr(db_store, "mutation_outbox_has_claimable", lambda: True)
    monkeypatch.setattr(db_store, "close_connection", lambda: None)
    monkeypatch.setattr(watchdog_app, "process_mutation_outbox_batch", interrupt)
    monkeypatch.setattr(watchdog_app, "write_status", lambda *_args, **_kwargs: None)

    with pytest.raises(interrupt_type, match="requested stop"):
        watchdog_app.index_worker_loop(MustNotWait())

    status = heavy_task_gate_status()
    assert status["physical_state"] == "free"
    assert status["last"]["operation"] == "watchdog-outbox-cycle"
    assert status["last"]["outcome"] == "failed"
    assert status["last"]["error"] == (
        f"{interrupt_type.__name__}: requested stop"
    )


def test_raw_watchdog_overflow_runs_one_complete_inventory(
    isolated_memory,
    monkeypatch,
):
    from vector_lake import tool_ingest

    raw_dir = isolated_memory / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for name in ("first.txt", "second.txt", "third.txt"):
        path = raw_dir / name
        path.write_text(name, encoding="utf-8")
        paths.append(path)

    first_started = threading.Event()
    release_first = threading.Event()
    scan_complete = threading.Event()
    calls = []

    def prepare(*, batch_size, candidate_paths, _enqueue_all=False):
        calls.append((batch_size, candidate_paths, _enqueue_all))
        if len(calls) == 1:
            first_started.set()
            assert release_first.wait(timeout=2)
            return "ok"
        assert candidate_paths is None
        assert _enqueue_all is True
        scan_complete.set()
        return (
            f"{tool_ingest.FULL_SCAN_COMPLETE_TOKEN}\n"
            "Successfully enqueued recovery inventory."
        )

    monkeypatch.setattr(tool_ingest, "prepare_ingest_batch", prepare)
    monkeypatch.setenv("VECTOR_LAKE_RAW_EVENT_BUFFER", "1")
    handler = RawWatchdogHandler(retry_base_seconds=0.01)
    try:
        handler.handle_event(
            SimpleNamespace(is_directory=False, src_path=str(paths[0]))
        )
        assert first_started.wait(timeout=2)
        handler.handle_event(
            SimpleNamespace(is_directory=False, src_path=str(paths[1]))
        )
        handler.handle_event(
            SimpleNamespace(is_directory=False, src_path=str(paths[2]))
        )
        release_first.set()
        assert scan_complete.wait(timeout=2)
    finally:
        release_first.set()
        handler.shutdown()

    assert calls == [
        (1, [str(paths[0].resolve())], False),
        (50, None, True),
    ]


def test_diary_watchdog_runs_one_trailing_sync_for_coalesced_events(monkeypatch):
    class FakeProcess:
        def __init__(self):
            self.completed = threading.Event()

        def poll(self):
            return 0 if self.completed.is_set() else None

        def wait(self):
            assert self.completed.wait(timeout=2)
            return 0

    processes = []
    launches = []

    def fake_popen(args, **kwargs):
        launches.append((args, kwargs))
        process = FakeProcess()
        processes.append(process)
        return process

    monkeypatch.setattr(
        "vector_lake.watchdog_app.os.path.exists",
        lambda _path: True,
    )
    monkeypatch.setattr("subprocess.Popen", fake_popen)

    handler = DiaryWatchdogHandler()
    handler.handle_event(SimpleNamespace(is_directory=False, src_path="Diary_Alpha.md"))
    handler.handle_event(SimpleNamespace(is_directory=False, src_path="Diary_Beta.md"))

    assert len(launches) == 1
    assert handler.sync_dirty is True

    processes[0].completed.set()
    deadline = time.monotonic() + 2
    while len(launches) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)

    assert len(launches) == 2
    assert handler.sync_process is processes[1]
    assert handler.sync_dirty is False
    processes[1].completed.set()
