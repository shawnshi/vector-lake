import json
from pathlib import Path

import pytest

from vector_lake import db_store, governance_store, tool_ingest
from vector_lake.tool_ingest import INGEST_CONTRACT_VERSION


def _current_payload(filepath: str, file_hash: str, *, candidates=None) -> dict:
    return {
        "filepath": filepath,
        "hash": file_hash,
        "canonical_name": f"Source_{Path(filepath).stem}.md",
        "source_hash": "",
        "source_projection_hash": "",
        "integration_candidates": list(candidates or []),
        "ingest_contract_version": INGEST_CONTRACT_VERSION,
        "instructions": "isolated current-contract instructions",
    }


def test_dispatch_claim_gate_only_leases_current_ingest_contract(isolated_memory):
    legacy_id = db_store.enqueue_job(
        "ingest",
        {
            "filepath": "raw/legacy-claim.md",
            "hash": "legacy-claim",
            "canonical_name": "Source_Legacy-Claim.md",
        },
    )
    current_id = db_store.enqueue_job(
        "ingest",
        _current_payload("raw/current-claim.md", "current-claim"),
    )
    other_id = db_store.enqueue_job(
        "research",
        _current_payload("raw/non-ingest.md", "non-ingest"),
    )

    claimed = db_store.claim_pending_jobs(
        limit=10,
        lease_seconds=60,
        task_type="ingest",
        required_ingest_contract_version=INGEST_CONTRACT_VERSION,
    )

    assert [row["job_id"] for row in claimed] == [current_id]
    statuses = {
        row["job_id"]: row["status"]
        for row in db_store.get_connection().execute(
            "SELECT job_id, status FROM jobs WHERE job_id IN (?, ?, ?)",
            (legacy_id, current_id, other_id),
        )
    }
    assert statuses == {
        legacy_id: "queued",
        current_id: "dispatched",
        other_id: "queued",
    }


def test_subagent_claim_gate_rejects_legacy_awaiting_packet(isolated_memory):
    legacy_id = db_store.enqueue_job(
        "ingest",
        {
            "filepath": "raw/legacy-subagent.md",
            "hash": "legacy-subagent",
            "canonical_name": "Source_Legacy-Subagent.md",
        },
    )
    current_id = db_store.enqueue_job(
        "ingest",
        _current_payload("raw/current-subagent.md", "current-subagent"),
    )
    db_store.mark_job_awaiting_subagent(legacy_id, "")
    db_store.mark_job_awaiting_subagent(current_id, "")

    claimed = db_store.claim_subagent_jobs(
        limit=10,
        lease_seconds=60,
        required_ingest_contract_version=INGEST_CONTRACT_VERSION,
    )

    assert [row["job_id"] for row in claimed] == [current_id]
    assert (
        db_store.get_connection()
        .execute("SELECT status FROM jobs WHERE job_id = ?", (legacy_id,))
        .fetchone()[0]
        == "awaiting_subagent"
    )


def test_job_queues_break_created_at_ties_with_stable_job_id(isolated_memory):
    db_store.init_db()
    conn = db_store.get_connection()
    created_at = "2026-01-01T00:00:00+00:00"
    with db_store.transaction():
        for job_id in ("dispatch-z", "dispatch-a"):
            conn.execute(
                "INSERT INTO jobs "
                "(job_id, task_type, payload, status, retries, created_at, updated_at) "
                "VALUES (?, 'ingest', '{}', 'queued', 0, ?, ?)",
                (job_id, created_at, created_at),
            )
        for job_id in ("subagent-z", "subagent-a"):
            conn.execute(
                "INSERT INTO jobs "
                "(job_id, task_type, payload, status, retries, created_at, updated_at) "
                "VALUES (?, 'ingest', '{}', 'awaiting_subagent', 0, ?, ?)",
                (job_id, created_at, created_at),
            )

    assert [row["job_id"] for row in db_store.get_pending_jobs(limit=2)] == [
        "dispatch-a",
        "dispatch-z",
    ]
    assert [
        row["job_id"]
        for row in db_store.get_jobs_by_status(["queued"], limit=2)
    ] == ["dispatch-a", "dispatch-z"]
    assert [
        row["job_id"]
        for row in db_store.claim_pending_jobs(
            limit=2,
            lease_seconds=60,
            lease_owner="stable-dispatch-order",
        )
    ] == ["dispatch-a", "dispatch-z"]
    assert [
        row["job_id"]
        for row in db_store.claim_subagent_jobs(
            limit=2,
            lease_seconds=60,
            lease_owner="stable-subagent-order",
        )
    ] == ["subagent-a", "subagent-z"]


