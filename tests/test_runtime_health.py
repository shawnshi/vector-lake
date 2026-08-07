import json
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from vector_lake import db_store, governance_store, indexer
from vector_lake.mutation_coordinator import execute_mutation_plan
from vector_lake.runtime_health import assess_runtime_health, assess_semantic_readiness
from vector_lake.tool_doctor import doctor_vector_lake
from vector_lake.watchdog_status import get_status_file, write_status


def test_runtime_health_uses_immutable_read_only_uri_when_wal_is_empty(
    isolated_memory,
    monkeypatch,
):
    from vector_lake import runtime_health

    db_store.init_db()
    db_store.close_all_connections()
    observed = []
    real_connect = sqlite3.connect

    def capture_connect(database, *args, **kwargs):
        observed.append(str(database))
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(runtime_health.sqlite3, "connect", capture_connect)
    connection, _db_path = runtime_health._open_runtime_database_read_only()
    connection.close()

    assert observed
    assert "mode=ro&immutable=1" in observed[0]


def test_canonical_entity_version_ignores_transport_only_raw_text_differences():
    lf_record = {
        "entity_id": "entity_transport",
        "page_key": "Source_Transport",
        "raw_text": "line one\nline two\n",
    }
    crlf_record = {
        **lf_record,
        "raw_text": "\ufeffline one\r\nline two\r\n",
    }
    changed_record = {
        **lf_record,
        "raw_text": "line one\nchanged\n",
    }

    lf_version = governance_store._canonical_entity_records_version(
        [("entity_transport", lf_record)]
    )

    assert (
        governance_store._canonical_entity_records_version(
            [("entity_transport", crlf_record)]
        )
        == lf_version
    )
    assert (
        governance_store._canonical_entity_records_version(
            [("entity_transport", changed_record)]
        )
        != lf_version
    )


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


def test_write_health_gate_reuses_recent_unchanged_deep_check(monkeypatch):
    from vector_lake import runtime_health

    calls = []
    monkeypatch.setenv("VECTOR_LAKE_WRITE_HEALTH_CACHE_SECONDS", "30")
    monkeypatch.setattr(
        runtime_health,
        "_write_health_surface_token",
        lambda: "stable-surface",
    )
    monkeypatch.setattr(
        runtime_health,
        "assess_runtime_health",
        lambda **kwargs: (
            calls.append(kwargs)
            or {
                "ok": True,
                "issues": [],
                "warnings": [],
                "detail": {},
            }
        ),
    )
    runtime_health._clear_health_caches_for_tests()

    runtime_health.enforce_runtime_write_health()
    runtime_health.enforce_runtime_write_health()

    assert calls == [{"deep_projection_checks": True}]


@pytest.mark.parametrize("configured", [None, "not-a-number"])
def test_write_health_gate_is_strict_by_default_and_on_invalid_ttl(
    monkeypatch,
    configured,
):
    from vector_lake import runtime_health

    calls = []
    if configured is None:
        monkeypatch.delenv("VECTOR_LAKE_WRITE_HEALTH_CACHE_SECONDS", raising=False)
    else:
        monkeypatch.setenv("VECTOR_LAKE_WRITE_HEALTH_CACHE_SECONDS", configured)
    monkeypatch.setattr(
        runtime_health,
        "_write_health_surface_token",
        lambda: "stable-surface",
    )
    monkeypatch.setattr(
        runtime_health,
        "assess_runtime_health",
        lambda **kwargs: (
            calls.append(kwargs)
            or {
                "ok": True,
                "issues": [],
                "warnings": [],
                "detail": {},
            }
        ),
    )
    runtime_health._clear_health_caches_for_tests()

    runtime_health.enforce_runtime_write_health()
    runtime_health.enforce_runtime_write_health()

    assert calls == [
        {"deep_projection_checks": True},
        {"deep_projection_checks": True},
    ]


def test_write_health_gate_invalidates_when_projection_identity_changes(monkeypatch):
    from vector_lake import runtime_health

    tokens = iter(["surface-a", "surface-a", "surface-b", "surface-b"])
    calls = []
    monkeypatch.setenv("VECTOR_LAKE_WRITE_HEALTH_CACHE_SECONDS", "30")
    monkeypatch.setattr(
        runtime_health,
        "_write_health_surface_token",
        lambda: next(tokens),
    )
    monkeypatch.setattr(
        runtime_health,
        "assess_runtime_health",
        lambda **kwargs: (
            calls.append(kwargs)
            or {
                "ok": True,
                "issues": [],
                "warnings": [],
                "detail": {},
            }
        ),
    )
    runtime_health._clear_health_caches_for_tests()

    runtime_health.enforce_runtime_write_health()
    runtime_health.enforce_runtime_write_health()

    assert len(calls) == 2


