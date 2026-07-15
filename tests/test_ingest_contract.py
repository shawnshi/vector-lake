import json
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from vector_lake import db_store, governance_store, mcp_server, mutation_coordinator
from vector_lake.ingest_worker import _ingest_finalization_proven, process_jobs
from vector_lake.mutation_coordinator import execute_mutation_plan
from vector_lake.tool_ingest import (
    _read_relevant_index_context,
    claim_ingest_tasks,
    list_ingest_tasks,
)
from tests.test_mutation_coordinator import _source_content, _write_purpose_contract


def _concept_content(title="Target Concept"):
    return f"""---
id: concept_target
title: {title}
type: concept
domain: General
status: Active
epistemic-status: seed
categories: [System_Architecture]
updated: 2026-07-13T00:00:00+00:00
sources: [raw/original.md]
strategic_scope: core
evidence_tier: primary
---
## 1. 编译事实

Target compiled truth.

## 2. 证据时间线
"""


def _synthesis_content(title="Target Synthesis"):
    return f"""---
id: synthesis_target
title: {title}
type: synthesis
domain: General
status: Active
epistemic-status: seed
categories: [System_Architecture]
updated: 2026-07-13T00:00:00+00:00
sources: [raw/original.md]
strategic_scope: core
evidence_tier: primary
---
## 核心合成论点 (Core Synthesized Claims)

Target synthesis.

## 支撑拓扑 (Supporting Topology)
"""

def test_finalize_ingest_accepts_payload_file_contract(tmp_path, monkeypatch):
    monkeypatch.setenv("VECTOR_LAKE_PAYLOAD_ROOT", str(tmp_path))
    files = [{"filename": "Source_Test.md", "content": "body"}]
    processed = {"filepath": "raw/test.pdf", "hash": "abc123"}
    files_path = tmp_path / "files.json"
    raw_path = tmp_path / "raw.json"
    files_path.write_text(json.dumps(files), encoding="utf-8")
    raw_path.write_text(json.dumps(processed), encoding="utf-8")
    captured = {}

    def fake_finalize(actual_files, actual_processed):
        captured["files"] = actual_files
        captured["processed"] = actual_processed
        return "ok"

    monkeypatch.setattr(mcp_server.tools, "finalize_ingest", fake_finalize)
    result = mcp_server.finalize_ingest(
        files_written_payload_file=str(files_path),
        raw_files_payload_file=str(raw_path),
    )

    assert result == "ok"
    assert captured == {"files": files, "processed": processed}


def test_ingest_finalization_requires_matching_processed_file(isolated_memory):
    db_store.init_db()
    assert _ingest_finalization_proven("raw/test.pdf", "abc123") is False
    db_store.mark_file_processed("raw/test.pdf", "different")
    assert _ingest_finalization_proven("raw/test.pdf", "abc123") is False
    db_store.mark_file_processed("raw/test.pdf", "abc123")
    assert _ingest_finalization_proven("raw/test.pdf", "abc123") is True


def test_job_claim_uses_a_lease(isolated_memory):
    db_store.init_db()
    job_id = db_store.enqueue_job("ingest", {"filepath": "raw/test.pdf"})

    first = db_store.claim_pending_jobs(limit=1, lease_seconds=60)
    second = db_store.claim_pending_jobs(limit=1, lease_seconds=60)

    assert [job["job_id"] for job in first] == [job_id]
    assert second == []


def test_ingest_job_enqueue_is_idempotent_by_file_hash(isolated_memory):
    db_store.init_db()
    payload = {
        "filepath": "raw/test.pdf",
        "hash": "abc123",
        "canonical_name": "Source_Test.md",
        "instructions": "large prompt",
    }

    first = db_store.enqueue_job("ingest", payload)
    second = db_store.enqueue_job("ingest", {**payload, "instructions": "different prompt"})

    assert second == first
    assert db_store.get_connection().execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1


def test_ingest_worker_creates_subagent_task_packet(isolated_memory):
    db_store.init_db()
    payload = {
        "filepath": "raw/native.md",
        "hash": "native-hash",
        "canonical_name": "Source_Native.md",
        "source_hash": "native-source-version",
        "instructions": "compile this source",
    }
    job_id = db_store.enqueue_job("ingest", payload)

    process_jobs()

    row = db_store.get_connection().execute(
        "SELECT status, error_msg FROM jobs WHERE job_id = ?",
        (job_id,),
    ).fetchone()
    assert row["status"] == "awaiting_subagent"
    task_path = row["error_msg"].split("Subagent task packet: ", 1)[1]
    with open(task_path, "r", encoding="utf-8") as handle:
        task = json.load(handle)
    assert task["task_type"] == "ingest"
    assert task["runtime"] == "current-environment-subagent"
    assert task["metadata"]["job_id"] == job_id
    assert task["metadata"]["processed_data"]["filepath"] == "raw/native.md"
    assert task["metadata"]["processed_data"]["source_hash"] == "native-source-version"
    assert task["metadata"]["processed_data"]["job_id"] == job_id
    assert "CURRENT-ENVIRONMENT SUBAGENT HANDOFF" in task["prompt"]
    listed = list_ingest_tasks(limit=5, include_queued=False)
    assert job_id in listed
    assert "task_packet=" in listed
    claimed = json.loads(claim_ingest_tasks(limit=1, lease_seconds=60))[0]
    claimed_processed = claimed["task_packet"]["metadata"]["processed_data"]
    assert claimed_processed["lease_owner"] == claimed["lease_owner"]
    assert claimed_processed["lease_token"] == claimed["lease_token"]
    assert claimed_processed["lease_generation"] == claimed["lease_generation"]
    import os
    os.remove(task_path)


