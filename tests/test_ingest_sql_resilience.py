import json
from pathlib import Path

import pytest

from vector_lake import db_store, governance_store, tool_ingest
from vector_lake.tool_ingest import (
    INGEST_CONTRACT_VERSION,
    calculate_hash,
    claim_ingest_tasks,
    reconcile_ingest_job_debt,
)
from tests.test_mutation_coordinator import _write_purpose_contract


def _v4_payload(filepath, file_hash, canonical_name):
    return {
        "filepath": str(filepath),
        "hash": file_hash,
        "canonical_name": canonical_name,
        "source_hash": "",
        "source_projection_hash": "",
        "integration_candidates": [],
        "ingest_contract_version": INGEST_CONTRACT_VERSION,
        "instructions": "compile this source",
    }


def _stub_maintenance_backup(isolated_memory, monkeypatch, label):
    backup_dir = isolated_memory / "wiki" / ".meta" / "backups" / label
    backup_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        "vector_lake.tool_projection.create_maintenance_backup",
        lambda _label: str(backup_dir),
    )
    return backup_dir


def test_current_queued_ingest_with_null_retries_is_claimable(isolated_memory):
    payload = _v4_payload(
        "raw/null-current.md", "null-current-hash", "Source_Null-Current.md"
    )
    job_id = db_store.enqueue_job("ingest", payload)
    with db_store.transaction() as connection:
        connection.execute(
            "UPDATE jobs SET retries = NULL WHERE job_id = ?",
            (job_id,),
        )

    claimed = db_store.claim_pending_jobs(
        limit=1,
        lease_seconds=60,
        lease_owner="null-retries-worker",
        task_type="ingest",
        required_ingest_contract_version=INGEST_CONTRACT_VERSION,
    )

    assert [row["job_id"] for row in claimed] == [job_id]
    assert claimed[0]["status"] == "dispatched"
    assert claimed[0]["lease_owner"] == "null-retries-worker"


def test_queued_legacy_ingest_with_null_retries_is_migrated(
    isolated_memory,
    monkeypatch,
):
    _write_purpose_contract(isolated_memory)
    raw_path = isolated_memory / "raw" / "null-legacy.md"
    raw_path.write_text("legacy source", encoding="utf-8")
    payload = {
        "filepath": str(raw_path),
        "hash": "legacy-stale-hash",
        "canonical_name": "Source_Null-Legacy.md",
        "ingest_contract_version": 1,
    }
    job_id = db_store.enqueue_job("ingest", payload)
    with db_store.transaction() as connection:
        connection.execute(
            "UPDATE jobs SET retries = NULL WHERE job_id = ?",
            (job_id,),
        )
    monkeypatch.setattr(
        tool_ingest,
        "_build_ingest_instructions",
        lambda *_args: "rebuilt v4 instructions",
    )
    monkeypatch.setattr(
        governance_store,
        "canonical_page_versions",
        lambda _keys: {},
    )

    migrated = tool_ingest.requeue_legacy_ingest_jobs()

    row = (
        db_store.get_connection()
        .execute(
            "SELECT status, retries, payload FROM jobs WHERE job_id = ?",
            (job_id,),
        )
        .fetchone()
    )
    rebuilt = json.loads(row["payload"])
    assert migrated == 1
    assert row["status"] == "queued"
    assert row["retries"] == 0
    assert rebuilt["hash"] == calculate_hash(str(raw_path))
    assert rebuilt["ingest_contract_version"] == INGEST_CONTRACT_VERSION
    assert rebuilt["instructions"] == "rebuilt v4 instructions"


