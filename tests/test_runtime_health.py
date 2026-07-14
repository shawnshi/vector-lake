import json

import pytest

from vector_lake import db_store, indexer
from vector_lake.mutation_coordinator import execute_mutation_plan
from vector_lake.runtime_health import assess_runtime_health
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

    health = assess_runtime_health(deep_projection_checks=True)

    assert health["ok"] is True
    assert health["detail"]["timeline_projection_drift"] == {
        "canonical": 1,
        "projection": 1,
        "missing": 1,
        "extra": 1,
    }
    assert any("timeline_projection_drift" in warning for warning in health["warnings"])


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
