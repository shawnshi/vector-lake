import hashlib
import sqlite3
from contextlib import contextmanager
from pathlib import Path

import pytest

from vector_lake import db_store, governance_store, tool_debt
from vector_lake.governance_metrics import (
    claim_governance_version,
    compute_debt_metrics,
    find_merge_candidate_report,
    find_merge_candidates,
    infer_claim_validity,
    read_only_governance_debt_snapshot,
)


def _publish_claim(claim: dict, page_key: str) -> None:
    claim["locator"] = {"page_key": page_key}
    governance_store.apply_change_set(
        {
            "affected_pages": [f"{page_key}.md"],
            "proposed_entities": [],
            "proposed_claims": [claim],
            "proposed_evidence": [],
            "proposed_source_updates": [],
            "proposed_edges": [],
        }
    )


def _database_identity(path: Path) -> dict:
    stat = path.stat()
    sidecars = tuple(
        candidate.name
        for candidate in (
            Path(str(path) + "-wal"),
            Path(str(path) + "-shm"),
        )
        if candidate.exists()
    )
    return {
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "sidecars": sidecars,
    }


def _fail_storage_initialization():
    raise AssertionError("read-only governance debt initialized storage")


def test_source_only_claim_is_provisional_not_unsupported():
    validity = infer_claim_validity({
        "status": "Active",
        "confidence": 0.8,
        "source_ids": ["source_primary"],
        "evidence_ids": [],
    })

    assert validity == {
        "validity_state": "provisional",
        "reasons": ["source_only_without_block_evidence"],
    }


def test_claim_without_source_or_evidence_is_unsupported():
    validity = infer_claim_validity({
        "status": "Active",
        "confidence": 0.8,
        "source_ids": [],
        "evidence_ids": [],
    })

    assert validity == {
        "validity_state": "unsupported",
        "reasons": ["missing_evidence_and_source"],
    }


def test_debt_metrics_stream_rows_without_full_store_loads(isolated_memory, monkeypatch):
    db_store.init_db()
    claim = {
        "claim_id": "claim_streaming",
        "claim_text": "Streaming debt metric",
        "status": "Active",
        "confidence": 0.8,
        "source_ids": [],
        "evidence_ids": [],
        "subject_entity_ids": [],
    }
    _publish_claim(claim, "Concept_Streaming-Metrics")
    for name in ("load_claims", "load_sources", "load_governance_queue", "load_memory_objects"):
        monkeypatch.setattr(
            governance_store,
            name,
            lambda: (_ for _ in ()).throw(AssertionError("full store load")),
        )

    metrics = compute_debt_metrics(skip_heavy=True)

    assert metrics["unsupported_claim_count"] == 1
    assert metrics["managed_unsupported_claim_count"] == 0
    assert metrics["unmanaged_unsupported_claim_count"] == 1
    assert metrics["validity_state_counts"]["unsupported"] == 1


def test_debt_metrics_read_only_bypasses_schema_initializer(
    isolated_memory,
    monkeypatch,
):
    db_store.init_db()
    db_store.close_all_connections()
    monkeypatch.setattr(
        governance_store,
        "initialize_meta_store",
        lambda: (_ for _ in ()).throw(
            AssertionError("read-only metrics initialized the database")
        ),
    )

    metrics = compute_debt_metrics(skip_heavy=True, read_only=True)

    assert metrics["unsupported_claim_count"] == 0