def test_write_health_gate_deep_check_is_single_flight(monkeypatch):
    from vector_lake import runtime_health

    calls = []
    errors = []
    started = threading.Event()
    release = threading.Event()
    monkeypatch.setenv("VECTOR_LAKE_WRITE_HEALTH_CACHE_SECONDS", "30")
    monkeypatch.setattr(
        runtime_health,
        "_write_health_surface_token",
        lambda: "stable-surface",
    )

    def assess(**kwargs):
        calls.append(kwargs)
        started.set()
        assert release.wait(timeout=2)
        return {"ok": True, "issues": [], "warnings": [], "detail": {}}

    monkeypatch.setattr(runtime_health, "assess_runtime_health", assess)
    runtime_health._clear_health_caches_for_tests()

    def enforce():
        try:
            runtime_health.enforce_runtime_write_health()
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    first = threading.Thread(target=enforce)
    second = threading.Thread(target=enforce)
    first.start()
    assert started.wait(timeout=2)
    second.start()
    time.sleep(0.05)
    release.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert errors == []
    assert not first.is_alive()
    assert not second.is_alive()
    assert calls == [{"deep_projection_checks": True}]


def test_write_health_gate_fails_closed_when_snapshot_keeps_changing(monkeypatch):
    from vector_lake import runtime_health

    tokens = iter(["surface-a", "surface-b", "surface-c"])
    calls = []
    monkeypatch.setenv("VECTOR_LAKE_WRITE_HEALTH_CACHE_SECONDS", "30")
    monkeypatch.setattr(
        runtime_health,
        "_write_health_surface_token",
        lambda: next(tokens),
    )
    monkeypatch.setattr(
        runtime_health,
        "assess_runtime_health",
        lambda **kwargs: (
            calls.append(kwargs)
            or {
                "ok": True,
                "issues": [],
                "warnings": [],
                "detail": {},
            }
        ),
    )
    runtime_health._clear_health_caches_for_tests()

    with pytest.raises(RuntimeError, match="changed during validation"):
        runtime_health.enforce_runtime_write_health()

    assert len(calls) == 2


def test_write_gate_migrates_existing_database_before_retry(
    isolated_memory, monkeypatch
):
    from vector_lake import runtime_health

    old_db = isolated_memory / "wiki" / ".meta" / "legacy-runtime.db"
    old_db.parent.mkdir(parents=True, exist_ok=True)
    legacy = sqlite3.connect(str(old_db))
    legacy.execute("CREATE TABLE legacy_marker (value TEXT)")
    legacy.commit()
    legacy.close()

    monkeypatch.setenv("VECTOR_LAKE_DB_PATH", str(old_db))
    db_store.close_connection()
    db_key = str(old_db.resolve())
    db_store._INITIALIZED_DB_PATHS.discard(db_key)
    runtime_health._clear_health_caches_for_tests()
    calls = []

    def assess(**kwargs):
        calls.append(kwargs)
        db_store.init_db()
        return {
            "ok": True,
            "issues": [],
            "warnings": [],
            "detail": {},
        }

    monkeypatch.setattr(runtime_health, "assess_runtime_health", assess)
    runtime_health.enforce_runtime_write_health()

    surfaces = {
        str(row[0])
        for row in db_store.get_connection().execute(
            "SELECT surface FROM runtime_generations"
        )
    }
    assert {
        "entities",
        "claims",
        "sources",
        "timeline_events",
        "mutation_outbox",
        "jobs",
    } <= surfaces
    assert len(calls) == 1


def test_write_health_token_changes_when_watchdog_becomes_stale(isolated_memory):
    from vector_lake import runtime_health

    db_store.init_db()
    write_status("idle", 0, 0, "Watchdog heartbeat", "", component="watchdog")
    fresh_token = runtime_health._write_health_surface_token()

    status_path = get_status_file()
    payload = json.loads(status_path.read_text(encoding="utf-8"))
    payload["updated_at"] = "2000-01-01T00:00:00+00:00"
    status_path.write_text(json.dumps(payload), encoding="utf-8")
    stale_token = runtime_health._write_health_surface_token()

    assert stale_token != fresh_token


