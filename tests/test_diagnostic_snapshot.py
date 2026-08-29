import json
import sqlite3
from contextlib import contextmanager

import pytest

from vector_lake import (
    db_store,
    diagnostic_snapshot,
    indexer,
    runtime_health,
    tool_doctor,
)


def _seed_committed_projection():
    db_store.init_db()
    indexer.generate_index()


def test_snapshot_metadata_is_generation_bound_and_path_private(isolated_memory):
    _seed_committed_projection()

    with diagnostic_snapshot.capture_diagnostic_snapshot() as snapshot:
        metadata = snapshot.metadata()

    assert metadata["contract_version"] == "vector-lake-diagnostic-snapshot/v1"
    assert metadata["captured_at"].endswith("+00:00")
    assert metadata["database"]["runtime_generations"]
    assert metadata["projection"]["generation"]
    assert metadata["wiki"]["file_count"] == 0
    assert metadata["generation_fingerprint"].startswith("sha256:")
    assert metadata["source_fingerprint"].startswith("sha256:")
    assert str(isolated_memory) not in json.dumps(metadata, ensure_ascii=False)


def test_runtime_semantic_and_doctor_reuse_one_open_snapshot(
    isolated_memory,
    monkeypatch,
):
    _seed_committed_projection()

    with diagnostic_snapshot.capture_diagnostic_snapshot() as snapshot:
        monkeypatch.setattr(
            runtime_health,
            "_open_runtime_database_read_only",
            lambda: (_ for _ in ()).throw(
                AssertionError("must reuse the diagnostic DB transaction")
            ),
        )
        monkeypatch.setattr(
            runtime_health,
            "_index_snapshot",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("must reuse the committed projection snapshot")
            ),
        )
        monkeypatch.setattr(
            indexer,
            "read_committed_index_snapshot",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("must not recapture the committed projection")
            ),
        )
        monkeypatch.setattr(
            tool_doctor,
            "_read_database_state",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("must reuse the diagnostic DB transaction")
            ),
        )

        health = runtime_health.assess_runtime_health(
            diagnostic_snapshot=snapshot
        )
        semantic = runtime_health.assess_semantic_readiness(
            diagnostic_snapshot=snapshot
        )
        doctor = tool_doctor.doctor_vector_lake(diagnostic_snapshot=snapshot)

        assert snapshot.connection.execute("SELECT 1").fetchone()[0] == 1

    fingerprint = snapshot.metadata()["source_fingerprint"]
    assert health["detail"]["diagnostic_snapshot"]["source_fingerprint"] == fingerprint
    assert semantic["detail"]["diagnostic_snapshot"]["source_fingerprint"] == fingerprint
    assert f"source_fingerprint={fingerprint}" in doctor


def test_doctor_owns_exactly_one_db_and_projection_capture(monkeypatch):
    _seed_committed_projection()
    calls = {"database": 0, "projection": 0}
    real_db_snapshot = db_store.read_only_transaction_snapshot
    real_projection_snapshot = indexer.read_committed_index_snapshot

    @contextmanager
    def counted_db_snapshot(*args, **kwargs):
        calls["database"] += 1
        with real_db_snapshot(*args, **kwargs) as connection:
            yield connection

    def counted_projection_snapshot(*args, **kwargs):
        calls["projection"] += 1
        return real_projection_snapshot(*args, **kwargs)

    monkeypatch.setattr(
        db_store,
        "read_only_transaction_snapshot",
        counted_db_snapshot,
    )
    monkeypatch.setattr(
        indexer,
        "read_committed_index_snapshot",
        counted_projection_snapshot,
    )

    report = tool_doctor.doctor_vector_lake()

    assert calls == {"database": 1, "projection": 1}
    assert "[OK] Diagnostic Snapshot:" in report


@pytest.mark.parametrize("surface", ["wiki", "projection"])
def test_external_surface_drift_fails_closed_as_snapshot_changed(
    isolated_memory,
    surface,
):
    from vector_lake.wiki_utils import get_projection_manifest_path, get_wiki_dir

    _seed_committed_projection()

    with pytest.raises(
        diagnostic_snapshot.DiagnosticSnapshotChanged,
        match="snapshot_changed",
    ):
        with diagnostic_snapshot.capture_diagnostic_snapshot():
            if surface == "wiki":
                (get_wiki_dir() / "Concept_Drift.md").write_text(
                    "# drift",
                    encoding="utf-8",
                )
            else:
                get_projection_manifest_path().write_text("{}", encoding="utf-8")


