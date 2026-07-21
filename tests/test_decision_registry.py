from __future__ import annotations

import hashlib
import json

import pytest

from vector_lake.decision_registry import (
    get_critical_decision,
    sync_verified_registry_document,
)


def _payload_text() -> str:
    return json.dumps(
        {
            "contract_version": "1.0",
            "decisions": [
                {
                    "decision_id": "CD-PINNED-001",
                    "title": "Pinned decision",
                    "owner": "owner:test",
                    "status": "active",
                    "risk_weight": 75,
                    "evidence_requirements": ["verified claim"],
                    "claim_refs": [],
                    "verification": "external-registry-record:1",
                }
            ],
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def test_registry_document_requires_matching_digest_and_records_operator(isolated_memory):
    payload_text = _payload_text()
    digest = hashlib.sha256(payload_text.encode("utf-8")).hexdigest()

    receipt = sync_verified_registry_document(
        payload_text,
        expected_sha256=digest,
        actor_id="operator:test",
    )
    decision = get_critical_decision("CD-PINNED-001")

    assert receipt["synced"] == 1
    assert receipt["file_sha256"] == digest
    assert decision["registry_verified"] is True
    assert decision["registry_import"]["actor_id"] == "operator:test"
    assert decision["registry_import"]["file_sha256"] == digest


def test_registry_document_rejects_digest_mismatch(isolated_memory):
    with pytest.raises(ValueError, match="digest mismatch"):
        sync_verified_registry_document(
            _payload_text(),
            expected_sha256="0" * 64,
            actor_id="operator:test",
        )