def test_write_health_token_tracks_blocking_policy_changes(
    isolated_memory, monkeypatch
):
    from vector_lake import runtime_health

    db_store.init_db()
    monkeypatch.delenv("VECTOR_LAKE_SUBAGENT_BACKLOG_BLOCKING", raising=False)
    monkeypatch.delenv("VECTOR_LAKE_TIMELINE_PARITY_BLOCKING", raising=False)
    monkeypatch.delenv("VECTOR_LAKE_MAX_READY_INGEST_AGE_SECONDS", raising=False)
    initial = runtime_health._write_health_surface_token()

    monkeypatch.setenv("VECTOR_LAKE_SUBAGENT_BACKLOG_BLOCKING", "1")
    backlog_blocking = runtime_health._write_health_surface_token()
    monkeypatch.setenv("VECTOR_LAKE_TIMELINE_PARITY_BLOCKING", "1")
    timeline_blocking = runtime_health._write_health_surface_token()
    monkeypatch.setenv("VECTOR_LAKE_MAX_READY_INGEST_AGE_SECONDS", "900")
    ready_age_policy = runtime_health._write_health_surface_token()

    assert len({initial, backlog_blocking, timeline_blocking, ready_age_policy}) == 4


def test_write_health_token_does_not_enumerate_wiki_files(isolated_memory, monkeypatch):
    from vector_lake import runtime_health

    db_store.init_db()

    def fail_scandir(*_args, **_kwargs):
        raise AssertionError("write-health token must not enumerate Wiki files")

    monkeypatch.setattr(runtime_health.os, "scandir", fail_scandir)

    assert runtime_health._write_health_surface_token()


def test_write_health_tracks_pending_wiki_reconcile_marker(isolated_memory):
    from vector_lake import runtime_health
    from vector_lake.wiki_utils import get_meta_dir

    db_store.init_db()
    runtime_health._clear_health_caches_for_tests()
    before = runtime_health._write_health_surface_token()
    marker = get_meta_dir() / "wiki_reconcile_required.json"
    marker.write_text(
        json.dumps({"required": True, "generation": 7}),
        encoding="utf-8",
    )

    pending = runtime_health._write_health_surface_token()
    health = runtime_health.assess_runtime_health()

    assert pending != before
    assert health["ok"] is False
    assert "wiki_reconcile_required:generation=7" in health["issues"]
    assert health["detail"]["wiki_reconcile_generation"] == 7

    marker.unlink()
    assert runtime_health._write_health_surface_token() != pending


def test_external_commit_advances_generation_and_invalidates_caches(isolated_memory):
    from vector_lake import runtime_health

    db_store.init_db()
    conn = db_store.get_connection()
    db_path = db_store.get_db_path()
    with db_store.transaction():
        conn.execute(
            "INSERT INTO entities "
            "(entity_id, canonical_name, data_json, updated_at) VALUES (?, ?, ?, ?)",
            (
                "entity_external",
                "External",
                '{"entity_id":"entity_external","page_key":"Concept_AA"}',
                "2026-07-27T00:00:00+00:00",
            ),
        )
    runtime_health._clear_health_caches_for_tests()
    before_generation = int(
        conn.execute(
            "SELECT generation FROM runtime_generations WHERE surface = 'entities'"
        ).fetchone()[0]
    )
    before_snapshot = runtime_health._canonical_snapshot(conn, db_path)
    before_token = runtime_health._write_health_surface_token()

    external = sqlite3.connect(str(db_path))
    try:
        external.execute(
            "UPDATE entities SET data_json = ? WHERE entity_id = ?",
            (
                '{"entity_id":"entity_external","page_key":"Concept_BB"}',
                "entity_external",
            ),
        )
        external.commit()
    finally:
        external.close()

    after_generation = int(
        conn.execute(
            "SELECT generation FROM runtime_generations WHERE surface = 'entities'"
        ).fetchone()[0]
    )
    after_token = runtime_health._write_health_surface_token()
    after_snapshot = runtime_health._canonical_snapshot(conn, db_path)

    assert after_generation == before_generation + 1
    assert after_token != before_token
    assert "Concept_AA" in before_snapshot
    assert "Concept_BB" in after_snapshot
    assert "Concept_AA" not in after_snapshot


