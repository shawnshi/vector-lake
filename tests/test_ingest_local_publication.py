import json
from pathlib import Path

import pytest

from tests.test_mutation_coordinator import _write_purpose_contract
from vector_lake import db_store, mutation_coordinator, tool_ingest


def _payload(isolated_memory, *, canonical_name="Source_Local-First.md"):
    raw_path = isolated_memory / "raw" / "local-first.md"
    raw_path.write_text("local deterministic publication", encoding="utf-8")
    revision = tool_ingest.calculate_hash(str(raw_path))
    return {
        "filepath": str(raw_path.resolve()),
        "hash": revision,
        "canonical_name": canonical_name,
        "source_hash": "",
        "source_projection_hash": "",
        "source_observed_at": "2026-08-31T12:00:00+00:00",
        "attempt_id": "2" * 32,
        "integration_candidates": [],
        "ingest_contract_version": tool_ingest.INGEST_CONTRACT_VERSION,
        "instructions": "remote enrichment instructions",
    }


@pytest.mark.parametrize(
    ("field", "value", "remove"),
    [
        ("source_observed_at", None, True),
        ("source_observed_at", "", False),
        ("source_observed_at", "not-a-timestamp", False),
        ("attempt_id", None, True),
        ("attempt_id", "", False),
        ("attempt_id", "A" * 32, False),
    ],
)
def test_v6_claim_admission_rejects_missing_or_invalid_correlation_fields(
    isolated_memory,
    field,
    value,
    remove,
):
    db_store.init_db()
    payload = _payload(isolated_memory)
    if remove:
        payload.pop(field)
    else:
        payload[field] = value
    job_id = db_store.enqueue_job("ingest", payload)
    db_store.mark_job_awaiting_subagent(job_id, "")

    assert (
        db_store.claim_subagent_jobs(
            limit=1,
            lease_owner="generator",
            required_ingest_contract_version=tool_ingest.INGEST_CONTRACT_VERSION,
        )
        == []
    )
    assert (
        db_store.get_connection()
        .execute(
            "SELECT status FROM jobs WHERE job_id = ?",
            (job_id,),
        )
        .fetchone()["status"]
        == "awaiting_subagent"
    )


def test_auto_source_page_is_byte_deterministic(isolated_memory):
    _write_purpose_contract(isolated_memory)
    payload = {
        "filepath": "C:/raw/local-first.md",
        "hash": "sha256:" + "a" * 64,
        "canonical_name": "Source_Local-First.md",
        "source_observed_at": "2026-08-31T12:00:00+00:00",
    }

    first = tool_ingest._auto_source_page(payload)
    second = tool_ingest._auto_source_page(payload)

    assert first == second
    assert "2026-08-31" in first["content"]
    assert 'evidence_tier: "primary"' in first["content"]
    assert "ingest-revision:sha256:" in first["content"]