def test_null_retries_debt_is_terminalized_and_releases_identity(
    isolated_memory,
    monkeypatch,
):
    original_payload = {
        "filepath": "raw/null-debt.md",
        "hash": "null-debt-hash",
        "canonical_name": "Source_Null-Debt.md",
    }
    job_id = db_store.enqueue_job("ingest", original_payload)
    original_key = (
        db_store.get_connection()
        .execute(
            "SELECT idempotency_key FROM jobs WHERE job_id = ?",
            (job_id,),
        )
        .fetchone()["idempotency_key"]
    )
    db_store.mark_job_awaiting_subagent(job_id, "")
    with db_store.transaction() as connection:
        connection.execute(
            "UPDATE jobs SET payload = '{', retries = NULL WHERE job_id = ?",
            (job_id,),
        )
    _stub_maintenance_backup(isolated_memory, monkeypatch, "null-debt")

    result = json.loads(reconcile_ingest_job_debt(dry_run=False, limit=1))

    row = (
        db_store.get_connection()
        .execute(
            "SELECT status, retries, idempotency_key, completed_at, result_json "
            "FROM jobs WHERE job_id = ?",
            (job_id,),
        )
        .fetchone()
    )
    assert result["applied_counts"] == {"blocked_invalid_payload": 1}
    assert row["status"] == "failed"
    assert row["retries"] >= 3
    assert row["idempotency_key"] is None
    assert row["completed_at"]
    assert json.loads(row["result_json"])["state"] == "blocked"

    replacement = db_store.enqueue_job("ingest", original_payload)
    replacement_row = (
        db_store.get_connection()
        .execute(
            "SELECT status, idempotency_key FROM jobs WHERE job_id = ?",
            (replacement,),
        )
        .fetchone()
    )
    assert replacement != job_id
    assert replacement_row["status"] == "queued"
    assert replacement_row["idempotency_key"] == original_key


def test_legacy_terminal_failure_releases_identity_for_replacement(
    isolated_memory,
    monkeypatch,
):
    raw_path = isolated_memory / "raw" / "legacy-terminal.md"
    raw_path.write_text("legacy terminal source", encoding="utf-8")
    payload = {
        "filepath": str(raw_path),
        "hash": calculate_hash(str(raw_path)),
        "canonical_name": "Source_Legacy-Terminal.md",
        "ingest_contract_version": 1,
    }
    job_id = db_store.enqueue_job("ingest", payload)
    original_key = (
        db_store.get_connection()
        .execute(
            "SELECT idempotency_key FROM jobs WHERE job_id = ?",
            (job_id,),
        )
        .fetchone()["idempotency_key"]
    )
    db_store.mark_job_awaiting_subagent(job_id, "")

    def fail_prompt_rebuild(*_args):
        raise RuntimeError("injected prompt rebuild failure")

    monkeypatch.setattr(
        tool_ingest,
        "_build_ingest_instructions",
        fail_prompt_rebuild,
    )

    migrated = tool_ingest.requeue_legacy_ingest_jobs()

    row = (
        db_store.get_connection()
        .execute(
            "SELECT status, retries, idempotency_key, completed_at, result_json "
            "FROM jobs WHERE job_id = ?",
            (job_id,),
        )
        .fetchone()
    )
    assert migrated == 0
    assert row["status"] == "failed"
    assert row["retries"] >= 3
    assert row["idempotency_key"] is None
    assert row["completed_at"]
    assert json.loads(row["result_json"])["state"] == "blocked"

    replacement = db_store.enqueue_job("ingest", payload)
    replacement_row = (
        db_store.get_connection()
        .execute(
            "SELECT status, idempotency_key FROM jobs WHERE job_id = ?",
            (replacement,),
        )
        .fetchone()
    )
    assert replacement != job_id
    assert replacement_row["status"] == "queued"
    assert replacement_row["idempotency_key"] == original_key