def test_ingest_worker_rebuilds_legacy_awaiting_packet_before_dispatch(isolated_memory):
    _write_purpose_contract(isolated_memory)
    db_store.init_db()
    raw_path = isolated_memory / "raw" / "legacy-awaiting.md"
    raw_path.write_text("Legacy source content.", encoding="utf-8")
    (isolated_memory / "wiki" / "index.json").write_text(
        json.dumps({"nodes": {}}),
        encoding="utf-8",
    )
    payload = {
        "filepath": str(raw_path),
        "hash": "legacy-awaiting-hash",
        "canonical_name": "Source_Legacy-Awaiting.md",
        "instructions": "legacy prompt without integration disposition",
    }
    job_id = db_store.enqueue_job("ingest", payload)
    from vector_lake.native_llm import create_subagent_task

    old_task = create_subagent_task("ingest", "legacy prompt", "legacy output", {"job_id": job_id})
    db_store.mark_job_awaiting_subagent(job_id, str(old_task))

    process_jobs()

    row = db_store.get_connection().execute(
        "SELECT status, payload, task_packet_path FROM jobs WHERE job_id = ?",
        (job_id,),
    ).fetchone()
    rebuilt = json.loads(row["payload"])
    assert row["status"] == "awaiting_subagent"
    assert rebuilt["source_hash"] == ""
    assert "semantic disposition" in rebuilt["instructions"]
    assert "canonical SQLite version tokens" in rebuilt["instructions"]
    assert row["task_packet_path"] != str(old_task)
    assert old_task.exists() is False
    Path(row["task_packet_path"]).unlink(missing_ok=True)


def test_subagent_task_claim_uses_lease_and_can_reclaim_expired_work(isolated_memory):
    db_store.init_db()
    payload = {
        "filepath": "raw/lease.md",
        "hash": "lease-hash",
        "canonical_name": "Source_Lease.md",
        "instructions": "compile this source",
    }
    job_id = db_store.enqueue_job("ingest", payload)
    db_store.mark_job_awaiting_subagent(job_id, "")

    first = json.loads(claim_ingest_tasks(limit=1, lease_seconds=60))
    second = json.loads(claim_ingest_tasks(limit=1, lease_seconds=60))

    assert [item["job_id"] for item in first] == [job_id]
    assert second == []
    assert first[0]["task_packet"] is None
    assert first[0]["lease_owner"]

    with db_store.transaction():
        db_store.get_connection().execute(
            "UPDATE jobs SET lease_until = '2000-01-01T00:00:00+00:00' WHERE job_id = ?",
            (job_id,),
        )
    reclaimed = json.loads(claim_ingest_tasks(limit=1, lease_seconds=60))
    assert [item["job_id"] for item in reclaimed] == [job_id]
    assert reclaimed[0]["lease_generation"] == first[0]["lease_generation"] + 1
    assert reclaimed[0]["lease_token"] != first[0]["lease_token"]


def test_finalize_ingest_rejects_mismatched_job_payload(isolated_memory, monkeypatch):
    db_store.init_db()
    monkeypatch.setattr("vector_lake.tool_ingest.load_purpose_contract", lambda: {})
    monkeypatch.setattr("vector_lake.tool_ingest.validate_ingest_payload", lambda files, contract: [])
    job_id = db_store.enqueue_job(
        "ingest",
        {
            "filepath": "raw/expected.md",
            "hash": "expected-hash",
            "canonical_name": "Source_Expected.md",
            "instructions": "compile this source",
        },
    )
    db_store.mark_job_awaiting_subagent(job_id, "")
    claim = json.loads(claim_ingest_tasks(limit=1, lease_seconds=60))[0]

    result = mcp_server.tools.finalize_ingest(
        [],
        {
            "filepath": "raw/other.md",
            "hash": "expected-hash",
            "canonical_name": "Source_Expected.md",
            "job_id": job_id,
            "lease_owner": claim["lease_owner"],
            "lease_token": claim["lease_token"],
            "lease_generation": claim["lease_generation"],
        },
    )

    row = db_store.get_connection().execute(
        "SELECT status FROM jobs WHERE job_id = ?",
        (job_id,),
    ).fetchone()
    assert result.startswith("Error finalizing ingestion")
    assert "filepath does not match" in result
    assert row["status"] == "subagent_processing"
    assert _ingest_finalization_proven("raw/other.md", "expected-hash") is False


