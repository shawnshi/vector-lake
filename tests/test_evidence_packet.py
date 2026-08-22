import json
from pathlib import Path

import pytest
from jsonschema.validators import Draft202012Validator

from vector_lake import db_store, governance_store
from vector_lake.claim_assessment import record_claim_assessment
from vector_lake.cli_app import build_parser
from vector_lake.tool_evidence import build_evidence_packet, export_evidence_packet


def _insert_packet_records():
    db_store.init_db()
    claim = {
        "claim_id": "claim_cbss_1",
        "claim_text": "Milestone M1 was accepted.",
        "claim_type": "fact",
        "claim_scope": "block",
        "status": "Active",
        "confidence": 0.91,
        "subject_entity_ids": ["contract_1"],
        "evidence_ids": ["evidence_cbss_1"],
        "source_ids": ["source_cbss_1"],
        "locator": {"page_key": "Concept_CBSS-Test", "heading": "Acceptance", "block_index": 1},
        "source_page": "Concept_CBSS-Test.md",
        "updated_at": "2026-07-21T00:00:00+00:00",
    }
    evidence = {
        "evidence_id": "evidence_cbss_1",
        "source_id": "source_cbss_1",
        "locator": {
            "page_key": "Concept_CBSS-Test",
            "page": 4,
            "paragraph": 2,
        },
        "evidence_text": "Signed acceptance record " + ("x" * 40),
        "evidence_type": "document-block",
        "supports_claim_ids": ["claim_cbss_1"],
        "contradicts_claim_ids": [],
    }
    source = {
        "source_id": "source_cbss_1",
        "raw_ref": "raw/contracts/acceptance.pdf",
        "canonical_source_page": "Source_Acceptance.md",
        "source_type": "pdf",
        "title": "Acceptance Record",
        "content_hash": "abc123",
    }
    entity = {
        "entity_id": "entity_cbss_1",
        "canonical_name": "CBSS Test",
        "page_key": "Concept_CBSS-Test",
        "title": "CBSS Test",
        "type": "concept",
        "status": "Active",
    }
    governance_store.apply_change_set(
        {
            "affected_pages": ["Concept_CBSS-Test.md"],
            "proposed_entities": [entity],
            "proposed_claims": [claim],
            "proposed_evidence": [evidence],
            "proposed_source_updates": [source],
            "proposed_edges": [],
        }
    )


def test_evidence_packet_is_read_only_candidate_without_text_by_default(isolated_memory):
    _insert_packet_records()

    packet = build_evidence_packet("claim_cbss_1")

    assert packet["packet_id"].startswith("ep_")
    assert packet["contract_version"] == "1.1"
    assert packet["disposition"] == {
        "state": "claim_candidate",
        "accepted_fact": False,
        "authority_required": True,
    }
    assert packet["claim"]["validity_state"] == "active"
    assert packet["provenance"]["evidence_complete"] is True
    assert packet["provenance"]["source_complete"] is True
    assert packet["provenance"]["source_integrity_complete"] is False
    assert packet["provenance"]["raw_locator_complete"] is False
    assert packet["provenance"]["assessment_complete"] is False
    assert packet["provenance"]["canonical_page_version"]
    assert "evidence_text" not in packet["evidence"][0]
    assert len(packet["evidence"][0]["evidence_text_hash"]) == 64


def test_evidence_packet_text_requires_explicit_opt_in_and_is_bounded(isolated_memory):
    _insert_packet_records()

    packet = build_evidence_packet(
        "claim_cbss_1",
        include_evidence_text=True,
        max_evidence_text_chars=12,
        actor_id="reviewer:test",
        purpose="acceptance review",
    )

    assert packet["evidence"][0]["evidence_text"] == "Signed accep"
    assert packet["evidence"][0]["evidence_text_truncated"] is True
    assert packet["provenance"]["evidence_text_export"] == {
        "included": True,
        "actor_id": "reviewer:test",
        "purpose": "acceptance review",
    }


def test_evidence_text_export_requires_actor_and_purpose(isolated_memory):
    _insert_packet_records()

    with pytest.raises(ValueError, match="requires non-empty actor_id and purpose"):
        build_evidence_packet("claim_cbss_1", include_evidence_text=True)