def test_cleanup_queue_order_does_not_depend_on_jobs_row_order(isolated_memory):
    db_store.init_db()
    conn = db_store.get_connection()
    created_at = "2000-01-01T00:00:00+00:00"
    shared_path = "raw/stable-cleanup-order.md"
    with db_store.transaction():
        for job_id in ("superseded-z", "superseded-a"):
            payload = {"filepath": shared_path, "hash": job_id}
            conn.execute(
                "INSERT INTO jobs "
                "(job_id, task_type, payload, status, retries, created_at, updated_at, "
                "task_packet_path) VALUES (?, 'ingest', ?, 'queued', 0, ?, ?, ?)",
                (
                    job_id,
                    json.dumps(payload),
                    created_at,
                    created_at,
                    str(isolated_memory / f"{job_id}.json"),
                ),
            )

    db_store.enqueue_job(
        "ingest",
        {"filepath": shared_path, "hash": "new-source-version"},
    )
    superseded_cleanup = conn.execute(
        "SELECT job_id FROM ingest_task_cleanup ORDER BY cleanup_id ASC"
    ).fetchall()
    assert [row["job_id"] for row in superseded_cleanup] == [
        "superseded-a",
        "superseded-z",
    ]

    with db_store.transaction():
        conn.execute("DELETE FROM ingest_task_cleanup")
        for job_id in ("expired-z", "expired-a"):
            conn.execute(
                "INSERT INTO jobs "
                "(job_id, task_type, payload, status, retries, created_at, updated_at, "
                "task_packet_path) VALUES (?, 'ingest', '{}', 'awaiting_subagent', "
                "0, ?, ?, ?)",
                (
                    job_id,
                    created_at,
                    created_at,
                    str(isolated_memory / f"{job_id}.json"),
                ),
            )

    assert db_store.expire_stale_subagent_jobs(max_age_seconds=1) == 2
    expired_cleanup = conn.execute(
        "SELECT job_id FROM ingest_task_cleanup ORDER BY cleanup_id ASC"
    ).fetchall()
    assert [row["job_id"] for row in expired_cleanup] == [
        "expired-a",
        "expired-z",
    ]


def test_legacy_migration_covers_every_claimable_status_and_refreshes_raw_hash(
    isolated_memory,
    monkeypatch,
):
    monkeypatch.setattr(
        tool_ingest,
        "_build_ingest_instructions",
        lambda *_args: "rebuilt current-contract instructions",
    )
    monkeypatch.setattr(governance_store, "canonical_page_versions", lambda _keys: {})
    statuses = (
        "queued",
        "failed",
        "dispatched",
        "subagent_processing",
        "awaiting_subagent",
    )
    jobs = {}
    for index, status in enumerate(statuses):
        raw_path = isolated_memory / "raw" / f"legacy-status-{index}.md"
        raw_path.write_text(f"current raw revision {index}", encoding="utf-8")
        job_id = db_store.enqueue_job(
            "ingest",
            {
                "filepath": str(raw_path),
                "hash": f"stale-{index}",
                "canonical_name": f"Source_Legacy-Status-{index}.md",
                "ingest_contract_version": 1,
            },
        )
        if status == "awaiting_subagent":
            db_store.mark_job_awaiting_subagent(job_id, "")
        elif status != "queued":
            with db_store.transaction():
                db_store.get_connection().execute(
                    "UPDATE jobs SET status = ?, retries = ?, available_at = ?, "
                    "lease_until = ?, lease_owner = ?, lease_token = ? WHERE job_id = ?",
                    (
                        status,
                        2 if status == "failed" else 0,
                        "2000-01-01T00:00:00+00:00",
                        "2000-01-01T00:00:00+00:00",
                        "expired-owner",
                        "expired-token",
                        job_id,
                    ),
                )
        jobs[job_id] = raw_path

    migrated = tool_ingest.requeue_legacy_ingest_jobs()

    assert migrated == len(statuses)
    rows = db_store.get_connection().execute(
        "SELECT job_id, status, retries, payload, idempotency_key FROM jobs"
    )
    by_id = {row["job_id"]: row for row in rows if row["job_id"] in jobs}
    assert set(by_id) == set(jobs)
    for job_id, raw_path in jobs.items():
        row = by_id[job_id]
        payload = json.loads(row["payload"])
        assert row["status"] == "queued"
        assert row["retries"] == 0
        assert payload["hash"] == tool_ingest.calculate_hash(str(raw_path))
        assert payload["integration_candidates"] == []
        assert payload["ingest_contract_version"] == INGEST_CONTRACT_VERSION
        assert row["idempotency_key"] == db_store._job_idempotency_key(
            "ingest", payload
        )