def test_finalize_ingest_rejects_source_hash_not_bound_to_job_payload(isolated_memory, monkeypatch):
    db_store.init_db()
    monkeypatch.setattr("vector_lake.tool_ingest.load_purpose_contract", lambda: {})
    monkeypatch.setattr("vector_lake.tool_ingest.validate_ingest_payload", lambda files, contract: [])
    job_id = db_store.enqueue_job(
        "ingest",
        {
            "filepath": "raw/source-version.md",
            "hash": "source-version-hash",
            "canonical_name": "Source_Version.md",
            "source_hash": "queued-version",
        },
    )
    db_store.mark_job_awaiting_subagent(job_id, "")
    claim = json.loads(claim_ingest_tasks(limit=1, lease_seconds=60))[0]

    result = mcp_server.tools.finalize_ingest(
        [],
        {
            "filepath": "raw/source-version.md",
            "hash": "source-version-hash",
            "canonical_name": "Source_Version.md",
            "source_hash": "substituted-version",
            "job_id": job_id,
            "lease_owner": claim["lease_owner"],
            "lease_token": claim["lease_token"],
            "lease_generation": claim["lease_generation"],
        },
    )

    assert result.startswith("Error finalizing ingestion")
    assert "source_hash does not match" in result


def test_finalize_ingest_marks_subagent_job_finalized(isolated_memory, monkeypatch):
    db_store.init_db()
    monkeypatch.setenv("VECTOR_LAKE_SUBAGENT_RUN_ID", "pytest-finalize")
    monkeypatch.setattr("vector_lake.tool_ingest.load_purpose_contract", lambda: {})
    monkeypatch.setattr("vector_lake.tool_ingest.validate_ingest_payload", lambda files, contract: [])
    job_id = db_store.enqueue_job(
        "ingest",
        {
            "filepath": "raw/finalize.md",
            "hash": "finalize-hash",
            "canonical_name": "Source_Finalize.md",
            "instructions": "compile this source",
        },
    )
    from vector_lake.native_llm import create_subagent_task

    task_path = create_subagent_task("ingest", "test", "JSON array", {"job_id": job_id})
    db_store.mark_job_awaiting_subagent(job_id, str(task_path))
    claim = json.loads(claim_ingest_tasks(limit=1, lease_seconds=60))[0]

    result = mcp_server.tools.finalize_ingest(
        [],
        {
            "filepath": "raw/finalize.md",
            "hash": "finalize-hash",
            "canonical_name": "Source_Finalize.md",
            "integration": {
                "disposition": "rejected",
                "reason": "Source is outside the active strategic purpose contract.",
            },
            "job_id": job_id,
            "lease_owner": claim["lease_owner"],
            "lease_token": claim["lease_token"],
            "lease_generation": claim["lease_generation"],
        },
    )

    row = db_store.get_connection().execute(
        "SELECT status FROM jobs WHERE job_id = ?",
        (job_id,),
    ).fetchone()
    assert result.startswith("Successfully finalized ingestion")
    assert row["status"] == "finalized"
    assert _ingest_finalization_proven("raw/finalize.md", "finalize-hash") is True
    assert task_path.exists() is False


def test_finalize_ingest_requires_claimed_job(isolated_memory, monkeypatch):
    db_store.init_db()
    monkeypatch.setattr("vector_lake.tool_ingest.load_purpose_contract", lambda: {})
    monkeypatch.setattr("vector_lake.tool_ingest.validate_ingest_payload", lambda files, contract: [])

    result = mcp_server.tools.finalize_ingest(
        [],
        {"filepath": "raw/unbound.md", "hash": "unbound-hash"},
    )

    assert result.startswith("Error finalizing ingestion")
    assert "claimed job_id" in result
    assert _ingest_finalization_proven("raw/unbound.md", "unbound-hash") is False


def test_stale_subagent_lease_cannot_finalize_after_reclaim(isolated_memory, monkeypatch):
    db_store.init_db()
    monkeypatch.setattr("vector_lake.tool_ingest.load_purpose_contract", lambda: {})
    monkeypatch.setattr("vector_lake.tool_ingest.validate_ingest_payload", lambda files, contract: [])
    job_id = db_store.enqueue_job(
        "ingest",
        {
            "filepath": "raw/fenced.md",
            "hash": "fenced-hash",
            "canonical_name": "Source_Fenced.md",
        },
    )
    db_store.mark_job_awaiting_subagent(job_id, "")
    stale = json.loads(claim_ingest_tasks(limit=1, lease_seconds=60))[0]
    with db_store.transaction():
        db_store.get_connection().execute(
            "UPDATE jobs SET lease_until = '2000-01-01T00:00:00+00:00' WHERE job_id = ?",
            (job_id,),
        )
    current = json.loads(claim_ingest_tasks(limit=1, lease_seconds=60))[0]

    stale_result = mcp_server.tools.finalize_ingest(
        [],
        {
            "filepath": "raw/fenced.md",
            "hash": "fenced-hash",
            "canonical_name": "Source_Fenced.md",
            "job_id": job_id,
            "lease_owner": stale["lease_owner"],
            "lease_token": stale["lease_token"],
            "lease_generation": stale["lease_generation"],
        },
    )

    assert stale_result.startswith("Error finalizing ingestion")
    assert "current lease" in stale_result
    row = db_store.get_connection().execute(
        "SELECT status, lease_token, lease_generation FROM jobs WHERE job_id = ?",
        (job_id,),
    ).fetchone()
    assert row["status"] == "subagent_processing"
    assert row["lease_token"] == current["lease_token"]
    assert row["lease_generation"] == current["lease_generation"]
    assert _ingest_finalization_proven("raw/fenced.md", "fenced-hash") is False


