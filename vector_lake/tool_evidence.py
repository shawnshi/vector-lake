"""Read-only CBSS evidence packet export from canonical Vector Lake records."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from vector_lake import governance_store
from vector_lake.claim_assessment import list_claim_assessments
from vector_lake.db_store import get_connection, init_db
from vector_lake.governance_metrics import infer_claim_validity


EVIDENCE_PACKET_CONTRACT_VERSION = "1.1"
DEFAULT_EVIDENCE_TEXT_LIMIT = 2000
MAX_EVIDENCE_TEXT_LIMIT = 10000


def _unique_strings(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    return list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))


def _load_json_records(table: str, id_column: str, identifiers: list[str]) -> tuple[list[dict], list[str]]:
    if table not in {"evidence", "sources"}:
        raise ValueError(f"Unsupported canonical table: {table}")
    if not identifiers:
        return [], []

    conn = get_connection()
    records_by_id: dict[str, dict] = {}
    for offset in range(0, len(identifiers), 500):
        batch = identifiers[offset : offset + 500]
        placeholders = ",".join("?" for _ in batch)
        rows = conn.execute(
            f"SELECT {id_column}, data_json, updated_at FROM {table} "
            f"WHERE {id_column} IN ({placeholders})",
            tuple(batch),
        ).fetchall()
        for row in rows:
            try:
                record = json.loads(row["data_json"])
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Canonical {table} record {row[id_column]} contains invalid JSON") from exc
            record.setdefault(id_column, str(row[id_column]))
            record.setdefault("updated_at", row["updated_at"])
            records_by_id[str(row[id_column])] = record

    return (
        [records_by_id[identifier] for identifier in identifiers if identifier in records_by_id],
        [identifier for identifier in identifiers if identifier not in records_by_id],
    )


def _canonical_claim(claim_id: str) -> dict:
    init_db()
    row = get_connection().execute(
        "SELECT claim_id, claim_text, status, data_json, updated_at FROM claims WHERE claim_id = ?",
        (claim_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"Claim not found: {claim_id}")
    try:
        claim = json.loads(row["data_json"])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Canonical claim {claim_id} contains invalid JSON") from exc
    claim.setdefault("claim_id", str(row["claim_id"]))
    claim.setdefault("claim_text", str(row["claim_text"] or ""))
    claim.setdefault("status", str(row["status"] or ""))
    claim.setdefault("updated_at", row["updated_at"])
    return claim


def _project_claim(claim: dict) -> dict:
    validity = infer_claim_validity(claim)
    fields = (
        "claim_id",
        "claim_text",
        "claim_type",
        "claim_scope",
        "status",
        "confidence",
        "confidence_kind",
        "calibrated_probability",
        "assessment_status",
        "extractor_name",
        "extractor_version",
        "extraction_run_id",
        "subject_entity_ids",
        "evidence_ids",
        "source_ids",
        "locator",
        "temporal_anchor",
        "valid_from",
        "valid_to",
        "created_at",
        "updated_at",
        "source_page",
    )
    projected = {field: claim.get(field) for field in fields if field in claim}
    projected["validity_state"] = claim.get("validity_state") or validity["validity_state"]
    projected["validity_reasons"] = claim.get("validity_reasons") or validity["reasons"]
    return projected


def _project_evidence(record: dict, include_text: bool, text_limit: int) -> dict:
    projected = {
        key: record.get(key)
        for key in (
            "evidence_id",
            "source_id",
            "locator",
            "projection_locator",
            "source_locator",
            "artifact_id",
            "extraction_run_id",
            "independence_status",
            "lineage_safe",
            "evidence_type",
            "created_at",
            "updated_at",
            "supports_claim_ids",
            "contradicts_claim_ids",
        )
        if key in record
    }
    text = str(record.get("evidence_text") or "")
    projected["evidence_text_hash"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
    if include_text:
        projected["evidence_text"] = text[:text_limit]
        projected["evidence_text_truncated"] = len(text) > text_limit
    return projected


def _project_source(record: dict) -> dict:
    return {
        key: record.get(key)
        for key in (
            "source_id",
            "raw_ref",
            "canonical_source_page",
            "source_type",
            "title",
            "ingested_at",
            "content_hash",
            "hash_algorithm",
            "byte_size",
            "mime_type",
            "storage_uri",
            "integrity_status",
            "artifact_id",
            "classification",
            "retention_policy",
            "legal_hold",
            "lineage_id",
            "generation_parent_refs",
            "updated_at",
        )
        if key in record
    }


def build_evidence_packet(
    claim_id: str,
    include_evidence_text: bool = False,
    max_evidence_text_chars: int = DEFAULT_EVIDENCE_TEXT_LIMIT,
    actor_id: str = "",
    purpose: str = "",
) -> dict:
    """Build a deterministic, read-only EvidencePacket for one canonical claim."""
    normalized_claim_id = str(claim_id or "").strip()
    if not normalized_claim_id:
        raise ValueError("claim_id must be a non-empty string")
    text_limit = int(max_evidence_text_chars)
    if not 1 <= text_limit <= MAX_EVIDENCE_TEXT_LIMIT:
        raise ValueError(
            f"max_evidence_text_chars must be between 1 and {MAX_EVIDENCE_TEXT_LIMIT}"
        )
    normalized_actor_id = str(actor_id or "").strip()
    normalized_purpose = str(purpose or "").strip()
    if include_evidence_text and (not normalized_actor_id or not normalized_purpose):
        raise ValueError(
            "Evidence text export requires non-empty actor_id and purpose"
        )

    claim = _canonical_claim(normalized_claim_id)
    evidence_ids = _unique_strings(claim.get("evidence_ids"))
    source_ids = _unique_strings(claim.get("source_ids"))
    evidence_records, missing_evidence_ids = _load_json_records(
        "evidence", "evidence_id", evidence_ids
    )
    evidence_source_ids = [
        str(record.get("source_id") or "").strip()
        for record in evidence_records
        if str(record.get("source_id") or "").strip()
    ]
    source_ids = list(dict.fromkeys([*source_ids, *evidence_source_ids]))
    source_records, missing_source_ids = _load_json_records("sources", "source_id", source_ids)
    assessments = list_claim_assessments(normalized_claim_id)

    source_page = str(
        claim.get("source_page")
        or (claim.get("locator") or {}).get("page_key")
        or ""
    ).strip()
    page_key = Path(source_page).stem if source_page else ""
    canonical_version = (
        governance_store.canonical_page_versions({page_key}).get(page_key, "")
        if page_key
        else ""
    )

    warnings: list[str] = []
    if not evidence_ids:
        warnings.append("claim_has_no_evidence_refs")
    if not source_ids:
        warnings.append("claim_has_no_source_refs")
    warnings.extend(f"missing_evidence:{identifier}" for identifier in missing_evidence_ids)
    warnings.extend(f"missing_source:{identifier}" for identifier in missing_source_ids)
    if source_page and not canonical_version:
        warnings.append(f"canonical_page_version_unavailable:{page_key}")
    integrity_complete = bool(source_records) and all(
        record.get("integrity_status") == "verified"
        and isinstance(record.get("content_hash"), str)
        and len(record["content_hash"]) == 64
        for record in source_records
    )
    raw_locator_complete = bool(evidence_records) and all(
        isinstance(record.get("source_locator"), dict)
        and record["source_locator"].get("kind") != "unresolved"
        for record in evidence_records
    )
    lineage_safe = bool(evidence_records) and all(
        record.get("lineage_safe") is True for record in evidence_records
    )
    if not integrity_complete:
        warnings.append("source_integrity_unverified")
    if not raw_locator_complete:
        warnings.append("raw_source_locator_incomplete")
    if not lineage_safe:
        warnings.append("evidence_lineage_unverified")
    if not assessments:
        warnings.append("claim_unassessed")

    packet_body = {
        "contract_version": EVIDENCE_PACKET_CONTRACT_VERSION,
        "packet_type": "claim_evidence",
        "disposition": {
            "state": "claim_candidate",
            "accepted_fact": False,
            "authority_required": True,
        },
        "claim": _project_claim(claim),
        "evidence": [
            _project_evidence(record, include_evidence_text, text_limit)
            for record in evidence_records
        ],
        "sources": [_project_source(record) for record in source_records],
        "assessments": assessments,
        "provenance": {
            "canonical_store": "sqlite",
            "source_page": source_page,
            "canonical_page_version": canonical_version,
            "evidence_complete": not missing_evidence_ids and bool(evidence_ids),
            "source_complete": not missing_source_ids and bool(source_ids),
            "source_integrity_complete": integrity_complete,
            "raw_locator_complete": raw_locator_complete,
            "assessment_complete": bool(assessments),
            "lineage_safe": lineage_safe,
            "evidence_text_export": {
                "included": bool(include_evidence_text),
                "actor_id": normalized_actor_id or None,
                "purpose": normalized_purpose or None,
            },
            "warnings": warnings,
        },
    }
    serialized = json.dumps(
        packet_body,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    packet = {
        "packet_id": "ep_" + hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:24],
        "generated_at": datetime.now(timezone.utc).isoformat(),
        **packet_body,
    }
    return packet


def export_evidence_packet(
    claim_id: str,
    include_evidence_text: bool = False,
    max_evidence_text_chars: int = DEFAULT_EVIDENCE_TEXT_LIMIT,
    actor_id: str = "",
    purpose: str = "",
) -> str:
    """Serialize one EvidencePacket for CLI and MCP consumers."""
    return json.dumps(
        build_evidence_packet(
            claim_id,
            include_evidence_text=include_evidence_text,
            max_evidence_text_chars=max_evidence_text_chars,
            actor_id=actor_id,
            purpose=purpose,
        ),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