def test_legacy_migration_terminalizes_bad_window_before_advancing_peer(
    isolated_memory,
    monkeypatch,
):
    monkeypatch.setattr(
        tool_ingest,
        "_build_ingest_instructions",
        lambda *_args: "rebuilt current-contract instructions",
    )
    monkeypatch.setattr(governance_store, "canonical_page_versions", lambda _keys: {})
    blocked_ids = []
    for index in range(100):
        job_id = db_store.enqueue_job(
            "ingest",
            {
                "filepath": f"raw/bad-{index}.md",
                "hash": f"bad-{index}",
                "canonical_name": f"Source_Bad-{index}.md",
            },
        )
        blocked_ids.append(job_id)
    with db_store.transaction():
        for index, job_id in enumerate(blocked_ids):
            db_store.get_connection().execute(
                "UPDATE jobs SET payload = '{', status = 'awaiting_subagent', "
                "created_at = ?, updated_at = ? WHERE job_id = ?",
                (f"2000-01-01T00:00:{index:02d}+00:00", "2000-01-01", job_id),
            )

    raw_path = isolated_memory / "raw" / "peer-after-blocked.md"
    raw_path.write_text("current peer revision", encoding="utf-8")
    peer_id = db_store.enqueue_job(
        "ingest",
        {
            "filepath": str(raw_path),
            "hash": "stale-peer",
            "canonical_name": "Source_Peer-After-Blocked.md",
            "ingest_contract_version": 1,
        },
    )
    db_store.mark_job_awaiting_subagent(peer_id, "")
    with db_store.transaction():
        db_store.get_connection().execute(
            "UPDATE jobs SET created_at = '9999-01-01', updated_at = '9999-01-01' "
            "WHERE job_id = ?",
            (peer_id,),
        )

    assert tool_ingest.requeue_legacy_ingest_jobs() == 0
    assert (
        db_store.get_connection()
        .execute(
            "SELECT COUNT(*) FROM jobs WHERE job_id IN ("
            + ", ".join("?" for _ in blocked_ids)
            + ") AND status = 'failed' AND retries = 3",
            tuple(blocked_ids),
        )
        .fetchone()[0]
        == 100
    )

    assert tool_ingest.requeue_legacy_ingest_jobs() == 1
    peer = (
        db_store.get_connection()
        .execute("SELECT status, payload FROM jobs WHERE job_id = ?", (peer_id,))
        .fetchone()
    )
    assert peer["status"] == "queued"
    assert json.loads(peer["payload"])["hash"] == tool_ingest.calculate_hash(
        str(raw_path)
    )


def test_debt_reconcile_persists_blocked_window_and_advances_next_job(
    isolated_memory,
):
    blocked_ids = []
    for index in range(100):
        job_id = db_store.enqueue_job(
            "ingest",
            {
                "filepath": f"raw/debt-bad-{index}.md",
                "hash": f"debt-bad-{index}",
                "canonical_name": f"Source_Debt-Bad-{index}.md",
            },
        )
        blocked_ids.append(job_id)
    with db_store.transaction():
        for index, job_id in enumerate(blocked_ids):
            db_store.get_connection().execute(
                "UPDATE jobs SET payload = '{', status = 'failed', retries = 3, "
                "created_at = ?, updated_at = ? WHERE job_id = ?",
                (f"2000-01-01T00:00:{index:02d}+00:00", "2000-01-01", job_id),
            )

    missing_path = isolated_memory / "raw" / "missing-after-blocked.md"
    peer_id = db_store.enqueue_job(
        "ingest",
        {
            "filepath": str(missing_path),
            "hash": "missing-after-blocked",
            "canonical_name": "Source_Missing-After-Blocked.md",
        },
    )
    db_store.mark_job_awaiting_subagent(peer_id, "")
    with db_store.transaction():
        db_store.get_connection().execute(
            "UPDATE jobs SET created_at = '9999-01-01', updated_at = '9999-01-01' "
            "WHERE job_id = ?",
            (peer_id,),
        )

    first = json.loads(tool_ingest.reconcile_ingest_job_debt(dry_run=False, limit=0))
    assert first["selected_jobs"] == 100
    assert first["remaining_unselected"] == 1
    assert first["applied_counts"] == {"blocked_invalid_payload": 100}
    assert Path(first["backup"]).is_dir()
    marker = (
        db_store.get_connection()
        .execute("SELECT result_json FROM jobs WHERE job_id = ?", (blocked_ids[0],))
        .fetchone()[0]
    )
    assert json.loads(marker) == {
        "action": "blocked_invalid_payload",
        "maintenance": "ingest_job_debt",
        "reason": "payload is not a JSON object",
        "state": "blocked",
    }

    second = json.loads(tool_ingest.reconcile_ingest_job_debt(dry_run=False, limit=0))
    assert second["selected_jobs"] == 1
    assert second["applied_counts"] == {"cancel_missing_raw": 1}
    assert (
        db_store.get_connection()
        .execute("SELECT status FROM jobs WHERE job_id = ?", (peer_id,))
        .fetchone()[0]
        == "cancelled"
    )
    third = json.loads(tool_ingest.reconcile_ingest_job_debt(dry_run=False, limit=0))
    assert third["selected_jobs"] == 0


