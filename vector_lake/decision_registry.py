"""Verified CriticalDecisionRegistry adapter owned outside Vector Lake."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timezone
from typing import Any, Callable

from vector_lake.db_store import get_connection, init_db, transaction


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_decision(decision: dict[str, Any]) -> dict[str, Any]:
    required = (
        "decision_id",
        "title",
        "owner",
        "status",
        "risk_weight",
        "evidence_requirements",
        "verification",
    )
    missing = [field for field in required if decision.get(field) in (None, "", [])]
    if missing:
        raise ValueError(f"Critical decision missing required fields: {', '.join(missing)}")
    normalized = dict(decision)
    normalized["decision_id"] = str(decision["decision_id"]).strip()
    normalized["status"] = str(decision["status"]).strip().lower()
    if normalized["status"] not in {"draft", "active", "retired"}:
        raise ValueError(f"Unsupported critical decision status: {normalized['status']}")
    normalized["risk_weight"] = float(decision["risk_weight"])
    if not 1 <= normalized["risk_weight"] <= 100:
        raise ValueError("Critical decision risk_weight must be between 1 and 100")
    for field in ("evidence_requirements", "policy_refs", "claim_refs"):
        values = decision.get(field) or []
        if not isinstance(values, list):
            raise ValueError(f"Critical decision {field} must be a list")
        normalized[field] = list(
            dict.fromkeys(str(value).strip() for value in values if str(value).strip())
        )
    normalized["verification"] = str(decision["verification"]).strip()
    return normalized


def sync_verified_registry_document(
    payload_text: str,
    *,
    expected_sha256: str,
    actor_id: str,
) -> dict[str, Any]:
    """Import an operator-pinned registry snapshot with a durable receipt."""
    normalized_hash = str(expected_sha256 or "").strip().lower()
    normalized_actor = str(actor_id or "").strip()
    if len(normalized_hash) != 64 or any(char not in "0123456789abcdef" for char in normalized_hash):
        raise ValueError("expected_sha256 must be a 64-character lowercase SHA-256 digest")
    if not normalized_actor:
        raise ValueError("actor_id must be a non-empty operator identifier")
    actual_hash = hashlib.sha256(str(payload_text).encode("utf-8")).hexdigest()
    if not hmac.compare_digest(actual_hash, normalized_hash):
        raise ValueError(
            f"CriticalDecisionRegistry digest mismatch: expected {normalized_hash}, got {actual_hash}"
        )
    try:
        payload = json.loads(payload_text)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("CriticalDecisionRegistry payload is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("CriticalDecisionRegistry payload must be an object")

    verified_at = _utc_now()
    result = sync_critical_decision_registry(
        payload,
        verification_validator=lambda decision: bool(decision.get("verification")),
    )
    conn = get_connection()
    with transaction():
        for decision in payload.get("decisions") or []:
            decision_id = str(decision.get("decision_id") or "").strip()
            row = conn.execute(
                "SELECT data_json FROM critical_decision_registry WHERE decision_id = ?",
                (decision_id,),
            ).fetchone()
            if row is None:
                continue
            stored = json.loads(row["data_json"])
            stored["registry_import"] = {
                "file_sha256": actual_hash,
                "actor_id": normalized_actor,
                "verified_at": verified_at,
            }
            conn.execute(
                "UPDATE critical_decision_registry SET data_json = ?, updated_at = ? "
                "WHERE decision_id = ?",
                (json.dumps(stored, ensure_ascii=False, sort_keys=True), verified_at, decision_id),
            )
    return {
        **result,
        "file_sha256": actual_hash,
        "actor_id": normalized_actor,
        "verified_at": verified_at,
    }


def sync_critical_decision_registry(
    payload: dict[str, Any],
    *,
    verification_validator: Callable[[dict[str, Any]], bool] | None = None,
) -> dict[str, Any]:
    """Import a verified external snapshot without claiming business ownership."""
    if not isinstance(payload, dict):
        raise ValueError("CriticalDecisionRegistry payload must be an object")
    registry_version = str(payload.get("contract_version") or "").strip()
    decisions = payload.get("decisions")
    if registry_version != "1.0" or not isinstance(decisions, list):
        raise ValueError("CriticalDecisionRegistry requires contract_version 1.0 and decisions[]")
    if verification_validator is None:
        raise ValueError("CriticalDecisionRegistry import requires a verification_validator")
    normalized = [_normalize_decision(decision) for decision in decisions]
    if len({item["decision_id"] for item in normalized}) != len(normalized):
        raise ValueError("CriticalDecisionRegistry contains duplicate decision_id values")
    for decision in normalized:
        if verification_validator(decision) is not True:
            raise ValueError(
                f"Critical decision verification failed: {decision['decision_id']}"
            )
        decision["registry_verification_status"] = "verified"

    init_db()
    conn = get_connection()
    now = _utc_now()
    with transaction():
        for decision in normalized:
            conn.execute(
                "INSERT INTO critical_decision_registry "
                "(decision_id, registry_version, status, risk_weight, verification, data_json, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(decision_id) DO UPDATE SET registry_version = excluded.registry_version, "
                "status = excluded.status, risk_weight = excluded.risk_weight, "
                "verification = excluded.verification, data_json = excluded.data_json, "
                "updated_at = excluded.updated_at",
                (
                    decision["decision_id"],
                    registry_version,
                    decision["status"],
                    decision["risk_weight"],
                    decision["verification"],
                    json.dumps(decision, ensure_ascii=False, sort_keys=True),
                    now,
                ),
            )
    return {"registry_version": registry_version, "synced": len(normalized), "updated_at": now}


def get_critical_decision(decision_id: str) -> dict[str, Any] | None:
    init_db()
    row = get_connection().execute(
        "SELECT data_json, registry_version, updated_at FROM critical_decision_registry "
        "WHERE decision_id = ?",
        (str(decision_id or "").strip(),),
    ).fetchone()
    if row is None:
        return None
    decision = json.loads(row["data_json"])
    decision["registry_version"] = row["registry_version"]
    decision["registry_updated_at"] = row["updated_at"]
    decision["registry_verified"] = (
        decision.get("registry_verification_status") == "verified"
    )
    return decision


def verified_decision_refs(references: list[str]) -> list[str]:
    refs = list(dict.fromkeys(str(value).strip() for value in references if str(value).strip()))
    if not refs:
        return []
    init_db()
    placeholders = ",".join("?" for _ in refs)
    rows = get_connection().execute(
        "SELECT decision_id FROM critical_decision_registry "
        f"WHERE decision_id IN ({placeholders}) AND status = 'active' "
        "AND json_extract(data_json, '$.registry_verification_status') = 'verified'",
        tuple(refs),
    ).fetchall()
    active = {str(row["decision_id"]) for row in rows}
    return [reference for reference in refs if reference in active]