def test_local_source_publication_is_atomic_and_queues_enrichment(isolated_memory):
    _write_purpose_contract(isolated_memory)
    db_store.init_db()
    payload = _payload(isolated_memory)
    identity_key = db_store._job_idempotency_key("ingest", payload)
    assert identity_key

    queued_payload, job_id = tool_ingest._publish_local_source_and_enqueue(
        payload,
        idempotency_key=identity_key,
        prepare_started_at="2026-08-31T11:59:59+00:00",
        prepare_duration_ms=17,
    )

    conn = db_store.get_connection()
    job = conn.execute(
        "SELECT status, payload FROM jobs WHERE job_id = ?",
        (job_id,),
    ).fetchone()
    assert job["status"] == "queued"
    persisted_payload = json.loads(job["payload"])
    assert persisted_payload == queued_payload
    assert queued_payload["source_hash"]
    assert len(queued_payload["source_projection_hash"]) == 64
    assert queued_payload["local_publication"]["contract"] == (
        "deterministic-source/v1"
    )
    assert (isolated_memory / "wiki" / payload["canonical_name"]).is_file()
    outbox_row = conn.execute(
        "SELECT validation_mode FROM mutation_outbox WHERE filename = ?",
        (payload["canonical_name"],),
    ).fetchone()
    assert outbox_row["validation_mode"] == "schema"
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM processed_files WHERE filepath = ?",
            (payload["filepath"],),
        ).fetchone()[0]
        == 0
    )
    events = conn.execute(
        "SELECT stage, transition, duration_ms FROM ingest_stage_events "
        "WHERE job_id = ? ORDER BY event_id",
        (job_id,),
    ).fetchall()
    assert [(row["stage"], row["transition"]) for row in events] == [
        ("prepare", "completed"),
        ("local_publication", "completed"),
        ("canonical_commit", "completed"),
        ("outbox", "completed"),
        ("enqueue", "completed"),
        ("markdown", "completed"),
    ]
    assert events[0]["duration_ms"] == 17

    with db_store.transaction():
        conn.execute(
            "UPDATE jobs SET status = 'awaiting_subagent' WHERE job_id = ?",
            (job_id,),
        )
    assert db_store.claim_subagent_jobs(limit=1, lease_owner="generator") == []

    claimed = db_store.claim_mutation_outbox(limit=1, lease_owner="indexer")
    assert len(claimed) == 1
    outbox = claimed[0]
    assert db_store.complete_mutation_outbox(
        int(outbox["id"]),
        str(outbox["lease_owner"]),
        str(outbox["lease_token"]),
        int(outbox["lease_generation"]),
    )
    visible = conn.execute(
        "SELECT transition, metadata_json FROM ingest_stage_events "
        "WHERE job_id = ? AND stage = 'index_visible'",
        (job_id,),
    ).fetchone()
    assert visible["transition"] == "completed"
    assert json.loads(visible["metadata_json"])["outbox_id"] == int(outbox["id"])
    enrichment_claims = db_store.claim_subagent_jobs(
        limit=1,
        lease_owner="generator",
    )
    assert [row["job_id"] for row in enrichment_claims] == [job_id]


def test_changed_raw_revision_updates_existing_source_seed(isolated_memory):
    _write_purpose_contract(isolated_memory)
    db_store.init_db()
    first = _payload(
        isolated_memory,
        canonical_name="Source_Local-Revision.md",
    )
    first_payload, first_job = tool_ingest._publish_local_source_and_enqueue(
        first,
        idempotency_key=db_store._job_idempotency_key("ingest", first),
        prepare_started_at="2026-08-31T11:59:59+00:00",
        prepare_duration_ms=1,
    )
    raw_path = Path(first["filepath"])
    raw_path.write_text("changed raw revision", encoding="utf-8")
    second = dict(first_payload)
    second.update(
        {
            "hash": tool_ingest.calculate_hash(str(raw_path)),
            "source_observed_at": "2026-08-31T12:01:00+00:00",
            "attempt_id": "3" * 32,
        }
    )

    queued_job = db_store.enqueue_job("ingest", second)
    second_payload, second_job = tool_ingest._publish_local_source_and_enqueue(
        second,
        idempotency_key=db_store._job_idempotency_key("ingest", second),
        prepare_started_at="2026-08-31T12:00:59+00:00",
        prepare_duration_ms=1,
    )

    assert second_job == queued_job
    assert second_job != first_job
    assert second_payload["source_hash"] != first_payload["source_hash"]
    stored_payload = json.loads(
        db_store.get_connection()
        .execute("SELECT payload FROM jobs WHERE job_id = ?", (second_job,))
        .fetchone()["payload"]
    )
    assert stored_payload["source_hash"] == second_payload["source_hash"]
    assert (
        stored_payload["source_projection_hash"]
        == second_payload["source_projection_hash"]
    )
    source_text = (isolated_memory / "wiki" / second["canonical_name"]).read_text(
        encoding="utf-8"
    )
    assert f"<!-- ingest-revision:{second['hash']} -->" in source_text
    publication = (
        db_store.get_connection()
        .execute(
            "SELECT metadata_json FROM ingest_stage_events "
            "WHERE job_id = ? AND stage = 'local_publication'",
            (second_job,),
        )
        .fetchone()
    )
    assert json.loads(publication["metadata_json"])["mode"] == "updated_source_seed"