def _integrated_processed_data(candidate: dict, relation: dict) -> dict:
    return {
        "canonical_name": "Source_Candidate-Test.md",
        "source_hash": "",
        "source_projection_hash": "",
        "_queued_integration_candidates": [candidate],
        "integration": {
            "disposition": "integrated",
            "relations": [
                {
                    "predicate": "validates",
                    "evidence": "The source directly supports this candidate.",
                    "confidence": 0.9,
                    "event_date": "2026-07-27",
                    "event_tag": "Validation",
                    **relation,
                }
            ],
        },
    }


@pytest.mark.parametrize(
    ("candidate", "relation", "message"),
    [
        (
            {
                "target": "Concept_Allowed.md",
                "target_hash": "v1",
                "target_projection_hash": "a" * 64,
            },
            {
                "target": "Concept_Not-Dispatched.md",
                "target_hash": "v1",
                "target_projection_hash": "a" * 64,
            },
            "not dispatched as a candidate",
        ),
        (
            {
                "target": "Concept_Allowed.md",
                "target_hash": "v1",
                "target_projection_hash": "a" * 64,
            },
            {
                "target": "Concept_Allowed.md",
                "target_hash": "v2",
                "target_projection_hash": "b" * 64,
            },
            "target_hash does not match the dispatched candidate",
        ),
        (
            {
                "target": "Concept_Allowed.md",
                "target_hash": "v1",
                "target_projection_hash": "a" * 64,
            },
            {
                "target": "Concept_Allowed.md",
                "target_hash": "v1",
                "target_projection_hash": "b" * 64,
            },
            "target_projection_hash does not match the dispatched candidate",
        ),
    ],
)
def test_integration_rejects_non_dispatched_or_tampered_candidate_tokens(
    monkeypatch,
    candidate,
    relation,
    message,
):
    monkeypatch.setattr(
        governance_store,
        "canonical_page_versions",
        lambda _keys: {"Concept_Allowed": "v1"},
    )
    files = [
        {
            "filename": "Source_Candidate-Test.md",
            "content": "# Source\n\n## Graph Integration\n",
        }
    ]

    with pytest.raises(ValueError, match=message):
        tool_ingest._apply_integration_disposition(
            files,
            _integrated_processed_data(candidate, relation),
        )


def test_integration_accepts_exact_dispatched_candidate_tokens(monkeypatch):
    candidate = {
        "target": "Concept_Allowed.md",
        "target_hash": "v1",
        "target_projection_hash": "a" * 64,
    }
    monkeypatch.setattr(
        governance_store,
        "canonical_page_versions",
        lambda _keys: {"Concept_Allowed": "v1"},
    )
    monkeypatch.setattr(
        tool_ingest,
        "_read_canonical_target_content",
        lambda *_args, **_kwargs: "# Target\n\n## 2. \u8bc1\u636e\u65f6\u95f4\u7ebf\n",
    )
    files = [
        {
            "filename": "Source_Candidate-Test.md",
            "content": "# Source\n\n## Graph Integration\n",
        }
    ]

    mutations, disposition, integration_targets = (
        tool_ingest._apply_integration_disposition(
            files,
            _integrated_processed_data(candidate, candidate),
        )
    )

    assert disposition == "integrated"
    assert integration_targets == {"Concept_Allowed.md"}
    assert [item["filename"] for item in mutations] == [
        "Source_Candidate-Test.md",
        "Concept_Allowed.md",
    ]
    assert mutations[1]["expected_version"] == "v1"
    assert mutations[1]["expected_projection_hash"] == "a" * 64


def test_governance_queue_load_uses_item_id_as_stable_tie_break(
    isolated_memory,
):
    governance_store.initialize_meta_store()
    connection = db_store.get_connection()
    timestamp = "2026-08-03T00:00:00+00:00"
    with db_store.transaction():
        connection.executemany(
            "INSERT INTO governance_queue (item_id, data_json, updated_at) "
            "VALUES (?, ?, ?)",
            [
                ("item-b", json.dumps({"item_id": "item-b"}), timestamp),
                ("item-a", json.dumps({"item_id": "item-a"}), timestamp),
            ],
        )

    before = governance_store.load_governance_queue()["items"]
    connection.execute("VACUUM main")
    after = governance_store.load_governance_queue()["items"]

    assert [item["item_id"] for item in before] == ["item-a", "item-b"]
    assert [item["item_id"] for item in after] == ["item-a", "item-b"]
