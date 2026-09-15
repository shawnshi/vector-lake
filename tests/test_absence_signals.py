"""Absence must not read as health on the operator surfaces.

Each signal here was a zero/empty value that the runtime scored as healthy: no
maintenance backup, a tripped-but-not-open breaker, and a truncated projection
object scan.  All three are invisible in a synthetic fixture, so they are
asserted directly.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from vector_lake import auto_ingest_worker, db_store
from vector_lake.runtime_health import assess_runtime_health


def _state(**overrides) -> None:
    state = auto_ingest_worker._empty_state()
    state.update(overrides)
    auto_ingest_worker._save_state(state)


def test_zero_maintenance_backups_is_reported_for_a_real_corpus(
    isolated_memory, monkeypatch
):
    db_store.init_db()
    monkeypatch.setenv("VECTOR_LAKE_MAINTENANCE_BACKUP_MODE", "full")
    # The live corpus is 3.6 GiB; the floor exists only so a fresh install with
    # nothing to protect is not reported as unprotected.
    monkeypatch.setenv("VECTOR_LAKE_BACKUP_ABSENCE_MIN_DATABASE_BYTES", "1")

    health = assess_runtime_health()

    assert health["detail"]["backup_capacity"]["current_backup_bytes"] == 0
    assert (
        "maintenance_backup_absent:current=0<database_bytes="
        + str(health["detail"]["storage"]["database_bytes"])
        in health["warnings"]
    )
    assert health["status"] != "ready"


def test_paused_maintenance_backup_policy_is_not_reported_as_absent(
    isolated_memory, monkeypatch
):
    db_store.init_db()
    monkeypatch.setenv("VECTOR_LAKE_MAINTENANCE_BACKUP_MODE", "skip")
    monkeypatch.setenv("VECTOR_LAKE_BACKUP_ABSENCE_MIN_DATABASE_BYTES", "1")

    health = assess_runtime_health()

    assert not any(
        warning.startswith("maintenance_backup_absent:")
        for warning in health["warnings"]
    )


def test_backup_absence_floor_keeps_small_corpora_quiet(
    isolated_memory, monkeypatch
):
    db_store.init_db()
    monkeypatch.setenv("VECTOR_LAKE_MAINTENANCE_BACKUP_MODE", "full")
    monkeypatch.setenv(
        "VECTOR_LAKE_BACKUP_ABSENCE_MIN_DATABASE_BYTES", str(1 << 40)
    )

    health = assess_runtime_health()

    assert not any(
        warning.startswith("maintenance_backup_absent:")
        for warning in health["warnings"]
    )


def test_tripped_breaker_is_reported_even_while_the_window_is_closed(
    isolated_memory,
):
    db_store.init_db()
    now = datetime.now(timezone.utc)
    _state(
        consecutive_infra_failures=10,
        circuit_open_until=(now - timedelta(hours=4)).isoformat(),
    )

    health = assess_runtime_health()

    assert health["detail"]["auto_ingest_budget"]["circuit"]["is_open"] is False
    assert (
        "auto_ingest_circuit_tripped:failures=10>=3:open=False"
        in health["warnings"]
    )


def test_healthy_breaker_state_is_not_reported(isolated_memory):
    db_store.init_db()
    _state(consecutive_infra_failures=0, circuit_open_until=None)

    health = assess_runtime_health()

    assert not any(
        warning.startswith("auto_ingest_circuit_tripped:")
        for warning in health["warnings"]
    )


def test_truncated_projection_object_scan_is_surfaced(isolated_memory):
    from vector_lake.storage_growth import record_storage_growth_sample

    db_store.init_db()
    meta = isolated_memory / "wiki" / ".meta"
    common = {
        "sampled_at": "2026-09-15T00:00:00Z",
        "database_bytes": 100,
        "wal_bytes": 0,
        "row_counts": {"claim_versions": 1, "evidence_versions": 2},
        "version_payload_bytes": 10,
        "backup_bytes": 10,
        "backup_files": 1,
        "backup_scan_complete": True,
    }
    record_storage_growth_sample(
        meta_dir=meta,
        sample={
            **common,
            "date_utc": "2026-09-14",
            "projection_object_scan_complete": True,
            "projection_object_error": None,
        },
    )
    record_storage_growth_sample(
        meta_dir=meta,
        sample={
            **common,
            "date_utc": "2026-09-15",
            "projection_object_scan_complete": False,
            "projection_object_error": "projection_object_file_limit_exceeded",
        },
    )

    health = assess_runtime_health()

    assert (
        "projection_object_scan_incomplete:projection_object_file_limit_exceeded"
        in health["warnings"]
    )


def test_complete_projection_object_scan_is_not_warned_about(isolated_memory):
    from vector_lake.storage_growth import record_storage_growth_sample

    db_store.init_db()
    meta = isolated_memory / "wiki" / ".meta"
    sample = {
        "date_utc": "2026-09-15",
        "sampled_at": "2026-09-15T00:00:00Z",
        "database_bytes": 100,
        "wal_bytes": 0,
        "row_counts": {"claim_versions": 1, "evidence_versions": 2},
        "version_payload_bytes": 10,
        "backup_bytes": 10,
        "backup_files": 1,
        "backup_scan_complete": True,
        "projection_object_scan_complete": True,
        "projection_object_error": None,
    }
    record_storage_growth_sample(meta_dir=meta, sample=sample)

    health = assess_runtime_health()

    assert not any(
        warning.startswith("projection_object_scan_incomplete:")
        for warning in health["warnings"]
    )


def test_doctor_report_contains_the_new_absence_signals(
    isolated_memory, monkeypatch
):
    from vector_lake.tool_doctor import quick_doctor_vector_lake

    db_store.init_db()
    monkeypatch.setenv("VECTOR_LAKE_MAINTENANCE_BACKUP_MODE", "full")
    monkeypatch.setenv("VECTOR_LAKE_BACKUP_ABSENCE_MIN_DATABASE_BYTES", "1")
    now = datetime.now(timezone.utc)
    _state(
        consecutive_infra_failures=10,
        circuit_open_until=(now - timedelta(hours=4)).isoformat(),
    )

    payload = json.loads(quick_doctor_vector_lake())
    warnings = payload["infrastructure"]["warnings"]

    assert any(
        warning.startswith("maintenance_backup_absent:") for warning in warnings
    )
    assert (
        "auto_ingest_circuit_tripped:failures=10>=3:open=False" in warnings
    )


def test_absence_signals_stay_out_of_the_write_gate(isolated_memory, monkeypatch):
    """Advisory findings must not block canonical writes.

    ``assess_runtime_health()["ok"]`` is ``not issues``, and
    ``enforce_runtime_write_health`` turns a false ``ok`` into a RuntimeError on
    every ordinary mutation.  Both new signals describe conditions that say
    nothing about whether a canonical write is safe, so they must be reported in
    the advisory tier and never in the gate-driving one.
    """
    from vector_lake import runtime_health

    db_store.init_db()
    monkeypatch.setenv("VECTOR_LAKE_MAINTENANCE_BACKUP_MODE", "full")
    monkeypatch.setenv("VECTOR_LAKE_BACKUP_ABSENCE_MIN_DATABASE_BYTES", "1")
    now = datetime.now(timezone.utc)
    _state(
        consecutive_infra_failures=10,
        circuit_open_until=(now - timedelta(hours=4)).isoformat(),
    )

    health = assess_runtime_health()

    gate_driving = ("maintenance_backup_absent", "auto_ingest_circuit_tripped")
    for prefix in gate_driving:
        assert any(
            warning.startswith(prefix) for warning in health["warnings"]
        ), prefix
        assert not any(
            issue.startswith(prefix) for issue in health["issues"]
        ), f"{prefix} would block the write gate"

    # The gate itself must not name either signal.
    try:
        runtime_health.enforce_runtime_write_health()
    except RuntimeError as exc:
        assert "maintenance_backup_absent" not in str(exc)
        assert "auto_ingest_circuit_tripped" not in str(exc)


def test_unreadable_budget_ledger_is_surfaced(isolated_memory, monkeypatch):
    """``auto_ingest_budget_status`` fails closed with no ``limits``/``circuit``.

    The breaker comparison cannot run in that state, so the ledger's own issue
    used to be dropped entirely.
    """
    db_store.init_db()
    auto_ingest_worker._state_path().write_text(
        json.dumps(
            {
                "schema_version": 1,
                "launches": [],
                # Out of the validated 0..10 range, so the ledger is rejected.
                "consecutive_infra_failures": 11,
                "circuit_open_until": None,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        ),
        encoding="utf-8",
    )

    health = assess_runtime_health()

    assert health["detail"]["auto_ingest_budget"]["status"] == "blocked"
    assert any(
        warning.startswith("auto_ingest_budget:budget_ledger_unavailable:")
        for warning in health["warnings"]
    ), health["warnings"]


def test_expired_subagent_lease_is_visible_and_advisory(isolated_memory):
    """A crashed consumer leaves a job leased; nothing reaped or reported it.

    ``expire_stale_subagent_jobs`` only scans ``awaiting_subagent``, and
    ``release_ingest_subagent_task_claim`` needs a live lease, so a dead owner
    strands the job indefinitely with every surface healthy (38 hours observed).
    It stays claimable, so the signal must be advisory.
    """
    from datetime import datetime, timezone

    db_store.init_db()
    conn = db_store.get_connection()
    now = datetime.now(timezone.utc)
    conn.execute(
        "INSERT INTO jobs(job_id, task_type, payload, status, retries, created_at, "
        "updated_at, lease_owner, lease_token, lease_generation, lease_until) "
        "VALUES ('stranded', 'ingest', '{}', 'subagent_processing', 0, ?, ?, "
        "'auto-ingest:1', 'tok', 1, ?)",
        (
            (now - timedelta(days=2)).isoformat(),
            (now - timedelta(days=2)).isoformat(),
            (now - timedelta(hours=38)).isoformat(),
        ),
    )
    conn.commit()

    health = assess_runtime_health()

    assert health["detail"]["expired_subagent_leases"] == 1
    assert "subagent_lease_expired:1" in health["warnings"]
    assert not any(
        issue.startswith("subagent_lease_expired:") for issue in health["issues"]
    ), "an expired lease is still claimable; it must not block the write gate"
    assert (
        health["detail"]["expired_subagent_lease_recovery"]["oldest_lease_until"]
    )


def test_live_subagent_lease_is_not_reported(isolated_memory):
    from datetime import datetime, timedelta, timezone

    db_store.init_db()
    conn = db_store.get_connection()
    now = datetime.now(timezone.utc)
    conn.execute(
        "INSERT INTO jobs(job_id, task_type, payload, status, retries, created_at, "
        "updated_at, lease_owner, lease_token, lease_generation, lease_until) "
        "VALUES ('healthy', 'ingest', '{}', 'subagent_processing', 0, ?, ?, "
        "'host:1', 'tok', 1, ?)",
        (now.isoformat(), now.isoformat(), (now + timedelta(hours=1)).isoformat()),
    )
    conn.commit()

    health = assess_runtime_health()

    assert health["detail"]["expired_subagent_leases"] == 0
    assert not any(
        warning.startswith("subagent_lease_expired:")
        for warning in health["warnings"]
    )