def test_reused_source_rejects_terminal_quarantine_owner(isolated_memory):
    _write_purpose_contract(isolated_memory)
    db_store.init_db()
    payload = _payload(
        isolated_memory,
        canonical_name="Source_Local-Quarantine.md",
    )
    identity_key = db_store._job_idempotency_key("ingest", payload)
    queued_payload, job_id = tool_ingest._publish_local_source_and_enqueue(
        payload,
        idempotency_key=identity_key,
        prepare_started_at="2026-08-31T11:59:59+00:00",
        prepare_duration_ms=1,
    )
    conn = db_store.get_connection()
    with db_store.transaction():
        conn.execute(
            "UPDATE jobs SET status = 'failed', retries = 3, result_json = ? "
            "WHERE job_id = ?",
            (
                json.dumps(
                    {
                        "maintenance": "auto_ingest_controller",
                        "state": "quarantined",
                    }
                ),
                job_id,
            ),
        )
    event_count = conn.execute(
        "SELECT COUNT(*) FROM ingest_stage_events WHERE job_id = ?",
        (job_id,),
    ).fetchone()[0]

    with pytest.raises(RuntimeError, match="retained by a terminal job"):
        tool_ingest._publish_local_source_and_enqueue(
            queued_payload,
            idempotency_key=identity_key,
            prepare_started_at="2026-08-31T12:01:00+00:00",
            prepare_duration_ms=1,
        )

    assert (
        conn.execute(
            "SELECT COUNT(*) FROM ingest_stage_events WHERE job_id = ?",
            (job_id,),
        ).fetchone()[0]
        == event_count
    )


def test_reused_source_inherits_pending_projection_barrier(isolated_memory):
    _write_purpose_contract(isolated_memory)
    db_store.init_db()
    first_payload = _payload(
        isolated_memory,
        canonical_name="Source_Local-Reused.md",
    )
    first_key = db_store._job_idempotency_key("ingest", first_payload)
    assert first_key
    first_queued, _first_job = tool_ingest._publish_local_source_and_enqueue(
        first_payload,
        idempotency_key=first_key,
        prepare_started_at="2026-08-31T11:59:59+00:00",
        prepare_duration_ms=1,
    )
    raw_path = isolated_memory / "raw" / "local-first.md"
    raw_path.write_text("second raw revision", encoding="utf-8")
    second_payload = dict(first_payload)
    second_payload.update(
        {
            "hash": tool_ingest.calculate_hash(str(raw_path)),
            "source_hash": first_queued["source_hash"],
            "source_projection_hash": first_queued["source_projection_hash"],
            "attempt_id": "6" * 32,
        }
    )
    second_key = db_store._job_idempotency_key("ingest", second_payload)
    assert second_key and second_key != first_key
    _second_queued, second_job = tool_ingest._publish_local_source_and_enqueue(
        second_payload,
        idempotency_key=second_key,
        prepare_started_at="2026-08-31T12:00:01+00:00",
        prepare_duration_ms=1,
    )
    conn = db_store.get_connection()
    link = conn.execute(
        "SELECT outbox_id FROM ingest_outbox_links WHERE job_id = ?",
        (second_job,),
    ).fetchone()
    assert link is not None
    with db_store.transaction():
        conn.execute(
            "UPDATE jobs SET status = 'awaiting_subagent' WHERE job_id = ?",
            (second_job,),
        )
    assert db_store.claim_subagent_jobs(limit=1, lease_owner="generator") == []

    outbox = db_store.claim_mutation_outbox(limit=1, lease_owner="indexer")[0]
    assert int(outbox["id"]) == int(link["outbox_id"])
    assert db_store.complete_mutation_outbox(
        int(outbox["id"]),
        str(outbox["lease_owner"]),
        str(outbox["lease_token"]),
        int(outbox["lease_generation"]),
    )
    claimed = db_store.claim_subagent_jobs(limit=1, lease_owner="generator")
    assert [row["job_id"] for row in claimed] == [second_job]