def test_write_health_gate_blocks_projection_drift_after_index_exists(isolated_memory):
    _write_purpose_contract(isolated_memory)
    execute_mutation_plan(
        "Source_Healthy.MD", content=_source_content("source_healthy", "Healthy Source")
    )
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
        execute_mutation_plan(
            "Source_Blocked.md",
            content=_source_content("source_blocked", "Blocked Source"),
        )


def test_default_write_gate_detects_external_in_place_wiki_edit(
    isolated_memory,
    monkeypatch,
):
    from vector_lake import runtime_health

    _write_purpose_contract(isolated_memory)
    execute_mutation_plan(
        "Source_Healthy.md",
        content=_source_content("source_healthy", "Healthy Source"),
    )
    indexer.generate_index()
    runtime_health._clear_health_caches_for_tests()
    monkeypatch.delenv("VECTOR_LAKE_WRITE_HEALTH_CACHE_SECONDS", raising=False)
    monkeypatch.setattr(
        runtime_health,
        "_write_health_surface_token",
        lambda: "intentionally-stable-token",
    )

    runtime_health.enforce_runtime_write_health()
    page = isolated_memory / "wiki" / "Source_Healthy.md"
    page.write_text(
        page.read_text(encoding="utf-8").replace(
            "Primary source content.",
            "Externally edited projection with different content.",
        ),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="write gate blocked"):
        runtime_health.enforce_runtime_write_health()


def test_schema_mode_bypasses_write_gate_for_bounded_repairs(isolated_memory):
    _write_purpose_contract(isolated_memory)
    execute_mutation_plan(
        "Source_Healthy.md", content=_source_content("source_healthy", "Healthy Source")
    )
    indexer.generate_index()
    (isolated_memory / "wiki" / "Concept_Orphan.md").write_text(
        "orphan", encoding="utf-8"
    )

    from vector_lake.mutation_coordinator import execute_mutation_batch

    ok, message = execute_mutation_batch(
        [
            {
                "filename": "Source_Repair.md",
                "content": _source_content("source_repair", "Repair Source"),
            }
        ],
        validation_mode="schema",
    )
    assert ok is True
    assert "committed" in message.lower()


def test_watchdog_error_component_is_not_cleared_by_heartbeat(isolated_memory):
    db_store.init_db()
    write_status(
        "error", 0, 0, "Outbox failed", "database is locked", component="outbox"
    )
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