def test_final_cas_rolls_back_processed_marker_if_lease_changes_after_validation(isolated_memory, monkeypatch):
    db_store.init_db()
    monkeypatch.setattr("vector_lake.tool_ingest.load_purpose_contract", lambda: {})
    job_id = db_store.enqueue_job(
        "ingest",
        {
            "filepath": "raw/race.md",
            "hash": "race-hash",
            "canonical_name": "Source_Race.md",
        },
    )
    db_store.mark_job_awaiting_subagent(job_id, "")
    stale = json.loads(claim_ingest_tasks(limit=1, lease_seconds=60))[0]

    def reclaim_during_payload_validation(files, contract):
        with db_store.transaction():
            db_store.get_connection().execute(
                "UPDATE jobs SET lease_until = '2000-01-01T00:00:00+00:00' WHERE job_id = ?",
                (job_id,),
            )
        json.loads(claim_ingest_tasks(limit=1, lease_seconds=60))
        return []

    monkeypatch.setattr("vector_lake.tool_ingest.validate_ingest_payload", reclaim_during_payload_validation)
    result = mcp_server.tools.finalize_ingest(
        [],
        {
            "filepath": "raw/race.md",
            "hash": "race-hash",
            "canonical_name": "Source_Race.md",
            "integration": {
                "disposition": "rejected",
                "reason": "Source is outside the active strategic purpose contract.",
            },
            "job_id": job_id,
            "lease_owner": stale["lease_owner"],
            "lease_token": stale["lease_token"],
            "lease_generation": stale["lease_generation"],
        },
    )

    assert result.startswith("Error finalizing ingestion")
    assert "no longer finalizable" in result
    assert _ingest_finalization_proven("raw/race.md", "race-hash") is False
    row = db_store.get_connection().execute(
        "SELECT status, lease_generation FROM jobs WHERE job_id = ?",
        (job_id,),
    ).fetchone()
    assert row["status"] == "subagent_processing"
    assert row["lease_generation"] == stale["lease_generation"] + 1


def test_relevant_index_context_searches_beyond_first_hundred_nodes(isolated_memory):
    _write_purpose_contract(isolated_memory)
    raw_path = isolated_memory / "raw" / "candidate.md"
    raw_path.write_text("国家级主数据治理要求进入持续运营阶段。", encoding="utf-8")
    target_content = _concept_content("主数据治理")
    execute_mutation_plan("Concept_主数据治理.md", content=target_content)
    nodes = {
        f"Concept_Noise-{index}": {
            "id": f"Concept_Noise-{index}",
            "title": f"Noise {index}",
            "type": "concept",
            "summary": "unrelated",
        }
        for index in range(150)
    }
    nodes["Concept_主数据治理"] = {
        "id": "Concept_主数据治理",
        "title": "主数据治理",
        "type": "concept",
        "summary": "跨机构数据质量治理",
    }
    (isolated_memory / "wiki" / "index.json").write_text(
        json.dumps({"nodes": nodes}, ensure_ascii=False),
        encoding="utf-8",
    )

    context = _read_relevant_index_context(str(raw_path), max_nodes=10)

    assert "Concept_主数据治理.md" in context
    assert governance_store.canonical_page_versions({"Concept_主数据治理"})[
        "Concept_主数据治理"
    ] in context


def test_relevant_index_context_includes_canonical_synthesis_candidates(isolated_memory):
    _write_purpose_contract(isolated_memory)
    raw_path = isolated_memory / "raw" / "synthesis-candidate.md"
    raw_path.write_text("Agentic clinical systems need a target synthesis.", encoding="utf-8")
    execute_mutation_plan(
        "Synthesis_Agentic-Clinical-Systems.md",
        content=_synthesis_content("Agentic Clinical Systems"),
    )
    (isolated_memory / "wiki" / "index.json").write_text(
        json.dumps({"nodes": {
            "Synthesis_Agentic-Clinical-Systems": {
                "type": "synthesis",
                "title": "Agentic Clinical Systems",
                "summary": "Clinical orchestration synthesis",
            }
        }}),
        encoding="utf-8",
    )

    context = _read_relevant_index_context(str(raw_path))

    assert "Synthesis_Agentic-Clinical-Systems.md" in context
    assert governance_store.canonical_page_versions({"Synthesis_Agentic-Clinical-Systems"})[
        "Synthesis_Agentic-Clinical-Systems"
    ] in context


def test_relevant_index_context_rejects_an_unreadable_index(isolated_memory):
    raw_path = isolated_memory / "raw" / "candidate.md"
    raw_path.write_text("source", encoding="utf-8")
    (isolated_memory / "wiki" / "index.json").write_text("{broken", encoding="utf-8")

    with pytest.raises(RuntimeError, match="source-relevant ingest context"):
        _read_relevant_index_context(str(raw_path))


