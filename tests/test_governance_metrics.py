import pytest

from vector_lake import db_store, governance_store
from vector_lake.governance_metrics import (
    claim_governance_version,
    compute_debt_metrics,
    infer_claim_validity,
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