@pytest.mark.parametrize(
    "packet_state", ["empty", "missing", "invalid_json", "wrong_identity"]
)
def test_invalid_task_packet_claim_repairs_packet_without_spending_retry_budget(
    isolated_memory,
    packet_state,
):
    payload = _v4_payload(
        f"raw/{packet_state}.md",
        f"{packet_state}-hash",
        f"Source_{packet_state.title()}.md",
    )
    job_id = db_store.enqueue_job("ingest", payload)
    packet_path = ""
    if packet_state == "wrong_identity":
        from vector_lake.native_llm import create_subagent_task

        packet_path = str(
            create_subagent_task(
                "ingest",
                "wrong identity",
                "JSON array",
                {"job_id": "different-job"},
            )
        )
    elif packet_state != "empty":
        packet = isolated_memory / "task-packets" / f"{packet_state}.json"
        packet.parent.mkdir(parents=True, exist_ok=True)
        if packet_state == "invalid_json":
            packet.write_text("{", encoding="utf-8")
        packet_path = str(packet)
    db_store.mark_job_awaiting_subagent(job_id, packet_path)

    claimed = json.loads(claim_ingest_tasks(limit=1, lease_seconds=3600))

    assert [item["job_id"] for item in claimed] == [job_id]
    repaired = claimed[0]
    task_packet = repaired["task_packet"]
    processed = task_packet["metadata"]["processed_data"]
    assert task_packet["task_type"] == "ingest"
    assert processed["job_id"] == job_id
    for key in (
        "filepath",
        "hash",
        "canonical_name",
        "source_hash",
        "source_projection_hash",
        "integration_candidates",
        "ingest_contract_version",
    ):
        assert processed[key] == payload[key]
    assert processed["lease_owner"] == repaired["lease_owner"]
    assert processed["lease_token"] == repaired["lease_token"]
    assert processed["lease_generation"] == repaired["lease_generation"]

    row = (
        db_store.get_connection()
        .execute(
            "SELECT status, retries, lease_until, lease_owner, lease_token, "
            "task_packet_path FROM jobs WHERE job_id = ?",
            (job_id,),
        )
        .fetchone()
    )
    assert row["status"] == "subagent_processing"
    assert row["retries"] == 0
    assert row["lease_until"]
    assert row["lease_owner"] == repaired["lease_owner"]
    assert row["lease_token"] == repaired["lease_token"]
    assert row["task_packet_path"] == repaired["task_packet_path"]
    assert Path(row["task_packet_path"]).is_file()
    if packet_path:
        assert Path(row["task_packet_path"]).resolve() != Path(packet_path).resolve()

    Path(row["task_packet_path"]).unlink(missing_ok=True)
    if packet_path:
        Path(packet_path).unlink(missing_ok=True)


def test_unrepairable_task_packet_claim_terminalizes_at_retry_limit(
    isolated_memory,
    monkeypatch,
):
    payload = _v4_payload(
        "raw/repeated-packet.md",
        "repeated-packet-hash",
        "Source_Repeated-Packet.md",
    )
    job_id = db_store.enqueue_job("ingest", payload)
    original_key = (
        db_store.get_connection()
        .execute(
            "SELECT idempotency_key FROM jobs WHERE job_id = ?",
            (job_id,),
        )
        .fetchone()["idempotency_key"]
    )
    db_store.mark_job_awaiting_subagent(job_id, "")
    with db_store.transaction() as connection:
        connection.execute(
            "UPDATE jobs SET retries = 2 WHERE job_id = ?",
            (job_id,),
        )

    def fail_packet_rebuild(*_args, **_kwargs):
        raise OSError("injected packet rebuild failure")

    monkeypatch.setattr(
        "vector_lake.native_llm.create_subagent_task",
        fail_packet_rebuild,
    )

    claimed = json.loads(claim_ingest_tasks(limit=1, lease_seconds=3600))

    row = (
        db_store.get_connection()
        .execute(
            "SELECT status, retries, idempotency_key, completed_at, "
            "lease_until, lease_owner, lease_token FROM jobs WHERE job_id = ?",
            (job_id,),
        )
        .fetchone()
    )
    assert claimed == []
    assert row["status"] == "failed"
    assert row["retries"] == 3
    assert row["idempotency_key"] is None
    assert row["completed_at"]
    assert row["lease_until"] is None
    assert row["lease_owner"] is None
    assert row["lease_token"] is None

    replacement = db_store.enqueue_job("ingest", payload)
    replacement_row = (
        db_store.get_connection()
        .execute(
            "SELECT status, idempotency_key FROM jobs WHERE job_id = ?",
            (replacement,),
        )
        .fetchone()
    )
    assert replacement != job_id
    assert replacement_row["status"] == "queued"
    assert replacement_row["idempotency_key"] == original_key