def test_raw_watchdog_uses_single_flight_path_scoped_ingest(
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


def test_raw_watchdog_moved_event_ingests_destination_path(
    isolated_memory, monkeypatch
):
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


def test_deep_health_detects_equal_count_timeline_id_drift_without_blocking(
    isolated_memory,
):
    db_store.init_db()
    conn = db_store.get_connection()
    claim = {
        "claim_id": "claim_timeline_health",
        "claim_text": "Canonical event",
        "claim_type": "timeline-event",
        "temporal_anchor": "2026-07-14",
        "subject_entity_ids": [],
        "source_ids": [],
        "locator": {"page_key": "Event_Timeline-Health"},
    }
    governance_store.apply_change_set(
        {
            "affected_pages": ["Event_Timeline-Health.md"],
            "proposed_entities": [],
            "proposed_claims": [claim],
            "proposed_evidence": [],
            "proposed_source_updates": [],
            "proposed_edges": [],
        }
    )
    with db_store.transaction():
        conn.execute(
            "UPDATE timeline_events SET id = ?, action = ?, description = ?",
            ("wrong-id", "old", "Wrong event"),
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


def test_watchdog_status_reports_publish_failure(monkeypatch, tmp_path):
    import vector_lake.watchdog_status as watchdog_status

    target = tmp_path / "status-target"
    target.mkdir()
    monkeypatch.setattr(watchdog_status, "get_status_file", lambda: target)

    assert watchdog_status.write_status("idle", 0, 0, "probe") is False


def test_deep_health_and_doctor_reject_equal_key_wiki_content_drift(isolated_memory):
    from vector_lake.watchdog_app import process_mutation_outbox_batch

    _write_purpose_contract(isolated_memory)
    content = _source_content("source_content_health", "Content Health")
    execute_mutation_plan("Source_Content-Health.md", content=content)
    assert process_mutation_outbox_batch()["completed"] == 1
    target = isolated_memory / "wiki" / "Source_Content-Health.md"
    target.write_text(
        content.replace("Primary source content.", "Drifted wiki content."),
        encoding="utf-8",
    )

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
    target.write_text(
        content.replace("Primary source content.", "Drifted wiki content."),
        encoding="utf-8",
    )

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


def test_timeline_parity_can_be_promoted_to_blocking_after_rebuild(
    isolated_memory, monkeypatch
):
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
            (
                claim["claim_id"],
                claim["claim_text"],
                "active",
                json.dumps(claim),
                "2026-07-14T00:00:00+00:00",
            ),
        )
    monkeypatch.setenv("VECTOR_LAKE_TIMELINE_PARITY_BLOCKING", "1")

    health = assess_runtime_health()

    assert health["ok"] is False
    assert any("timeline_projection_drift" in issue for issue in health["issues"])


def test_subagent_backlog_is_visible_without_blocking_by_default(
    isolated_memory, monkeypatch
):
    db_store.init_db()
    conn = db_store.get_connection()
    with db_store.transaction():
        for index in range(2):
            conn.execute(
                "INSERT INTO jobs (job_id, task_type, payload, status, created_at, updated_at, available_at) "
                "VALUES (?, 'ingest', '{}', 'awaiting_subagent', ?, ?, ?)",
                (
                    f"job-{index}",
                    "2000-01-01T00:00:00+00:00",
                    "2000-01-01T00:00:00+00:00",
                    "2000-01-01T00:00:00+00:00",
                ),
            )
    monkeypatch.setenv("VECTOR_LAKE_MAX_AWAITING_SUBAGENT_JOBS", "1")
    monkeypatch.setenv("VECTOR_LAKE_MAX_AWAITING_SUBAGENT_AGE_SECONDS", "60")

    health = assess_runtime_health()

    assert health["ok"] is True
    assert any("subagent_backlog" in warning for warning in health["warnings"])


def test_ready_ingest_queue_age_detects_stalled_dispatch_worker(
    isolated_memory,
    monkeypatch,
):
    db_store.init_db()
    old = "2000-01-01T00:00:00+00:00"
    with db_store.transaction():
        db_store.get_connection().execute(
            "INSERT INTO jobs "
            "(job_id, task_type, payload, status, retries, created_at, updated_at, available_at) "
            "VALUES ('stalled-ingest', 'ingest', '{}', 'queued', 0, ?, ?, ?)",
            (old, old, old),
        )
    monkeypatch.setenv("VECTOR_LAKE_MAX_READY_INGEST_AGE_SECONDS", "60")

    health = assess_runtime_health()

    assert health["ok"] is False
    assert health["detail"]["ready_ingest_jobs"] == 1
    assert health["detail"]["oldest_ready_ingest_age_seconds"] > 60
    assert any(
        issue.startswith("ingest_dispatch_stalled:count=1,")
        for issue in health["issues"]
    )


def test_empty_health_timestamp_aggregates_use_outer_coalesce(
    isolated_memory,
    monkeypatch,
):
    from vector_lake import runtime_health

    db_store.init_db()
    db_path = db_store.get_db_path().resolve()
    traced_sql = []

    def open_traced_read_only():
        connection = sqlite3.connect(
            f"{db_path.as_uri()}?mode=ro",
            uri=True,
        )
        connection.row_factory = sqlite3.Row
        connection.set_trace_callback(traced_sql.append)
        return connection, db_path

    monkeypatch.setattr(
        runtime_health,
        "_open_runtime_database_read_only",
        open_traced_read_only,
    )

    health = assess_runtime_health()
    readiness = assess_semantic_readiness(index_data={})

    min_queries = [
        " ".join(statement.casefold().split())
        for statement in traced_sql
        if "min(" in statement.casefold()
    ]
    assert len(min_queries) == 4
    assert all("coalesce(min(" in statement for statement in min_queries)
    assert health["detail"]["ready_ingest_jobs"] == 0
    assert health["detail"]["awaiting_subagent_jobs"] == 0
    assert "oldest_ready_ingest_age_seconds" not in health["detail"]
    assert readiness["detail"]["awaiting_subagent_jobs"] == 0
    assert "oldest_awaiting_subagent_age_seconds" not in readiness["detail"]


def test_recently_expired_ingest_lease_uses_lease_age_not_job_age(
    isolated_memory,
    monkeypatch,
):
    db_store.init_db()
    old = "2000-01-01T00:00:00+00:00"
    lease_until = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    with db_store.transaction():
        db_store.get_connection().execute(
            "INSERT INTO jobs "
            "(job_id, task_type, payload, status, retries, created_at, updated_at, "
            "available_at, lease_until) "
            "VALUES ('recent-lease', 'ingest', '{}', 'dispatched', 0, ?, ?, ?, ?)",
            (old, old, old, lease_until),
        )
    monkeypatch.setenv("VECTOR_LAKE_MAX_READY_INGEST_AGE_SECONDS", "not-an-integer")

    health = assess_runtime_health()

    assert health["detail"]["ready_ingest_jobs"] == 1
    assert health["detail"]["oldest_ready_ingest_age_seconds"] < 10
    assert not any(
        issue.startswith("ingest_dispatch_stalled:") for issue in health["issues"]
    )


def test_long_expired_ingest_lease_is_reported_as_stalled(
    isolated_memory,
    monkeypatch,
):
    db_store.init_db()
    old = "2000-01-01T00:00:00+00:00"
    lease_until = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
    with db_store.transaction():
        db_store.get_connection().execute(
            "INSERT INTO jobs "
            "(job_id, task_type, payload, status, retries, created_at, updated_at, "
            "available_at, lease_until) "
            "VALUES ('old-lease', 'ingest', '{}', 'dispatched', 0, ?, ?, ?, ?)",
            (old, old, old, lease_until),
        )
    monkeypatch.setenv("VECTOR_LAKE_MAX_READY_INGEST_AGE_SECONDS", "60")

    health = assess_runtime_health()

    assert health["detail"]["ready_ingest_jobs"] == 1
    assert health["detail"]["oldest_ready_ingest_age_seconds"] >= 120
    assert any(
        issue.startswith("ingest_dispatch_stalled:count=1,")
        for issue in health["issues"]
    )


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

    monkeypatch.setattr(
        governance_store, "canonical_page_version_from_content", observed
    )

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


def test_semantic_readiness_can_be_ready_when_runtime_has_no_semantic_debt(
    isolated_memory,
):
    db_store.init_db()

    readiness = assess_semantic_readiness(
        index_data={"nodes": {}, "graph_state": {"dirty": False, "reason": "fresh"}}
    )

    assert readiness == {
        "ready": True,
        "status": "ready",
        "issues": [],
        "warnings": [],
        "detail": {
            "graph_state": {"dirty": False, "reason": "fresh"},
            "graph_insight_count": 0,
            "pending_governance_by_type": {},
            "pending_governance_total": 0,
            "critical_pending_governance": 0,
            "runtime_validity_state_counts": {},
            "evidence_foundation": {
                "claim_total": 0,
                "claim_with_evidence_refs": 0,
                "claim_with_extraction_run": 0,
                "claim_with_supported_assessment": 0,
                "evidence_total": 0,
                "evidence_with_raw_locator": 0,
                "evidence_lineage_safe": 0,
                "source_total": 0,
                "source_integrity_verified": 0,
                "source_artifact_total": 0,
                "source_artifact_verified": 0,
                "extraction_run_total": 0,
                "claim_evidence_coverage": 1.0,
                "claim_extraction_coverage": 1.0,
                "claim_assessment_coverage": 1.0,
                "evidence_raw_locator_coverage": 1.0,
                "evidence_lineage_coverage": 1.0,
                "source_integrity_coverage": 1.0,
            },
            "awaiting_subagent_jobs": 0,
        },
    }


def test_global_semantic_readiness_surfaces_evidence_foundation_coverage(
    isolated_memory,
):
    db_store.init_db()
    claim = {
        "claim_id": "claim_legacy_gap",
        "claim_text": "Legacy claim without evidence foundation.",
        "status": "Active",
        "evidence_ids": [],
    }
    with db_store.transaction():
        db_store.get_connection().execute(
            "INSERT INTO claims (claim_id, claim_text, status, data_json, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                claim["claim_id"],
                claim["claim_text"],
                "Active",
                json.dumps(claim),
                "2026-07-21",
            ),
        )

    readiness = assess_semantic_readiness(
        index_data={"nodes": {}, "graph_state": {"dirty": False, "reason": "fresh"}}
    )

    coverage = readiness["detail"]["evidence_foundation"]
    assert readiness["status"] == "degraded"
    assert coverage["claim_total"] == 1
    assert coverage["claim_evidence_coverage"] == 0.0
    assert "claim_evidence_coverage_low:0.0000<0.9500" in readiness["warnings"]


