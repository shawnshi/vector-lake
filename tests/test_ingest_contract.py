import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from vector_lake import db_store, mcp_server
from vector_lake.ingest_worker import _ingest_finalization_proven, process_jobs
from vector_lake.tool_ingest import claim_ingest_tasks, list_ingest_tasks


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