def test_external_surface_drift_across_capture_barrier_fails_closed(
    monkeypatch,
):
    _seed_committed_projection()
    real_identity = diagnostic_snapshot._capture_external_identity
    calls = 0

    def drifting_identity():
        nonlocal calls
        calls += 1
        identity = real_identity()
        if calls == 2:
            return {
                **identity,
                "wiki": identity["wiki"] + (("capture-drift",),),
            }
        return identity

    monkeypatch.setattr(
        diagnostic_snapshot,
        "_capture_external_identity",
        drifting_identity,
    )

    with pytest.raises(
        diagnostic_snapshot.DiagnosticSnapshotChanged,
        match="snapshot_changed",
    ):
        with diagnostic_snapshot.capture_diagnostic_snapshot():
            pass


def test_database_reader_barrier_observes_all_n_then_all_n_plus_one(
    isolated_memory,
):
    _seed_committed_projection()

    with diagnostic_snapshot.capture_diagnostic_snapshot() as first:
        generation_n = first.database_runtime_generations["governance_queue"]
        external = sqlite3.connect(str(db_store.peek_db_path()))
        try:
            external.execute(
                "UPDATE runtime_generations SET generation = generation + 1 "
                "WHERE surface = 'governance_queue'"
            )
            external.commit()
        finally:
            external.close()
        pinned = first.connection.execute(
            "SELECT generation FROM runtime_generations "
            "WHERE surface = 'governance_queue'"
        ).fetchone()[0]
        assert pinned == generation_n

    with diagnostic_snapshot.capture_diagnostic_snapshot() as second:
        generation_n_plus_one = second.database_runtime_generations[
            "governance_queue"
        ]

    assert generation_n_plus_one == generation_n + 1


def test_database_snapshot_timeout_fails_closed(monkeypatch):
    _seed_committed_projection()

    @contextmanager
    def timed_out_snapshot(*_args, **_kwargs):
        raise db_store.ReadOnlySnapshotUnavailable(
            "database_read_only_snapshot_unavailable:database is locked"
        )
        yield  # pragma: no cover

    monkeypatch.setattr(
        db_store,
        "read_only_transaction_snapshot",
        timed_out_snapshot,
    )

    with pytest.raises(
        diagnostic_snapshot.DiagnosticSnapshotUnavailable,
        match="snapshot_timeout",
    ):
        with diagnostic_snapshot.capture_diagnostic_snapshot():
            pass

    report = tool_doctor.doctor_vector_lake()
    assert "[FAIL] Diagnostic Snapshot: snapshot_timeout" in report


@pytest.mark.parametrize(
    ("configured", "profile", "warning", "issue", "doctor_state"),
    [
        ("full", "full", None, None, "OK"),
        (
            "best_effort",
            "best_effort",
            "durability_profile_best_effort",
            None,
            "WARN",
        ),
        (
            "invalid-value",
            "invalid",
            None,
            "durability_profile_invalid",
            "FAIL",
        ),
    ],
)
def test_durability_profile_is_visible_in_health_and_doctor(
    monkeypatch,
    configured,
    profile,
    warning,
    issue,
    doctor_state,
):
    _seed_committed_projection()
    monkeypatch.setenv("VECTOR_LAKE_DURABILITY_PROFILE", configured)

    with diagnostic_snapshot.capture_diagnostic_snapshot() as snapshot:
        health = runtime_health.assess_runtime_health(
            diagnostic_snapshot=snapshot
        )
        report = tool_doctor.doctor_vector_lake(diagnostic_snapshot=snapshot)

    assert health["detail"]["durability"]["profile"] == profile
    if warning:
        assert warning in health["warnings"]
        assert warning in report
    if issue:
        assert issue in health["issues"]
        assert issue in report
    assert f"[{doctor_state}] Durability: profile={profile}" in report
