"""Assessment CAS is enforced after BEGIN IMMEDIATE, including replay."""
import json
from contextlib import contextmanager

import pytest

from tests.test_claim_provenance_repair import _claim
from vector_lake import claim_assessment, db_store, governance_store
from vector_lake.governance_metrics import claim_governance_version


@pytest.fixture
def assessment_case(isolated_memory):
    db_store.init_db()
    governance_store.apply_change_set({
        "affected_pages": ["Concept_CAS.md"], "proposed_entities": [],
        "proposed_claims": [_claim("claim_cas", "Synthetic assessment claim", "Concept_CAS")],
        "proposed_evidence": [], "proposed_source_updates": [], "proposed_edges": [],
    })
    claim = governance_store.load_claims()["items"]["claim_cas"]
    return {
        "assessment_type": "semantic-review", "outcome": "supported",
        "actor_id": "operator:test", "method_version": "manual/v1",
        "reason": "Synthetic review", "assessment_id": "assessment_cas",
        "expected_claim_version": claim_governance_version(claim),
    }


@pytest.mark.parametrize("replay", [False, True])
@pytest.mark.parametrize("race", ["change", "delete"])
def test_assessment_transaction_time_cas_zero_writes(assessment_case, monkeypatch, replay, race):
    args = assessment_case
    if replay:
        claim_assessment.record_claim_assessment("claim_cas", **args)
    conn = db_store.get_connection()
    before = [tuple(row) for row in conn.execute("SELECT * FROM claim_assessments")]
    original = claim_assessment.transaction

    @contextmanager
    def racing_transaction():
        with original():
            if race == "change":
                conn.execute("UPDATE claims SET data_json=json_set(data_json, '$.claim_text', 'changed') WHERE claim_id='claim_cas'")
            else:
                conn.execute("DELETE FROM claims WHERE claim_id='claim_cas'")
            changes = conn.total_changes
            try:
                yield
            finally:
                assert conn.total_changes == changes

    monkeypatch.setattr(claim_assessment, "transaction", racing_transaction)
    with pytest.raises(ValueError, match="version changed|Claim not found"):
        claim_assessment.record_claim_assessment("claim_cas", **args)
    assert [tuple(row) for row in conn.execute("SELECT * FROM claim_assessments")] == before


def test_assessment_replay_and_legacy_signature(assessment_case):
    args = assessment_case
    first = claim_assessment.record_claim_assessment("claim_cas", **args)
    assert claim_assessment.record_claim_assessment("claim_cas", **args) == first
    conn = db_store.get_connection()
    conn.execute("UPDATE claims SET data_json=json_set(data_json, '$.claim_text', 'changed') WHERE claim_id='claim_cas'")
    conn.commit()
    with pytest.raises(ValueError, match="version changed"):
        claim_assessment.record_claim_assessment("claim_cas", **args)
    args.pop("expected_claim_version")
    args.pop("assessment_id")
    latest = claim_assessment.record_claim_assessment("claim_cas", **args)
    current = json.loads(conn.execute("SELECT data_json FROM claims WHERE claim_id='claim_cas'").fetchone()[0])
    assert latest["claim_version"] == claim_governance_version(current)
    assert latest["claim_version"] != first["claim_version"]
    assert len(claim_assessment.list_claim_assessments("claim_cas")) == 2
    assert "AcceptedFact" not in current
