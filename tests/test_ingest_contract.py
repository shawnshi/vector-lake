import json
import re
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
from pathlib import Path

import pytest

from vector_lake import db_store, governance_store, mcp_server, mutation_coordinator
from vector_lake.ingest_worker import _ingest_finalization_proven, process_jobs
from vector_lake.mutation_coordinator import execute_mutation_plan
from vector_lake.tool_ingest import (
    INGEST_CONTRACT_VERSION,
    _read_canonical_target_content,
    _read_relevant_index_context,
    canonical_source_name,
    claim_ingest_tasks,
    calculate_hash,
    list_ingest_tasks,
    prepare_ingest_batch,
    process_ingest_task_cleanup,
    reconcile_ingest_job_debt,
    reconcile_orphan_ingest_task_packets,
)
from tests.test_mutation_coordinator import _source_content, _write_purpose_contract


def test_candidate_ingest_is_path_scoped_and_nested_names_do_not_collide(
    isolated_memory,
    monkeypatch,
):
    raw_dir = isolated_memory / "raw"
    left = raw_dir / "team-a" / "report.txt"
    right = raw_dir / "team-b" / "report.txt"
    unrelated = raw_dir / "unrelated.txt"
    for path, text in ((left, "left"), (right, "right"), (unrelated, "unrelated")):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    enqueued = []
    monkeypatch.setattr(
        db_store,
        "enqueue_job",
        lambda task_type, payload: enqueued.append((task_type, payload)) or f"job-{len(enqueued)}",
    )

    prepare_ingest_batch(batch_size=2, candidate_paths=[str(left), str(right)])

    assert [item[1]["canonical_name"] for item in enqueued] == [
        "Source_team-a__report.md",
        "Source_team-b__report.md",
    ]
    assert {item[1]["filepath"] for item in enqueued} == {str(left), str(right)}
    assert all(item[1]["filepath"] != str(unrelated) for item in enqueued)


def test_same_content_at_different_paths_is_tracked_independently(isolated_memory, monkeypatch):
    raw_dir = isolated_memory / "raw"
    left = raw_dir / "team-a" / "shared.txt"
    right = raw_dir / "team-b" / "shared.txt"
    for path in (left, right):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("same content", encoding="utf-8")
    enqueued = []
    monkeypatch.setattr(
        db_store,
        "enqueue_job",
        lambda task_type, payload: enqueued.append((task_type, payload)) or f"job-{len(enqueued)}",
    )

    prepare_ingest_batch(batch_size=1, candidate_paths=[str(left)])
    prepare_ingest_batch(batch_size=1, candidate_paths=[str(right)])

    assert [item[1]["canonical_name"] for item in enqueued] == [
        "Source_team-a__shared.md",
        "Source_team-b__shared.md",
    ]


def test_canonical_source_name_reuses_existing_source_identity_for_same_raw_path(
    isolated_memory,
):
    _write_purpose_contract(isolated_memory)
    raw_path = isolated_memory / "raw" / "team-a" / "report.md"
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_text("updated source", encoding="utf-8")
    content = (
        _source_content()
        .replace("id: source_test", "id: source_legacy")
        .replace("title: Test Source", "title: Legacy Source")
        .replace("sources: [raw/test.pdf]", "sources: [raw/team-a/report.md]")
    )
    execute_mutation_plan("Source_Legacy-Report.MD", content=content)

    assert canonical_source_name(str(raw_path)) == "Source_Legacy-Report.MD"


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


def test_dispatch_reclaim_fences_stale_handoff_and_failure_updates(
    isolated_memory,
):
    job_id = db_store.enqueue_job(
        "ingest",
        {"filepath": "raw/fenced-dispatch.md"},
    )
    stale = db_store.claim_pending_jobs(
        limit=1,
        lease_seconds=60,
        lease_owner="dispatch-a",
    )[0]
    assert stale["lease_owner"] == "dispatch-a"
    assert stale["lease_token"]
    assert stale["lease_generation"] == 1

    with db_store.transaction():
        db_store.get_connection().execute(
            "UPDATE jobs SET lease_until = '2000-01-01T00:00:00+00:00' "
            "WHERE job_id = ?",
            (job_id,),
        )
    current = db_store.claim_pending_jobs(
        limit=1,
        lease_seconds=60,
        lease_owner="dispatch-b",
    )[0]
    assert current["lease_generation"] == stale["lease_generation"] + 1
    assert current["lease_token"] != stale["lease_token"]

    current_packet = isolated_memory / "current-dispatch-packet.json"
    stale_packet = isolated_memory / "stale-dispatch-packet.json"
    assert db_store.mark_job_awaiting_subagent(
        job_id,
        str(current_packet),
        lease_owner=current["lease_owner"],
        lease_token=current["lease_token"],
        lease_generation=current["lease_generation"],
    )
    assert not db_store.mark_job_awaiting_subagent(
        job_id,
        str(stale_packet),
        lease_owner=stale["lease_owner"],
        lease_token=stale["lease_token"],
        lease_generation=stale["lease_generation"],
    )
    assert not db_store.update_job_status(
        job_id,
        "failed",
        "late failure",
        lease_owner=stale["lease_owner"],
        lease_token=stale["lease_token"],
        lease_generation=stale["lease_generation"],
    )

    row = db_store.get_connection().execute(
        "SELECT status, retries, task_packet_path, lease_generation "
        "FROM jobs WHERE job_id = ?",
        (job_id,),
    ).fetchone()
    assert dict(row) == {
        "status": "awaiting_subagent",
        "retries": 0,
        "task_packet_path": str(current_packet),
        "lease_generation": current["lease_generation"],
    }
    assert db_store.get_connection().execute(
        "SELECT COUNT(*) FROM ingest_task_cleanup WHERE job_id = ?",
        (job_id,),
    ).fetchone()[0] == 0


