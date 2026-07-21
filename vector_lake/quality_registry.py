"""Versioned knowledge dialect schemas and immutable quality evaluations."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from vector_lake.db_store import get_connection, init_db, transaction


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def register_schema(
    schema_id: str,
    version: str,
    *,
    dialect_id: str,
    schema: dict[str, Any],
    status: str = "active",
) -> dict[str, Any]:
    normalized_status = str(status).strip().lower()
    if normalized_status not in {"draft", "active", "retired"}:
        raise ValueError(f"Unsupported schema status: {status}")
    if not all(str(value or "").strip() for value in (schema_id, version, dialect_id)):
        raise ValueError("schema_id, version, and dialect_id are required")
    if not isinstance(schema, dict) or not schema:
        raise ValueError("schema must be a non-empty object")
    schema_json = json.dumps(schema, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    record = {
        "schema_id": str(schema_id).strip(),
        "version": str(version).strip(),
        "dialect_id": str(dialect_id).strip(),
        "status": normalized_status,
        "schema_hash": hashlib.sha256(schema_json.encode("utf-8")).hexdigest(),
        "schema": schema,
        "recorded_at": _utc_now(),
    }
    init_db()
    conn = get_connection()
    with transaction():
        existing = conn.execute(
            "SELECT schema_hash, data_json FROM schema_registry "
            "WHERE schema_id = ? AND version = ?",
            (record["schema_id"], record["version"]),
        ).fetchone()
        if existing is not None:
            previous = json.loads(existing["data_json"])
            if (
                existing["schema_hash"] != record["schema_hash"]
                or previous.get("dialect_id") != record["dialect_id"]
                or previous.get("status") != record["status"]
            ):
                raise ValueError(
                    f"Schema version is immutable: {record['schema_id']}@{record['version']}"
                )
            return previous
        conn.execute(
            "INSERT INTO schema_registry "
            "(schema_id, version, dialect_id, status, schema_hash, data_json, recorded_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                record["schema_id"],
                record["version"],
                record["dialect_id"],
                record["status"],
                record["schema_hash"],
                json.dumps(record, ensure_ascii=False, sort_keys=True),
                record["recorded_at"],
            ),
        )
    return record


def record_quality_evaluation(
    dataset_id: str,
    dataset_version: str,
    evaluator_version: str,
    *,
    metrics: dict[str, float],
    thresholds: dict[str, float],
    sample_count: int,
) -> dict[str, Any]:
    if not all(str(value or "").strip() for value in (dataset_id, dataset_version, evaluator_version)):
        raise ValueError("dataset_id, dataset_version, and evaluator_version are required")
    if int(sample_count) <= 0:
        raise ValueError("sample_count must be positive")
    normalized_metrics = {str(key): float(value) for key, value in metrics.items()}
    normalized_thresholds = {str(key): float(value) for key, value in thresholds.items()}
    missing = sorted(set(normalized_thresholds) - set(normalized_metrics))
    if missing:
        raise ValueError(f"Quality metrics missing threshold keys: {missing}")
    failures = {
        key: {"actual": normalized_metrics[key], "required": required}
        for key, required in normalized_thresholds.items()
        if normalized_metrics[key] < required
    }
    identity = json.dumps(
        {
            "dataset_id": dataset_id,
            "dataset_version": dataset_version,
            "evaluator_version": evaluator_version,
            "metrics": normalized_metrics,
            "thresholds": normalized_thresholds,
            "sample_count": int(sample_count),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    record = {
        "evaluation_id": "quality_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24],
        "dataset_id": str(dataset_id),
        "dataset_version": str(dataset_version),
        "evaluator_version": str(evaluator_version),
        "metrics": normalized_metrics,
        "thresholds": normalized_thresholds,
        "sample_count": int(sample_count),
        "failures": failures,
        "status": "pass" if not failures else "fail",
        "recorded_at": _utc_now(),
    }
    init_db()
    with transaction():
        get_connection().execute(
            "INSERT OR IGNORE INTO quality_evaluation_runs "
            "(evaluation_id, dataset_id, dataset_version, evaluator_version, status, data_json, recorded_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                record["evaluation_id"],
                record["dataset_id"],
                record["dataset_version"],
                record["evaluator_version"],
                record["status"],
                json.dumps(record, ensure_ascii=False, sort_keys=True),
                record["recorded_at"],
            ),
        )
    return record
