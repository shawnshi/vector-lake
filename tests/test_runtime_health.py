import json
import threading

import pytest

from vector_lake import db_store, governance_store, indexer
from vector_lake.mutation_coordinator import execute_mutation_plan
from vector_lake.runtime_health import assess_runtime_health
from vector_lake.tool_doctor import doctor_vector_lake
from vector_lake.watchdog_status import get_status_file, write_status


def _write_purpose_contract(memory_dir):
    (memory_dir / "purpose.md").write_text(
        """---
purpose_version: "12.0"
intent_keywords: [test]
scope:
  core: [test]
  edge: [edge]
  excluded: [excluded]
  marketing_noise: [noise]
evidence_tiers:
  primary: Primary evidence
sir_registry:
  - id: SIR_TEST
    status: active
    review_after: 2099-01-01
    signal_keywords: [test]
synthesis_policy:
  min_distinct_sources: 2
  min_tension_intensity: 0.5
---
Test purpose.
""",
        encoding="utf-8",
    )


def _source_content(entity_id: str, title: str):
    return f"""---
id: {entity_id}
title: {title}
type: source
domain: General
status: Active
epistemic-status: seed
categories: [Source]
updated: 2026-07-13T00:00:00+00:00
sources: [raw/test.pdf]
strategic_scope: core
evidence_tier: primary
---
Primary source content.
"""


def test_write_health_gate_blocks_projection_drift_after_index_exists(isolated_memory):
    _write_purpose_contract(isolated_memory)
    execute_mutation_plan("Source_Healthy.md", content=_source_content("source_healthy", "Healthy Source"))
    indexer.generate_index()
    assert assess_runtime_health()["ok"] is True

    orphan = isolated_memory / "wiki" / "Concept_Orphan.md"
    orphan.write_text(
        """---
id: concept_orphan
title: Orphan
type: concept
domain: General
status: Active
epistemic-status: seed
categories: [Concept]
updated: 2026-07-13T00:00:00+00:00
sources: []
strategic_scope: core
evidence_tier: primary
---
Orphan.
""",
        encoding="utf-8",
    )

    health = assess_runtime_health()
    assert health["ok"] is False
    assert any("projection_drift" in issue for issue in health["issues"])
    with pytest.raises(RuntimeError, match="write gate blocked"):
        execute_mutation_plan("Source_Blocked.md", content=_source_content("source_blocked", "Blocked Source"))


def test_schema_mode_bypasses_write_gate_for_bounded_repairs(isolated_memory):
    _write_purpose_contract(isolated_memory)
    execute_mutation_plan("Source_Healthy.md", content=_source_content("source_healthy", "Healthy Source"))
    indexer.generate_index()
    (isolated_memory / "wiki" / "Concept_Orphan.md").write_text("orphan", encoding="utf-8")

    from vector_lake.mutation_coordinator import execute_mutation_batch

    ok, message = execute_mutation_batch(
        [{"filename": "Source_Repair.md", "content": _source_content("source_repair", "Repair Source")}],
        validation_mode="schema",
    )
    assert ok is True
    assert "committed" in message.lower()


def test_watchdog_error_component_is_not_cleared_by_heartbeat(isolated_memory):
    db_store.init_db()
    write_status("error", 0, 0, "Outbox failed", "database is locked", component="outbox")
    write_status("idle", 0, 0, "Watchdog heartbeat", "", component="watchdog")

    status = json.loads(get_status_file().read_text(encoding="utf-8"))
    assert status["status"] == "error"
    assert status["components"]["outbox"]["last_error"] == "database is locked"
    health = assess_runtime_health()
    assert health["ok"] is False
    assert any("watchdog_unhealthy" in issue for issue in health["issues"])


def test_fresh_watchdog_heartbeat_is_not_reported_stale(isolated_memory):
    db_store.init_db()
    write_status("idle", 0, 0, "Watchdog heartbeat", "", component="watchdog")

    health = assess_runtime_health()

    assert health["detail"]["watchdog_age_seconds"] <= 2
    assert not any("watchdog_stale" in issue for issue in health["issues"])


def test_watchdog_rejects_second_instance_for_same_memory_root(isolated_memory):
    from filelock import FileLock
    from vector_lake.watchdog_app import start_watchdog
    from vector_lake.wiki_utils import get_meta_dir

    lock = FileLock(str(get_meta_dir() / ".watchdog.instance.lock"))
    lock.acquire(timeout=0)
    try:
        with pytest.raises(RuntimeError, match="already running"):
            start_watchdog()
    finally:
        lock.release()