def test_concurrent_duplicate_local_publication_has_one_durable_owner(
    isolated_memory,
):
    from concurrent.futures import ThreadPoolExecutor
    import threading

    _write_purpose_contract(isolated_memory)
    db_store.init_db()
    payload = _payload(
        isolated_memory,
        canonical_name="Source_Local-Concurrent.md",
    )
    identity_key = db_store._job_idempotency_key("ingest", payload)
    assert identity_key
    barrier = threading.Barrier(2)

    def publish():
        barrier.wait(timeout=5)
        return tool_ingest._publish_local_source_and_enqueue(
            dict(payload),
            idempotency_key=identity_key,
            prepare_started_at="2026-08-31T11:59:59+00:00",
            prepare_duration_ms=1,
        )

    results = []
    errors = []
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(publish) for _ in range(2)]
        for future in futures:
            try:
                results.append(future.result(timeout=15))
            except Exception as exc:
                errors.append(exc)

    assert results
    assert len(results) + len(errors) == 2
    conn = db_store.get_connection()
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE task_type = 'ingest'",
        ).fetchone()[0]
        == 1
    )
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM mutation_outbox WHERE filename = ?",
            (payload["canonical_name"],),
        ).fetchone()[0]
        == 1
    )
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM entities WHERE json_extract(data_json, '$.page_key') = ?",
            ("Source_Local-Concurrent",),
        ).fetchone()[0]
        == 1
    )


def test_local_publication_rolls_back_when_enrichment_enqueue_fails(
    isolated_memory,
    monkeypatch,
):
    _write_purpose_contract(isolated_memory)
    db_store.init_db()
    payload = _payload(
        isolated_memory,
        canonical_name="Source_Local-Rollback.md",
    )
    identity_key = db_store._job_idempotency_key("ingest", payload)
    assert identity_key

    def fail_enqueue(*_args, **_kwargs):
        raise RuntimeError("injected enqueue failure")

    monkeypatch.setattr(db_store, "enqueue_job", fail_enqueue)

    with pytest.raises(RuntimeError, match="injected enqueue failure"):
        tool_ingest._publish_local_source_and_enqueue(
            payload,
            idempotency_key=identity_key,
            prepare_started_at="2026-08-31T11:59:59+00:00",
            prepare_duration_ms=1,
        )

    conn = db_store.get_connection()
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM mutation_outbox").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM ingest_stage_events").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM ingest_outbox_links").fetchone()[0] == 0
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM entities WHERE json_extract(data_json, '$.page_key') = ?",
            ("Source_Local-Rollback",),
        ).fetchone()[0]
        == 0
    )
    assert not (isolated_memory / "wiki" / payload["canonical_name"]).exists()


def test_complete_stage_trace_uses_one_attempt_id_without_raw_content(
    isolated_memory,
    monkeypatch,
):
    from pathlib import Path
    import threading

    from tests.test_auto_ingest_worker import _write_config
    from vector_lake import auto_ingest_worker, runtime_health

    _write_config(isolated_memory, auto_finalize_rejected=True)
    _write_purpose_contract(isolated_memory)
    db_store.init_db()
    sentinel = "SENTINEL_RAW_CONTENT_MUST_NOT_ENTER_TELEMETRY"
    raw_path = isolated_memory / "raw" / "trace.md"
    raw_path.write_text(sentinel, encoding="utf-8")
    revision = tool_ingest.calculate_hash(str(raw_path))
    payload = {
        "filepath": str(raw_path.resolve()),
        "hash": revision,
        "canonical_name": "Source_Local-Trace.md",
        "source_hash": "",
        "source_projection_hash": "",
        "source_observed_at": "2026-08-31T12:00:00+00:00",
        "attempt_id": "5" * 32,
        "integration_candidates": [],
        "ingest_contract_version": tool_ingest.INGEST_CONTRACT_VERSION,
        "instructions": "remote enrichment instructions",
    }
    identity_key = db_store._job_idempotency_key("ingest", payload)
    assert identity_key
    _queued_payload, job_id = tool_ingest._publish_local_source_and_enqueue(
        payload,
        idempotency_key=identity_key,
        prepare_started_at="2026-08-31T11:59:59+00:00",
        prepare_duration_ms=2,
    )
    source_outbox = db_store.claim_mutation_outbox(limit=1, lease_owner="indexer")[0]
    assert db_store.complete_mutation_outbox(
        int(source_outbox["id"]),
        str(source_outbox["lease_owner"]),
        str(source_outbox["lease_token"]),
        int(source_outbox["lease_generation"]),
    )
    conn = db_store.get_connection()
    with db_store.transaction():
        conn.execute(
            "UPDATE jobs SET status = 'awaiting_subagent' WHERE job_id = ?",
            (job_id,),
        )

    generated = {
        "schema_version": 1,
        "job_id": job_id,
        "purpose_scope": "excluded",
        "purpose_evidence": "The source is outside the active strategic scope.",
        "decision_confidence": 0.99,
        "files": [],
        "integration": {
            "disposition": "rejected",
            "reason": "No source-supported healthcare IT relation is present.",
            "relations": [],
        },
    }
    monkeypatch.setattr(auto_ingest_worker, "_claimable_job_exists", lambda: True)
    monkeypatch.setattr(
        auto_ingest_worker,
        "_probe_codex_runner",
        lambda _config: Path("C:/codex.exe"),
    )
    monkeypatch.setattr(
        auto_ingest_worker,
        "_run_codex_generator",
        lambda *_args: generated,
    )
    monkeypatch.setattr(
        runtime_health,
        "enforce_runtime_write_health",
        lambda validation_mode="full": {"status": "ok", "mode": validation_mode},
    )

    assert auto_ingest_worker.AutoIngestController().tick(threading.Event()) == (
        "finalized"
    )
    events = conn.execute(
        "SELECT stage, attempt_id, error_code, error_fingerprint, metadata_json "
        "FROM ingest_stage_events WHERE job_id = ? ORDER BY event_id",
        (job_id,),
    ).fetchall()
    stages = {row["stage"] for row in events}
    assert {
        "prepare",
        "enqueue",
        "claim",
        "raw_verify",
        "local_publication",
        "model",
        "validation",
        "finalization",
        "canonical_commit",
        "markdown",
        "outbox",
        "index_visible",
    }.issubset(stages)
    assert {row["attempt_id"] for row in events} == {payload["attempt_id"]}
    serialized_events = json.dumps(
        [dict(row) for row in events],
        ensure_ascii=False,
        sort_keys=True,
    )
    assert sentinel not in serialized_events