def test_dispatch_handoff_rechecks_expiry_after_waiting_for_write_lock(
    isolated_memory,
    monkeypatch,
):
    job_id = db_store.enqueue_job(
        "ingest",
        {"filepath": "raw/lock-wait-dispatch.md"},
    )
    claim = db_store.claim_pending_jobs(
        limit=1,
        lease_seconds=60,
        lease_owner="lock-wait-dispatch",
    )[0]
    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=0.05)).isoformat()
    with db_store.transaction():
        db_store.get_connection().execute(
            "UPDATE jobs SET lease_until = ? WHERE job_id = ?",
            (expires_at, job_id),
        )
    db_path = db_store.get_db_path()
    db_store.close_connection()

    locker = sqlite3.connect(str(db_path), timeout=0.1)
    locker.execute("BEGIN IMMEDIATE")
    real_transaction = db_store.transaction
    waiting = threading.Event()

    @contextmanager
    def observed_transaction(*args, **kwargs):
        waiting.set()
        with real_transaction(*args, **kwargs):
            yield

    monkeypatch.setattr(db_store, "transaction", observed_transaction)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(
                db_store.mark_job_awaiting_subagent,
                job_id,
                str(isolated_memory / "expired-after-lock-wait.json"),
                lease_owner=claim["lease_owner"],
                lease_token=claim["lease_token"],
                lease_generation=claim["lease_generation"],
            )
            assert waiting.wait(timeout=1)
            time.sleep(0.1)
            locker.rollback()
            assert result.result(timeout=2) is False
    finally:
        if locker.in_transaction:
            locker.rollback()
        locker.close()

    row = db_store.get_connection().execute(
        "SELECT status, task_packet_path FROM jobs WHERE job_id = ?",
        (job_id,),
    ).fetchone()
    assert dict(row) == {"status": "dispatched", "task_packet_path": None}

def test_dispatch_lease_renewal_is_fenced(isolated_memory):
    job_id = db_store.enqueue_job(
        "ingest",
        {"filepath": "raw/renew-dispatch.md"},
    )
    claim = db_store.claim_pending_jobs(
        limit=1,
        lease_seconds=1,
        lease_owner="dispatch-renew",
    )[0]
    original_until = claim["lease_until"]

    assert db_store.renew_job_dispatch_lease(
        job_id,
        claim["lease_owner"],
        claim["lease_token"],
        claim["lease_generation"],
        lease_seconds=60,
    )
    renewed = db_store.get_connection().execute(
        "SELECT lease_until, lease_token, lease_generation FROM jobs WHERE job_id = ?",
        (job_id,),
    ).fetchone()
    assert renewed["lease_until"] > original_until
    assert renewed["lease_token"] == claim["lease_token"]
    assert renewed["lease_generation"] == claim["lease_generation"]
    assert not db_store.renew_job_dispatch_lease(
        job_id,
        claim["lease_owner"],
        "stale-token",
        claim["lease_generation"],
        lease_seconds=60,
    )