def test_relevant_index_context_excludes_sources_and_ascii_substring_false_positives(isolated_memory):
    _write_purpose_contract(isolated_memory)
    raw_path = isolated_memory / "raw" / "candidate.md"
    raw_path.write_text("The team will investigate the clinical workflow and platform system. 医疗平台体系。", encoding="utf-8")
    execute_mutation_plan(
        "Concept_GATE.md",
        content=_concept_content("GATE").replace("id: concept_target", "id: concept_gate"),
    )
    execute_mutation_plan(
        "Concept_Noise.md",
        content=_concept_content("Noise").replace("id: concept_target", "id: concept_noise"),
    )
    (isolated_memory / "wiki" / "index.json").write_text(
        json.dumps({"nodes": {
            "Source_Candidate": {"type": "source", "title": "Candidate"},
            "Concept_GATE": {"type": "concept", "title": "GATE", "summary": "execution test"},
            "Concept_Noise": {"type": "concept", "title": "Noise", "aliases": ["体系", "平台"]},
        }}),
        encoding="utf-8",
    )

    context = _read_relevant_index_context(str(raw_path))

    assert "Source_Candidate.md" not in context
    assert "Concept_GATE.md" not in context
    assert "Concept_Noise.md" not in context


def test_finalize_ingest_rejects_missing_semantic_disposition(isolated_memory, monkeypatch):
    db_store.init_db()
    monkeypatch.setattr("vector_lake.tool_ingest.load_purpose_contract", lambda: {})
    monkeypatch.setattr("vector_lake.tool_ingest.validate_ingest_payload", lambda files, contract: [])
    job_id = db_store.enqueue_job(
        "ingest",
        {"filepath": "raw/no-disposition.md", "hash": "no-disposition", "canonical_name": "Source_No-Disposition.md"},
    )
    db_store.mark_job_awaiting_subagent(job_id, "")
    claim = json.loads(claim_ingest_tasks(limit=1, lease_seconds=60))[0]

    result = mcp_server.tools.finalize_ingest(
        [],
        {
            "filepath": "raw/no-disposition.md",
            "hash": "no-disposition",
            "canonical_name": "Source_No-Disposition.md",
            "job_id": job_id,
            "lease_owner": claim["lease_owner"],
            "lease_token": claim["lease_token"],
            "lease_generation": claim["lease_generation"],
        },
    )

    assert result.startswith("Error finalizing ingestion")
    assert "integration disposition" in result
    assert db_store.get_connection().execute(
        "SELECT status FROM jobs WHERE job_id = ?", (job_id,)
    ).fetchone()["status"] == "subagent_processing"
    assert _ingest_finalization_proven("raw/no-disposition.md", "no-disposition") is False


def test_finalize_ingest_accepts_audited_standalone_source(isolated_memory):
    _write_purpose_contract(isolated_memory)
    db_store.init_db()
    job_id = db_store.enqueue_job(
        "ingest",
        {"filepath": "raw/standalone.md", "hash": "standalone-hash", "canonical_name": "Source_Standalone.md"},
    )
    db_store.mark_job_awaiting_subagent(job_id, "")
    claim = json.loads(claim_ingest_tasks(limit=1, lease_seconds=60))[0]

    result = mcp_server.tools.finalize_ingest(
        [{"filename": "Source_Standalone.md", "content": _source_content()}],
        {
            "filepath": "raw/standalone.md",
            "hash": "standalone-hash",
            "canonical_name": "Source_Standalone.md",
            "integration": {
                "disposition": "standalone",
                "reason": "No existing node has a direct, source-supported semantic relation.",
            },
            "job_id": job_id,
            "lease_owner": claim["lease_owner"],
            "lease_token": claim["lease_token"],
            "lease_generation": claim["lease_generation"],
        },
    )

    assert result.startswith("Successfully finalized ingestion")
    assert (isolated_memory / "wiki" / "Source_Standalone.md").exists()
    stored_result = db_store.get_connection().execute(
        "SELECT result_json FROM jobs WHERE job_id = ?", (job_id,)
    ).fetchone()["result_json"]
    assert json.loads(stored_result)["integration"]["disposition"] == "standalone"


def test_standalone_ingest_cannot_overwrite_existing_source_without_queued_version(isolated_memory):
    _write_purpose_contract(isolated_memory)
    execute_mutation_plan("Source_Standalone.md", content=_source_content())
    payload = {
        "filepath": "raw/standalone-rewrite.md",
        "hash": "standalone-rewrite",
        "canonical_name": "Source_Standalone.md",
        "source_hash": "",
    }
    job_id = db_store.enqueue_job("ingest", payload)
    db_store.mark_job_awaiting_subagent(job_id, "")
    claim = json.loads(claim_ingest_tasks(limit=1, lease_seconds=60))[0]

    result = mcp_server.tools.finalize_ingest(
        [{
            "filename": "Source_Standalone.md",
            "content": _source_content().replace("Primary source content.", "Unauthorized rewrite."),
        }],
        {
            **payload,
            "integration": {
                "disposition": "standalone",
                "reason": "No existing node has a direct, source-supported semantic relation.",
            },
            "job_id": job_id,
            "lease_owner": claim["lease_owner"],
            "lease_token": claim["lease_token"],
            "lease_generation": claim["lease_generation"],
        },
    )

    assert result.startswith("Error finalizing ingestion")
    assert "Canonical version conflict" in result
    assert _ingest_finalization_proven("raw/standalone-rewrite.md", "standalone-rewrite") is False
    assert db_store.get_connection().execute(
        "SELECT status FROM jobs WHERE job_id = ?", (job_id,)
    ).fetchone()["status"] == "subagent_processing"
    assert "Unauthorized rewrite." not in (
        isolated_memory / "wiki" / "Source_Standalone.md"
    ).read_text(encoding="utf-8")


