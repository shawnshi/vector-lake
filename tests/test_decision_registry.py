from __future__ import annotations

import hashlib
import json

import pytest

from vector_lake import db_store
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


def _trust_policy(tmp_path, monkeypatch, digest, actor="operator:test"):
    policy = tmp_path / "registry-trust.json"
    policy.write_text(
        json.dumps(
            {
                "contract_version": "vector-lake-registry-trust/v1",
                "policy_id": "test-policy-v1",
                "trusted_snapshots": [
                    {
                        "actor_id": actor,
                        "sha256": digest,
                        "status": "active",
                    }
                ],
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("VECTOR_LAKE_DECISION_REGISTRY_TRUST_POLICY", str(policy))
    return policy


def test_registry_document_requires_matching_digest_and_records_operator(
    isolated_memory,
    tmp_path,
    monkeypatch,
):
    payload_text = _payload_text()
    digest = hashlib.sha256(payload_text.encode("utf-8")).hexdigest()
    policy = _trust_policy(tmp_path, monkeypatch, digest)

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
    assert decision["registry_import"]["policy_id"] == "test-policy-v1"
    assert decision["registry_import"]["policy_sha256"] == hashlib.sha256(
        policy.read_bytes()
    ).hexdigest()
    assert receipt["receipt"]["receipt_sha256"] == decision["registry_import"][
        "receipt_sha256"
    ]


def test_registry_document_rejects_digest_mismatch(isolated_memory):
    with pytest.raises(ValueError, match="digest mismatch"):
        sync_verified_registry_document(
            _payload_text(),
            expected_sha256="0" * 64,
            actor_id="operator:test",
        )


def test_registry_document_rejects_self_pinned_digest_without_operator_policy(
    isolated_memory,
    monkeypatch,
):
    monkeypatch.delenv("VECTOR_LAKE_DECISION_REGISTRY_TRUST_POLICY", raising=False)
    payload_text = _payload_text()
    digest = hashlib.sha256(payload_text.encode("utf-8")).hexdigest()

    with pytest.raises(ValueError, match="requires operator trust policy"):
        sync_verified_registry_document(
            payload_text,
            expected_sha256=digest,
            actor_id="operator:test",
        )


def test_registry_document_rejects_actor_not_bound_to_trusted_digest(
    isolated_memory,
    tmp_path,
    monkeypatch,
):
    payload_text = _payload_text()
    digest = hashlib.sha256(payload_text.encode("utf-8")).hexdigest()
    _trust_policy(tmp_path, monkeypatch, digest, actor="operator:trusted")

    with pytest.raises(ValueError, match="not uniquely pinned"):
        sync_verified_registry_document(
            payload_text,
            expected_sha256=digest,
            actor_id="operator:attacker",
        )


def test_registry_document_rolls_back_snapshot_and_receipt_in_one_transaction(
    isolated_memory,
    tmp_path,
    monkeypatch,
):
    payload = json.loads(_payload_text())
    payload["decisions"].append(
        {
            **payload["decisions"][0],
            "decision_id": "CD-FAIL-002",
            "title": "Injected failure",
        }
    )
    payload_text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    digest = hashlib.sha256(payload_text.encode("utf-8")).hexdigest()
    _trust_policy(tmp_path, monkeypatch, digest)
    db_store.init_db()
    conn = db_store.get_connection()
    conn.execute(
        "CREATE TRIGGER reject_test_registry_row BEFORE INSERT "
        "ON critical_decision_registry WHEN NEW.decision_id = 'CD-FAIL-002' "
        "BEGIN SELECT RAISE(ABORT, 'injected registry failure'); END"
    )
    conn.commit()

    with pytest.raises(Exception, match="injected registry failure"):
        sync_verified_registry_document(
            payload_text,
            expected_sha256=digest,
            actor_id="operator:test",
        )

    assert conn.execute(
        "SELECT COUNT(*) FROM critical_decision_registry"
    ).fetchone()[0] == 0