def test_historical_terminal_failed_identity_is_not_an_inventory_gate(
    isolated_memory,
):
    raw_path = isolated_memory / "raw" / "historical-terminal.md"
    raw_path.write_text("historical terminal source", encoding="utf-8")
    first_result = json.loads(
        tool_ingest.prepare_ingest_batch(
            batch_size=1,
            candidate_paths=[str(raw_path)],
        )
    )
    connection = db_store.get_connection()
    first = connection.execute(
        "SELECT job_id, payload, idempotency_key FROM jobs WHERE task_type = 'ingest'"
    ).fetchone()
    original_key = first["idempotency_key"]
    assert original_key
    with db_store.transaction() as transaction_connection:
        transaction_connection.execute(
            "UPDATE jobs SET status = 'failed', retries = 3 WHERE job_id = ?",
            (first["job_id"],),
        )

    durable_keys = tool_ingest._existing_durable_ingest_keys(
        connection,
        [original_key],
    )
    second_result = json.loads(
        tool_ingest.prepare_ingest_batch(
            batch_size=1,
            candidate_paths=[str(raw_path)],
        )
    )

    assert durable_keys == set()
    assert first_result["filepath"] == str(raw_path.resolve())
    assert second_result["filepath"] == str(raw_path.resolve())
    rows = connection.execute(
        "SELECT job_id, status, retries, idempotency_key FROM jobs "
        "WHERE task_type = 'ingest' ORDER BY created_at, job_id"
    ).fetchall()
    assert len(rows) == 2
    historical = next(row for row in rows if row["job_id"] == first["job_id"])
    replacement = next(row for row in rows if row["job_id"] != first["job_id"])
    assert historical["status"] == "failed"
    assert historical["retries"] == 3
    assert historical["idempotency_key"] is None
    assert replacement["status"] == "queued"
    assert replacement["retries"] == 0
    assert replacement["idempotency_key"] == original_key


def _packet_processed_data(payload, job_id):
    return {
        "filepath": payload["filepath"],
        "hash": payload["hash"],
        "canonical_name": payload["canonical_name"],
        "source_hash": payload["source_hash"],
        "source_projection_hash": payload["source_projection_hash"],
        "integration_candidates": payload["integration_candidates"],
        "ingest_contract_version": payload["ingest_contract_version"],
        "job_id": job_id,
    }


def _create_valid_ingest_packet(payload, job_id):
    from vector_lake.ingest_worker import _subagent_ingest_prompt
    from vector_lake.native_llm import create_subagent_task

    return create_subagent_task(
        "ingest",
        _subagent_ingest_prompt(payload["instructions"]),
        "JSON array consumable by finalize_ingest(files_written, processed_data)",
        {
            "job_id": job_id,
            "processed_data": _packet_processed_data(payload, job_id),
            "finalize_tool": "finalize_ingest",
        },
    )


def _control_plane_payload(label):
    payload = _v4_payload(
        f"raw/control-plane-{label}.md",
        f"control-plane-{label}-hash",
        f"Source_Control-Plane-{label}.md",
    )
    payload["source_hash"] = "source-version"
    payload["source_projection_hash"] = "a" * 64
    payload["integration_candidates"] = [
        {
            "target": "Concept_Target.md",
            "target_hash": "target-version",
            "target_projection_hash": "b" * 64,
        }
    ]
    payload["instructions"] = f"compile durable instructions for {label}"
    return payload


def _assert_claimed_control_plane_contract(task_packet, payload, job_id):
    from vector_lake.ingest_worker import _subagent_ingest_prompt

    assert set(task_packet) == {
        "task_id",
        "task_type",
        "created_at",
        "runtime",
        "cost_boundary",
        "expected_output",
        "metadata",
        "prompt",
    }
    assert task_packet["task_type"] == "ingest"
    assert task_packet["runtime"] == "current-environment-subagent"
    assert (
        task_packet["cost_boundary"]
        == "no non-embedding model API calls from Vector Lake runtime"
    )
    assert (
        task_packet["expected_output"]
        == "JSON array consumable by finalize_ingest(files_written, processed_data)"
    )
    assert task_packet["prompt"] == _subagent_ingest_prompt(payload["instructions"])
    assert set(task_packet["metadata"]) == {
        "job_id",
        "processed_data",
        "finalize_tool",
    }
    assert task_packet["metadata"]["job_id"] == job_id
    assert task_packet["metadata"]["finalize_tool"] == "finalize_ingest"
    processed = task_packet["metadata"]["processed_data"]
    assert {
        key: processed[key] for key in _packet_processed_data(payload, job_id)
    } == _packet_processed_data(payload, job_id)
    assert set(processed) == {
        *_packet_processed_data(payload, job_id),
        "lease_owner",
        "lease_token",
        "lease_generation",
    }