def test_finalize_ingest_integrates_source_and_target_atomically(isolated_memory):
    _write_purpose_contract(isolated_memory)
    target_content = _concept_content()
    target_path = isolated_memory / "wiki" / "Concept_Target.md"
    execute_mutation_plan("Concept_Target.md", content=target_content)
    target_content = target_path.read_text(encoding="utf-8")
    target_version = governance_store.canonical_page_versions({"Concept_Target"})["Concept_Target"]
    outbox_before = db_store.get_connection().execute("SELECT COUNT(*) FROM mutation_outbox").fetchone()[0]
    db_store.init_db()
    job_id = db_store.enqueue_job(
        "ingest",
        {"filepath": "raw/integrated.md", "hash": "integrated-hash", "canonical_name": "Source_Integrated.md"},
    )
    db_store.mark_job_awaiting_subagent(job_id, "")
    claim = json.loads(claim_ingest_tasks(limit=1, lease_seconds=60))[0]

    result = mcp_server.tools.finalize_ingest(
        [{"filename": "Source_Integrated.md", "content": _source_content()}],
        {
            "filepath": "raw/integrated.md",
            "hash": "integrated-hash",
            "canonical_name": "Source_Integrated.md",
            "integration": {
                "disposition": "integrated",
                "relations": [{
                    "target": "Concept_Target.md",
                    "target_hash": target_version,
                    "predicate": "validates",
                    "evidence": "The source directly supports the target mechanism.",
                    "confidence": 0.93,
                    "event_date": "2026-07-15",
                    "event_tag": "Validation",
                }],
            },
            "job_id": job_id,
            "lease_owner": claim["lease_owner"],
            "lease_token": claim["lease_token"],
            "lease_generation": claim["lease_generation"],
        },
    )

    assert result.startswith("Successfully finalized ingestion")
    source = (isolated_memory / "wiki" / "Source_Integrated.md").read_text(encoding="utf-8")
    target = target_path.read_text(encoding="utf-8")
    assert "[validates:: [[Concept_Target]]]" in source
    assert "(Source: [[Source_Integrated]])" in target
    assert db_store.get_connection().execute("SELECT COUNT(*) FROM mutation_outbox").fetchone()[0] == outbox_before + 2


def test_integration_uses_canonical_outbox_snapshot_when_markdown_projection_is_stale(
    isolated_memory,
    monkeypatch,
):
    _write_purpose_contract(isolated_memory)
    target_path = isolated_memory / "wiki" / "Concept_Target.md"
    execute_mutation_plan("Concept_Target.md", content=_concept_content())
    stale_projection = target_path.read_text(encoding="utf-8")
    canonical_v2 = _concept_content().replace(
        "Target compiled truth.",
        "Canonical V2 content must survive integration.",
    )
    real_materialize = mutation_coordinator.materialize_markdown_projection
    fail_projection = True

    def fail_once_for_target(filename, mutation_type, payload_text=None, validation_mode="full"):
        if fail_projection and filename == "Concept_Target.md":
            raise OSError("injected projection failure")
        return real_materialize(filename, mutation_type, payload_text, validation_mode)

    monkeypatch.setattr(
        mutation_coordinator,
        "materialize_markdown_projection",
        fail_once_for_target,
    )
    execute_mutation_plan("Concept_Target.md", content=canonical_v2)
    assert target_path.read_text(encoding="utf-8") == stale_projection
    target_version = governance_store.canonical_page_versions({"Concept_Target"})["Concept_Target"]
    fail_projection = False

    payload = {
        "filepath": "raw/stale-projection.md",
        "hash": "stale-projection",
        "canonical_name": "Source_Stale-Projection.md",
        "source_hash": "",
    }
    job_id = db_store.enqueue_job("ingest", payload)
    db_store.mark_job_awaiting_subagent(job_id, "")
    claim = json.loads(claim_ingest_tasks(limit=1, lease_seconds=60))[0]
    result = mcp_server.tools.finalize_ingest(
        [{"filename": "Source_Stale-Projection.md", "content": _source_content()}],
        {
            **payload,
            "integration": {
                "disposition": "integrated",
                "relations": [{
                    "target": "Concept_Target.md",
                    "target_hash": target_version,
                    "predicate": "validates",
                    "evidence": "The source supports the canonical V2 target content.",
                    "confidence": 0.94,
                    "event_date": "2026-07-15",
                    "event_tag": "Validation",
                }],
            },
            "job_id": job_id,
            "lease_owner": claim["lease_owner"],
            "lease_token": claim["lease_token"],
            "lease_generation": claim["lease_generation"],
        },
    )

    assert result.startswith("Successfully finalized ingestion")
    updated_target = target_path.read_text(encoding="utf-8")
    assert "Canonical V2 content must survive integration." in updated_target
    assert "The source supports the canonical V2 target content." in updated_target