def test_semantic_readiness_reports_topology_claim_governance_and_ingest_debt(
    isolated_memory, monkeypatch
):
    db_store.init_db()
    conn = db_store.get_connection()
    old = "2000-01-01T00:00:00+00:00"
    memory = {
        "memory_id": "memory_unsupported",
        "memory_type": "fact",
        "validity_state": "unsupported",
    }
    governance_item = {
        "item_id": "gov_critical",
        "type": "contradiction",
        "status": "pending",
    }
    with db_store.transaction():
        conn.execute(
            "INSERT INTO operational_memory "
            "(memory_id, memory_type, score, data_json, updated_at, status, ttl) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (memory["memory_id"], "fact", 1.0, json.dumps(memory), old, "Active", 365),
        )
        conn.execute(
            "INSERT INTO governance_queue (item_id, data_json, updated_at) VALUES (?, ?, ?)",
            (governance_item["item_id"], json.dumps(governance_item), old),
        )
        conn.execute(
            "INSERT INTO jobs "
            "(job_id, task_type, payload, status, created_at, updated_at, available_at) "
            "VALUES (?, 'ingest', '{}', 'awaiting_subagent', ?, ?, ?)",
            ("job_semantic_old", old, old, old),
        )
    monkeypatch.setenv("VECTOR_LAKE_MAX_PENDING_GOVERNANCE_ITEMS", "0")
    monkeypatch.setenv("VECTOR_LAKE_MAX_UNSUPPORTED_RUNTIME_CLAIMS", "0")
    monkeypatch.setenv("VECTOR_LAKE_MAX_AWAITING_SUBAGENT_AGE_SECONDS", "60")

    readiness = assess_semantic_readiness(
        index_data={
            "nodes": {},
            "graph_state": {"dirty": True, "reason": "awaiting async clustering"},
        }
    )

    assert readiness["ready"] is False
    assert readiness["status"] == "not_ready"
    assert any(
        issue.startswith("graph_topology_dirty:") for issue in readiness["issues"]
    )
    assert "critical_governance_pending:1" in readiness["issues"]
    assert "governance_backlog:1>0" in readiness["issues"]
    assert "unmanaged_unsupported_runtime_claims:1>0" in readiness["issues"]
    assert any(
        issue.startswith("semantic_ingest_backlog:") for issue in readiness["issues"]
    )