def test_superseded_source_projection_quarantines_then_reconciles_job(
    isolated_memory,
):
    _write_purpose_contract(isolated_memory)
    db_store.init_db()
    payload = _payload(
        isolated_memory,
        canonical_name="Source_Local-Superseded.md",
    )
    identity_key = db_store._job_idempotency_key("ingest", payload)
    assert identity_key
    queued_payload, job_id = tool_ingest._publish_local_source_and_enqueue(
        payload,
        idempotency_key=identity_key,
        prepare_started_at="2026-08-31T11:59:59+00:00",
        prepare_duration_ms=1,
    )
    source_path = isolated_memory / "wiki" / payload["canonical_name"]
    newer_content = (
        source_path.read_text(encoding="utf-8") + "\n<!-- newer intent -->\n"
    )
    mutation_coordinator.execute_mutation_batch(
        [
            {
                "filename": payload["canonical_name"],
                "content": newer_content,
                "expected_version": queued_payload["source_hash"],
                "expected_projection_hash": queued_payload["source_projection_hash"],
            }
        ],
        origin="test_newer_source_intent",
    )
    conn = db_store.get_connection()
    with db_store.transaction():
        conn.execute(
            "UPDATE jobs SET status = 'awaiting_subagent' WHERE job_id = ?",
            (job_id,),
        )

    assert db_store.claim_subagent_jobs(limit=1, lease_owner="generator") == []
    quarantined = conn.execute(
        "SELECT status, retries, result_json FROM jobs WHERE job_id = ?",
        (job_id,),
    ).fetchone()
    assert quarantined["status"] == "failed"
    assert quarantined["retries"] >= 3
    assert json.loads(quarantined["result_json"])["failure_class"] == (
        "local_source_projection_conflict"
    )

    reconciled = json.loads(
        tool_ingest.reconcile_ingest_job_debt(dry_run=False, limit=10)
    )
    assert reconciled["applied_counts"]["requeue_current"] == 1
    recovered = conn.execute(
        "SELECT status, retries FROM jobs WHERE job_id = ?",
        (job_id,),
    ).fetchone()
    assert recovered["status"] == "queued"
    assert recovered["retries"] == 0
    active_link = conn.execute(
        "SELECT link.outbox_id, outbox.status FROM ingest_outbox_links AS link "
        "JOIN mutation_outbox AS outbox ON outbox.id = link.outbox_id "
        "WHERE link.job_id = ?",
        (job_id,),
    ).fetchone()
    assert active_link["status"] == "pending"
    with db_store.transaction():
        conn.execute(
            "UPDATE jobs SET status = 'awaiting_subagent' WHERE job_id = ?",
            (job_id,),
        )
    assert db_store.claim_subagent_jobs(limit=1, lease_owner="generator") == []

    newer_outbox = db_store.claim_mutation_outbox(limit=10, lease_owner="indexer")
    assert len(newer_outbox) == 1
    row = newer_outbox[0]
    assert int(row["id"]) == int(active_link["outbox_id"])
    assert db_store.complete_mutation_outbox(
        int(row["id"]),
        str(row["lease_owner"]),
        str(row["lease_token"]),
        int(row["lease_generation"]),
    )
    claims = db_store.claim_subagent_jobs(limit=1, lease_owner="generator")
    assert [claim["job_id"] for claim in claims] == [job_id]