def test_reingest_replaces_relation_evidence_without_duplicate_anchors(isolated_memory):
    _write_purpose_contract(isolated_memory)
    execute_mutation_plan("Concept_Target.md", content=_concept_content())

    def finalize_version(filepath, file_hash, source_hash, target_hash, evidence, predicate, event_tag):
        payload = {
            "filepath": filepath,
            "hash": file_hash,
            "canonical_name": "Source_Integrated.md",
            "source_hash": source_hash,
        }
        job_id = db_store.enqueue_job("ingest", payload)
        db_store.mark_job_awaiting_subagent(job_id, "")
        claim = json.loads(claim_ingest_tasks(limit=1, lease_seconds=60))[0]
        return mcp_server.tools.finalize_ingest(
            [{"filename": "Source_Integrated.md", "content": _source_content()}],
            {
                **payload,
                "integration": {
                    "disposition": "integrated",
                    "relations": [{
                        "target": "Concept_Target.md",
                        "target_hash": target_hash,
                        "predicate": predicate,
                        "evidence": evidence,
                        "confidence": 0.91,
                        "event_date": "2026-07-15",
                        "event_tag": event_tag,
                    }],
                },
                "job_id": job_id,
                "lease_owner": claim["lease_owner"],
                "lease_token": claim["lease_token"],
                "lease_generation": claim["lease_generation"],
            },
        )

    first = finalize_version(
        "raw/integrated-v1.md",
        "integrated-v1",
        "",
        governance_store.canonical_page_versions({"Concept_Target"})["Concept_Target"],
        "The original source evidence supports the target mechanism.",
        "validates",
        "Validation",
    )
    assert first.startswith("Successfully finalized ingestion")

    source_path = isolated_memory / "wiki" / "Source_Integrated.md"
    target_path = isolated_memory / "wiki" / "Concept_Target.md"
    legacy_source = re.sub(
        r"\s*<!-- vector-lake-relation:[0-9a-f]+ -->",
        "",
        source_path.read_text(encoding="utf-8"),
    )
    source_legacy_line = next(
        line for line in legacy_source.splitlines() if "[[Concept_Target]]" in line
    )
    execute_mutation_plan(
        "Source_Integrated.md",
        content=f"{legacy_source.rstrip()}\n{source_legacy_line.replace('original source evidence', 'second stale duplicate')}\n",
    )
    legacy_target = re.sub(
        r"\s*<!-- vector-lake-relation:[0-9a-f]+ -->",
        "",
        target_path.read_text(encoding="utf-8"),
    )
    target_legacy_line = next(
        line for line in legacy_target.splitlines() if "(Source: [[Source_Integrated]])" in line
    )
    execute_mutation_plan(
        "Concept_Target.md",
        content=f"{legacy_target.rstrip()}\n{target_legacy_line.replace('original source evidence', 'second stale duplicate')}\n",
    )

    second = finalize_version(
        "raw/integrated-v2.md",
        "integrated-v2",
        governance_store.canonical_page_versions({"Source_Integrated"})["Source_Integrated"],
        governance_store.canonical_page_versions({"Concept_Target"})["Concept_Target"],
        "The revised source evidence changes the supported mechanism.",
        "related_to",
        "Observation",
    )
    assert second.startswith("Successfully finalized ingestion")

    source = source_path.read_text(encoding="utf-8")
    target = target_path.read_text(encoding="utf-8")
    assert "The revised source evidence" in source
    assert "The revised source evidence" in target
    assert "The original source evidence" not in source
    assert "The original source evidence" not in target
    assert "second stale duplicate" not in source
    assert "second stale duplicate" not in target
    assert source.count("vector-lake-relation:") == 1
    assert target.count("vector-lake-relation:") == 1
    assert target.count("(Source: [[Source_Integrated]])") == 1


def test_finalize_ingest_integrates_into_synthesis_supporting_topology(isolated_memory):
    _write_purpose_contract(isolated_memory)
    execute_mutation_plan("Synthesis_Target.md", content=_synthesis_content())
    target_version = governance_store.canonical_page_versions({"Synthesis_Target"})["Synthesis_Target"]
    payload = {
        "filepath": "raw/synthesis-support.md",
        "hash": "synthesis-support",
        "canonical_name": "Source_Synthesis-Support.md",
        "source_hash": "",
    }
    job_id = db_store.enqueue_job("ingest", payload)
    db_store.mark_job_awaiting_subagent(job_id, "")
    claim = json.loads(claim_ingest_tasks(limit=1, lease_seconds=60))[0]

    result = mcp_server.tools.finalize_ingest(
        [{"filename": "Source_Synthesis-Support.md", "content": _source_content()}],
        {
            **payload,
            "integration": {
                "disposition": "integrated",
                "relations": [{
                    "target": "Synthesis_Target.md",
                    "target_hash": target_version,
                    "predicate": "validates",
                    "evidence": "The source supports a critical synthesized claim directly.",
                    "confidence": 0.88,
                    "event_date": "2026-07-15",
                    "event_tag": "Validation",
                }],
            },
            "job_id": job_id,
            "lease_owner": claim["lease_owner"],
            "lease_token": claim["lease_token"],
            "lease_generation": claim["lease_generation"],
        },
    )

    assert result.startswith("Successfully finalized ingestion")
    target = (isolated_memory / "wiki" / "Synthesis_Target.md").read_text(encoding="utf-8")
    assert "## 支撑拓扑 (Supporting Topology)" in target
    assert "[depends-on:: [[Source_Synthesis-Support]]]" in target
    assert target.count("vector-lake-relation:") == 1