def test_semantic_readiness_treats_acknowledged_unsupported_claim_as_managed_debt(
    isolated_memory, monkeypatch
):
    from vector_lake.governance_metrics import claim_governance_version

    db_store.init_db()
    conn = db_store.get_connection()
    claim = {
        "claim_id": "claim_managed_unsupported",
        "claim_text": "Legacy claim awaiting evidence remediation.",
        "status": "Active",
        "evidence_ids": [],
        "source_ids": [],
    }
    memory = {
        "memory_id": "memory_managed_unsupported",
        "memory_type": "fact",
        "source_claim_id": claim["claim_id"],
        "validity_state": "unsupported",
    }
    governance_item = {
        "item_id": "gov_managed_unsupported",
        "type": "evidence-gap",
        "status": "acknowledged",
        "claim_id": claim["claim_id"],
        "claim_version": claim_governance_version(claim),
        "owner": "test-owner",
        "due_at": "2099-01-01T00:00:00+00:00",
    }
    with db_store.transaction():
        conn.execute(
            "INSERT INTO claims (claim_id, claim_text, status, data_json, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                claim["claim_id"],
                claim["claim_text"],
                "Active",
                json.dumps(claim),
                "2026-07-21",
            ),
        )
        conn.execute(
            "INSERT INTO operational_memory "
            "(memory_id, memory_type, score, data_json, updated_at, status, ttl) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                memory["memory_id"],
                "fact",
                1.0,
                json.dumps(memory),
                "2026-07-21",
                "Active",
                365,
            ),
        )
        conn.execute(
            "INSERT INTO governance_queue (item_id, data_json, updated_at) VALUES (?, ?, ?)",
            (governance_item["item_id"], json.dumps(governance_item), "2026-07-21"),
        )
    monkeypatch.setenv("VECTOR_LAKE_MAX_UNSUPPORTED_RUNTIME_CLAIMS", "0")

    readiness = assess_semantic_readiness(
        index_data={"nodes": {}, "graph_state": {"dirty": False, "reason": "fresh"}}
    )

    assert readiness["ready"] is False
    assert readiness["status"] == "degraded"
    assert not any(
        issue.startswith("unmanaged_unsupported_runtime_claims:")
        for issue in readiness["issues"]
    )
    assert "managed_unsupported_runtime_claims:1" in readiness["warnings"]
    assert readiness["detail"]["runtime_unsupported_governance"] == {
        "total": 1,
        "managed": 1,
        "unmanaged": 0,
    }