def test_terminal_source_projection_failure_is_explicitly_quarantined(
    isolated_memory,
):
    _write_purpose_contract(isolated_memory)
    db_store.init_db()
    payload = _payload(
        isolated_memory,
        canonical_name="Source_Local-Projection-Failed.md",
    )
    identity_key = db_store._job_idempotency_key("ingest", payload)
    assert identity_key
    _queued_payload, job_id = tool_ingest._publish_local_source_and_enqueue(
        payload,
        idempotency_key=identity_key,
        prepare_started_at="2026-08-31T11:59:59+00:00",
        prepare_duration_ms=1,
    )
    claimed = db_store.claim_mutation_outbox(limit=1, lease_owner="indexer")
    assert len(claimed) == 1
    outbox = claimed[0]
    assert (
        db_store.fail_mutation_outbox(
            int(outbox["id"]),
            "terminal projection failure",
            str(outbox["lease_owner"]),
            str(outbox["lease_token"]),
            int(outbox["lease_generation"]),
            max_attempts=1,
        )
        == "failed"
    )
    conn = db_store.get_connection()
    with db_store.transaction():
        conn.execute(
            "UPDATE jobs SET status = 'awaiting_subagent' WHERE job_id = ?",
            (job_id,),
        )

    assert db_store.claim_subagent_jobs(limit=1, lease_owner="generator") == []
    job = conn.execute(
        "SELECT status, retries, result_json FROM jobs WHERE job_id = ?",
        (job_id,),
    ).fetchone()
    assert job["status"] == "failed"
    assert job["retries"] >= 3
    result = json.loads(job["result_json"])
    assert result["failure_class"] == "local_source_projection_conflict"
    assert result["outbox_statuses"] == ["failed"]


def test_retry_and_quarantine_events_follow_durable_job_state(isolated_memory):
    db_store.init_db()
    payload = _payload(isolated_memory)
    job_id = db_store.enqueue_job("ingest", payload)
    conn = db_store.get_connection()

    def install_lease(generation):
        with db_store.transaction():
            conn.execute(
                "UPDATE jobs SET status = 'subagent_processing', retries = ?, "
                "lease_owner = 'owner', lease_token = 'token', "
                "lease_generation = ?, lease_until = '2999-01-01T00:00:00+00:00' "
                "WHERE job_id = ?",
                (generation - 1, generation, job_id),
            )

    install_lease(1)
    assert db_store.fail_auto_ingest_subagent_task_claim(
        job_id,
        "owner",
        "token",
        1,
        "retryable failure",
        retryable=True,
        failure_class="generator_infrastructure",
    )
    install_lease(3)
    assert db_store.fail_auto_ingest_subagent_task_claim(
        job_id,
        "owner",
        "token",
        3,
        "terminal failure",
        retryable=True,
        failure_class="generator_infrastructure",
    )

    job = conn.execute(
        "SELECT status, retries, result_json FROM jobs WHERE job_id = ?",
        (job_id,),
    ).fetchone()
    assert job["status"] == "failed"
    assert job["retries"] == 3
    assert json.loads(job["result_json"])["state"] == "quarantined"
    transitions = conn.execute(
        "SELECT transition, attempt_id FROM ingest_stage_events "
        "WHERE job_id = ? AND stage = 'retry' ORDER BY event_id",
        (job_id,),
    ).fetchall()
    assert [row["transition"] for row in transitions] == [
        "retry_scheduled",
        "quarantined",
    ]
    attempt_ids = [row["attempt_id"] for row in transitions]
    assert all(len(value) == 32 for value in attempt_ids)
    assert len(set(attempt_ids)) == 2