def test_evidence_packet_reports_missing_references(isolated_memory):
    db_store.init_db()
    claim = {
        "claim_id": "claim_missing_refs",
        "claim_text": "Unresolved claim",
        "status": "Active",
        "confidence": 0.5,
        "evidence_ids": ["missing_evidence"],
        "source_ids": ["missing_source"],
        "locator": {"page_key": "Concept_Missing-References"},
    }
    governance_store.apply_change_set(
        {
            "affected_pages": ["Concept_Missing-References.md"],
            "proposed_entities": [],
            "proposed_claims": [claim],
            "proposed_evidence": [],
            "proposed_source_updates": [],
            "proposed_edges": [],
        }
    )

    packet = build_evidence_packet(claim["claim_id"])

    assert packet["provenance"]["evidence_complete"] is False
    assert packet["provenance"]["source_complete"] is False
    assert "missing_evidence:missing_evidence" in packet["provenance"]["warnings"]
    assert "missing_source:missing_source" in packet["provenance"]["warnings"]


def test_claim_assessment_is_append_only_and_does_not_promote_fact(isolated_memory):
    _insert_packet_records()
    reviewed_packet = build_evidence_packet("claim_cbss_1")
    claim_version = reviewed_packet["claim"]["claim_version"]
    assessment = record_claim_assessment(
        "claim_cbss_1",
        assessment_type="evidence_review",
        outcome="supported",
        actor_id="reviewer:test",
        method_version="review-v1",
        reason="Source and locator were reviewed.",
        expected_claim_version=claim_version,
    )
    replay = record_claim_assessment(
        "claim_cbss_1",
        assessment_type="evidence_review",
        outcome="supported",
        actor_id="reviewer:test",
        method_version="review-v1",
        reason="Source and locator were reviewed.",
        expected_claim_version=claim_version,
    )

    packet = build_evidence_packet("claim_cbss_1")

    assert assessment["assessment_id"] == replay["assessment_id"]
    assert packet["assessments"][0]["outcome"] == "supported"
    assert packet["provenance"]["assessment_complete"] is True
    assert packet["disposition"]["accepted_fact"] is False
    assert db_store.get_connection().execute(
        "SELECT COUNT(*) FROM claim_assessments"
    ).fetchone()[0] == 1


def test_claim_assessment_rejects_stale_claim_version(isolated_memory):
    _insert_packet_records()

    with pytest.raises(ValueError, match="Claim version changed before assessment"):
        record_claim_assessment(
            "claim_cbss_1",
            assessment_type="evidence_review",
            outcome="supported",
            actor_id="reviewer:test",
            method_version="review-v1",
            reason="Reviewed stale material.",
            expected_claim_version="sha256:stale",
        )


def test_evidence_packet_rejects_unknown_claim_and_invalid_text_limit(isolated_memory):
    db_store.init_db()
    with pytest.raises(ValueError, match="Claim not found"):
        build_evidence_packet("missing")
    with pytest.raises(ValueError, match="between 1 and 10000"):
        build_evidence_packet("missing", max_evidence_text_chars=0)


def test_evidence_packet_cli_and_mcp_surfaces_are_registered(isolated_memory):
    args = build_parser().parse_args(["evidence-packet", "claim_cbss_1"])
    assert args.claim_id == "claim_cbss_1"

    from vector_lake import mcp_server

    assert callable(mcp_server.export_evidence_packet)


def test_evidence_packet_schema_is_valid_json():
    schema_path = Path(__file__).resolve().parents[1] / "contracts" / "cbss" / "evidence-packet.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    assert schema["properties"]["disposition"]["properties"]["accepted_fact"]["const"] is False


def test_generated_evidence_packet_matches_contract(isolated_memory):
    _insert_packet_records()
    schema_path = Path(__file__).resolve().parents[1] / "contracts" / "cbss" / "evidence-packet.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))

    Draft202012Validator(schema).validate(build_evidence_packet("claim_cbss_1"))


def test_export_evidence_packet_returns_json(isolated_memory):
    _insert_packet_records()
    assert json.loads(export_evidence_packet("claim_cbss_1"))["claim"]["claim_id"] == "claim_cbss_1"