def test_raw_watchdog_uses_single_flight_path_scoped_ingest(isolated_memory, monkeypatch):
    from vector_lake import tool_ingest
    from vector_lake.watchdog_app import RawWatchdogHandler

    first_path = isolated_memory / "raw" / "first.txt"
    second_path = isolated_memory / "raw" / "second.txt"
    first_path.parent.mkdir(parents=True, exist_ok=True)
    first_path.write_text("first", encoding="utf-8")
    second_path.write_text("second", encoding="utf-8")
    started = threading.Event()
    release = threading.Event()
    completed = threading.Event()
    calls = []

    def prepare(*, batch_size, candidate_paths):
        calls.append((batch_size, candidate_paths))
        if len(calls) == 1:
            started.set()
            assert release.wait(timeout=2)
        else:
            completed.set()
        return "ok"

    class Event:
        is_directory = False

        def __init__(self, path):
            self.src_path = str(path)

    monkeypatch.setattr(tool_ingest, "prepare_ingest_batch", prepare)
    handler = RawWatchdogHandler()
    try:
        handler.handle_event(Event(first_path))
        assert started.wait(timeout=2)
        handler.handle_event(Event(second_path))
        assert len(calls) == 1
        release.set()
        assert completed.wait(timeout=2)
    finally:
        release.set()
        handler.shutdown()

    assert calls == [
        (1, [str(first_path.resolve())]),
        (1, [str(second_path.resolve())]),
    ]


def test_raw_watchdog_moved_event_ingests_destination_path(isolated_memory, monkeypatch):
    from vector_lake import tool_ingest
    from vector_lake.watchdog_app import RawWatchdogHandler

    source = isolated_memory / "raw" / "old.txt"
    destination = isolated_memory / "raw" / "new.txt"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("moved", encoding="utf-8")
    completed = threading.Event()
    calls = []

    def prepare(*, batch_size, candidate_paths):
        calls.append((batch_size, candidate_paths))
        completed.set()
        return "ok"

    class Event:
        is_directory = False
        src_path = str(source)
        dest_path = str(destination)

    monkeypatch.setattr(tool_ingest, "prepare_ingest_batch", prepare)
    handler = RawWatchdogHandler()
    try:
        handler.on_moved(Event())
        assert completed.wait(timeout=2)
    finally:
        handler.shutdown()

    assert calls == [(1, [str(destination.resolve())])]


def test_raw_watchdog_shutdown_drains_buffer_without_resubmitting_to_closed_executor(
    isolated_memory, monkeypatch
):
    from vector_lake import tool_ingest
    from vector_lake.watchdog_app import RawWatchdogHandler

    first_path = isolated_memory / "raw" / "first.txt"
    second_path = isolated_memory / "raw" / "second.txt"
    first_path.parent.mkdir(parents=True, exist_ok=True)
    first_path.write_text("first", encoding="utf-8")
    second_path.write_text("second", encoding="utf-8")
    started = threading.Event()
    release = threading.Event()
    calls = []

    def prepare(*, batch_size, candidate_paths):
        calls.append((batch_size, candidate_paths))
        if len(calls) == 1:
            started.set()
            assert release.wait(timeout=2)
        return "ok"

    class Event:
        is_directory = False

        def __init__(self, path):
            self.src_path = str(path)

    monkeypatch.setattr(tool_ingest, "prepare_ingest_batch", prepare)
    handler = RawWatchdogHandler()
    handler.handle_event(Event(first_path))
    assert started.wait(timeout=2)
    handler.handle_event(Event(second_path))
    shutdown = threading.Thread(target=handler.shutdown)
    shutdown.start()
    release.set()
    shutdown.join(timeout=3)

    assert not shutdown.is_alive()
    assert calls == [
        (1, [str(first_path.resolve())]),
        (1, [str(second_path.resolve())]),
    ]