def test_ingest_worker_does_not_overwrite_reclaimed_dispatch_packet(
    isolated_memory,
    monkeypatch,
):
    monkeypatch.setenv("VECTOR_LAKE_SUBAGENT_RUN_ID", "pytest-dispatch-race")
    payload = {
        "filepath": "raw/dispatch-race.md",
        "hash": "dispatch-race-hash",
        "canonical_name": "Source_Dispatch-Race.md",
        "source_hash": "",
        "ingest_contract_version": INGEST_CONTRACT_VERSION,
        "instructions": "compile this source",
    }
    job_id = db_store.enqueue_job("ingest", payload)

    import vector_lake.ingest_worker as ingest_worker

    real_create = ingest_worker.create_subagent_task
    packets = {}

    def reclaim_during_packet_creation(*args, **kwargs):
        stale_packet = real_create(*args, **kwargs)
        with db_store.transaction():
            db_store.get_connection().execute(
                "UPDATE jobs SET lease_until = '2000-01-01T00:00:00+00:00' "
                "WHERE job_id = ?",
                (job_id,),
            )
        current = db_store.claim_pending_jobs(
            limit=1,
            lease_seconds=60,
            lease_owner="replacement-dispatch",
        )[0]
        current_packet = real_create(
            "ingest",
            "replacement",
            "JSON array",
            {"job_id": job_id},
        )
        assert db_store.mark_job_awaiting_subagent(
            job_id,
            str(current_packet),
            lease_owner=current["lease_owner"],
            lease_token=current["lease_token"],
            lease_generation=current["lease_generation"],
        )
        packets.update(stale=stale_packet, current=current_packet)
        return stale_packet

    monkeypatch.setattr(
        ingest_worker,
        "create_subagent_task",
        reclaim_during_packet_creation,
    )

    process_jobs()

    row = db_store.get_connection().execute(
        "SELECT status, retries, task_packet_path FROM jobs WHERE job_id = ?",
        (job_id,),
    ).fetchone()
    cleanup = db_store.get_connection().execute(
        "SELECT status, task_packet_path FROM ingest_task_cleanup WHERE job_id = ?",
        (job_id,),
    ).fetchone()
    assert dict(row) == {
        "status": "awaiting_subagent",
        "retries": 0,
        "task_packet_path": str(packets["current"]),
    }
    assert dict(cleanup) == {
        "status": "pending",
        "task_packet_path": str(packets["stale"].resolve()),
    }

    replay = process_ingest_task_cleanup(limit=20)

    row = db_store.get_connection().execute(
        "SELECT task_packet_path FROM jobs WHERE job_id = ?",
        (job_id,),
    ).fetchone()
    assert replay["completed"] == 1
    assert row["task_packet_path"] == str(packets["current"])
    assert packets["stale"].exists() is False
    assert packets["current"].exists()
    packets["current"].unlink()

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
        "ingest_contract_version": INGEST_CONTRACT_VERSION,
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
    assert task["metadata"]["processed_data"]["ingest_contract_version"] == INGEST_CONTRACT_VERSION
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
        "source_hash": "stale-but-present",
        "ingest_contract_version": 1,
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
    assert rebuilt["ingest_contract_version"] == INGEST_CONTRACT_VERSION
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


def test_finalize_ingest_rejects_contract_version_not_bound_to_job_payload(isolated_memory, monkeypatch):
    db_store.init_db()
    monkeypatch.setattr("vector_lake.tool_ingest.load_purpose_contract", lambda: {})
    monkeypatch.setattr("vector_lake.tool_ingest.validate_ingest_payload", lambda files, contract: [])
    job_id = db_store.enqueue_job(
        "ingest",
        {
            "filepath": "raw/contract-version.md",
            "hash": "contract-version-hash",
            "canonical_name": "Source_Contract-Version.md",
            "source_hash": "",
            "ingest_contract_version": INGEST_CONTRACT_VERSION,
        },
    )
    db_store.mark_job_awaiting_subagent(job_id, "")
    claim = json.loads(claim_ingest_tasks(limit=1, lease_seconds=60))[0]

    result = mcp_server.tools.finalize_ingest(
        [],
        {
            "filepath": "raw/contract-version.md",
            "hash": "contract-version-hash",
            "canonical_name": "Source_Contract-Version.md",
            "source_hash": "",
            "ingest_contract_version": 1,
            "job_id": job_id,
            "lease_owner": claim["lease_owner"],
            "lease_token": claim["lease_token"],
            "lease_generation": claim["lease_generation"],
        },
    )

    assert result.startswith("Error finalizing ingestion")
    assert "ingest_contract_version does not match" in result


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

    def fail_once_for_target(
        filename,
        mutation_type,
        payload_text=None,
        validation_mode="full",
        projection_base_hash=None,
    ):
        if fail_projection and filename == "Concept_Target.md":
            raise OSError("injected projection failure")
        return real_materialize(
            filename,
            mutation_type,
            payload_text,
            validation_mode,
            projection_base_hash,
        )

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


def test_canonical_target_snapshot_searches_beyond_twenty_newer_outbox_rows(isolated_memory):
    _write_purpose_contract(isolated_memory)
    execute_mutation_plan("Concept_Target.md", content=_concept_content())
    expected_content = (isolated_memory / "wiki" / "Concept_Target.md").read_text(encoding="utf-8")
    expected_version = governance_store.canonical_page_versions({"Concept_Target"})["Concept_Target"]
    for index in range(25):
        db_store.enqueue_mutation(
            "Concept_Target.md",
            "update",
            _concept_content(f"Noise Snapshot {index}"),
            idempotency_key=f"noise-snapshot-{index}",
        )

    recovered = _read_canonical_target_content("Concept_Target.md", expected_version)

    assert recovered == expected_content