def test_debt_metrics_read_only_reads_committed_wal_without_mutating_files(
    isolated_memory,
):
    db_store.init_db()
    writer = db_store.get_connection()
    writer.execute("PRAGMA wal_autocheckpoint=0")
    claim = {
        "claim_id": "claim_wal_read_only",
        "claim_text": "Read-only metrics include committed WAL state",
        "status": "Active",
        "confidence": 0.8,
        "source_ids": [],
        "evidence_ids": [],
        "subject_entity_ids": [],
    }
    _publish_claim(claim, "Concept_WAL-Read-Only")
    db_path = db_store.get_db_path()
    wal_path = Path(str(db_path) + "-wal")
    assert wal_path.stat().st_size > 0

    def digest(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    before = {
        db_path: (db_path.stat().st_size, digest(db_path)),
        wal_path: (wal_path.stat().st_size, digest(wal_path)),
    }

    metrics = compute_debt_metrics(skip_heavy=True, read_only=True)

    after = {
        db_path: (db_path.stat().st_size, digest(db_path)),
        wal_path: (wal_path.stat().st_size, digest(wal_path)),
    }
    assert metrics["unsupported_claim_count"] == 1
    assert after == before


def test_debt_metrics_read_only_returns_zero_shape_without_creating_database(
    isolated_memory,
    monkeypatch,
):
    database_path = db_store.peek_db_path()
    assert not database_path.exists()
    monkeypatch.setattr(
        governance_store,
        "initialize_meta_store",
        lambda: (_ for _ in ()).throw(
            AssertionError("read-only metrics initialized the database")
        ),
    )

    metrics = compute_debt_metrics(skip_heavy=True, read_only=True)

    assert metrics["unsupported_claim_count"] == 0
    assert metrics["pending_change_set_count"] == 0
    assert metrics["memory_type_counts"] == {}
    assert metrics["validity_state_counts"] == {}
    assert not database_path.exists()


def test_debt_metrics_read_only_rejects_mutation_capable_heavy_paths(
    isolated_memory,
):
    db_store.init_db()

    with pytest.raises(ValueError, match="require skip_heavy=True"):
        compute_debt_metrics(read_only=True)


def test_governance_debt_missing_database_is_structured_and_side_effect_free(
    isolated_memory,
    monkeypatch,
):
    database_path = db_store.peek_db_path()
    forbidden_initializer = _fail_storage_initialization
    monkeypatch.setattr(db_store, "init_db", forbidden_initializer)
    monkeypatch.setattr(governance_store, "init_db", forbidden_initializer)
    monkeypatch.setattr(
        governance_store,
        "initialize_meta_store",
        forbidden_initializer,
    )

    snapshot = read_only_governance_debt_snapshot(limit=5)
    report = find_merge_candidate_report(
        limit=5,
        run_preflight=False,
        read_only=True,
    )
    candidates = find_merge_candidates(limit=5, run_preflight=False)
    dashboard = tool_debt.debt_vector_lake(top=5)

    assert snapshot["available"] is False
    assert snapshot["unavailable_reason"] == "database_missing"
    assert snapshot["metrics"]["unsupported_claim_count"] == 0
    assert report["available"] is False
    assert report["unavailable_reason"] == "database_missing"
    assert report["returned_count"] == 0
    assert candidates == []
    assert "availability: unavailable" in dashboard
    assert "unavailable_reason: database_missing" in dashboard
    assert not database_path.exists()
    assert not Path(str(database_path) + "-wal").exists()
    assert not Path(str(database_path) + "-shm").exists()
    assert not database_path.parent.exists()


def test_governance_debt_missing_tables_is_structured_and_read_only(
    isolated_memory,
    monkeypatch,
):
    database_path = db_store.peek_db_path()
    database_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE unrelated (value TEXT)")
    before = _database_identity(database_path)
    forbidden_initializer = _fail_storage_initialization
    monkeypatch.setattr(db_store, "init_db", forbidden_initializer)
    monkeypatch.setattr(governance_store, "init_db", forbidden_initializer)
    monkeypatch.setattr(
        governance_store,
        "initialize_meta_store",
        forbidden_initializer,
    )

    snapshot = read_only_governance_debt_snapshot(limit=5)
    report = find_merge_candidate_report(
        limit=5,
        run_preflight=False,
        read_only=True,
    )

    assert snapshot["available"] is False
    assert snapshot["metrics"]["unavailable_reason"] == "missing_tables"
    assert "claims" in snapshot["metrics"]["missing_tables"]
    assert snapshot["merge_candidate_report"]["returned_count"] == 0
    assert report["available"] is False
    assert report["unavailable_reason"] == "missing_tables"
    assert report["missing_tables"] == ["entities"]
    assert _database_identity(database_path) == before


def test_debt_and_merge_candidates_use_authorized_read_only_snapshots_without_drift(
    isolated_memory,
    monkeypatch,
):
    db_store.init_db()
    database_path = db_store.get_db_path()
    before = _database_identity(database_path)
    real_snapshot = db_store.read_only_transaction_snapshot
    authorizer_actions = []
    snapshot_calls = []
    write_actions = {
        getattr(sqlite3, name)
        for name in (
            "SQLITE_ALTER_TABLE",
            "SQLITE_ATTACH",
            "SQLITE_CREATE_INDEX",
            "SQLITE_CREATE_TABLE",
            "SQLITE_CREATE_TEMP_INDEX",
            "SQLITE_CREATE_TEMP_TABLE",
            "SQLITE_CREATE_TEMP_TRIGGER",
            "SQLITE_CREATE_TEMP_VIEW",
            "SQLITE_CREATE_TRIGGER",
            "SQLITE_CREATE_VIEW",
            "SQLITE_DELETE",
            "SQLITE_DETACH",
            "SQLITE_DROP_INDEX",
            "SQLITE_DROP_TABLE",
            "SQLITE_DROP_TEMP_INDEX",
            "SQLITE_DROP_TEMP_TABLE",
            "SQLITE_DROP_TEMP_TRIGGER",
            "SQLITE_DROP_TEMP_VIEW",
            "SQLITE_DROP_TRIGGER",
            "SQLITE_DROP_VIEW",
            "SQLITE_INSERT",
            "SQLITE_REINDEX",
            "SQLITE_UPDATE",
        )
    }

    @contextmanager
    def guarded_snapshot(*args, **kwargs):
        snapshot_calls.append((args, kwargs))
        with real_snapshot(*args, **kwargs) as connection:
            def authorize(action, _arg1, _arg2, _database, _trigger):
                authorizer_actions.append(action)
                return (
                    sqlite3.SQLITE_DENY
                    if action in write_actions
                    else sqlite3.SQLITE_OK
                )

            connection.set_authorizer(authorize)
            try:
                yield connection
            finally:
                connection.set_authorizer(None)

    monkeypatch.setattr(
        db_store,
        "read_only_transaction_snapshot",
        guarded_snapshot,
    )
    forbidden_initializer = _fail_storage_initialization
    monkeypatch.setattr(db_store, "init_db", forbidden_initializer)
    monkeypatch.setattr(governance_store, "init_db", forbidden_initializer)
    monkeypatch.setattr(
        governance_store,
        "initialize_meta_store",
        forbidden_initializer,
    )

    dashboard = tool_debt.debt_vector_lake(top=5)

    assert "availability: available" in dashboard
    assert len(snapshot_calls) == 1
    assert find_merge_candidates(limit=5, run_preflight=False) == []
    assert len(snapshot_calls) == 2
    assert authorizer_actions
    assert not write_actions.intersection(authorizer_actions)
    assert _database_identity(database_path) == before


def test_debt_metrics_do_not_transfer_full_claim_or_evidence_payloads(
    isolated_memory,
):
    db_store.init_db()
    claim = {
        "claim_id": "claim_scalar_metrics",
        "claim_text": "Scalar governance metric",
        "status": "Active",
        "confidence": 0.8,
        "source_ids": [],
        "evidence_ids": [],
        "subject_entity_ids": [],
    }
    _publish_claim(claim, "Concept_Scalar-Metrics")
    conn = db_store.get_connection()
    statements = []
    conn.set_trace_callback(statements.append)
    try:
        compute_debt_metrics(skip_heavy=True)
    finally:
        conn.set_trace_callback(None)

    normalized = [" ".join(statement.casefold().split()) for statement in statements]
    assert not any(
        statement.startswith("select data_json from claims")
        for statement in normalized
    )
    assert not any(
        statement.startswith("select data_json from evidence")
        for statement in normalized
    )


def test_acknowledged_evidence_gap_is_managed_debt(isolated_memory):
    db_store.init_db()
    claim = {
        "claim_id": "claim_acknowledged",
        "claim_text": "Claim awaiting evidence",
        "status": "Active",
        "confidence": 0.8,
        "source_ids": [],
        "evidence_ids": [],
        "subject_entity_ids": [],
    }
    _publish_claim(claim, "Concept_Acknowledged-Debt")
    governance_store.upsert_governance_item(
        {
            "item_id": "gov_evidence_claim_acknowledged",
            "type": "evidence-gap",
            "status": "acknowledged",
            "claim_id": claim["claim_id"],
            "claim_version": claim_governance_version(claim),
            "owner": "test-owner",
            "due_at": "2099-01-01T00:00:00+00:00",
        },
        insert_only=True,
    )

    metrics = compute_debt_metrics(skip_heavy=True)

    assert metrics["unsupported_claim_count"] == 1
    assert metrics["managed_unsupported_claim_count"] == 1
    assert metrics["unmanaged_unsupported_claim_count"] == 0


def test_stale_claim_version_does_not_count_as_managed_debt(isolated_memory):
    db_store.init_db()
    claim = {
        "claim_id": "claim_version_changed",
        "claim_text": "Current claim text",
        "status": "Active",
        "confidence": 0.8,
        "source_ids": [],
        "evidence_ids": [],
        "subject_entity_ids": [],
    }
    _publish_claim(claim, "Concept_Stale-Debt")
    governance_store.upsert_governance_item(
        {
            "item_id": "gov_stale_claim_version",
            "type": "evidence-gap",
            "status": "acknowledged",
            "claim_id": claim["claim_id"],
            "claim_version": "stale-version",
            "owner": "test-owner",
            "due_at": "2099-01-01T00:00:00+00:00",
        }
    )

    metrics = compute_debt_metrics(skip_heavy=True)

    assert metrics["managed_unsupported_claim_count"] == 0
    assert metrics["unmanaged_unsupported_claim_count"] == 1


def test_expired_missing_link_target_is_not_managed_debt(isolated_memory):
    db_store.init_db()
    governance_store.upsert_governance_item(
        {
            "item_id": "gov_missing_link_current",
            "type": "missing-link-target",
            "status": "acknowledged",
            "owner": "test-owner",
            "due_at": "2099-01-01T00:00:00+00:00",
        }
    )
    governance_store.upsert_governance_item(
        {
            "item_id": "gov_missing_link_expired",
            "type": "missing-link-target",
            "status": "acknowledged",
            "owner": "test-owner",
            "due_at": "2000-01-01T00:00:00+00:00",
        }
    )

    metrics = compute_debt_metrics(skip_heavy=True)

    assert metrics["acknowledged_missing_link_target_count"] == 2
    assert metrics["managed_missing_link_target_count"] == 1
    assert metrics["unmanaged_missing_link_target_count"] == 1


def test_source_referenced_through_evidence_is_not_counted_as_orphan(isolated_memory):
    db_store.init_db()
    source = {"source_id": "source_via_evidence"}
    evidence = {
        "evidence_id": "evidence_source_ref",
        "source_id": source["source_id"],
        "locator": {"page_key": "Concept_Evidence-Source-Ref"},
    }
    governance_store.apply_change_set(
        {
            "affected_pages": ["Concept_Evidence-Source-Ref.md"],
            "proposed_entities": [],
            "proposed_claims": [],
            "proposed_evidence": [evidence],
            "proposed_source_updates": [source],
            "proposed_edges": [],
        }
    )

    metrics = compute_debt_metrics(skip_heavy=True)

    assert metrics["orphan_source_count"] == 0


def test_claim_governance_version_ignores_foundation_backfill_metadata():
    claim = {
        "claim_id": "claim_1",
        "claim_text": "Stable business claim",
        "evidence_ids": [],
        "source_ids": [],
    }
    before = claim_governance_version(claim)
    claim.update({
        "claim_family_id": "claimfamily_1",
        "confidence_kind": "legacy_prior",
        "calibrated_probability": None,
        "assessment_status": "unreviewed",
        "extractor_name": "vector_lake.foundation_backfill",
        "extractor_version": "1.0",
        "extraction_run_id": "extractrun_1",
    })
    assert claim_governance_version(claim) == before


def test_claim_graph_projection_loads_only_bounded_claim_rows(isolated_memory, monkeypatch):
    db_store.init_db()
    claim = {
        "claim_id": "claim_graph_streaming",
        "claim_text": "Bounded graph claim",
        "claim_type": "claim",
        "status": "Active",
        "confidence": 0.8,
        "source_ids": [],
        "evidence_ids": [],
        "subject_entity_ids": [],
        "updated_at": "2026-07-19T00:00:00+00:00",
    }
    _publish_claim(claim, "Concept_Graph-Streaming")
    monkeypatch.setattr(
        governance_store,
        "annotated_claims",
        lambda: (_ for _ in ()).throw(AssertionError("full claim load")),
    )
    monkeypatch.setattr(
        governance_store,
        "load_entities",
        lambda: (_ for _ in ()).throw(AssertionError("full entity load")),
    )
    monkeypatch.setattr(
        governance_store,
        "load_sources",
        lambda: (_ for _ in ()).throw(AssertionError("full source load")),
    )

    graph = governance_store.build_claim_graph_projection(limit_nodes=10)

    assert [node["id"] for node in graph["nodes"]] == ["claim_graph_streaming"]