def test_deep_health_detects_equal_count_timeline_id_drift_without_blocking(isolated_memory):
    db_store.init_db()
    conn = db_store.get_connection()
    claim = {
        "claim_id": "claim_timeline_health",
        "claim_text": "Canonical event",
        "claim_type": "timeline-event",
        "temporal_anchor": "2026-07-14",
        "subject_entity_ids": [],
        "source_ids": [],
    }
    with db_store.transaction():
        conn.execute(
            "INSERT INTO claims (claim_id, claim_text, status, data_json, updated_at) VALUES (?, ?, ?, ?, ?)",
            (claim["claim_id"], claim["claim_text"], "active", json.dumps(claim), "2026-07-14T00:00:00+00:00"),
        )
        conn.execute(
            "INSERT INTO timeline_events "
            "(id, event_date, action, sentiment, description, entity_id, entity_title, source_file, extracted_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("wrong-id", "2026-07-14", "old", "neutral", "Wrong event", "", "", "", "2026-07-14"),
        )
    indexer.refresh_claim_graph_projection()

    health = assess_runtime_health(deep_projection_checks=True)

    assert health["ok"] is True
    assert health["detail"]["timeline_projection_drift"] == {
        "canonical": 1,
        "projection": 1,
        "missing": 1,
        "extra": 1,
    }
    assert any("timeline_projection_drift" in warning for warning in health["warnings"])


def test_deep_health_and_doctor_reject_equal_key_wiki_content_drift(isolated_memory):
    from vector_lake.watchdog_app import process_mutation_outbox_batch

    _write_purpose_contract(isolated_memory)
    content = _source_content("source_content_health", "Content Health")
    execute_mutation_plan("Source_Content-Health.md", content=content)
    assert process_mutation_outbox_batch()["completed"] == 1
    target = isolated_memory / "wiki" / "Source_Content-Health.md"
    target.write_text(content.replace("Primary source content.", "Drifted wiki content."), encoding="utf-8")

    shallow = assess_runtime_health()
    deep = assess_runtime_health(deep_projection_checks=True)
    doctor = doctor_vector_lake()

    assert shallow["detail"]["projection_drift"] == {
        "wiki": 1,
        "index": 1,
        "canonical": 1,
        "missing_index": 0,
        "extra_index": 0,
        "missing_canonical": 0,
        "extra_canonical": 0,
    }
    assert deep["ok"] is False
    assert deep["detail"]["projection_content_drift"]["wiki_canonical"] == 1
    assert any("projection_content_drift" in issue for issue in deep["issues"])
    assert "[FAIL] Write Gate:" in doctor
    assert "projection_content_drift" in doctor


def test_full_write_gate_rejects_equal_key_wiki_content_drift(isolated_memory):
    from vector_lake.watchdog_app import process_mutation_outbox_batch

    _write_purpose_contract(isolated_memory)
    content = _source_content("source_gate_content", "Gate Content")
    execute_mutation_plan("Source_Gate-Content.md", content=content)
    assert process_mutation_outbox_batch()["completed"] == 1
    target = isolated_memory / "wiki" / "Source_Gate-Content.md"
    target.write_text(content.replace("Primary source content.", "Drifted wiki content."), encoding="utf-8")

    with pytest.raises(RuntimeError, match="write gate blocked"):
        execute_mutation_plan(
            "Source_Gate-Blocked.md",
            content=_source_content("source_gate_blocked", "Gate Blocked"),
        )


def test_deep_health_rejects_index_body_drift_with_equal_keys(isolated_memory):
    from vector_lake.watchdog_app import process_mutation_outbox_batch

    _write_purpose_contract(isolated_memory)
    content = _source_content("source_index_health", "Index Health")
    execute_mutation_plan("Source_Index-Health.md", content=content)
    assert process_mutation_outbox_batch()["completed"] == 1
    index_path = isolated_memory / "wiki" / "index.json"
    data = json.loads(index_path.read_text(encoding="utf-8"))
    data["nodes"]["Source_Index-Health"]["raw_text"] = "Drifted index content."
    index_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    health = assess_runtime_health(deep_projection_checks=True)

    assert health["ok"] is False
    assert health["detail"]["projection_content_drift"]["index_canonical"] == 1


def test_deep_health_rejects_index_title_drift_with_equal_body(isolated_memory):
    from vector_lake.watchdog_app import process_mutation_outbox_batch

    _write_purpose_contract(isolated_memory)
    content = _source_content("source_index_title", "Canonical Title")
    execute_mutation_plan("Source_Index-Title.md", content=content)
    assert process_mutation_outbox_batch()["completed"] == 1
    index_path = isolated_memory / "wiki" / "index.json"
    data = json.loads(index_path.read_text(encoding="utf-8"))
    data["nodes"]["Source_Index-Title"]["title"] = "Wrong Title"
    index_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    health = assess_runtime_health(deep_projection_checks=True)

    assert health["ok"] is False
    assert health["detail"]["projection_content_drift"]["index_canonical"] == 1