def test_decision_scoped_readiness_uses_verified_registry_and_ignores_unmapped_debt(
    isolated_memory,
):
    from vector_lake.claim_assessment import record_claim_assessment
    from vector_lake.decision_registry import sync_critical_decision_registry

    db_store.init_db()
    claim = {
        "claim_id": "claim_decision_ready",
        "claim_text": "Decision evidence is complete.",
        "status": "Active",
        "evidence_ids": ["evidence_decision_ready"],
        "source_ids": ["source_decision_ready"],
        "locator": {"page_key": "Concept_Decision-Ready"},
    }
    evidence = {
        "evidence_id": "evidence_decision_ready",
        "source_id": "source_decision_ready",
        "source_locator": {"kind": "text", "paragraph": 2},
        "lineage_safe": True,
        "locator": {"page_key": "Concept_Decision-Ready"},
    }
    source = {
        "source_id": "source_decision_ready",
        "integrity_status": "verified",
        "content_hash": "a" * 64,
    }
    unrelated = {
        "item_id": "gov_unrelated",
        "type": "contradiction",
        "status": "pending",
        "critical_decision_refs": ["CD-OTHER"],
    }
    governance_store.apply_change_set(
        {
            "affected_pages": ["Concept_Decision-Ready.md"],
            "proposed_entities": [],
            "proposed_claims": [claim],
            "proposed_evidence": [evidence],
            "proposed_source_updates": [source],
            "proposed_edges": [],
        }
    )
    governance_store.upsert_governance_item(unrelated)
    record_claim_assessment(
        claim["claim_id"],
        assessment_type="evidence_review",
        outcome="supported",
        actor_id="reviewer:test",
        method_version="review-v1",
        reason="Verified for the scoped decision.",
    )
    sync_critical_decision_registry(
        {
            "contract_version": "1.0",
            "decisions": [
                {
                    "decision_id": "CD-READY-001",
                    "title": "Ready decision",
                    "owner": "owner:test",
                    "status": "active",
                    "risk_weight": 80,
                    "evidence_requirements": ["verified claim"],
                    "claim_refs": [claim["claim_id"]],
                    "verification": "cbss-registry-signature:test",
                }
            ],
        },
        verification_validator=lambda decision: decision["verification"].startswith(
            "cbss-registry-signature:"
        ),
    )

    readiness = assess_semantic_readiness(
        index_data={"graph_state": {"dirty": False}},
        decision_id="CD-READY-001",
    )

    assert readiness["ready"] is True
    assert readiness["status"] == "ready"
    assert readiness["detail"]["scoped_pending_governance_ids"] == []
    assert all(
        value is True
        for key, value in readiness["detail"]["claim_checks"][0].items()
        if key != "claim_id"
    )


def test_decision_scoped_readiness_rejects_unverified_registry_reference(
    isolated_memory,
):
    db_store.init_db()

    readiness = assess_semantic_readiness(
        index_data={"graph_state": {"dirty": False}},
        decision_id="CD-MISSING",
    )

    assert readiness["ready"] is False
    assert "critical_decision_unverified:CD-MISSING" in readiness["issues"]


def test_doctor_labels_infrastructure_and_semantic_status_separately(
    isolated_memory, monkeypatch
):
    from vector_lake import indexer, tool_doctor

    db_store.init_db()
    indexer.generate_index()

    monkeypatch.setattr(
        tool_doctor,
        "assess_semantic_readiness",
        lambda index_data=None: {
            "ready": False,
            "status": "not_ready",
            "issues": ["critical_governance_pending:2"],
            "warnings": ["provisional_runtime_claims:3"],
            "detail": {},
        },
    )

    doctor = tool_doctor.doctor_vector_lake()

    assert "Infrastructure Summary:" in doctor
    assert "Semantic Readiness: not_ready" in doctor
    assert "Semantic issues: critical_governance_pending:2" in doctor
    assert "Summary: infrastructure " in doctor