def test_finalize_ingest_rejects_stale_target_hash(isolated_memory):
    _write_purpose_contract(isolated_memory)
    target_path = isolated_memory / "wiki" / "Concept_Target.md"
    target_path.write_text(_concept_content(), encoding="utf-8")
    db_store.init_db()
    job_id = db_store.enqueue_job(
        "ingest",
        {"filepath": "raw/stale-target.md", "hash": "stale-target", "canonical_name": "Source_Stale-Target.md"},
    )
    db_store.mark_job_awaiting_subagent(job_id, "")
    claim = json.loads(claim_ingest_tasks(limit=1, lease_seconds=60))[0]

    result = mcp_server.tools.finalize_ingest(
        [{"filename": "Source_Stale-Target.md", "content": _source_content()}],
        {
            "filepath": "raw/stale-target.md",
            "hash": "stale-target",
            "canonical_name": "Source_Stale-Target.md",
            "integration": {
                "disposition": "integrated",
                "relations": [{
                    "target": "Concept_Target.md",
                    "target_hash": "stale",
                    "predicate": "validates",
                    "evidence": "The source directly supports the target mechanism.",
                    "confidence": 0.9,
                    "event_date": "2026-07-15",
                    "event_tag": "Validation",
                }],
            },
            "job_id": job_id,
            "lease_owner": claim["lease_owner"],
            "lease_token": claim["lease_token"],
            "lease_generation": claim["lease_generation"],
        },
    )

    assert result.startswith("Error finalizing ingestion")
    assert "target_hash" in result
    assert not (isolated_memory / "wiki" / "Source_Stale-Target.md").exists()
    assert _ingest_finalization_proven("raw/stale-target.md", "stale-target") is False


def test_init_db_migrates_legacy_jobs_without_changing_payload(isolated_memory):
    path = db_store.get_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE jobs (job_id TEXT PRIMARY KEY, task_type TEXT, payload TEXT, status TEXT, "
        "retries INTEGER DEFAULT 0, error_msg TEXT, created_at TEXT, updated_at TEXT)"
    )
    connection.execute(
        "INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("legacy-job", "ingest", '{"filepath":"raw/legacy.md"}', "awaiting_subagent", 0, "", "2026-01-01", "2026-01-01"),
    )
    connection.commit()
    connection.close()

    db_store.init_db()

    columns = {row["name"] for row in db_store.get_connection().execute("PRAGMA table_info(jobs)")}
    assert {"lease_owner", "lease_token", "lease_generation"} <= columns
    row = db_store.get_connection().execute(
        "SELECT payload, status, lease_generation FROM jobs WHERE job_id = 'legacy-job'"
    ).fetchone()
    assert dict(row) == {
        "payload": '{"filepath":"raw/legacy.md"}',
        "status": "awaiting_subagent",
        "lease_generation": 0,
    }


def test_concurrent_subagent_claim_has_single_winner(isolated_memory):
    db_store.init_db()
    job_id = db_store.enqueue_job("ingest", {"filepath": "raw/concurrent.md", "hash": "hash"})
    db_store.mark_job_awaiting_subagent(job_id, "")

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(
            lambda owner: db_store.claim_subagent_jobs(limit=1, lease_seconds=60, lease_owner=owner),
            ["owner-a", "owner-b"],
        ))

    claimed = [row for result in results for row in result]
    assert len(claimed) == 1
    assert claimed[0]["job_id"] == job_id


@pytest.mark.parametrize("missing_key", ["lease_owner", "lease_token", "lease_generation"])
def test_finalize_requires_every_lease_credential(isolated_memory, missing_key):
    db_store.init_db()
    payload = {
        "filepath": "raw/credentials.md",
        "hash": "credentials-hash",
        "canonical_name": "Source_Credentials.md",
    }
    job_id = db_store.enqueue_job("ingest", payload)
    db_store.mark_job_awaiting_subagent(job_id, "")
    claim = db_store.claim_subagent_jobs(limit=1, lease_seconds=60, lease_owner="test-owner")[0]
    processed = {
        **payload,
        "job_id": job_id,
        "lease_owner": claim["lease_owner"],
        "lease_token": claim["lease_token"],
        "lease_generation": claim["lease_generation"],
    }
    processed.pop(missing_key)

    with pytest.raises(ValueError, match=missing_key):
        db_store.validate_ingest_job_finalization(job_id, processed)