def test_timeline_parity_can_be_promoted_to_blocking_after_rebuild(isolated_memory, monkeypatch):
    db_store.init_db()
    conn = db_store.get_connection()
    claim = {
        "claim_id": "claim_timeline_blocking",
        "claim_text": "Canonical event",
        "claim_type": "timeline-event",
    }
    with db_store.transaction():
        conn.execute(
            "INSERT INTO claims (claim_id, claim_text, status, data_json, updated_at) VALUES (?, ?, ?, ?, ?)",
            (claim["claim_id"], claim["claim_text"], "active", json.dumps(claim), "2026-07-14T00:00:00+00:00"),
        )
    monkeypatch.setenv("VECTOR_LAKE_TIMELINE_PARITY_BLOCKING", "1")

    health = assess_runtime_health()

    assert health["ok"] is False
    assert any("timeline_projection_drift" in issue for issue in health["issues"])


def test_subagent_backlog_is_visible_without_blocking_by_default(isolated_memory, monkeypatch):
    db_store.init_db()
    conn = db_store.get_connection()
    with db_store.transaction():
        for index in range(2):
            conn.execute(
                "INSERT INTO jobs (job_id, task_type, payload, status, created_at, updated_at, available_at) "
                "VALUES (?, 'ingest', '{}', 'awaiting_subagent', ?, ?, ?)",
                (f"job-{index}", "2000-01-01T00:00:00+00:00", "2000-01-01T00:00:00+00:00", "2000-01-01T00:00:00+00:00"),
            )
    monkeypatch.setenv("VECTOR_LAKE_MAX_AWAITING_SUBAGENT_JOBS", "1")
    monkeypatch.setenv("VECTOR_LAKE_MAX_AWAITING_SUBAGENT_AGE_SECONDS", "60")

    health = assess_runtime_health()

    assert health["ok"] is True
    assert any("subagent_backlog" in warning for warning in health["warnings"])


def test_deep_health_reuses_unchanged_wiki_parse_and_invalidates_on_write(
    isolated_memory, monkeypatch
):
    from vector_lake import governance_store
    from vector_lake.runtime_health import _clear_health_caches_for_tests
    from vector_lake.watchdog_app import process_mutation_outbox_batch

    _write_purpose_contract(isolated_memory)
    content = _source_content("source_cached_health", "Cached Health")
    execute_mutation_plan("Source_Cached-Health.md", content=content)
    assert process_mutation_outbox_batch()["completed"] == 1
    _clear_health_caches_for_tests()
    original = governance_store.canonical_page_version_from_content
    calls = []

    def observed(filename, page_content):
        calls.append(filename)
        return original(filename, page_content)

    monkeypatch.setattr(governance_store, "canonical_page_version_from_content", observed)

    assert assess_runtime_health(deep_projection_checks=True)["ok"] is True
    first_count = len(calls)
    assert first_count == 1
    assert assess_runtime_health(deep_projection_checks=True)["ok"] is True
    assert len(calls) == first_count

    target = isolated_memory / "wiki" / "Source_Cached-Health.md"
    target.write_text(
        content.replace("Primary source content.", "Same-size drifted body!"),
        encoding="utf-8",
    )
    health = assess_runtime_health(deep_projection_checks=True)
    assert len(calls) == first_count + 1
    assert health["detail"]["projection_content_drift"]["wiki_canonical"] == 1


def test_pending_outbox_does_not_hide_conflicting_manual_wiki_edit(isolated_memory):
    from vector_lake.watchdog_app import process_mutation_outbox_batch

    _write_purpose_contract(isolated_memory)
    content = _source_content("source_pending_conflict", "Pending Conflict")
    execute_mutation_plan("Source_Pending-Conflict.md", content=content)
    assert process_mutation_outbox_batch()["completed"] == 1
    base_version = governance_store.canonical_page_versions(
        {"Source_Pending-Conflict"}
    )["Source_Pending-Conflict"]
    with db_store.transaction():
        db_store.enqueue_mutation(
            "Source_Pending-Conflict.md",
            "update",
            payload_text=content,
            idempotency_key="pending-conflict-replay",
            validation_mode="schema",
            base_version=base_version,
        )
    target = isolated_memory / "wiki" / "Source_Pending-Conflict.md"
    target.write_text(
        content.replace("Primary source content.", "Conflicting manual edit."),
        encoding="utf-8",
    )

    health = assess_runtime_health(deep_projection_checks=True)

    assert health["ok"] is False
    assert health["detail"]["projection_content_drift"]["wiki_canonical"] == 1
    assert health["detail"]["projection_content_drift"]["managed_reconciliation"] == 0
