from concurrent.futures import ThreadPoolExecutor

import pytest

from vector_lake import governance_service, governance_store
from vector_lake.tool_review import _combined_pending_items, _format_combined_report


def _raise_full_queue_access(*_args, **_kwargs):
    raise AssertionError("full governance queue access is forbidden for row-level mutation")


def test_concurrent_enqueue_uses_row_level_writes(isolated_memory, monkeypatch):
    monkeypatch.setattr(governance_store, "load_governance_queue", _raise_full_queue_access)
    monkeypatch.setattr(governance_store, "save_governance_queue", _raise_full_queue_access)

    def enqueue(index):
        return governance_store.enqueue_governance_item(
            "suggestion",
            f"Candidate {index}",
            "Concurrent candidate",
            "test",
            [],
            [f"Concept_{index}.md"],
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        items = list(pool.map(enqueue, range(12)))

    rows = governance_store.get_connection().execute(
        "SELECT item_id, data_json FROM governance_queue ORDER BY item_id"
    ).fetchall()
    assert len(rows) == 12
    assert len({item["item_id"] for item in items}) == 12


def test_resolve_updates_one_row_without_overwriting_peer(isolated_memory, monkeypatch):
    target = governance_store.enqueue_governance_item(
        "suggestion", "Target", "Resolve me", "test", [], ["Concept_Target.md"]
    )
    peer = governance_store.enqueue_governance_item(
        "suggestion", "Peer", "Keep me", "test", [], ["Concept_Peer.md"]
    )
    monkeypatch.setattr(governance_store, "load_governance_queue", _raise_full_queue_access)
    monkeypatch.setattr(governance_store, "save_governance_queue", _raise_full_queue_access)

    resolved = governance_service.resolve_governance_item(target["item_id"], resolution="skip")

    assert resolved["status"] == "resolved"
    assert governance_store.get_governance_item(peer["item_id"])["status"] == "pending"
    assert governance_store.get_governance_item(target["item_id"])["resolution"] == "skip"


def test_legacy_queue_save_cannot_delete_rows_missing_from_snapshot(isolated_memory):
    left = governance_store.enqueue_governance_item(
        "suggestion", "Left", "Left", "test", [], ["Concept_Left.md"]
    )
    right = governance_store.enqueue_governance_item(
        "suggestion", "Right", "Right", "test", [], ["Concept_Right.md"]
    )

    governance_store.save_governance_queue({"items": [left]})

    assert governance_store.get_governance_item(left["item_id"]) is not None
    assert governance_store.get_governance_item(right["item_id"]) is not None


def test_governance_priority_uses_explicit_critical_decision_refs(isolated_memory):
    from vector_lake.decision_registry import sync_critical_decision_registry

    sync_critical_decision_registry({
        "contract_version": "1.0",
        "decisions": [{
            "decision_id": "CD-PAY-001",
            "title": "Payment acceptance",
            "owner": "contract-owner",
            "status": "active",
            "risk_weight": 90,
            "evidence_requirements": ["signed acceptance"],
            "claim_refs": [],
            "verification": "cbss-registry-signature:test",
        }],
    }, verification_validator=lambda decision: decision["verification"].startswith(
        "cbss-registry-signature:"
    ))
    generic = governance_store.enqueue_governance_item(
        "community_naming", "Generic", "Naming", "test", [], ["System_Generic.md"]
    )
    critical = governance_store.enqueue_governance_item(
        "suggestion",
        "Payment decision evidence",
        "Missing acceptance evidence",
        "test",
        [],
        ["Concept_Payment.md"],
        critical_decision_refs=["CD-PAY-001"],
    )
    contradiction = governance_store.enqueue_governance_item(
        "contradiction", "Conflict", "Conflicting sources", "test", [], []
    )

    items = _combined_pending_items()

    assert [item["item_id"] for item in items] == [
        critical["item_id"],
        contradiction["item_id"],
        generic["item_id"],
    ]
    assert [item["priority"] for item in items] == ["P0", "P1", "P3"]
    assert critical["decision_relevance"] == "critical"
    report = _format_combined_report(items)
    assert "Priority: P0" in report
    assert "Critical decisions: CD-PAY-001" in report


def test_unverified_decision_reference_does_not_auto_escalate_to_p0(isolated_memory):
    item = governance_store.enqueue_governance_item(
        "suggestion",
        "Unverified decision reference",
        "No registry record exists.",
        "test",
        [],
        [],
        critical_decision_refs=["CD-UNKNOWN"],
    )

    assert item["priority"] == "P3"
    assert item["decision_relevance"] == "unverified"
    assert item["verified_critical_decision_refs"] == []
    assert item["unverified_critical_decision_refs"] == ["CD-UNKNOWN"]


def test_registry_import_rejects_unvalidated_verification_text(isolated_memory):
    from vector_lake.decision_registry import sync_critical_decision_registry

    payload = {
        "contract_version": "1.0",
        "decisions": [{
            "decision_id": "CD-UNVERIFIED",
            "title": "Unverified",
            "owner": "owner:test",
            "status": "active",
            "risk_weight": 50,
            "evidence_requirements": ["claim"],
            "verification": "merely-non-empty",
        }],
    }
    with pytest.raises(ValueError, match="requires a verification_validator"):
        sync_critical_decision_registry(payload)
    with pytest.raises(ValueError, match="verification failed"):
        sync_critical_decision_registry(
            payload,
            verification_validator=lambda decision: False,
        )


def test_explicit_governance_priority_overrides_default(isolated_memory):
    item = governance_store.enqueue_governance_item(
        "community_naming",
        "Explicit",
        "Explicit priority",
        "test",
        [],
        [],
        priority="P1",
        critical_decision_refs=["CD-EXPLICIT"],
    )

    assert item["priority"] == "P1"
    assert item["priority_score"] == 300


def test_critical_decision_registry_contract_is_valid_json():
    from pathlib import Path
    import json

    schema_path = (
        Path(__file__).resolve().parents[1]
        / "contracts"
        / "cbss"
        / "critical-decision-registry.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    assert schema["properties"]["decisions"]["items"]["properties"]["risk_weight"]["maximum"] == 100