@pytest.mark.parametrize(
    "tamper",
    [
        "outside_root",
        "filepath",
        "hash",
        "canonical_name",
        "source_hash",
        "source_projection_hash",
        "integration_candidates",
        "ingest_contract_version",
        "prompt",
    ],
)
def test_claim_rebuilds_packet_when_path_or_durable_binding_is_tampered(
    isolated_memory,
    tamper,
):
    from vector_lake.ingest_worker import _subagent_ingest_prompt
    from vector_lake.native_llm import create_subagent_task, get_subagent_scratch_dir

    payload = _v4_payload(
        f"raw/tampered-{tamper}.md",
        f"tampered-{tamper}-hash",
        f"Source_Tampered-{tamper}.md",
    )
    payload["source_hash"] = "source-version"
    payload["source_projection_hash"] = "a" * 64
    payload["integration_candidates"] = [
        {
            "target": "Concept_Target.md",
            "target_hash": "target-version",
            "target_projection_hash": "b" * 64,
        }
    ]
    payload["instructions"] = f"compile durable instructions for {tamper}"
    job_id = db_store.enqueue_job("ingest", payload)
    bad_path = create_subagent_task(
        "ingest",
        _subagent_ingest_prompt(payload["instructions"]),
        "JSON array consumable by finalize_ingest(files_written, processed_data)",
        {
            "job_id": job_id,
            "processed_data": _packet_processed_data(payload, job_id),
            "finalize_tool": "finalize_ingest",
        },
    )
    packet = json.loads(bad_path.read_text(encoding="utf-8"))
    if tamper == "outside_root":
        outside_dir = isolated_memory / "outside-controlled-task-root"
        outside_dir.mkdir()
        outside_path = outside_dir / bad_path.name
        bad_path.replace(outside_path)
        bad_path = outside_path
    elif tamper == "prompt":
        packet["prompt"] = "attacker-controlled prompt"
        bad_path.write_text(json.dumps(packet), encoding="utf-8")
    else:
        replacement = (
            []
            if tamper == "integration_candidates"
            else INGEST_CONTRACT_VERSION - 1
            if tamper == "ingest_contract_version"
            else f"tampered-{tamper}"
        )
        packet["metadata"]["processed_data"][tamper] = replacement
        bad_path.write_text(json.dumps(packet), encoding="utf-8")
    db_store.mark_job_awaiting_subagent(job_id, str(bad_path))

    claimed = json.loads(claim_ingest_tasks(limit=1, lease_seconds=3600))

    assert [item["job_id"] for item in claimed] == [job_id]
    repaired = claimed[0]
    repaired_path = Path(repaired["task_packet_path"]).resolve()
    task_root = (get_subagent_scratch_dir() / "subagent_tasks").resolve()
    assert repaired_path.is_relative_to(task_root)
    assert repaired_path != bad_path.resolve()
    repaired_packet = repaired["task_packet"]
    assert repaired_packet["prompt"] == _subagent_ingest_prompt(payload["instructions"])
    repaired_processed = repaired_packet["metadata"]["processed_data"]
    assert {
        key: repaired_processed[key] for key in _packet_processed_data(payload, job_id)
    } == _packet_processed_data(payload, job_id)
    row = (
        db_store.get_connection()
        .execute(
            "SELECT status, retries, task_packet_path, lease_token FROM jobs "
            "WHERE job_id = ?",
            (job_id,),
        )
        .fetchone()
    )
    assert row["status"] == "subagent_processing"
    assert row["retries"] == 0
    assert Path(row["task_packet_path"]).resolve() == repaired_path
    assert row["lease_token"] == repaired["lease_token"]
    if tamper == "outside_root":
        assert bad_path.is_file()
        assert (
            db_store.get_connection()
            .execute(
                "SELECT COUNT(*) FROM ingest_task_cleanup WHERE task_packet_path = ?",
                (str(bad_path.resolve()),),
            )
            .fetchone()[0]
            == 0
        )

    repaired_path.unlink(missing_ok=True)
    bad_path.unlink(missing_ok=True)


