"""Quick diagnostics must be bounded without weakening deep/write checks."""
import json

import pytest

from vector_lake import db_store, governance_store, runtime_health, tool_doctor
from tests.test_operational_memory import (
    _integrity_test_records,
    _seed_isolated_search_memories,
    _tamper_memory_fts_with_equal_counts,
)


def _ready(monkeypatch):
    monkeypatch.setenv("VECTOR_LAKE_OPERATIONAL_MEMORY_FTS", "1")
    conn = _seed_isolated_search_memories(_integrity_test_records())
    assert governance_store.maintain_operational_memory_search_index(100)["ready"]
    return conn


def _count_scans(monkeypatch):
    calls = []
    original = db_store.inspect_operational_memory_search_integrity

    def inspect(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(db_store, "inspect_operational_memory_search_integrity", inspect)
    return calls


def _quick():
    result = json.loads(tool_doctor.quick_doctor_vector_lake())
    assert result["semantic_readiness"]["status"] == "not_checked"
    return result["infrastructure"]["detail"]["operational_memory_search"]


def test_quick_uses_read_only_durable_proof_without_full_digest(isolated_memory, monkeypatch):
    _ready(monkeypatch)
    calls = _count_scans(monkeypatch)
    original_open = runtime_health._open_runtime_database_read_only
    opened = []

    def open_read_only():
        conn, path = original_open()
        assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
        opened.append(conn)
        return conn, path

    monkeypatch.setattr(runtime_health, "_open_runtime_database_read_only", open_read_only)
    for _ in range(2):
        status = _quick()
        assert status["ready"] is True
        assert status["integrity_verification_kind"] == "durable_proof"
        assert status["integrity_inspected_rows"] == 0
    assert len(opened) == 2 and opened[0] is not opened[1]
    assert calls == []


def test_quick_zero_interval_defers_without_fake_backfill_or_stall(isolated_memory, monkeypatch):
    conn = _ready(monkeypatch)
    with db_store.transaction():
        conn.execute("UPDATE operational_memory_search_state SET updated_at='2000-01-01T00:00:00+00:00'")
    monkeypatch.setenv("VECTOR_LAKE_OPERATIONAL_MEMORY_ATTESTATION_SECONDS", "0")
    calls = _count_scans(monkeypatch)
    status = _quick()
    assert calls == []
    assert status["ready"] is False
    assert status["status"] == "deferred"
    assert status["integrity_verification_kind"] == "deferred"
    assert status["progress_stalled"] is False
    assert not any("stalled" in warning for warning in status["warnings"])


@pytest.mark.parametrize("change", ["missing_proof", "invalid_proof", "pending", "backfill", "revision"])
def test_quick_does_not_certify_incomplete_index(change, isolated_memory, monkeypatch):
    conn = _ready(monkeypatch)
    with db_store.transaction():
        if change == "missing_proof":
            conn.execute("DELETE FROM operational_memory_search_state")
        elif change == "invalid_proof":
            conn.execute("UPDATE operational_memory_search_state SET proof_generation='invalid'")
        elif change == "pending":
            conn.execute("INSERT INTO operational_memory_search_pending (memory_id, operation, queued_at) VALUES ('memory_new', 'upsert', '2026-09-07')")
        elif change == "backfill":
            conn.execute("UPDATE operational_memory_search_state SET backfill_cursor='', backfill_target='memory_3'")
        else:
            record = _integrity_test_records()[0]
            record["text"] = "Changed canonical memory"
            conn.execute("UPDATE operational_memory SET data_json=? WHERE memory_id=?", (json.dumps(record), record["memory_id"]))
    calls = _count_scans(monkeypatch)
    assert _quick()["ready"] is False
    assert calls == []


@pytest.mark.parametrize("deep", [False, True])
def test_default_health_still_attests_and_detects_equal_count_tamper(deep, isolated_memory, monkeypatch):
    _ready(monkeypatch)
    calls = _count_scans(monkeypatch)
    healthy = runtime_health.assess_runtime_health(deep_projection_checks=deep)
    assert healthy["detail"]["operational_memory_search"]["ready"] is True
    assert calls == [1]
    _tamper_memory_fts_with_equal_counts("memory_3")
    failed = runtime_health.assess_runtime_health(deep_projection_checks=deep)
    status = failed["detail"]["operational_memory_search"]
    assert status["ready"] is False
    assert status["canonical_documents"] == status["indexed_documents"] == 4
    assert "operational_memory_search_integrity" in status["warnings"]
    assert calls == [1, 1]


def test_quick_proof_is_not_a_deep_content_attestation(isolated_memory, monkeypatch):
    _ready(monkeypatch)
    _tamper_memory_fts_with_equal_counts("memory_3")
    status = _quick()
    assert status["integrity_verification_kind"] == "durable_proof"
    deep = runtime_health.assess_runtime_health(deep_projection_checks=True)
    assert deep["detail"]["operational_memory_search"]["ready"] is False


def test_existing_zero_interval_retrieval_attestation_contract_is_preserved(isolated_memory, monkeypatch):
    conn = _ready(monkeypatch)
    monkeypatch.setenv("VECTOR_LAKE_OPERATIONAL_MEMORY_ATTESTATION_SECONDS", "0")
    calls = _count_scans(monkeypatch)
    result = db_store.verify_operational_memory_search_integrity(
        conn, allow_full_scan=False, allow_durable_proof=True,
    )
    assert result["status"] == "ready" and result["verification_kind"] == "full"
    assert calls == [1]


@pytest.mark.parametrize("stronger_check", ["deep_projection_checks", "bounded_write_checks"])
def test_quick_flag_cannot_weaken_deep_or_write_health(stronger_check, isolated_memory, monkeypatch):
    _ready(monkeypatch)
    calls = _count_scans(monkeypatch)
    result = runtime_health.assess_runtime_health(bounded_memory_checks=True, **{stronger_check: True})
    assert result["detail"]["operational_memory_search"]["integrity_verification_kind"] == "full"
    assert calls == [1]


@pytest.mark.parametrize("pending", [False, True])
def test_deferred_attestation_does_not_require_auto_maintenance(pending, isolated_memory, monkeypatch):
    conn = _ready(monkeypatch)
    monkeypatch.setenv("VECTOR_LAKE_OPERATIONAL_MEMORY_ATTESTATION_SECONDS", "0")
    monkeypatch.setenv("VECTOR_LAKE_OPERATIONAL_MEMORY_AUTO_MAINTAIN", "0")
    if pending:
        with db_store.transaction():
            conn.execute("INSERT INTO operational_memory_search_pending (memory_id, operation, queued_at) VALUES ('memory_new', 'upsert', '2026-09-07')")
    result = json.loads(tool_doctor.quick_doctor_vector_lake())["infrastructure"]
    issue = "operational_memory_search_auto_maintenance_disabled"
    assert (issue in result["issues"]) is pending
    assert result["status"] == ("blocked" if pending else "degraded")
