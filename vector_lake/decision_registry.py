"""Verified CriticalDecisionRegistry adapter owned outside Vector Lake."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from vector_lake.db_store import get_connection, init_db, transaction


_TRUST_POLICY_ENV = "VECTOR_LAKE_DECISION_REGISTRY_TRUST_POLICY"
_TRUST_POLICY_CONTRACT = "vector-lake-registry-trust/v1"
_REGISTRY_RECEIPT_CONTRACT = "vector-lake-registry-import-receipt/v1"
_ACTOR_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._@-]{0,127}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_MAX_TRUST_POLICY_BYTES = 65_536


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


def _canonical_json(payload: Any) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _load_trusted_snapshot(actor_id: str, digest: str) -> dict[str, str]:
    """Resolve an operator-controlled actor+digest pin outside the MCP request."""
    policy_value = os.environ.get(_TRUST_POLICY_ENV, "").strip()
    if not policy_value:
        raise ValueError(
            f"CriticalDecisionRegistry requires operator trust policy {_TRUST_POLICY_ENV}"
        )
    policy_path = Path(policy_value).expanduser().resolve()
    try:
        size = policy_path.stat().st_size
    except OSError as exc:
        raise ValueError("CriticalDecisionRegistry trust policy is unavailable") from exc
    if size > _MAX_TRUST_POLICY_BYTES:
        raise ValueError("CriticalDecisionRegistry trust policy exceeds the hard size limit")
    try:
        policy_bytes = policy_path.read_bytes()
        policy = json.loads(policy_bytes.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("CriticalDecisionRegistry trust policy is unreadable") from exc
    if not isinstance(policy, dict) or policy.get("contract_version") != _TRUST_POLICY_CONTRACT:
        raise ValueError("Unsupported CriticalDecisionRegistry trust policy contract")
    policy_id = str(policy.get("policy_id") or "").strip()
    snapshots = policy.get("trusted_snapshots")
    if not policy_id or not isinstance(snapshots, list):
        raise ValueError(
            "CriticalDecisionRegistry trust policy requires policy_id and trusted_snapshots[]"
        )
    matches = [
        snapshot
        for snapshot in snapshots
        if isinstance(snapshot, dict)
        and snapshot.get("status") == "active"
        and hmac.compare_digest(str(snapshot.get("actor_id") or ""), actor_id)
        and hmac.compare_digest(str(snapshot.get("sha256") or "").casefold(), digest)
    ]
    if len(matches) != 1:
        raise ValueError(
            "CriticalDecisionRegistry actor and digest are not uniquely pinned by the operator trust policy"
        )
    return {
        "policy_id": policy_id,
        "policy_sha256": hashlib.sha256(policy_bytes).hexdigest(),
    }


def _normalize_registry_payload(
    payload: dict[str, Any],
    *,
    verification_validator: Callable[[dict[str, Any]], bool] | None = None,
    externally_pinned: bool = False,
) -> tuple[str, list[dict[str, Any]]]:
    if not isinstance(payload, dict):
        raise ValueError("CriticalDecisionRegistry payload must be an object")
    registry_version = str(payload.get("contract_version") or "").strip()
    decisions = payload.get("decisions")
    if registry_version != "1.0" or not isinstance(decisions, list):
        raise ValueError("CriticalDecisionRegistry requires contract_version 1.0 and decisions[]")
    if not externally_pinned and verification_validator is None:
        raise ValueError("CriticalDecisionRegistry import requires a verification_validator")
    normalized = [_normalize_decision(decision) for decision in decisions]
    if len({item["decision_id"] for item in normalized}) != len(normalized):
        raise ValueError("CriticalDecisionRegistry contains duplicate decision_id values")
    for decision in normalized:
        if not externally_pinned and verification_validator(decision) is not True:
            raise ValueError(
                f"Critical decision verification failed: {decision['decision_id']}"
            )
        decision["registry_verification_status"] = "verified"
    return registry_version, normalized


def _store_registry_snapshot(
    registry_version: str,
    normalized: list[dict[str, Any]],
    *,
    registry_import: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist the snapshot and optional receipt in one database transaction."""
    init_db()
    conn = get_connection()
    now = _utc_now()
    with transaction():
        for decision in normalized:
            stored = dict(decision)
            if registry_import is not None:
                stored["registry_import"] = dict(registry_import)
            conn.execute(
                "INSERT INTO critical_decision_registry "
                "(decision_id, registry_version, status, risk_weight, verification, data_json, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(decision_id) DO UPDATE SET registry_version = excluded.registry_version, "
                "status = excluded.status, risk_weight = excluded.risk_weight, "
                "verification = excluded.verification, data_json = excluded.data_json, "
                "updated_at = excluded.updated_at",
                (
                    stored["decision_id"],
                    registry_version,
                    stored["status"],
                    stored["risk_weight"],
                    stored["verification"],
                    json.dumps(stored, ensure_ascii=False, sort_keys=True),
                    now,
                ),
            )
    return {"registry_version": registry_version, "synced": len(normalized), "updated_at": now}


def sync_verified_registry_document(
    payload_text: str,
    *,
    expected_sha256: str,
    actor_id: str,
) -> dict[str, Any]:
    """Import an externally pinned registry snapshot with an atomic receipt."""
    if not isinstance(payload_text, str):
        raise ValueError("CriticalDecisionRegistry payload must be UTF-8 text")
    normalized_hash = str(expected_sha256 or "").strip().lower()
    normalized_actor = str(actor_id or "").strip()
    if not _SHA256_PATTERN.fullmatch(normalized_hash):
        raise ValueError("expected_sha256 must be a 64-character lowercase SHA-256 digest")
    if not _ACTOR_ID_PATTERN.fullmatch(normalized_actor):
        raise ValueError("actor_id must be a valid operator identifier")
    actual_hash = hashlib.sha256(payload_text.encode("utf-8")).hexdigest()
    if not hmac.compare_digest(actual_hash, normalized_hash):
        raise ValueError(
            f"CriticalDecisionRegistry digest mismatch: expected {normalized_hash}, got {actual_hash}"
        )
    trust = _load_trusted_snapshot(normalized_actor, actual_hash)
    try:
        payload = json.loads(payload_text)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("CriticalDecisionRegistry payload is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("CriticalDecisionRegistry payload must be an object")

    verified_at = _utc_now()
    receipt_core = {
        "contract_version": _REGISTRY_RECEIPT_CONTRACT,
        "file_sha256": actual_hash,
        "actor_id": normalized_actor,
        "verified_at": verified_at,
        "verification_method": "operator-pinned-actor-and-digest",
        **trust,
    }
    registry_import = {
        **receipt_core,
        "receipt_sha256": hashlib.sha256(
            _canonical_json(receipt_core).encode("utf-8")
        ).hexdigest(),
    }
    registry_version, normalized = _normalize_registry_payload(
        payload,
        externally_pinned=True,
    )
    result = _store_registry_snapshot(
        registry_version,
        normalized,
        registry_import=registry_import,
    )
    return {
        **result,
        "file_sha256": actual_hash,
        "actor_id": normalized_actor,
        "verified_at": verified_at,
        "receipt": registry_import,
    }


def sync_critical_decision_registry(
    payload: dict[str, Any],
    *,
    verification_validator: Callable[[dict[str, Any]], bool] | None = None,
) -> dict[str, Any]:
    """Import a verified external snapshot without claiming business ownership."""
    registry_version, normalized = _normalize_registry_payload(
        payload,
        verification_validator=verification_validator,
    )
    return _store_registry_snapshot(registry_version, normalized)


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