@pytest.mark.parametrize(
    "tamper",
    [
        "processed_integration",
        "metadata_extra",
        "finalize_tool",
        "runtime",
        "cost_boundary",
        "expected_output",
        "top_level_extra",
    ],
)
def test_claim_rebuilds_packet_when_control_plane_is_tampered(
    isolated_memory,
    tamper,
):
    payload = _control_plane_payload(tamper)
    job_id = db_store.enqueue_job("ingest", payload)
    bad_path = _create_valid_ingest_packet(payload, job_id)
    packet = json.loads(bad_path.read_text(encoding="utf-8"))
    if tamper == "processed_integration":
        packet["metadata"]["processed_data"]["integration"] = {
            "action": "update",
            "target": "Concept_Attacker-Controlled.md",
        }
    elif tamper == "metadata_extra":
        packet["metadata"]["untrusted_control"] = "run a different finalizer"
    elif tamper == "finalize_tool":
        packet["metadata"]["finalize_tool"] = "delete"
    elif tamper == "top_level_extra":
        packet["untrusted_control"] = {"action": "bypass"}
    else:
        packet[tamper] = f"tampered-{tamper}"
    bad_path.write_text(json.dumps(packet), encoding="utf-8")
    db_store.mark_job_awaiting_subagent(job_id, str(bad_path))

    claimed = json.loads(claim_ingest_tasks(limit=1, lease_seconds=3600))

    assert [item["job_id"] for item in claimed] == [job_id]
    repaired = claimed[0]
    repaired_path = Path(repaired["task_packet_path"]).resolve()
    assert repaired_path != bad_path.resolve()
    _assert_claimed_control_plane_contract(repaired["task_packet"], payload, job_id)
    cleanup = (
        db_store.get_connection()
        .execute(
            "SELECT status FROM ingest_task_cleanup WHERE job_id = ? "
            "AND task_packet_path = ?",
            (job_id, str(bad_path.resolve())),
        )
        .fetchone()
    )
    assert cleanup is not None
    assert cleanup["status"] == "pending"

    repaired_path.unlink(missing_ok=True)
    bad_path.unlink(missing_ok=True)


def test_claim_accepts_untampered_packet_without_rebuild(isolated_memory):
    payload = _control_plane_payload("valid")
    job_id = db_store.enqueue_job("ingest", payload)
    packet_path = _create_valid_ingest_packet(payload, job_id).resolve()
    original_packet = json.loads(packet_path.read_text(encoding="utf-8"))
    db_store.mark_job_awaiting_subagent(job_id, str(packet_path))

    claimed = json.loads(claim_ingest_tasks(limit=1, lease_seconds=3600))

    assert [item["job_id"] for item in claimed] == [job_id]
    accepted = claimed[0]
    assert Path(accepted["task_packet_path"]).resolve() == packet_path
    assert accepted["task_packet"]["task_id"] == original_packet["task_id"]
    _assert_claimed_control_plane_contract(accepted["task_packet"], payload, job_id)
    assert (
        db_store.get_connection()
        .execute(
            "SELECT COUNT(*) FROM ingest_task_cleanup WHERE job_id = ?",
            (job_id,),
        )
        .fetchone()[0]
        == 0
    )

    packet_path.unlink(missing_ok=True)