def test_integration_recovers_missing_target_projection_from_canonical_outbox(isolated_memory):
    _write_purpose_contract(isolated_memory)
    target_path = isolated_memory / "wiki" / "Concept_Target.md"
    execute_mutation_plan("Concept_Target.md", content=_concept_content())
    target_version = governance_store.canonical_page_versions({"Concept_Target"})["Concept_Target"]
    target_path.unlink()
    payload = {
        "filepath": "raw/missing-projection.md",
        "hash": "missing-projection",
        "canonical_name": "Source_Missing-Projection.md",
        "source_hash": "",
        "ingest_contract_version": INGEST_CONTRACT_VERSION,
    }
    job_id = db_store.enqueue_job("ingest", payload)
    db_store.mark_job_awaiting_subagent(job_id, "")
    claim = json.loads(claim_ingest_tasks(limit=1, lease_seconds=60))[0]

    result = mcp_server.tools.finalize_ingest(
        [{"filename": "Source_Missing-Projection.md", "content": _source_content()}],
        {
            **payload,
            "integration": {
                "disposition": "integrated",
                "relations": [{
                    "target": "Concept_Target.md",
                    "target_hash": target_version,
                    "predicate": "validates",
                    "evidence": "The recovered target is supported by this source.",
                    "confidence": 0.92,
                    "event_date": "2026-07-16",
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
    assert target_path.exists()
    target = target_path.read_text(encoding="utf-8")
    assert "Target compiled truth." in target
    assert "(Source: [[Source_Missing-Projection]])" in target


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


def test_reconcile_ingest_job_debt_recovers_terminal_and_retires_safe_debt(
    isolated_memory,
    monkeypatch,
):
    _write_purpose_contract(isolated_memory)
    monkeypatch.setenv("VECTOR_LAKE_SUBAGENT_RUN_ID", "pytest-reconcile")
    raw_dir = isolated_memory / "raw"
    current = raw_dir / "current.md"
    processed = raw_dir / "processed.md"
    missing = raw_dir / "missing.md"
    current.write_text("current", encoding="utf-8")
    processed.write_text("processed", encoding="utf-8")
    current_hash = calculate_hash(str(current))
    processed_hash = calculate_hash(str(processed))

    terminal_id = db_store.enqueue_job(
        "ingest",
        {
            "filepath": str(current),
            "hash": current_hash,
            "canonical_name": "Source_Current.md",
            "source_hash": "",
            "ingest_contract_version": INGEST_CONTRACT_VERSION,
        },
    )
    missing_id = db_store.enqueue_job(
        "ingest",
        {
            "filepath": str(missing),
            "hash": "missing",
            "canonical_name": "Source_Missing.md",
            "source_hash": "",
            "ingest_contract_version": INGEST_CONTRACT_VERSION,
        },
    )
    processed_id = db_store.enqueue_job(
        "ingest",
        {
            "filepath": str(processed),
            "hash": processed_hash,
            "canonical_name": "Source_Processed.md",
            "source_hash": "",
            "ingest_contract_version": INGEST_CONTRACT_VERSION,
        },
    )
    from vector_lake.native_llm import create_subagent_task

    packets = {}
    for job_id in (terminal_id, missing_id, processed_id):
        packet = create_subagent_task(
            "ingest",
            "test",
            "JSON array",
            {"job_id": job_id},
        )
        packets[job_id] = packet
        db_store.mark_job_awaiting_subagent(job_id, str(packet))
    with db_store.transaction():
        db_store.get_connection().execute(
            "UPDATE jobs SET status = 'failed', retries = 3 WHERE job_id = ?",
            (terminal_id,),
        )
    db_store.mark_file_processed(str(processed), processed_hash)

    preview = json.loads(reconcile_ingest_job_debt(dry_run=True, limit=0))
    assert preview["counts"] == {
        "cancel_missing_raw": 1,
        "complete_already_processed": 1,
        "requeue_current": 1,
    }
    assert db_store.get_connection().execute(
        "SELECT status FROM jobs WHERE job_id = ?",
        (terminal_id,),
    ).fetchone()[0] == "failed"

    result = json.loads(reconcile_ingest_job_debt(dry_run=False, limit=0))

    rows = {
        row["job_id"]: dict(row)
        for row in db_store.get_connection().execute(
            "SELECT job_id, status, retries, task_packet_path, idempotency_key "
            "FROM jobs WHERE job_id IN (?, ?, ?)",
            (terminal_id, missing_id, processed_id),
        )
    }
    assert result["terminal_failed_after"] == 0
    assert Path(result["backup"]).is_dir()
    assert rows[terminal_id]["status"] == "queued"
    assert rows[terminal_id]["retries"] == 0
    assert rows[missing_id]["status"] == "cancelled"
    assert rows[missing_id]["idempotency_key"] is None
    assert rows[processed_id]["status"] == "completed"
    assert all(row["task_packet_path"] is None for row in rows.values())
    assert result["cleanup"]["completed"] == 3
    assert result["cleanup"]["failed"] == 0
    assert all(packet.exists() is False for packet in packets.values())
    cleanup_rows = db_store.get_connection().execute(
        "SELECT status FROM ingest_task_cleanup ORDER BY cleanup_id"
    ).fetchall()
    assert [row["status"] for row in cleanup_rows] == [
        "completed",
        "completed",
        "completed",
    ]


def test_reconcile_ingest_job_debt_deduplicates_current_raw_identity(
    isolated_memory,
):
    raw_path = isolated_memory / "raw" / "drift.md"
    raw_path.write_text("current", encoding="utf-8")
    jobs = []
    for old_hash in ("old-a", "old-b"):
        job_id = db_store.enqueue_job(
            "ingest",
            {
                "filepath": str(raw_path),
                "hash": old_hash,
                "canonical_name": "Source_Drift.md",
                "source_hash": "",
                "ingest_contract_version": INGEST_CONTRACT_VERSION,
            },
        )
        db_store.mark_job_awaiting_subagent(job_id, "")
        jobs.append(job_id)

    preview = json.loads(reconcile_ingest_job_debt(dry_run=True, limit=0))
    assert preview["counts"] == {
        "requeue_current": 1,
        "supersede_duplicate": 1,
    }

    reconcile_ingest_job_debt(dry_run=False, limit=0)

    rows = db_store.get_connection().execute(
        "SELECT job_id, status, idempotency_key, payload FROM jobs "
        "WHERE job_id IN (?, ?) ORDER BY status",
        jobs,
    ).fetchall()
    assert [row["status"] for row in rows] == ["queued", "superseded"]
    queued = next(row for row in rows if row["status"] == "queued")
    assert queued["idempotency_key"]
    assert json.loads(queued["payload"])["hash"] == calculate_hash(str(raw_path))
    superseded = next(row for row in rows if row["status"] == "superseded")
    assert superseded["idempotency_key"] is None


def test_reconcile_preview_does_not_create_missing_database(isolated_memory):
    db_path = db_store.get_db_path()
    assert db_path.exists() is False

    result = json.loads(reconcile_ingest_job_debt(dry_run=True, limit=0))

    assert result["preview_error"].startswith("database_missing:")
    assert db_path.exists() is False


def test_reconcile_preview_does_not_migrate_legacy_schema(isolated_memory):
    db_path = db_store.get_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    connection.execute(
        "CREATE TABLE jobs (job_id TEXT PRIMARY KEY, task_type TEXT, payload TEXT, "
        "status TEXT, retries INTEGER DEFAULT 0, created_at TEXT, updated_at TEXT)"
    )
    connection.execute(
        "CREATE TABLE processed_files (filepath TEXT PRIMARY KEY, file_hash TEXT)"
    )
    connection.commit()
    connection.close()
    before = db_path.read_bytes()

    result = json.loads(reconcile_ingest_job_debt(dry_run=True, limit=0))

    assert result["preview_error"].startswith("schema_not_ready:")
    assert db_path.read_bytes() == before
    connection = sqlite3.connect(db_path)
    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(jobs)").fetchall()
    }
    connection.close()
    assert "lease_generation" not in columns


def test_reconcile_preview_missing_canonical_name_stays_read_only(
    isolated_memory,
    monkeypatch,
):
    raw_path = isolated_memory / "raw" / "preview-no-canonical.md"
    raw_path.write_text("current", encoding="utf-8")
    job_id = db_store.enqueue_job(
        "ingest",
        {
            "filepath": str(raw_path),
            "hash": "stale",
            "source_hash": "",
            "ingest_contract_version": INGEST_CONTRACT_VERSION,
        },
    )
    db_store.mark_job_awaiting_subagent(job_id, "")
    connection = db_store.get_connection()
    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    connection.execute("PRAGMA journal_mode=DELETE")
    db_store.close_all_connections()
    db_path = db_store.get_db_path()
    before = db_path.read_bytes()
    wal_path = Path(f"{db_path}-wal")
    shm_path = Path(f"{db_path}-shm")
    assert wal_path.exists() is False
    assert shm_path.exists() is False

    def fail_if_governance_initializes():
        raise AssertionError("dry-run entered mutable governance initialization")

    monkeypatch.setattr(
        governance_store,
        "initialize_meta_store",
        fail_if_governance_initializes,
    )

    result = json.loads(reconcile_ingest_job_debt(dry_run=True, limit=0))

    assert result["preview_error"] == ""
    assert result["counts"] == {"requeue_current": 1}
    assert db_path.read_bytes() == before
    assert wal_path.exists() is False
    assert shm_path.exists() is False


def test_reconcile_cas_does_not_take_concurrently_claimed_job(
    isolated_memory,
    monkeypatch,
):
    _write_purpose_contract(isolated_memory)
    monkeypatch.setenv("VECTOR_LAKE_SUBAGENT_RUN_ID", "pytest-cas")
    raw_path = isolated_memory / "raw" / "cas.md"
    raw_path.write_text("current", encoding="utf-8")
    job_id = db_store.enqueue_job(
        "ingest",
        {
            "filepath": str(raw_path),
            "hash": "stale",
            "canonical_name": "Source_CAS.md",
            "source_hash": "",
            "ingest_contract_version": INGEST_CONTRACT_VERSION,
        },
    )
    from vector_lake.native_llm import create_subagent_task

    packet = create_subagent_task(
        "ingest",
        "test",
        "JSON array",
        {"job_id": job_id},
    )
    db_store.mark_job_awaiting_subagent(job_id, str(packet))
    backup_dir = isolated_memory / "wiki" / ".meta" / "backups" / "cas"
    backup_dir.mkdir(parents=True)

    def claim_during_backup(_label):
        claimed = db_store.claim_subagent_jobs(
            limit=1,
            lease_seconds=60,
            lease_owner="concurrent-review",
        )
        assert [row["job_id"] for row in claimed] == [job_id]
        return str(backup_dir)

    monkeypatch.setattr(
        "vector_lake.tool_projection.create_maintenance_backup",
        claim_during_backup,
    )

    result = json.loads(reconcile_ingest_job_debt(dry_run=False, limit=0))

    row = db_store.get_connection().execute(
        "SELECT status, task_packet_path, lease_owner FROM jobs WHERE job_id = ?",
        (job_id,),
    ).fetchone()
    assert row["status"] == "subagent_processing"
    assert row["task_packet_path"] == str(packet)
    assert row["lease_owner"] == "concurrent-review"
    assert result["applied_counts"] == {}
    assert result["concurrent_skips"][0]["job_id"] == job_id
    assert db_store.get_connection().execute(
        "SELECT COUNT(*) FROM ingest_task_cleanup"
    ).fetchone()[0] == 0
    assert packet.exists()
    packet.unlink()


def test_ingest_task_cleanup_replays_after_delete_failure(
    isolated_memory,
    monkeypatch,
):
    _write_purpose_contract(isolated_memory)
    monkeypatch.setenv("VECTOR_LAKE_SUBAGENT_RUN_ID", "pytest-cleanup-replay")
    raw_path = isolated_memory / "raw" / "cleanup.md"
    raw_path.write_text("current", encoding="utf-8")
    job_id = db_store.enqueue_job(
        "ingest",
        {
            "filepath": str(raw_path),
            "hash": "stale",
            "canonical_name": "Source_Cleanup.md",
            "source_hash": "",
            "ingest_contract_version": INGEST_CONTRACT_VERSION,
        },
    )
    from vector_lake.native_llm import create_subagent_task

    packet = create_subagent_task(
        "ingest",
        "test",
        "JSON array",
        {"job_id": job_id},
    )
    db_store.mark_job_awaiting_subagent(job_id, str(packet))

    def fail_delete(*_args, **_kwargs):
        raise OSError("injected cleanup failure")

    with monkeypatch.context() as cleanup_patch:
        cleanup_patch.setattr(
            "vector_lake.native_llm.remove_subagent_task",
            fail_delete,
        )
        result = json.loads(reconcile_ingest_job_debt(dry_run=False, limit=0))
    row = db_store.get_connection().execute(
        "SELECT status, task_packet_path FROM jobs WHERE job_id = ?",
        (job_id,),
    ).fetchone()
    cleanup = db_store.get_connection().execute(
        "SELECT status FROM ingest_task_cleanup WHERE job_id = ?",
        (job_id,),
    ).fetchone()
    assert result["cleanup"]["failed"] == 1
    assert row["status"] == "queued"
    assert row["task_packet_path"] == str(packet)
    assert cleanup["status"] == "failed"

    with db_store.transaction():
        db_store.get_connection().execute(
            "UPDATE ingest_task_cleanup SET available_at = "
            "'2000-01-01T00:00:00+00:00' WHERE job_id = ?",
            (job_id,),
        )
    replay = process_ingest_task_cleanup(limit=20)
    row = db_store.get_connection().execute(
        "SELECT task_packet_path FROM jobs WHERE job_id = ?",
        (job_id,),
    ).fetchone()
    cleanup = db_store.get_connection().execute(
        "SELECT status FROM ingest_task_cleanup WHERE job_id = ?",
        (job_id,),
    ).fetchone()
    assert replay["completed"] == 1
    assert row["task_packet_path"] is None
    assert cleanup["status"] == "completed"
    assert packet.exists() is False


def test_replacing_ingest_packet_persists_cleanup_without_clearing_new_pointer(
    isolated_memory,
    monkeypatch,
):
    monkeypatch.setenv("VECTOR_LAKE_SUBAGENT_RUN_ID", "pytest-packet-replace")
    job_id = db_store.enqueue_job(
        "ingest",
        {"filepath": "raw/replace.md", "hash": "hash"},
    )
    from vector_lake.native_llm import create_subagent_task

    old_packet = create_subagent_task(
        "ingest",
        "old",
        "JSON array",
        {"job_id": job_id},
    )
    new_packet = create_subagent_task(
        "ingest",
        "new",
        "JSON array",
        {"job_id": job_id},
    )
    db_store.mark_job_awaiting_subagent(job_id, str(old_packet))
    db_store.mark_job_awaiting_subagent(job_id, str(new_packet))

    row = db_store.get_connection().execute(
        "SELECT task_packet_path FROM jobs WHERE job_id = ?",
        (job_id,),
    ).fetchone()
    cleanup = db_store.get_connection().execute(
        "SELECT status, task_packet_path FROM ingest_task_cleanup WHERE job_id = ?",
        (job_id,),
    ).fetchone()
    assert row["task_packet_path"] == str(new_packet)
    assert dict(cleanup) == {
        "status": "pending",
        "task_packet_path": str(old_packet.resolve()),
    }

    replay = process_ingest_task_cleanup(limit=20)

    row = db_store.get_connection().execute(
        "SELECT task_packet_path FROM jobs WHERE job_id = ?",
        (job_id,),
    ).fetchone()
    assert replay["completed"] == 1
    assert row["task_packet_path"] == str(new_packet)
    assert old_packet.exists() is False
    assert new_packet.exists()
    new_packet.unlink()


def test_expiring_ingest_packet_persists_cleanup_before_retry(
    isolated_memory,
    monkeypatch,
):
    monkeypatch.setenv("VECTOR_LAKE_SUBAGENT_RUN_ID", "pytest-packet-expire")
    job_id = db_store.enqueue_job(
        "ingest",
        {"filepath": "raw/expire.md", "hash": "hash"},
    )
    from vector_lake.native_llm import create_subagent_task

    packet = create_subagent_task(
        "ingest",
        "expire",
        "JSON array",
        {"job_id": job_id},
    )
    db_store.mark_job_awaiting_subagent(job_id, str(packet))
    with db_store.transaction():
        db_store.get_connection().execute(
            "UPDATE jobs SET updated_at = '2000-01-01T00:00:00+00:00' "
            "WHERE job_id = ?",
            (job_id,),
        )

    assert db_store.expire_stale_subagent_jobs(max_age_seconds=1) == 1
    row = db_store.get_connection().execute(
        "SELECT status, retries, task_packet_path FROM jobs WHERE job_id = ?",
        (job_id,),
    ).fetchone()
    cleanup = db_store.get_connection().execute(
        "SELECT status FROM ingest_task_cleanup WHERE job_id = ?",
        (job_id,),
    ).fetchone()
    assert dict(row) == {
        "status": "failed",
        "retries": 1,
        "task_packet_path": str(packet),
    }
    assert cleanup["status"] == "pending"

    replay = process_ingest_task_cleanup(limit=20)

    row = db_store.get_connection().execute(
        "SELECT status, task_packet_path FROM jobs WHERE job_id = ?",
        (job_id,),
    ).fetchone()
    assert replay["completed"] == 1
    assert dict(row) == {"status": "failed", "task_packet_path": None}
    assert packet.exists() is False


def test_expiry_rolls_back_when_packet_cleanup_cannot_be_persisted(
    isolated_memory,
    monkeypatch,
):
    monkeypatch.setenv("VECTOR_LAKE_SUBAGENT_RUN_ID", "pytest-expire-rollback")
    job_id = db_store.enqueue_job(
        "ingest",
        {"filepath": "raw/expire-rollback.md", "hash": "hash"},
    )
    from vector_lake.native_llm import create_subagent_task

    packet = create_subagent_task(
        "ingest",
        "expire rollback",
        "JSON array",
        {"job_id": job_id},
    )
    db_store.mark_job_awaiting_subagent(job_id, str(packet))
    with db_store.transaction():
        db_store.get_connection().execute(
            "UPDATE jobs SET updated_at = '2000-01-01T00:00:00+00:00' "
            "WHERE job_id = ?",
            (job_id,),
        )

    def fail_cleanup(*_args, **_kwargs):
        raise sqlite3.OperationalError("injected cleanup persistence failure")

    monkeypatch.setattr(db_store, "enqueue_ingest_task_cleanup", fail_cleanup)
    with pytest.raises(sqlite3.OperationalError, match="injected cleanup"):
        db_store.expire_stale_subagent_jobs(max_age_seconds=1)

    row = db_store.get_connection().execute(
        "SELECT status, retries, task_packet_path FROM jobs WHERE job_id = ?",
        (job_id,),
    ).fetchone()
    assert dict(row) == {
        "status": "awaiting_subagent",
        "retries": 0,
        "task_packet_path": str(packet),
    }
    assert packet.exists()
    packet.unlink()


def test_orphan_ingest_packet_cleanup_is_preview_first_and_pointer_safe(
    isolated_memory,
    monkeypatch,
):
    monkeypatch.setenv("VECTOR_LAKE_SUBAGENT_RUN_ID", "pytest-orphan-cleanup")
    job_id = db_store.enqueue_job(
        "ingest",
        {"filepath": "raw/orphan.md", "hash": "hash"},
    )
    from vector_lake.native_llm import create_subagent_task

    orphan = create_subagent_task(
        "ingest",
        "orphan",
        "JSON array",
        {"job_id": job_id},
    )
    other_orphan = create_subagent_task(
        "ingest",
        "other orphan",
        "JSON array",
        {"job_id": job_id},
    )
    current = create_subagent_task(
        "ingest",
        "current",
        "JSON array",
        {"job_id": job_id},
    )
    db_store.mark_job_awaiting_subagent(job_id, str(current))

    preview = json.loads(
        reconcile_orphan_ingest_task_packets(
            dry_run=True,
            min_age_seconds=0,
        )
    )

    assert preview["candidate_count"] == 2
    assert preview["selected_count"] == 2
    assert preview["removed"] == 0
    assert {sample["path"] for sample in preview["samples"]} == {
        str(orphan.resolve()),
        str(other_orphan.resolve()),
    }
    assert orphan.exists()
    assert other_orphan.exists()
    assert current.exists()

    real_transaction = db_store.transaction
    transaction_calls = []

    @contextmanager
    def observed_transaction(*args, **kwargs):
        transaction_calls.append(1)
        with real_transaction(*args, **kwargs):
            yield

    monkeypatch.setattr(db_store, "transaction", observed_transaction)
    applied = json.loads(
        reconcile_orphan_ingest_task_packets(
            dry_run=False,
            min_age_seconds=0,
        )
    )

    row = db_store.get_connection().execute(
        "SELECT task_packet_path FROM jobs WHERE job_id = ?",
        (job_id,),
    ).fetchone()
    assert applied["candidate_count"] == 2
    assert applied["removed"] == 2
    assert len(transaction_calls) == 2
    assert row["task_packet_path"] == str(current)
    assert orphan.exists() is False
    assert other_orphan.exists() is False
    assert current.exists()
    current.unlink()


def test_orphan_ingest_packet_apply_does_not_create_missing_database(
    isolated_memory,
):
    db_path = db_store.get_db_path()
    assert db_path.exists() is False

    result = json.loads(
        reconcile_orphan_ingest_task_packets(dry_run=False, min_age_seconds=0)
    )

    assert result["preview_error"].startswith("database_missing:")
    assert db_path.exists() is False


def test_orphan_ingest_packet_apply_rejects_unmigrated_database(
    isolated_memory,
):
    db_store.init_db()
    conn = db_store.get_connection()
    with db_store.transaction():
        conn.execute("DROP TABLE ingest_task_cleanup")

    result = json.loads(
        reconcile_orphan_ingest_task_packets(dry_run=False, min_age_seconds=0)
    )

    assert result["preview_error"] == (
        "schema_not_ready:missing_table:ingest_task_cleanup"
    )
    assert (
        conn.execute(
            "SELECT 1 FROM sqlite_schema WHERE name = 'ingest_task_cleanup'"
        ).fetchone()
        is None
    )


def test_remove_subagent_task_rejects_cross_session_identity(
    monkeypatch,
):
    monkeypatch.setenv("VECTOR_LAKE_SUBAGENT_RUN_ID", "pytest-session-b")
    from vector_lake.native_llm import create_subagent_task, remove_subagent_task

    packet = create_subagent_task(
        "ingest",
        "test",
        "JSON array",
        {"job_id": "job-b"},
    )

    with pytest.raises(ValueError, match="job id does not match"):
        remove_subagent_task(
            packet,
            expected_job_id="job-a",
            expected_task_type="ingest",
            expected_task_id=packet.stem,
        )

    assert packet.exists()
    packet.unlink()


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
