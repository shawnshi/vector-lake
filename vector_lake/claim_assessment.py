"""Append-only knowledge assessments for canonical claims.

An assessment records review evidence about a claim.  It is not a business
AcceptedFact and does not mutate the canonical claim or any CBSS authority
projection.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from vector_lake.db_store import get_connection, init_db, transaction


ALLOWED_OUTCOMES = {
    "supported",
    "unsupported",
    "contradicted",
    "inconclusive",
    "needs_review",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def record_claim_assessment(
    claim_id: str,
    *,
    assessment_type: str,
    outcome: str,
    actor_id: str,
    method_version: str,
    reason: str,
    details: dict[str, Any] | None = None,
    assessment_id: str | None = None,
) -> dict[str, Any]:
    normalized = {
        "claim_id": str(claim_id or "").strip(),
        "assessment_type": str(assessment_type or "").strip(),
        "outcome": str(outcome or "").strip().lower(),
        "actor_id": str(actor_id or "").strip(),
        "method_version": str(method_version or "").strip(),
        "reason": str(reason or "").strip(),
        "details": dict(details or {}),
    }
    missing = [
        field
        for field in ("claim_id", "assessment_type", "actor_id", "method_version", "reason")
        if not normalized[field]
    ]
    if missing:
        raise ValueError(f"ClaimAssessment missing required fields: {', '.join(missing)}")
    if normalized["outcome"] not in ALLOWED_OUTCOMES:
        raise ValueError(f"Unsupported ClaimAssessment outcome: {normalized['outcome']}")

    init_db()
    conn = get_connection()
    if conn.execute(
        "SELECT 1 FROM claims WHERE claim_id = ?", (normalized["claim_id"],)
    ).fetchone() is None:
        raise ValueError(f"Claim not found: {normalized['claim_id']}")

    identity_json = json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    normalized["assessment_id"] = str(assessment_id or "").strip() or (
        "assessment_" + hashlib.sha256(identity_json.encode("utf-8")).hexdigest()[:24]
    )
    normalized["recorded_at"] = _utc_now()
    serialized = json.dumps(normalized, ensure_ascii=False, sort_keys=True)
    with transaction():
        existing = conn.execute(
            "SELECT data_json FROM claim_assessments WHERE assessment_id = ?",
            (normalized["assessment_id"],),
        ).fetchone()
        if existing is not None:
            previous = json.loads(existing["data_json"])
            comparable_previous = {key: previous.get(key) for key in normalized if key != "recorded_at"}
            comparable_new = {key: value for key, value in normalized.items() if key != "recorded_at"}
            if comparable_previous != comparable_new:
                raise ValueError(
                    f"ClaimAssessment id collision with different content: {normalized['assessment_id']}"
                )
            return previous
        conn.execute(
            "INSERT INTO claim_assessments "
            "(assessment_id, claim_id, assessment_type, outcome, actor_id, method_version, "
            "data_json, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                normalized["assessment_id"],
                normalized["claim_id"],
                normalized["assessment_type"],
                normalized["outcome"],
                normalized["actor_id"],
                normalized["method_version"],
                serialized,
                normalized["recorded_at"],
            ),
        )
    return normalized


def list_claim_assessments(claim_id: str) -> list[dict[str, Any]]:
    init_db()
    rows = get_connection().execute(
        "SELECT data_json FROM claim_assessments WHERE claim_id = ? "
        "ORDER BY recorded_at, assessment_id",
        (str(claim_id or "").strip(),),
    ).fetchall()
    return [json.loads(row["data_json"]) for row in rows]