def _seed_terminal_owner_and_recoverable(
    isolated_memory,
    *,
    legacy_candidate,
):
    raw_path = (
        isolated_memory
        / "raw"
        / ("terminal-owner-legacy.md" if legacy_candidate else "terminal-owner-debt.md")
    )
    raw_path.write_text("current raw revision", encoding="utf-8")
    current_hash = calculate_hash(str(raw_path))
    canonical_name = (
        "Source_Terminal-Owner-Legacy.md"
        if legacy_candidate
        else "Source_Terminal-Owner-Debt.md"
    )
    owner_payload = _v4_payload(str(raw_path), current_hash, canonical_name)
    owner_job = db_store.enqueue_job("ingest", owner_payload)
    target_key = db_store._job_idempotency_key("ingest", owner_payload)
    with db_store.transaction() as connection:
        connection.execute(
            "UPDATE jobs SET status = 'failed', retries = 3, result_json = ? "
            "WHERE job_id = ?",
            (
                json.dumps(
                    {"maintenance": "ingest_job_debt", "state": "blocked"},
                    sort_keys=True,
                ),
                owner_job,
            ),
        )
    if legacy_candidate:
        candidate_payload = {
            "filepath": str(raw_path),
            "hash": "stale-legacy-hash",
            "canonical_name": canonical_name,
            "ingest_contract_version": 1,
        }
    else:
        candidate_payload = _v4_payload(
            str(raw_path),
            "stale-debt-hash",
            canonical_name,
        )
    candidate_job = db_store.enqueue_job("ingest", candidate_payload)
    db_store.mark_job_awaiting_subagent(candidate_job, "")
    return {
        "raw_path": raw_path,
        "current_hash": current_hash,
        "owner_job": owner_job,
        "candidate_job": candidate_job,
        "target_key": target_key,
    }


def _assert_terminal_owner_released_and_candidate_requeued(seed):
    rows = {
        row["job_id"]: row
        for row in db_store.get_connection().execute(
            "SELECT job_id, status, retries, idempotency_key, payload FROM jobs "
            "WHERE job_id IN (?, ?)",
            (seed["owner_job"], seed["candidate_job"]),
        )
    }
    owner = rows[seed["owner_job"]]
    candidate = rows[seed["candidate_job"]]
    assert owner["status"] == "failed"
    assert owner["retries"] == 3
    assert owner["idempotency_key"] is None
    assert candidate["status"] == "queued"
    assert candidate["retries"] == 0
    assert candidate["idempotency_key"] == seed["target_key"]
    rebuilt = json.loads(candidate["payload"])
    assert rebuilt["hash"] == seed["current_hash"]
    assert rebuilt["ingest_contract_version"] == INGEST_CONTRACT_VERSION


def test_legacy_recovery_releases_terminal_owner_instead_of_superseding_candidate(
    isolated_memory,
    monkeypatch,
):
    seed = _seed_terminal_owner_and_recoverable(
        isolated_memory,
        legacy_candidate=True,
    )
    monkeypatch.setattr(
        tool_ingest,
        "_build_ingest_instructions",
        lambda *_args: "rebuilt legacy instructions",
    )
    monkeypatch.setattr(
        governance_store,
        "canonical_page_versions",
        lambda _keys: {},
    )

    migrated = tool_ingest.requeue_legacy_ingest_jobs()

    assert migrated == 1
    _assert_terminal_owner_released_and_candidate_requeued(seed)


def test_debt_recovery_releases_terminal_owner_instead_of_superseding_candidate(
    isolated_memory,
    monkeypatch,
):
    seed = _seed_terminal_owner_and_recoverable(
        isolated_memory,
        legacy_candidate=False,
    )
    monkeypatch.setattr(
        tool_ingest,
        "_build_ingest_instructions",
        lambda *_args: "rebuilt debt instructions",
    )
    monkeypatch.setattr(
        governance_store,
        "canonical_page_versions",
        lambda _keys: {},
    )
    _stub_maintenance_backup(isolated_memory, monkeypatch, "terminal-owner-debt")

    result = json.loads(reconcile_ingest_job_debt(dry_run=False, limit=1))

    assert result["applied_counts"] == {"requeue_current": 1}
    assert result["concurrent_skips"] == []
    _assert_terminal_owner_released_and_candidate_requeued(seed)
