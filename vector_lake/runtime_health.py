"""Runtime health checks used by write gates and doctor surfaces."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from vector_lake.index_snapshot import (
    clear_index_snapshot_cache_for_tests,
    load_index_snapshot,
)

from typing import Any


_INDEX_PARITY_FIELDS = (
    "id",
    "title",
    "summary",
    "raw_text",
    "type",
    "domain",
    "topic_cluster",
    "status",
    "epistemic_status",
    "categories",
    "tags",
    "aliases",
    "relations",
    "sources",
    "tension_edges",
    "links",
    "outbound_links",
    "triples",
    "updated",
    "updated_at",
)

_CACHE_LOCK = threading.RLock()
_WRITE_GATE_DEEP_LOCK = threading.Lock()
_CANONICAL_CACHE: dict[str, Any] = {"key": None, "value": None}
_WRITE_GATE_CACHE: dict[str, Any] = {
    "token": None,
    "checked_at": 0.0,
    "health": None,
}
_SEMANTIC_READINESS_EVALUATION_LOCK = threading.Lock()
_SEMANTIC_READINESS_ENVELOPE_CACHE: dict[str, Any] = {
    "fingerprint": None,
    "checked_at": 0.0,
    "envelope": None,
}
_WIKI_VERSION_CACHE: dict[
    str,
    tuple[tuple[int, int, int, int, int], str | None, str | None],
] = {}
_AUTO_INGEST_RECEIPT_SCAN_CAP = 256
_AUTO_INGEST_RECEIPT_MAX_SCAN_CAP = 4096
_AUTO_INGEST_RECEIPT_RETENTION_DAYS = 14
_SEMANTIC_READINESS_CACHE_TTL_SECONDS = 5.0
_SEMANTIC_READINESS_MAX_ITEMS = 8
_SEMANTIC_READINESS_MAX_ITEM_CHARS = 256
_SEMANTIC_READINESS_GENERATION_SURFACES = (
    "canonical_identities",
    "change_sets",
    "claim_graph_edges",
    "claim_versions",
    "claims",
    "entities",
    "evidence",
    "evidence_versions",
    "governance_queue",
    "jobs",
    "mutation_outbox",
    "operational_memory",
    "page_graph_edges",
    "sources",
    "timeline_events",
)
_SEMANTIC_READINESS_CANONICAL_SURFACES = (
    "canonical_identities",
    "claim_graph_edges",
    "claim_versions",
    "claims",
    "entities",
    "evidence",
    "evidence_versions",
    "page_graph_edges",
    "sources",
)
_SEMANTIC_READINESS_GOVERNANCE_SURFACES = (
    "governance_queue",
    "jobs",
    "operational_memory",
)
_SEMANTIC_READINESS_POLICY_ENVIRONMENTS = (
    "VECTOR_LAKE_MAX_PENDING_GOVERNANCE_ITEMS",
    "VECTOR_LAKE_MAX_UNMANAGED_UNSUPPORTED_RUNTIME_CLAIMS",
    "VECTOR_LAKE_MAX_UNSUPPORTED_RUNTIME_CLAIMS",
    "VECTOR_LAKE_MIN_CLAIM_EVIDENCE_COVERAGE",
    "VECTOR_LAKE_MIN_CLAIM_EXTRACTION_COVERAGE",
    "VECTOR_LAKE_MIN_CLAIM_ASSESSMENT_COVERAGE",
    "VECTOR_LAKE_MIN_EVIDENCE_LOCATOR_COVERAGE",
    "VECTOR_LAKE_MIN_EVIDENCE_LINEAGE_COVERAGE",
    "VECTOR_LAKE_MIN_SOURCE_INTEGRITY_COVERAGE",
    "VECTOR_LAKE_MAX_AWAITING_SUBAGENT_AGE_SECONDS",
)


def _index_projection_signature(node: dict[str, Any]) -> str:
    stable_projection = {field: node.get(field) for field in _INDEX_PARITY_FIELDS}
    return json.dumps(stable_projection, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _parse_dt(value: Any):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except Exception:
        return None


def _read_watchdog_status(path: Path) -> dict[str, Any]:
    """Read the watchdog snapshot across Windows atomic-replace races."""
    from vector_lake import watchdog_status

    last_error: PermissionError | None = None
    for attempt in range(5):
        try:
            with watchdog_status._status_lock:
                payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("watchdog_status_is_not_an_object")
            return payload
        except PermissionError as exc:
            last_error = exc
            if attempt < 4:
                time.sleep(0.02)
    assert last_error is not None
    raise last_error


def _bounded_env_int(name: str, default: int, minimum: int) -> int:
    raw = os.environ.get(name, str(default))
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = default
    return max(minimum, value)


def _auto_ingest_health_config(meta_dir: Path) -> tuple[bool, dict[str, Any], str]:
    path = meta_dir / "auto_ingest_config.json"
    if not path.exists():
        return False, {}, ""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return False, {}, f"{type(exc).__name__}:{exc}"
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        return False, {}, "schema"
    enabled = payload.get("enabled")
    if not isinstance(enabled, bool):
        return False, {}, "enabled_must_be_boolean"
    if enabled:
        consent = payload.get("allow_model_processing_raw_text")
        if not isinstance(consent, bool):
            return False, payload, "allow_model_processing_raw_text_must_be_boolean"
        if not consent:
            return False, payload, "model_raw_text_processing_not_authorized"
    return enabled, payload, ""


def _auto_ingest_attempt_receipt_summary(
    meta_dir: Path,
    *,
    stale_after_seconds: int,
    retention_days: int,
    scan_cap: int,
) -> tuple[dict[str, Any], list[str]]:
    """Inspect a bounded recent receipt window without enumerating all history."""
    root = meta_dir / "auto_ingest_attempt_receipts"
    retention_days = max(1, min(90, int(retention_days)))
    scan_cap = max(1, min(_AUTO_INGEST_RECEIPT_MAX_SCAN_CAP, int(scan_cap)))
    summary: dict[str, Any] = {
        "count": 0,
        "outcomes": {},
        "invalid": 0,
        "stale_started": 0,
        "latest_ended_at": "",
        "retention_days": retention_days,
        "scan_cap": scan_cap,
        "scanned_entries": 0,
        "expired_ignored": 0,
        "truncated": False,
    }
    warnings: list[str] = []
    now = datetime.now(timezone.utc)
    retention_cutoff = now - timedelta(days=retention_days)
    paths: list[Path] = []
    read_errors = 0

    def scan_directory(directory: Path, *, legacy_flat: bool) -> None:
        nonlocal read_errors
        if summary["truncated"]:
            return
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    if summary["scanned_entries"] >= scan_cap:
                        summary["truncated"] = True
                        return
                    summary["scanned_entries"] += 1
                    try:
                        is_receipt = (
                            entry.is_file(follow_symlinks=False)
                            and entry.name.lower().endswith(".json")
                        )
                    except OSError:
                        read_errors += 1
                        continue
                    if not is_receipt:
                        continue
                    path = Path(entry.path)
                    if legacy_flat:
                        try:
                            modified_at = datetime.fromtimestamp(
                                entry.stat(follow_symlinks=False).st_mtime,
                                tz=timezone.utc,
                            )
                        except OSError:
                            read_errors += 1
                            continue
                        if modified_at < retention_cutoff:
                            summary["expired_ignored"] += 1
                            continue
                    paths.append(path)
        except FileNotFoundError:
            return
        except OSError:
            read_errors += 1

    # New receipts are date-partitioned, so only the explicit retention window
    # is addressed. The legacy flat directory is sampled with the same global
    # entry cap and filtered by mtime; neither path can grow beyond scan_cap.
    for day_offset in range(retention_days):
        bucket = (now - timedelta(days=day_offset)).date().isoformat()
        scan_directory(root / bucket, legacy_flat=False)
        if summary["truncated"]:
            break
    if not summary["truncated"]:
        scan_directory(root, legacy_flat=True)

    outcomes: dict[str, int] = {}
    latest_ended: datetime | None = None
    for path in paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            summary["invalid"] += 1
            continue
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") != 1
            or str(payload.get("attempt_id") or "") != path.stem
        ):
            summary["invalid"] += 1
            continue
        outcome = str(payload.get("outcome") or "")
        if not outcome:
            summary["invalid"] += 1
            continue
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
        if outcome == "started":
            started = _parse_dt(payload.get("started_at"))
            if started is None:
                summary["invalid"] += 1
            elif (now - started).total_seconds() > stale_after_seconds:
                summary["stale_started"] += 1
        ended = _parse_dt(payload.get("ended_at"))
        if ended is not None and (latest_ended is None or ended > latest_ended):
            latest_ended = ended

    summary["count"] = len(paths)
    summary["outcomes"] = dict(sorted(outcomes.items()))
    summary["latest_ended_at"] = latest_ended.isoformat() if latest_ended else ""
    summary["read_errors"] = read_errors
    if read_errors:
        warnings.append("auto_ingest_attempt_receipts_unreadable")
    if summary["truncated"]:
        warnings.append(f"auto_ingest_attempt_receipts_truncated:{scan_cap}")
    if summary["invalid"]:
        warnings.append(
            f"auto_ingest_attempt_receipts_invalid:{summary['invalid']}"
        )
    if summary["stale_started"]:
        warnings.append(
            f"auto_ingest_attempt_receipts_stale:{summary['stale_started']}"
        )
    finalized_with_warning = outcomes.get("finalized_with_warning", 0)
    if finalized_with_warning:
        warnings.append(
            f"auto_ingest_finalized_with_warning:{finalized_with_warning}"
        )
    return summary, warnings


def _effective_watchdog_component_status(
    component_name: str,
    component: dict[str, Any],
) -> tuple[str, str]:
    """Normalize bounded auto-ingest pauses without hiding worker crashes."""
    raw_status = str(component.get("status", "")).lower()
    action = str(component.get("current_action") or "")
    last_error = str(component.get("last_error") or "")
    if component_name == "auto_ingest" and raw_status in {
        "idle",
        "paused",
        "error",
        "halted",
    }:
        pause_actions = {
            "Automatic ingest budget gate closed",
            "Automatic ingest paused by budget gate",
            "Automatic ingest runner unavailable",
        }
        if action in pause_actions or (
            action == "Automatic ingest waiting" and last_error
        ):
            return "paused", action
    return raw_status, ""


_RUNTIME_HEALTH_REQUIRED_COLUMNS = {
    "entities": {"entity_id", "data_json"},
    "claims": {"claim_id", "data_json", "status"},
    "evidence": {"evidence_id", "data_json"},
    "sources": {"source_id", "data_json"},
    "source_artifacts": {"source_id", "data_json"},
    "extraction_runs": {"run_id", "data_json"},
    "claim_assessments": {"assessment_id", "claim_id", "outcome", "data_json"},
    "governance_queue": {"item_id", "data_json"},
    "mutation_outbox": {
        "id",
        "filename",
        "status",
        "mutation_type",
        "payload_text",
        "base_version",
        "available_at",
        "created_at",
    },
    "jobs": {
        "task_type",
        "status",
        "retries",
        "lease_until",
        "available_at",
        "created_at",
        "updated_at",
        "result_json",
    },
    "runtime_generations": {"surface", "generation"},
    "operational_memory": {"memory_id", "data_json"},
    "embedding_metadata_v8": {
        "entity_id",
        "content_sha256",
        "input_contract",
        "model",
        "dimension",
    },
    "search_projection_state_v8": {
        "singleton",
        "status",
        "projection_generation",
        "expected_row_count",
        "expected_corpus_sha256",
    },
}


class RuntimeDatabaseBlocked(RuntimeError):
    """The runtime database cannot be inspected safely without mutation."""


def _open_runtime_database_read_only():
    """Open the existing database without creating or migrating it."""
    from vector_lake.db_store import peek_db_path

    db_path = peek_db_path().resolve()
    if not db_path.is_file():
        raise RuntimeDatabaseBlocked(f"database_missing:{db_path}")
    wal_path = Path(str(db_path) + "-wal")
    try:
        wal_size = wal_path.stat().st_size
    except FileNotFoundError:
        wal_size = 0
    uri = f"{db_path.as_uri()}?mode=ro"
    if wal_size == 0:
        # A checkpointed database can be inspected without asking SQLite to
        # create WAL/SHM sidecars in a protected canonical directory.
        uri += "&immutable=1"
    try:
        conn = sqlite3.connect(
            uri,
            uri=True,
            timeout=5.0,
        )
    except sqlite3.Error as exc:
        raise RuntimeDatabaseBlocked(f"database_read_only_open_failed:{exc}") from exc
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only=ON")
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
        available_tables = {str(row["name"]) for row in rows}
        missing_tables = sorted(
            set(_RUNTIME_HEALTH_REQUIRED_COLUMNS) - available_tables
        )
        missing_columns: list[str] = []
        for table, required_columns in _RUNTIME_HEALTH_REQUIRED_COLUMNS.items():
            if table not in available_tables:
                continue
            columns = {
                str(row["name"])
                for row in conn.execute(f'PRAGMA table_info("{table}")').fetchall()
            }
            missing_columns.extend(
                f"{table}.{column}"
                for column in sorted(required_columns - columns)
            )
        if missing_tables or missing_columns:
            parts = []
            if missing_tables:
                parts.append("missing_tables=" + ",".join(missing_tables))
            if missing_columns:
                parts.append("missing_columns=" + ",".join(missing_columns))
            raise RuntimeDatabaseBlocked("schema_not_ready:" + ";".join(parts))
        conn.execute("SELECT 1").fetchone()
    except Exception:
        conn.close()
        raise
    return conn, db_path


def _open_runtime_generation_database_read_only():
    """Open only the generation ledger needed by readiness cache hits."""
    from vector_lake.db_store import peek_db_path

    db_path = peek_db_path().resolve()
    if not db_path.is_file():
        raise RuntimeDatabaseBlocked("database_missing")
    wal_path = Path(str(db_path) + "-wal")
    try:
        wal_size = wal_path.stat().st_size
    except FileNotFoundError:
        wal_size = 0
    uri = f"{db_path.as_uri()}?mode=ro"
    if wal_size == 0:
        uri += "&immutable=1"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute("SELECT 1 FROM runtime_generations LIMIT 1").fetchone()
    except sqlite3.Error as exc:
        if "conn" in locals():
            conn.close()
        raise RuntimeDatabaseBlocked(
            "semantic_runtime_generation_ledger_unavailable"
        ) from exc
    return conn, db_path


def _sqlite_snapshot_identity(conn, db_path: Path) -> tuple:
    identities = []
    for path in (db_path, db_path.with_name(db_path.name + "-wal")):
        try:
            stat = path.stat()
            identities.append((str(path.resolve()), stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size))
        except OSError:
            identities.append((str(path.resolve()), "missing"))
    data_version = int(
        conn.execute("PRAGMA data_version").fetchone()[0]
    )
    return data_version, tuple(identities)



def _canonical_snapshot(conn, db_path: Path) -> dict[str, list[tuple[str, dict[str, Any]]]]:
    """Load canonical entities once for an unchanged database generation."""
    generation_row = conn.execute(
        "SELECT generation FROM runtime_generations WHERE surface = 'entities'"
    ).fetchone()
    key = (
        str(db_path.resolve()),
        int(generation_row["generation"] or 0),
        _sqlite_snapshot_identity(conn, db_path),
    )
    with _CACHE_LOCK:
        if _CANONICAL_CACHE.get("key") == key:
            return _CANONICAL_CACHE["value"]

    by_page: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for row in conn.execute("SELECT entity_id, data_json FROM entities ORDER BY entity_id"):
        raw = str(row["data_json"])
        try:
            entity = json.loads(raw)
        except (TypeError, ValueError):
            continue
        page_key = str(entity.get("page_key") or "")
        if not page_key or page_key.startswith("System_"):
            continue
        by_page.setdefault(page_key, []).append((str(row["entity_id"]), entity))
    with _CACHE_LOCK:
        _CANONICAL_CACHE.update({"key": key, "value": by_page})
    return by_page


def _index_snapshot(index_path: Path) -> tuple[dict[str, Any], Exception | None]:
    """Load the process-wide shared index snapshot."""
    try:
        return load_index_snapshot(index_path), None
    except Exception as exc:
        return {"nodes": {}}, exc


def _semantic_readiness_file_fingerprint(paths: tuple[Path, ...]) -> str:
    """Hash file identities without exposing canonical paths to callers."""
    identities: list[tuple[Any, ...]] = []
    for path in paths:
        try:
            stat = path.stat()
            identities.append(
                (
                    int(stat.st_dev),
                    int(stat.st_ino),
                    int(stat.st_mtime_ns),
                    int(stat.st_ctime_ns),
                    int(stat.st_size),
                )
            )
        except OSError:
            identities.append(("missing",))
    encoded = json.dumps(identities, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _semantic_readiness_generation_binding(
    index_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Capture a cheap, verifiable generation token for semantic readiness.

    Runtime generations cover canonical and governance surfaces. Database/WAL
    identity additionally catches semantic tables that do not have a dedicated
    runtime-generation counter. The projection is accepted only when its
    verified canonical binding still matches the database.
    """
    from vector_lake.search_projection_contract import (
        CANONICAL_PROJECTION_SURFACES,
        verified_projection_runtime_generations,
    )
    from vector_lake.indexer import read_committed_index_snapshot
    from vector_lake.wiki_utils import (
        get_claim_graph_path,
        get_index_path,
        get_projection_manifest_path,
    )

    conn = None
    index_path = get_index_path()
    if not index_path.is_file():
        raise RuntimeDatabaseBlocked("semantic_projection_missing")
    try:
        conn, db_path = _open_runtime_generation_database_read_only()
        placeholders = ",".join(
            "?" for _ in _SEMANTIC_READINESS_GENERATION_SURFACES
        )
        rows = conn.execute(
            "SELECT surface, generation FROM runtime_generations "
            f"WHERE surface IN ({placeholders})",
            _SEMANTIC_READINESS_GENERATION_SURFACES,
        ).fetchall()
        runtime_generations = {
            str(row["surface"]): int(row["generation"])
            for row in rows
        }
        missing = set(_SEMANTIC_READINESS_GENERATION_SURFACES) - set(
            runtime_generations
        )
        if missing:
            raise RuntimeDatabaseBlocked("semantic_runtime_generations_incomplete")
        database_fingerprint = _semantic_readiness_file_fingerprint(
            (db_path, db_path.with_name(db_path.name + "-wal"))
        )
        committed_index_data = read_committed_index_snapshot(
            index_path,
            connection=conn,
            _acquire_lock=False,
        )
    finally:
        if conn is not None:
            conn.close()

    if not isinstance(committed_index_data, dict):
        raise RuntimeDatabaseBlocked("semantic_projection_invalid")
    if index_data is not None and (
        not isinstance(index_data, dict)
        or index_data.get("projection_manifest")
        != committed_index_data.get("projection_manifest")
        or index_data.get("graph_state") != committed_index_data.get("graph_state")
    ):
        raise RuntimeDatabaseBlocked("semantic_projection_snapshot_mismatch")
    index_data = committed_index_data

    projection_generations = verified_projection_runtime_generations(index_data)
    if projection_generations is None:
        raise RuntimeDatabaseBlocked("semantic_projection_binding_unverified")
    current_projection_generations = {
        surface: runtime_generations[surface]
        for surface in CANONICAL_PROJECTION_SURFACES
    }
    if projection_generations != current_projection_generations:
        raise RuntimeDatabaseBlocked("semantic_projection_generation_mismatch")

    manifest = index_data.get("projection_manifest")
    if not isinstance(manifest, dict):
        raise RuntimeDatabaseBlocked("semantic_projection_manifest_invalid")
    projection_generation = str(manifest.get("generation") or "").strip()
    if not projection_generation:
        raise RuntimeDatabaseBlocked("semantic_projection_generation_missing")
    projection_material = {
        "generation": projection_generation,
        "canonical_generation": projection_generations,
        "graph_state": index_data.get("graph_state"),
    }
    projection_fingerprint = "sha256:" + hashlib.sha256(
        json.dumps(
            projection_material,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    policy_fingerprint = "sha256:" + hashlib.sha256(
        json.dumps(
            {
                name: os.environ.get(name)
                for name in _SEMANTIC_READINESS_POLICY_ENVIRONMENTS
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    canonical_generations = {
        surface: runtime_generations[surface]
        for surface in _SEMANTIC_READINESS_CANONICAL_SURFACES
    }
    governance_generations = {
        surface: runtime_generations[surface]
        for surface in _SEMANTIC_READINESS_GOVERNANCE_SURFACES
    }
    supporting_generations = {
        surface: runtime_generations[surface]
        for surface in _SEMANTIC_READINESS_GENERATION_SURFACES
        if surface not in _SEMANTIC_READINESS_CANONICAL_SURFACES
        and surface not in _SEMANTIC_READINESS_GOVERNANCE_SURFACES
    }
    binding: dict[str, Any] = {
        "canonical": canonical_generations,
        "governance": governance_generations,
        "supporting": supporting_generations,
        "projection": {
            "generation": projection_generation,
            "fingerprint": projection_fingerprint,
            "file_fingerprint": _semantic_readiness_file_fingerprint(
                (
                    index_path,
                    get_claim_graph_path(),
                    get_projection_manifest_path(),
                )
            ),
        },
        "database_fingerprint": database_fingerprint,
        "policy_fingerprint": policy_fingerprint,
    }
    binding["fingerprint"] = "sha256:" + hashlib.sha256(
        json.dumps(
            binding,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return binding


def _bounded_semantic_readiness_items(value: Any) -> tuple[list[str], int]:
    raw_items = value if isinstance(value, (list, tuple)) else []
    normalized = [
        str(item).replace("\r", " ").replace("\n", " ")[
            :_SEMANTIC_READINESS_MAX_ITEM_CHARS
        ]
        for item in raw_items[:_SEMANTIC_READINESS_MAX_ITEMS]
    ]
    return normalized, len(raw_items)


def _semantic_readiness_debt_summary(assessment: dict[str, Any]) -> dict[str, Any]:
    detail = assessment.get("detail")
    if not isinstance(detail, dict):
        detail = {}
    validity = detail.get("runtime_validity_state_counts")
    if not isinstance(validity, dict):
        validity = {}
    bounded_validity: dict[str, int] = {}
    for key, value in sorted(validity.items(), key=lambda item: str(item[0]))[:8]:
        if isinstance(value, bool):
            continue
        try:
            bounded_validity[str(key)[:64]] = max(0, int(value or 0))
        except (TypeError, ValueError):
            continue

    def count(name: str) -> int:
        value = detail.get(name, 0)
        if isinstance(value, bool):
            return 0
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

    return {
        "pending_governance_total": count("pending_governance_total"),
        "critical_pending_governance": count("critical_pending_governance"),
        "runtime_validity_state_counts": bounded_validity,
        "awaiting_subagent_jobs": count("awaiting_subagent_jobs"),
    }


def _semantic_readiness_envelope(
    assessment: dict[str, Any],
    binding: dict[str, Any] | None,
) -> dict[str, Any]:
    issues, issue_count = _bounded_semantic_readiness_items(assessment.get("issues"))
    warnings, warning_count = _bounded_semantic_readiness_items(
        assessment.get("warnings")
    )
    requested_status = str(assessment.get("status") or "unknown").casefold()
    if requested_status not in {"ready", "degraded", "not_ready", "unknown"}:
        requested_status = "unknown"
    ready = (
        assessment.get("ready") is True
        and requested_status == "ready"
        and issue_count == 0
        and warning_count == 0
        and binding is not None
    )
    status = "ready" if ready else requested_status
    if not ready and status == "ready":
        status = "unknown"
    captured_generation = None
    captured_fingerprint = None
    if isinstance(binding, dict):
        captured_generation = {
            key: copy.deepcopy(value)
            for key, value in binding.items()
            if key != "fingerprint"
        }
        captured_fingerprint = binding.get("fingerprint")
    return {
        "contract_version": "vector-lake-semantic-readiness-envelope/v1",
        "ready": ready,
        "status": status,
        "issues": issues,
        "warnings": warnings,
        "issue_count": issue_count,
        "warning_count": warning_count,
        "issues_omitted": max(0, issue_count - len(issues)),
        "warnings_omitted": max(0, warning_count - len(warnings)),
        "debt_summary": _semantic_readiness_debt_summary(assessment),
        "captured_generation": captured_generation,
        "captured_fingerprint": captured_fingerprint,
        "results_are_not_accepted_facts": True,
    }


def _unknown_semantic_readiness_envelope(
    issue: str,
    binding: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return _semantic_readiness_envelope(
        {
            "ready": False,
            "status": "unknown",
            "issues": [issue],
            "warnings": [],
            "detail": {},
        },
        binding,
    )


def _clear_semantic_readiness_envelope_cache_for_tests() -> None:
    """Reset the readiness snapshot cache; intentionally private to tests."""
    with _SEMANTIC_READINESS_EVALUATION_LOCK:
        with _CACHE_LOCK:
            _SEMANTIC_READINESS_ENVELOPE_CACHE.update(
                {"fingerprint": None, "checked_at": 0.0, "envelope": None}
            )


def get_semantic_readiness_envelope(
    *,
    cache_ttl_seconds: float = _SEMANTIC_READINESS_CACHE_TTL_SECONDS,
    index_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a generation-bound readiness snapshot without blocking retrieval."""
    try:
        ttl_seconds = min(60.0, max(0.0, float(cache_ttl_seconds)))
    except (TypeError, ValueError):
        ttl_seconds = _SEMANTIC_READINESS_CACHE_TTL_SECONDS

    with _SEMANTIC_READINESS_EVALUATION_LOCK:
        try:
            before = _semantic_readiness_generation_binding(index_data)
        except Exception as exc:
            return _unknown_semantic_readiness_envelope(
                f"semantic_readiness_binding_unavailable:{type(exc).__name__}"
            )

        now = time.monotonic()
        with _CACHE_LOCK:
            cached = _SEMANTIC_READINESS_ENVELOPE_CACHE.get("envelope")
            if (
                cached is not None
                and _SEMANTIC_READINESS_ENVELOPE_CACHE.get("fingerprint")
                == before.get("fingerprint")
                and now
                - float(
                    _SEMANTIC_READINESS_ENVELOPE_CACHE.get("checked_at") or 0.0
                )
                <= ttl_seconds
            ):
                return copy.deepcopy(cached)

        try:
            # The generation binding above verifies the durable projection pair.
            # Re-read through the shared snapshot cache instead of trusting a
            # caller-supplied mapping as semantic evidence.
            assessment = assess_semantic_readiness(index_data=None)
            if not isinstance(assessment, dict):
                raise TypeError("semantic readiness assessment is not a mapping")
        except Exception as exc:
            assessment = {
                "ready": False,
                "status": "unknown",
                "issues": [
                    f"semantic_readiness_assessment_unavailable:{type(exc).__name__}"
                ],
                "warnings": [],
                "detail": {},
            }

        try:
            after = _semantic_readiness_generation_binding(index_data)
        except Exception as exc:
            return _unknown_semantic_readiness_envelope(
                f"semantic_readiness_binding_unavailable:{type(exc).__name__}"
            )
        if before.get("fingerprint") != after.get("fingerprint"):
            return _unknown_semantic_readiness_envelope(
                "semantic_readiness_generation_changed",
                after,
            )

        envelope = _semantic_readiness_envelope(assessment, after)
        with _CACHE_LOCK:
            _SEMANTIC_READINESS_ENVELOPE_CACHE.update(
                {
                    "fingerprint": after.get("fingerprint"),
                    "checked_at": time.monotonic(),
                    "envelope": copy.deepcopy(envelope),
                }
            )
        return envelope


def _wiki_cache_key(path: Path) -> str:
    """Return a stable lexical key without resolving every Wiki page on disk."""
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _wiki_file_identity(path: Path) -> tuple[int, int, int, int, int]:
    stat = path.stat()
    return (
        int(stat.st_dev),
        int(stat.st_ino),
        int(stat.st_mtime_ns),
        int(stat.st_ctime_ns),
        int(stat.st_size),
    )


def _wiki_projection_version(governance_store, path: Path) -> tuple[str | None, str | None]:
    """Return a page version using a stat-keyed, process-local parse cache."""
    cache_key = _wiki_cache_key(path)
    try:
        identity = _wiki_file_identity(path)
    except OSError as exc:
        with _CACHE_LOCK:
            _WIKI_VERSION_CACHE.pop(cache_key, None)
        return None, str(exc)
    with _CACHE_LOCK:
        cached = _WIKI_VERSION_CACHE.get(cache_key)
        if cached and cached[0] == identity:
            return cached[1], cached[2]
    try:
        version = governance_store.canonical_page_version_from_content(
            path.name,
            path.read_text(encoding="utf-8"),
        )
        observed_identity = _wiki_file_identity(path)
        error = None
    except Exception as exc:
        version = None
        error = str(exc)
        observed_identity = None
    if observed_identity != identity:
        with _CACHE_LOCK:
            _WIKI_VERSION_CACHE.pop(cache_key, None)
        return None, error or "wiki_page_changed_during_read"
    with _CACHE_LOCK:
        _WIKI_VERSION_CACHE[cache_key] = (identity, version, error)
    return version, error


def _write_health_surface_token() -> str:
    """Hash cheap projection identities without reading page or index bodies.

    Directory metadata catches Wiki create/delete/rename operations in O(1).
    External in-place page edits do not reliably change directory metadata, so
    write-health caching is opt-in and strict per-write validation is the
    default.
    """
    from vector_lake.db_store import peek_db_path
    from vector_lake.wiki_utils import (
        get_claim_graph_path,
        get_index_path,
        get_legacy_claim_graph_path,
        peek_meta_dir,
        get_wiki_dir,
    )

    digest = hashlib.blake2b(digest_size=16)
    wiki_dir = get_wiki_dir()
    try:
        stat = wiki_dir.stat()
        wiki_identity = (
            str(wiki_dir.resolve()),
            stat.st_mtime_ns,
            stat.st_ctime_ns,
            stat.st_size,
        )
    except OSError:
        wiki_identity = (str(wiki_dir.resolve()), "missing")
    digest.update(repr(wiki_identity).encode("utf-8"))

    for path in (
        get_index_path(),
        get_claim_graph_path(),
        get_legacy_claim_graph_path(),
        peek_meta_dir() / "wiki_reconcile_required.json",
    ):
        try:
            stat = path.stat()
            identity = (str(path.resolve()), stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size)
        except OSError:
            identity = (str(path.resolve()), "missing")
        digest.update(repr(identity).encode("utf-8"))

    watchdog_status_path = peek_meta_dir() / ".watchdog_status.json"
    try:
        watchdog_status = _read_watchdog_status(watchdog_status_path)
        components = watchdog_status.get("components")
        component_signature = ()
        if isinstance(components, dict):
            now_utc = datetime.now(timezone.utc)
            component_max_age = _bounded_env_int(
                "VECTOR_LAKE_WATCHDOG_COMPONENT_MAX_AGE_SECONDS",
                default=120,
                minimum=5,
            )
            component_signature = tuple(
                sorted(
                    (
                        str(name),
                        str(detail.get("status")),
                        str(detail.get("current_action") or ""),
                        (
                            (heartbeat := _parse_dt(
                                detail.get("heartbeat_at") or detail.get("updated_at")
                            ))
                            is not None
                            and max(0, int((now_utc - heartbeat).total_seconds()))
                            <= component_max_age
                        ),
                    )
                    for name, detail in components.items()
                    if isinstance(detail, dict)
                )
            )
        updated_at = _parse_dt(watchdog_status.get("updated_at"))
        watchdog_age = None
        if updated_at is not None:
            watchdog_age = max(
                0,
                int((datetime.now(timezone.utc) - updated_at).total_seconds()),
            )
        watchdog_signature = (
            str(watchdog_status.get("status")),
            component_signature,
            watchdog_age is not None and watchdog_age <= 120,
        )
    except FileNotFoundError:
        watchdog_signature = ("missing",)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        watchdog_signature = ("unreadable", type(exc).__name__)
    digest.update(repr(watchdog_signature).encode("utf-8"))

    generation_surfaces = (
        "entities",
        "claims",
        "sources",
        "timeline_events",
        "mutation_outbox",
        "jobs",
    )
    policy_signature = (
        os.environ.get("VECTOR_LAKE_OUTBOX_MAX_PENDING_AGE_SECONDS", "300"),
        os.environ.get("VECTOR_LAKE_MAX_READY_INGEST_AGE_SECONDS", "300"),
        os.environ.get("VECTOR_LAKE_MAX_AWAITING_SUBAGENT_JOBS", "500"),
        os.environ.get("VECTOR_LAKE_MAX_AWAITING_SUBAGENT_AGE_SECONDS", "86400"),
        os.environ.get("VECTOR_LAKE_SUBAGENT_BACKLOG_BLOCKING") == "1",
        os.environ.get("VECTOR_LAKE_TIMELINE_PARITY_BLOCKING") == "1",
        os.environ.get("VECTOR_LAKE_WATCHDOG_COMPONENT_MAX_AGE_SECONDS", "120"),
    )
    digest.update(repr(policy_signature).encode("utf-8"))

    conn = None
    try:
        db_path = peek_db_path()
        if not db_path.exists():
            raise FileNotFoundError(db_path)
        conn, db_path = _open_runtime_database_read_only()
        placeholders = ", ".join("?" for _ in generation_surfaces)
        generation_rows = conn.execute(
            f"SELECT surface, generation FROM runtime_generations WHERE surface IN ({placeholders})",
            tuple(generation_surfaces),
        ).fetchall()
        generations = {
            str(row["surface"]): int(row["generation"])
            for row in generation_rows
        }
        missing = set(generation_surfaces) - set(generations)
        if missing:
            raise RuntimeError(f"missing runtime generations: {sorted(missing)}")
        generation_signature = tuple(
            (surface, generations[surface]) for surface in generation_surfaces
        )
        db_signature = (generation_signature, _sqlite_snapshot_identity(conn, db_path))
        digest.update(repr(db_signature).encode("utf-8"))
    except FileNotFoundError:
        digest.update(b"database:missing")
    except Exception as exc:
        digest.update(f"database-error:{type(exc).__name__}".encode("ascii"))
    finally:
        if conn is not None:
            conn.close()

    return digest.hexdigest()


def _prune_wiki_version_cache(active_paths: set[str]) -> None:
    with _CACHE_LOCK:
        stale = [key for key in _WIKI_VERSION_CACHE if key not in active_paths]
        for key in stale:
            _WIKI_VERSION_CACHE.pop(key, None)


def _clear_health_caches_for_tests() -> None:
    """Reset process-local caches; intentionally private to runtime tests."""
    clear_index_snapshot_cache_for_tests()
    with _CACHE_LOCK:
        _CANONICAL_CACHE.update({"key": None, "value": None})
        _WIKI_VERSION_CACHE.clear()
        _WRITE_GATE_CACHE.update({"token": None, "checked_at": 0.0, "health": None})
    _clear_semantic_readiness_envelope_cache_for_tests()

def _recent_write_gate_health(token: str, cache_seconds: float):
    if cache_seconds <= 0:
        return None
    now = time.monotonic()
    with _CACHE_LOCK:
        if (
            _WRITE_GATE_CACHE.get("token") == token
            and now - float(_WRITE_GATE_CACHE.get("checked_at") or 0.0)
            <= cache_seconds
        ):
            return _WRITE_GATE_CACHE.get("health")
    return None



def assess_runtime_health(
    max_watchdog_age_seconds: int = 120,
    deep_projection_checks: bool = False,
    diagnostic_snapshot=None,
) -> dict[str, Any]:
    from vector_lake.diagnostic_snapshot import current_durability_status
    from vector_lake.wiki_utils import (
        get_index_path,
        get_wiki_dir,
        iter_markdown_files,
        peek_meta_dir,
    )

    issues: list[str] = []
    warnings: list[str] = []
    detail: dict[str, Any] = {}

    durability = (
        dict(diagnostic_snapshot.durability)
        if diagnostic_snapshot is not None
        else current_durability_status()
    )
    detail["durability"] = durability
    if durability["profile"] == "best_effort":
        warnings.append("durability_profile_best_effort")
    elif durability["profile"] == "invalid":
        issues.append("durability_profile_invalid")

    owns_connection = diagnostic_snapshot is None

    if diagnostic_snapshot is not None:
        conn = diagnostic_snapshot.connection
        db_path = diagnostic_snapshot.db_path
        detail["diagnostic_snapshot"] = diagnostic_snapshot.metadata()
    else:
        try:
            conn, db_path = _open_runtime_database_read_only()
        except RuntimeDatabaseBlocked as exc:
            issues.append(f"database_blocked:{exc}")
            detail.update(
                {"database_access": "read_only", "database_error": str(exc)}
            )
            return {
                "ok": False,
                "status": "blocked",
                "issues": issues,
                "warnings": warnings,
                "detail": detail,
            }

    detail["db_path"] = str(db_path)
    detail["database_access"] = "read_only"
    storage = {
        "database_bytes": db_path.stat().st_size if db_path.exists() else 0,
        "wal_bytes": Path(str(db_path) + "-wal").stat().st_size
        if Path(str(db_path) + "-wal").exists()
        else 0,
        "shm_bytes": Path(str(db_path) + "-shm").stat().st_size
        if Path(str(db_path) + "-shm").exists()
        else 0,
    }
    if deep_projection_checks:
        storage["row_counts"] = {
            table_name: int(
                conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0]
                or 0
            )
            for table_name in (
                "claim_versions",
                "evidence_versions",
                "operational_memory",
                "claims",
                "evidence",
                "change_sets",
                "governance_queue",
            )
        }
    detail["storage"] = storage
    database_warning_bytes = _bounded_env_int(
        "VECTOR_LAKE_DATABASE_WARNING_BYTES",
        4 * 1024 * 1024 * 1024,
        1024 * 1024,
    )
    if int(storage["database_bytes"]) >= database_warning_bytes:
        warnings.append(
            "database_size_high:"
            f"{int(storage['database_bytes'])}>={database_warning_bytes}"
        )

    from vector_lake.storage_growth import storage_growth_status

    storage_growth = storage_growth_status(meta_dir=peek_meta_dir())
    detail["storage_growth"] = storage_growth
    if storage_growth["status"] == "invalid":
        warnings.append("storage_growth_history_invalid")
    elif deep_projection_checks and storage_growth["status"] == "not_initialized":
        warnings.append("storage_growth_baseline_missing")
    delta = storage_growth.get("per_day_delta") or storage_growth.get("delta") or {}
    database_growth_warning_bytes = _bounded_env_int(
        "VECTOR_LAKE_DATABASE_DAILY_GROWTH_WARNING_BYTES",
        256 * 1024 * 1024,
        1024 * 1024,
    )
    if int(delta.get("database_bytes") or 0) >= database_growth_warning_bytes:
        warnings.append(
            "database_daily_growth_high:"
            f"{int(delta['database_bytes'])}>={database_growth_warning_bytes}"
        )

    from vector_lake.backup_capacity import backup_capacity_status

    backup_capacity = backup_capacity_status()
    detail["backup_capacity"] = backup_capacity
    if not backup_capacity["inventory_complete"]:
        issues.append("backup_capacity_inventory_incomplete")
    if backup_capacity["free_space_insufficient"]:
        issues.append("backup_minimum_free_space_not_preserved")
    if (
        backup_capacity["quota_exceeded"]
        and backup_capacity["policy"]["quota_mode"] == "enforce"
    ):
        issues.append("backup_max_total_bytes_exceeded")
    database_bytes = max(1, int(storage["database_bytes"] or 0))
    backup_database_ratio = round(
        int(backup_capacity["current_backup_bytes"] or 0) / database_bytes,
        4,
    )
    backup_capacity["database_ratio"] = backup_database_ratio
    if not backup_capacity["quota_configured"] and backup_database_ratio >= 8.0:
        warnings.append(
            "backup_quota_unconfigured_high_database_ratio:"
            f"{backup_database_ratio:.4f}>=8.0000"
        )

    from vector_lake.governance_store import operational_memory_search_index_status

    try:
        memory_search = operational_memory_search_index_status(connection=conn)
    except sqlite3.Error as exc:
        memory_search = {
            "configured": True,
            "available": False,
            "ready": False,
            "status": "unavailable",
            "warnings": [f"status_query_failed:{type(exc).__name__}"],
        }
    detail["operational_memory_search"] = memory_search
    if memory_search.get("configured") and not memory_search.get("ready"):
        warnings.append(
            "operational_memory_search_not_ready:"
            f"{memory_search.get('status') or 'unknown'}"
        )
        if not memory_search.get("auto_maintenance_configured"):
            issues.append("operational_memory_search_auto_maintenance_disabled")
        if memory_search.get("progress_stalled"):
            issues.append("operational_memory_search_progress_stalled")
    version_growth = sum(
        int(value or 0) for value in (delta.get("row_counts") or {}).values()
    )
    version_growth_warning_rows = _bounded_env_int(
        "VECTOR_LAKE_VERSION_DAILY_GROWTH_WARNING_ROWS",
        50_000,
        1_000,
    )
    if version_growth >= version_growth_warning_rows:
        warnings.append(
            "version_rows_daily_growth_high:"
            f"{version_growth}>={version_growth_warning_rows}"
        )

    try:
        from vector_lake.tool_gc import verify_gc_recovery_receipts

        gc_receipts = verify_gc_recovery_receipts(deep=deep_projection_checks)
        detail["gc_recovery_receipts"] = gc_receipts
        issues.extend(str(item) for item in gc_receipts.get("issues") or [])
        warnings.extend(str(item) for item in gc_receipts.get("warnings") or [])
        if int(gc_receipts.get("aborted") or 0):
            warnings.append(
                f"gc_receipt_aborted:{int(gc_receipts.get('aborted') or 0)}"
            )
    except Exception as exc:
        issues.append(f"gc_receipt_check_failed:{type(exc).__name__}:{exc}")
    meta_dir = peek_meta_dir()
    auto_ingest_enabled, auto_ingest_config, auto_ingest_config_error = (
        _auto_ingest_health_config(meta_dir)
    )
    detail["auto_ingest_enabled"] = auto_ingest_enabled
    try:
        from vector_lake.tool_auto_ingest import auto_ingest_budget_status

        detail["auto_ingest_budget"] = auto_ingest_budget_status(
            include_actual_usage=False
        )
    except Exception as exc:
        issues.append(f"auto_ingest_budget_status_failed:{type(exc).__name__}:{exc}")
    if auto_ingest_config_error:
        issues.append(f"auto_ingest_config_invalid:{auto_ingest_config_error}")
    elif not auto_ingest_enabled:
        warnings.append("auto_ingest_disabled")
    if auto_ingest_enabled:
        receipt_summary, receipt_warnings = _auto_ingest_attempt_receipt_summary(
            meta_dir,
            stale_after_seconds=max(
                120,
                int(auto_ingest_config.get("lease_seconds") or 1320) + 60,
            ),
            retention_days=max(
                1,
                min(
                    90,
                    int(
                        auto_ingest_config.get("scratch_retention_days")
                        or _AUTO_INGEST_RECEIPT_RETENTION_DAYS
                    ),
                ),
            ),
            scan_cap=min(
                _AUTO_INGEST_RECEIPT_MAX_SCAN_CAP,
                _bounded_env_int(
                    "VECTOR_LAKE_AUTO_INGEST_RECEIPT_SCAN_CAP",
                    _AUTO_INGEST_RECEIPT_SCAN_CAP,
                    1,
                ),
            ),
        )
        detail["auto_ingest_attempt_receipts"] = receipt_summary
        warnings.extend(receipt_warnings)

    outbox_counts = {
        row["status"]: row["count"]
        for row in conn.execute("SELECT status, COUNT(*) AS count FROM mutation_outbox GROUP BY status")
    }
    detail["outbox_counts"] = outbox_counts
    if outbox_counts.get("failed", 0):
        issues.append(f"mutation_outbox_failed:{outbox_counts.get('failed', 0)}")
    oldest_pending = conn.execute(
        "SELECT COALESCE(MIN(COALESCE(available_at, created_at)), '') FROM mutation_outbox "
        "WHERE status IN ('pending', 'processing')"
    ).fetchone()[0]
    oldest_pending_dt = _parse_dt(oldest_pending)
    if oldest_pending_dt is not None:
        pending_age = max(0, int((datetime.now(timezone.utc) - oldest_pending_dt).total_seconds()))
        detail["oldest_pending_outbox_age_seconds"] = pending_age
        max_pending_age = max(1, int(os.environ.get("VECTOR_LAKE_OUTBOX_MAX_PENDING_AGE_SECONDS", "300")))
        if pending_age > max_pending_age:
            issues.append(f"mutation_outbox_stalled:{pending_age}s")

    from vector_lake.db_store import _ingest_result_is_auto_quarantine

    terminal_rows = conn.execute(
        "SELECT task_type, result_json FROM jobs "
        "WHERE status = 'failed' AND retries >= 3"
    ).fetchall()
    terminal_jobs = len(terminal_rows)
    auto_quarantined_jobs = sum(
        1
        for row in terminal_rows
        if str(row["task_type"] or "") == "ingest"
        and _ingest_result_is_auto_quarantine(row["result_json"])
    )
    blocking_terminal_jobs = terminal_jobs - auto_quarantined_jobs
    detail["terminal_failed_jobs"] = terminal_jobs
    detail["blocking_terminal_failed_jobs"] = blocking_terminal_jobs
    detail["auto_ingest_quarantined_jobs"] = auto_quarantined_jobs
    if auto_quarantined_jobs:
        detail["auto_ingest_quarantine_recovery"] = {
            "preview": "reconcile_ingest_job_debt(dry_run=True)",
            "apply": "reconcile_ingest_job_debt(dry_run=False)",
        }
        warnings.append(f"auto_ingest_quarantined_jobs:{auto_quarantined_jobs}")
    if blocking_terminal_jobs:
        issues.append(f"terminal_failed_jobs:{blocking_terminal_jobs}")

    auto_ingest_active_jobs = 0
    if auto_ingest_enabled:
        auto_ingest_active_jobs = int(
            conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE task_type = 'ingest' "
                "AND status IN ('awaiting_subagent', 'subagent_processing')"
            ).fetchone()[0]
            or 0
        )
        detail["auto_ingest_active_jobs"] = auto_ingest_active_jobs
    now_text = datetime.now(timezone.utc).isoformat()
    ready_ingest_row = conn.execute(
        "SELECT COUNT(*) AS count, "
        "COALESCE(MIN(CASE WHEN status = 'dispatched' THEN lease_until "
        "ELSE COALESCE(available_at, created_at) END), '') AS oldest "
        "FROM jobs WHERE task_type = 'ingest' AND ("
        "(status IN ('queued', 'failed') AND retries < 3 "
        "AND COALESCE(available_at, created_at, '') <= ?) OR "
        "(status = 'dispatched' AND COALESCE(lease_until, '') <= ?))",
        (now_text, now_text),
    ).fetchone()
    ready_ingest_count = int(ready_ingest_row["count"] or 0)
    detail["ready_ingest_jobs"] = ready_ingest_count
    oldest_ready_ingest = _parse_dt(ready_ingest_row["oldest"])
    if oldest_ready_ingest is not None:
        ready_ingest_age = max(
            0,
            int(
                (datetime.now(timezone.utc) - oldest_ready_ingest).total_seconds()
            ),
        )
        detail["oldest_ready_ingest_age_seconds"] = ready_ingest_age
        max_ready_age = _bounded_env_int(
            "VECTOR_LAKE_MAX_READY_INGEST_AGE_SECONDS",
            default=300,
            minimum=30,
        )
        if ready_ingest_age > max_ready_age:
            message = (
                f"ingest_dispatch_stalled:count={ready_ingest_count},"
                f"oldest={ready_ingest_age}s"
            )
            if auto_ingest_enabled and auto_ingest_active_jobs >= 1:
                warnings.append("auto_ingest_backpressure:" + message)
            else:
                issues.append(message)
    awaiting_row = conn.execute(
        "SELECT COUNT(*) AS count, COALESCE(MIN(updated_at), '') AS oldest "
        "FROM jobs WHERE status = 'awaiting_subagent'"
    ).fetchone()
    awaiting_count = int(awaiting_row["count"] or 0)
    detail["awaiting_subagent_jobs"] = awaiting_count
    backlog_messages = []
    max_awaiting = max(1, int(os.environ.get("VECTOR_LAKE_MAX_AWAITING_SUBAGENT_JOBS", "500")))
    if auto_ingest_enabled:
        max_awaiting = min(max_awaiting, 1)
    if awaiting_count > max_awaiting:
        backlog_messages.append(f"count={awaiting_count}")
    oldest_awaiting = _parse_dt(awaiting_row["oldest"])
    if oldest_awaiting is not None:
        awaiting_age = max(0, int((datetime.now(timezone.utc) - oldest_awaiting).total_seconds()))
        detail["oldest_awaiting_subagent_age_seconds"] = awaiting_age
        max_age = max(60, int(os.environ.get("VECTOR_LAKE_MAX_AWAITING_SUBAGENT_AGE_SECONDS", "86400")))
        if auto_ingest_enabled:
            max_age = min(max_age, 60)
        if awaiting_age > max_age:
            backlog_messages.append(f"oldest={awaiting_age}s")
    if backlog_messages:
        message = "subagent_backlog:" + ",".join(backlog_messages)
        if (
            not auto_ingest_enabled
            and os.environ.get("VECTOR_LAKE_SUBAGENT_BACKLOG_BLOCKING") == "1"
        ):
            issues.append(message)
        else:
            warnings.append(message)

    if auto_ingest_enabled:
        processing_row = conn.execute(
            "SELECT COUNT(*) AS count, COALESCE(MIN(updated_at), '') AS oldest "
            "FROM jobs WHERE status = 'subagent_processing'"
        ).fetchone()
        processing_count = int(processing_row["count"] or 0)
        detail["subagent_processing_jobs"] = processing_count
        if processing_count > 1:
            warnings.append(
                f"auto_ingest_concurrency_violation:count={processing_count}"
            )
        oldest_processing = _parse_dt(processing_row["oldest"])
        if oldest_processing is not None:
            processing_age = max(
                0,
                int((datetime.now(timezone.utc) - oldest_processing).total_seconds()),
            )
            detail["oldest_subagent_processing_age_seconds"] = processing_age
            try:
                configured_timeout = int(
                    auto_ingest_config.get("timeout_seconds", 1200)
                )
            except (TypeError, ValueError):
                configured_timeout = 1200
            max_processing_age = max(120, configured_timeout + 120)
            if processing_age > max_processing_age:
                warnings.append(
                    "auto_ingest_processing_stalled:"
                    f"count={processing_count},oldest={processing_age}s"
                )

    reconcile_marker = meta_dir / "wiki_reconcile_required.json"
    if reconcile_marker.exists():
        try:
            reconcile_state = json.loads(reconcile_marker.read_text(encoding="utf-8"))
            generation = int(reconcile_state.get("generation", 0))
            detail["wiki_reconcile_generation"] = generation
            issues.append(f"wiki_reconcile_required:generation={generation}")
        except Exception as exc:
            issues.append(f"wiki_reconcile_marker_unreadable:{exc}")

    status_path = meta_dir / ".watchdog_status.json"
    if status_path.exists():
        try:
            status = _read_watchdog_status(status_path)
            now_utc = datetime.now(timezone.utc)
            updated_at = _parse_dt(status.get("updated_at"))
            age = None
            if updated_at is not None:
                age = max(0, int((now_utc - updated_at).total_seconds()))
            detail["watchdog_age_seconds"] = age
            detail["watchdog_status"] = status.get("status")
            if age is None or age > max_watchdog_age_seconds:
                issues.append(f"watchdog_stale:{age if age is not None else 'unknown'}")

            status_schema = int(status.get("schema_version") or 0)
            if status_schema >= 3:
                from vector_lake.watchdog_status import _process_is_alive

                process_id = status.get("process_id")
                process_alive = _process_is_alive(process_id)
                detail["watchdog_process_id"] = process_id
                detail["watchdog_process_alive"] = process_alive
                if not process_alive:
                    issues.append(
                        "watchdog_process_not_alive:"
                        f"{process_id if process_id is not None else 'missing'}"
                    )

            components = status.get("components")
            unhealthy_states = {"error", "halted", "stopped"}
            unhealthy_components: list[str] = []
            paused_components: dict[str, str] = {}
            effective_component_statuses: dict[str, str] = {}
            if isinstance(components, dict) and components:
                component_max_age = _bounded_env_int(
                    "VECTOR_LAKE_WATCHDOG_COMPONENT_MAX_AGE_SECONDS",
                    default=max(5, int(max_watchdog_age_seconds)),
                    minimum=5,
                )
                component_ages: dict[str, int | None] = {}
                stale_components: list[str] = []
                for name, raw_component in components.items():
                    component = raw_component if isinstance(raw_component, dict) else {}
                    heartbeat = _parse_dt(
                        component.get("heartbeat_at") or component.get("updated_at")
                    )
                    component_age = (
                        max(0, int((now_utc - heartbeat).total_seconds()))
                        if heartbeat is not None
                        else None
                    )
                    component_name = str(name)
                    component_ages[component_name] = component_age
                    effective_status, pause_reason = (
                        _effective_watchdog_component_status(
                            component_name,
                            component,
                        )
                    )
                    effective_component_statuses[component_name] = effective_status
                    if pause_reason:
                        paused_components[component_name] = pause_reason
                    if component_age is None or component_age > component_max_age:
                        stale_components.append(component_name)
                    if effective_status in unhealthy_states:
                        unhealthy_components.append(component_name)

                required_components = {
                    item.strip()
                    for item in os.environ.get(
                        "VECTOR_LAKE_WATCHDOG_REQUIRED_COMPONENTS",
                        "watchdog,outbox,scheduler,ingest",
                    ).split(",")
                    if item.strip()
                }
                if auto_ingest_enabled:
                    required_components.add("auto_ingest")
                missing_components = sorted(required_components - set(components))
                detail["watchdog_component_max_age_seconds"] = component_max_age
                detail["watchdog_component_ages_seconds"] = component_ages
                detail["watchdog_component_effective_statuses"] = (
                    effective_component_statuses
                )
                detail["watchdog_paused_components"] = paused_components
                detail["watchdog_missing_components"] = missing_components
                for component_name, pause_reason in sorted(paused_components.items()):
                    warnings.append(
                        f"watchdog_component_paused:{component_name}:{pause_reason}"
                    )
                for component_name in sorted(stale_components):
                    component_age = component_ages[component_name]
                    issues.append(
                        "watchdog_component_stale:"
                        f"{component_name}:"
                        f"{component_age if component_age is not None else 'unknown'}"
                    )
                if missing_components:
                    missing_message = "watchdog_components_missing:" + ",".join(
                        missing_components
                    )
                    if auto_ingest_enabled and "auto_ingest" in missing_components:
                        issues.append(missing_message)
                    else:
                        warnings.append(missing_message)
            else:
                detail["watchdog_status_schema"] = "legacy"
                warnings.append("watchdog_component_status_legacy")

            aggregate_status = str(status.get("status", "")).lower()
            aggregate_is_paused = bool(
                aggregate_status in unhealthy_states
                and not unhealthy_components
                and paused_components
                and any(
                    str((components.get(name) or {}).get("status", "")).lower()
                    == aggregate_status
                    for name in paused_components
                )
            )
            if (
                (aggregate_status in unhealthy_states and not aggregate_is_paused)
                or unhealthy_components
            ):
                issues.append(
                    "watchdog_unhealthy:"
                    + (",".join(sorted(unhealthy_components)) or str(status.get("status")))
                )
        except Exception as exc:
            issues.append(f"watchdog_status_unreadable:{exc}")
    else:
        warnings.append("watchdog_status_missing")

    excluded = {"index.md", "log.md", "overview.md", "orphan_pages.md", "wiki_link_stats.md", "synthesis_log.md"}
    wiki_dir = get_wiki_dir()
    diagnostic_wiki_paths = (
        diagnostic_snapshot.wiki_paths
        if diagnostic_snapshot is not None
        else iter_markdown_files(wiki_dir)
    )
    wiki_paths = {
        path.stem: path
        for path in diagnostic_wiki_paths
        if path.name.casefold() not in excluded
        and not path.name.casefold().startswith("system_")
    }
    wiki_keys = set(wiki_paths)
    canonical_entities_by_page = _canonical_snapshot(conn, db_path)
    canonical_keys = set(canonical_entities_by_page)
    index_path = get_index_path()
    index_available = index_path.exists()
    index_data: dict[str, Any] = {"nodes": {}}
    if diagnostic_snapshot is not None:
        index_data = diagnostic_snapshot.index_data
        if diagnostic_snapshot.projection_status == "committed_current":
            index_error = None
            detail["projection_pair"] = "committed_current"
        else:
            index_error = RuntimeError(
                diagnostic_snapshot.projection_error
                or "diagnostic_projection_unavailable"
            )
            detail["projection_pair"] = "invalid"
            detail["projection_pair_error"] = str(index_error)
            issues.append(
                "projection_pair_invalid:unavailable:" + str(index_error)
            )
        if index_error is None:
            index_keys = {
                key for key in index_data.get("nodes", {})
                if not str(key).startswith("System_")
            }
        else:
            index_keys = set()
    elif index_path.exists():
        from vector_lake.indexer import (
            ProjectionPairContractError,
            read_committed_index_snapshot,
        )

        try:
            index_data = read_committed_index_snapshot(
                index_path,
                lock_timeout=1.0,
                connection=conn,
                _acquire_lock=False,
            )
            index_error = None
            detail["projection_pair"] = "committed_current"
        except Exception as exc:
            detail["projection_pair"] = "invalid"
            detail["projection_pair_error"] = str(exc)
            issue_kind = (
                "contract"
                if isinstance(exc, ProjectionPairContractError)
                else "unavailable"
            )
            issues.append(f"projection_pair_invalid:{issue_kind}:{exc}")
            # Diagnostics still inspect the raw artifact so drift detail remains
            # useful; operational consumers never use this low-level fallback.
            index_data, index_error = _index_snapshot(index_path)
        if index_error is None:
            index_keys = {
                key for key in index_data.get("nodes", {})
                if not str(key).startswith("System_")
            }
        else:
            index_keys = set()
            issues.append(f"index_unreadable:{index_error}")
    else:
        index_keys = set()
        detail["projection_pair"] = "missing"
        warnings.append("index_missing")

    drift = {
        "wiki": len(wiki_keys),
        "index": len(index_keys),
        "canonical": len(canonical_keys),
        "missing_index": len(canonical_keys - index_keys) if index_available else 0,
        "extra_index": len(index_keys - canonical_keys) if index_available else 0,
        "missing_canonical": len(wiki_keys - canonical_keys),
        "extra_canonical": len(canonical_keys - wiki_keys),
    }
    detail["projection_drift"] = drift
    if any(drift[key] for key in ("missing_index", "extra_index", "missing_canonical", "extra_canonical")):
        issues.append(
            "projection_drift:"
            f"missing_index={drift['missing_index']},"
            f"extra_index={drift['extra_index']},"
            f"missing_canonical={drift['missing_canonical']},"
            f"extra_canonical={drift['extra_canonical']}"
        )

    if index_available and detail.get("projection_pair") == "committed_current":
        state_row = conn.execute(
            "SELECT status, projection_generation, expected_row_count, "
            "expected_corpus_sha256, updated_at FROM search_projection_state_v8 "
            "WHERE singleton = 1"
        ).fetchone()
        actual_fts_count = int(
            conn.execute("SELECT COUNT(*) FROM wiki_search_index").fetchone()[0]
        )
        manifest_generation = str(
            (index_data.get("projection_manifest") or {}).get("generation") or ""
        )
        search_state = {
            "status": str(state_row["status"] if state_row is not None else "missing"),
            "projection_generation": str(
                state_row["projection_generation"] if state_row is not None else ""
            ),
            "manifest_generation": manifest_generation,
            "expected_row_count": int(
                state_row["expected_row_count"] if state_row is not None else 0
            ),
            "actual_row_count": actual_fts_count,
            "expected_corpus_sha256": str(
                state_row["expected_corpus_sha256"] if state_row is not None else ""
            ),
        }
        detail["search_projection"] = search_state
        if search_state["status"] != "ready":
            issues.append(f"fts_projection_state:{search_state['status']}")
        if search_state["projection_generation"] != manifest_generation:
            issues.append("fts_projection_generation_mismatch")
        if search_state["expected_row_count"] != actual_fts_count:
            issues.append(
                "fts_projection_row_count_mismatch:"
                f"expected={search_state['expected_row_count']},actual={actual_fts_count}"
            )

        if deep_projection_checks:
            from vector_lake.indexer import _search_projection_upserts
            from vector_lake.search_projection_contract import fts_corpus_sha256

            expected_fts_rows = _search_projection_upserts(index_data)
            actual_fts_rows = [
                tuple(str(value) for value in row)
                for row in conn.execute(
                    "SELECT node_key, title, summary, text FROM wiki_search_index "
                    "ORDER BY node_key"
                )
            ]
            expected_fts_digest = fts_corpus_sha256(expected_fts_rows)
            actual_fts_digest = fts_corpus_sha256(actual_fts_rows)
            search_state["computed_expected_corpus_sha256"] = expected_fts_digest
            search_state["actual_corpus_sha256"] = actual_fts_digest
            if (
                search_state["expected_corpus_sha256"] != expected_fts_digest
                or actual_fts_digest != expected_fts_digest
            ):
                issues.append("fts_projection_corpus_mismatch")

    if deep_projection_checks:
        from vector_lake import governance_store
        from vector_lake.claim_extractor import extract_page_objects
        from vector_lake.indexer import (
            ProjectionSnapshotChanged,
            _entity_to_index_node,
            claim_graph_projection_parity,
        )
        from vector_lake.wiki_utils import split_frontmatter

        active_projection_rows = conn.execute(
            "SELECT filename, mutation_type, payload_text, base_version "
            "FROM mutation_outbox WHERE status IN ('pending', 'processing') "
            "ORDER BY id DESC"
        ).fetchall()
        active_projection_intents = {}
        for row in active_projection_rows:
            page_key = Path(str(row["filename"])).stem
            active_projection_intents.setdefault(page_key, row)

        wiki_content_drift: list[str] = []
        unreadable_wiki: list[str] = []
        shared_wiki_keys = wiki_keys & canonical_keys
        canonical_versions = {
            page_key: governance_store._canonical_entity_records_version(
                canonical_entities_by_page[page_key]
            )
            for page_key in shared_wiki_keys
        }
        managed_base_versions = {}
        for page_key, row in active_projection_intents.items():
            if page_key not in canonical_versions:
                continue
            if str(row["mutation_type"]) != "update" or row["payload_text"] is None:
                continue
            base_version = row["base_version"]
            if base_version is None:
                continue
            try:
                payload_version = governance_store.canonical_page_version_from_content(
                    str(row["filename"]),
                    str(row["payload_text"]),
                )
            except Exception:
                continue
            if payload_version == canonical_versions.get(page_key):
                managed_base_versions[page_key] = str(base_version)
        active_wiki_paths = {
            _wiki_cache_key(wiki_paths[page_key]) for page_key in shared_wiki_keys
        }
        _prune_wiki_version_cache(active_wiki_paths)
        managed_reconciliation_drift: set[str] = set()
        managed_wiki_pages: set[str] = set()
        for page_key in sorted(shared_wiki_keys):
            path = wiki_paths[page_key]
            observed, observed_error = _wiki_projection_version(governance_store, path)
            if observed_error is not None:
                unreadable_wiki.append(page_key)
                continue
            if observed != canonical_versions.get(page_key):
                if observed == managed_base_versions.get(page_key):
                    managed_reconciliation_drift.add(page_key)
                    managed_wiki_pages.add(page_key)
                else:
                    wiki_content_drift.append(page_key)

        index_content_drift: list[str] = []
        for page_key in sorted(index_keys & canonical_keys):
            node = index_data.get("nodes", {}).get(page_key) or {}
            expected_signatures = set()
            for entity_id, entity in canonical_entities_by_page.get(page_key, []):
                try:
                    projected_key, projected_node = _entity_to_index_node(entity, entity_id)
                except Exception:
                    continue
                if projected_key == page_key:
                    expected_signatures.add(_index_projection_signature(projected_node))
            if _index_projection_signature(node) not in expected_signatures:
                managed_index = False
                if page_key in managed_wiki_pages:
                    try:
                        path = wiki_paths[page_key]
                        content = path.read_text(encoding="utf-8")
                        frontmatter, body = split_frontmatter(content)
                        observed_entities = extract_page_objects(
                            path.name,
                            frontmatter,
                            body,
                        ).get("entities", [])
                        observed_signatures = {
                            _index_projection_signature(projected_node)
                            for entity in observed_entities
                            for projected_key, projected_node in [
                                _entity_to_index_node(entity, str(entity.get("entity_id") or ""))
                            ]
                            if projected_key == page_key
                        }
                        managed_index = _index_projection_signature(node) in observed_signatures
                    except Exception:
                        managed_index = False
                if managed_index:
                    managed_reconciliation_drift.add(page_key)
                else:
                    index_content_drift.append(page_key)

        content_drift = {
            "wiki_canonical": len(wiki_content_drift),
            "index_canonical": len(index_content_drift),
            "unreadable_wiki": len(unreadable_wiki),
            "wiki_samples": wiki_content_drift[:10],
            "index_samples": index_content_drift[:10],
            "unreadable_samples": unreadable_wiki[:10],
            "managed_reconciliation": len(managed_reconciliation_drift),
            "managed_reconciliation_samples": sorted(managed_reconciliation_drift)[:10],
        }
        detail["projection_content_drift"] = content_drift
        if wiki_content_drift or index_content_drift or unreadable_wiki:
            issues.append(
                "projection_content_drift:"
                f"wiki_canonical={len(wiki_content_drift)},"
                f"index_canonical={len(index_content_drift)},"
                f"unreadable_wiki={len(unreadable_wiki)}"
            )
        if managed_reconciliation_drift:
            warnings.append(f"projection_reconciliation_pending:{len(managed_reconciliation_drift)}")

        try:
            claim_graph_drift = claim_graph_projection_parity(connection=conn)
            detail["claim_graph_projection_drift"] = claim_graph_drift
            if any(
                claim_graph_drift[key]
                for key in ("missing_nodes", "extra_nodes", "missing_edges", "extra_edges")
            ):
                message = (
                    "claim_graph_projection_drift:"
                    f"missing_nodes={claim_graph_drift['missing_nodes']},"
                    f"extra_nodes={claim_graph_drift['extra_nodes']},"
                    f"missing_edges={claim_graph_drift['missing_edges']},"
                    f"extra_edges={claim_graph_drift['extra_edges']}"
                )
                if active_projection_rows:
                    warnings.append("claim_graph_reconciliation_pending:" + message)
                else:
                    issues.append(message)
        except ProjectionSnapshotChanged as exc:
            warnings.append(f"claim_graph_projection_transient:{exc}")
        except Exception as exc:
            issues.append(f"claim_graph_projection_unavailable:{exc}")

    strict_timeline_parity = os.environ.get("VECTOR_LAKE_TIMELINE_PARITY_BLOCKING") == "1"
    if deep_projection_checks or strict_timeline_parity:
        from vector_lake.tool_timeline import timeline_projection_parity

        timeline_drift = timeline_projection_parity(connection=conn)
        detail["timeline_projection_drift"] = timeline_drift
        if timeline_drift["missing"] or timeline_drift["extra"]:
            message = (
                "timeline_projection_drift:"
                f"missing={timeline_drift['missing']},extra={timeline_drift['extra']}"
            )
            if strict_timeline_parity:
                issues.append(message)
            else:
                warnings.append(message)

    if owns_connection:
        conn.close()
    return {
        "ok": not issues,
        "status": "blocked" if issues else ("degraded" if warnings else "ready"),
        "issues": issues,
        "warnings": warnings,
        "detail": detail,
    }


def _evidence_foundation_metrics(conn) -> dict[str, Any]:
    """Summarize strict evidence-chain coverage without promoting legacy claims."""
    def count(sql: str) -> int:
        row = conn.execute(sql).fetchone()
        return int(row[0] or 0)

    claim_total = count("SELECT COUNT(*) FROM claims")
    evidence_total = count("SELECT COUNT(*) FROM evidence")
    source_total = count("SELECT COUNT(*) FROM sources")
    metrics = {
        "claim_total": claim_total,
        "claim_with_evidence_refs": count(
            "SELECT COUNT(*) FROM claims WHERE "
            "COALESCE(json_array_length(json_extract(data_json, '$.evidence_ids')), 0) > 0"
        ),
        "claim_with_extraction_run": count(
            "SELECT COUNT(*) FROM claims WHERE "
            "NULLIF(json_extract(data_json, '$.extraction_run_id'), '') IS NOT NULL"
        ),
        "claim_with_supported_assessment": count(
            "SELECT COUNT(DISTINCT c.claim_id) FROM claims AS c "
            "JOIN claim_assessments AS a ON a.claim_id = c.claim_id "
            "WHERE a.outcome = 'supported'"
        ),
        "evidence_total": evidence_total,
        "evidence_with_raw_locator": count(
            "SELECT COUNT(*) FROM evidence WHERE "
            "json_type(data_json, '$.source_locator') = 'object' AND "
            "COALESCE(json_extract(data_json, '$.source_locator.kind'), 'unresolved') != 'unresolved'"
        ),
        "evidence_lineage_safe": count(
            "SELECT COUNT(*) FROM evidence WHERE "
            "json_extract(data_json, '$.lineage_safe') = 1"
        ),
        "source_total": source_total,
        "source_integrity_verified": count(
            "SELECT COUNT(*) FROM sources WHERE "
            "json_extract(data_json, '$.integrity_status') = 'verified' AND "
            "length(json_extract(data_json, '$.content_hash')) = 64"
        ),
        "source_artifact_total": count("SELECT COUNT(*) FROM source_artifacts"),
        "source_artifact_verified": count(
            "SELECT COUNT(*) FROM source_artifacts WHERE "
            "integrity_status = 'verified' AND length(sha256) = 64"
        ),
        "extraction_run_total": count("SELECT COUNT(*) FROM extraction_runs"),
    }

    def ratio(numerator: int, denominator: int) -> float:
        return round(numerator / denominator, 4) if denominator else 1.0

    metrics.update(
        {
            "claim_evidence_coverage": ratio(metrics["claim_with_evidence_refs"], claim_total),
            "claim_extraction_coverage": ratio(metrics["claim_with_extraction_run"], claim_total),
            "claim_assessment_coverage": ratio(
                metrics["claim_with_supported_assessment"], claim_total
            ),
            "evidence_raw_locator_coverage": ratio(
                metrics["evidence_with_raw_locator"], evidence_total
            ),
            "evidence_lineage_coverage": ratio(metrics["evidence_lineage_safe"], evidence_total),
            "source_integrity_coverage": ratio(
                metrics["source_integrity_verified"], source_total
            ),
        }
    )
    return metrics


def _runtime_unsupported_governance(conn) -> dict[str, int]:
    """Classify unsupported runtime memories by active claim governance."""
    from vector_lake.governance_metrics import claim_governance_version

    unsupported_rows = conn.execute(
        "SELECT memory_id, NULLIF(json_extract(data_json, '$.source_claim_id'), '') "
        "AS source_claim_id FROM operational_memory "
        "WHERE json_extract(data_json, '$.validity_state') = 'unsupported'"
    ).fetchall()
    if not unsupported_rows:
        return {"total": 0, "managed": 0, "unmanaged": 0}

    claims_by_id = {
        str(row["claim_id"]): json.loads(row["data_json"])
        for row in conn.execute(
            "SELECT c.claim_id, c.data_json FROM claims AS c JOIN ("
            "SELECT DISTINCT json_extract(data_json, '$.source_claim_id') AS claim_id "
            "FROM operational_memory "
            "WHERE json_extract(data_json, '$.validity_state') = 'unsupported'"
            ") AS runtime_claims ON runtime_claims.claim_id = c.claim_id"
        )
    }
    governance_by_claim: dict[str, list[dict[str, Any]]] = {}
    for row in conn.execute(
        "SELECT data_json FROM governance_queue "
        "WHERE json_extract(data_json, '$.type') = 'evidence-gap' "
        "AND json_extract(data_json, '$.status') = 'acknowledged' "
        "AND json_extract(data_json, '$.claim_id') IS NOT NULL"
    ):
        item = json.loads(row["data_json"])
        claim_id = str(item.get("claim_id") or "")
        governance_by_claim.setdefault(claim_id, []).append(item)

    now = datetime.now(timezone.utc)
    managed = 0
    for row in unsupported_rows:
        claim_id = str(row["source_claim_id"] or "")
        claim = claims_by_id.get(claim_id)
        if claim is None:
            continue
        claim_version = claim_governance_version(claim)
        if any(
            str(item.get("owner") or "").strip()
            and (due_at := _parse_dt(item.get("due_at"))) is not None
            and due_at >= now
            and str(item.get("claim_version") or "") == claim_version
            for item in governance_by_claim.get(claim_id, [])
        ):
            managed += 1

    total = len(unsupported_rows)
    return {"total": total, "managed": managed, "unmanaged": total - managed}


def _assess_decision_scope(
    conn,
    decision_id: str,
    index_data: dict[str, Any],
    initial_issues: list[str],
) -> dict[str, Any]:
    """Evaluate only evidence and governance debt mapped to one verified decision."""
    from vector_lake.decision_registry import get_critical_decision

    issues = list(initial_issues)
    warnings: list[str] = []
    detail: dict[str, Any] = {"scope": "critical_decision", "decision_id": decision_id}
    decision = get_critical_decision(decision_id)
    if not decision or not decision.get("registry_verified") or decision.get("status") != "active":
        issues.append(f"critical_decision_unverified:{decision_id}")
        detail["decision"] = decision
    else:
        detail["decision"] = decision

    graph_state = dict((index_data or {}).get("graph_state") or {})
    detail["graph_state"] = graph_state
    if graph_state.get("dirty") is True:
        warnings.append(
            f"global_graph_debt_outside_decision_scope:{graph_state.get('reason') or 'unknown'}"
        )
    elif not graph_state:
        warnings.append("graph_state_missing")

    scoped_pending = []
    for row in conn.execute(
        "SELECT item_id, data_json FROM governance_queue "
        "WHERE json_extract(data_json, '$.status') = 'pending'"
    ).fetchall():
        item = json.loads(row["data_json"])
        if decision_id in list(item.get("critical_decision_refs") or []):
            scoped_pending.append(str(row["item_id"]))
    detail["scoped_pending_governance_ids"] = scoped_pending
    if scoped_pending:
        issues.append(f"decision_governance_pending:{len(scoped_pending)}")

    claim_refs = list((decision or {}).get("claim_refs") or [])
    detail["claim_refs"] = claim_refs
    claim_checks = []
    if decision and not claim_refs:
        issues.append("decision_claim_scope_missing")
    for claim_id in claim_refs:
        check = {
            "claim_id": claim_id,
            "claim_exists": False,
            "evidence_complete": False,
            "source_integrity_complete": False,
            "raw_locator_complete": False,
            "lineage_safe": False,
            "assessment_supported": False,
        }
        row = conn.execute(
            "SELECT data_json FROM claims WHERE claim_id = ?", (claim_id,)
        ).fetchone()
        if row is None:
            issues.append(f"decision_claim_missing:{claim_id}")
            claim_checks.append(check)
            continue
        check["claim_exists"] = True
        claim = json.loads(row["data_json"])
        evidence_ids = list(dict.fromkeys(claim.get("evidence_ids") or []))
        source_ids = list(dict.fromkeys(claim.get("source_ids") or []))
        evidence_records = []
        for evidence_id in evidence_ids:
            evidence_row = conn.execute(
                "SELECT data_json FROM evidence WHERE evidence_id = ?", (evidence_id,)
            ).fetchone()
            if evidence_row is not None:
                evidence = json.loads(evidence_row["data_json"])
                evidence_records.append(evidence)
                source_id = str(evidence.get("source_id") or "")
                if source_id and source_id not in source_ids:
                    source_ids.append(source_id)
        check["evidence_complete"] = bool(evidence_ids) and len(evidence_records) == len(evidence_ids)
        check["raw_locator_complete"] = bool(evidence_records) and all(
            isinstance(record.get("source_locator"), dict)
            and record["source_locator"].get("kind") != "unresolved"
            for record in evidence_records
        )
        check["lineage_safe"] = bool(evidence_records) and all(
            record.get("lineage_safe") is True for record in evidence_records
        )
        source_records = []
        for source_id in source_ids:
            source_row = conn.execute(
                "SELECT data_json FROM sources WHERE source_id = ?", (source_id,)
            ).fetchone()
            if source_row is not None:
                source_records.append(json.loads(source_row["data_json"]))
        check["source_integrity_complete"] = bool(source_ids) and len(source_records) == len(source_ids) and all(
            record.get("integrity_status") == "verified"
            and isinstance(record.get("content_hash"), str)
            and len(record["content_hash"]) == 64
            for record in source_records
        )
        check["assessment_supported"] = conn.execute(
            "SELECT 1 FROM claim_assessments WHERE claim_id = ? AND outcome = 'supported' LIMIT 1",
            (claim_id,),
        ).fetchone() is not None
        for field in (
            "evidence_complete",
            "source_integrity_complete",
            "raw_locator_complete",
            "lineage_safe",
            "assessment_supported",
        ):
            if not check[field]:
                issues.append(f"decision_claim_{field}_failed:{claim_id}")
        claim_checks.append(check)
    detail["claim_checks"] = claim_checks
    detail["global_debt_policy"] = "reported_only_when_not_mapped_to_decision"
    status = "not_ready" if issues else ("degraded" if warnings else "ready")
    return {
        "ready": status == "ready",
        "status": status,
        "issues": issues,
        "warnings": warnings,
        "detail": detail,
    }


def assess_semantic_readiness(
    index_data: dict[str, Any] | None = None,
    decision_id: str | None = None,
    diagnostic_snapshot=None,
) -> dict[str, Any]:
    """Assess whether governed knowledge is ready for decision-support use.

    This surface is deliberately separate from runtime health. Semantic debt
    never blocks canonical repair writes, while infrastructure health never
    implies that claims or topology are ready for business decisions. An
    unsupported runtime claim blocks only while it lacks active, version-bound
    governance ownership.
    """
    from vector_lake.wiki_utils import get_index_path

    issues: list[str] = []
    warnings: list[str] = []
    detail: dict[str, Any] = {}
    owns_connection = diagnostic_snapshot is None
    if diagnostic_snapshot is not None:
        conn = diagnostic_snapshot.connection
        db_path = diagnostic_snapshot.db_path
        detail["diagnostic_snapshot"] = diagnostic_snapshot.metadata()
        if index_data is None:
            index_data = diagnostic_snapshot.index_data
        if diagnostic_snapshot.projection_status != "committed_current":
            issues.append(
                "projection_pair_invalid:unavailable:"
                + (
                    diagnostic_snapshot.projection_error
                    or "diagnostic_projection_unavailable"
                )
            )
    else:
        try:
            conn, db_path = _open_runtime_database_read_only()
        except RuntimeDatabaseBlocked as exc:
            return {
                "ready": False,
                "status": "not_ready",
                "issues": [f"database_blocked:{exc}"],
                "warnings": [],
                "detail": {
                    "database_access": "read_only",
                    "database_error": str(exc),
                },
            }

    if index_data is None:
        index_path = get_index_path()
        if index_path.exists():
            index_data, index_error = _index_snapshot(index_path)
            if index_error is not None:
                issues.append(f"index_unreadable:{index_error}")
        else:
            index_data = {}
            issues.append("index_missing")

    normalized_decision_id = str(decision_id or "").strip()
    if normalized_decision_id:
        result = _assess_decision_scope(
            conn,
            normalized_decision_id,
            dict(index_data or {}),
            issues,
        )
        if diagnostic_snapshot is not None:
            result.setdefault("detail", {})["diagnostic_snapshot"] = (
                diagnostic_snapshot.metadata()
            )
        if owns_connection:
            conn.close()
        return result

    graph_state = dict((index_data or {}).get("graph_state") or {})
    detail["graph_state"] = graph_state
    if graph_state.get("dirty") is True:
        issues.append(f"graph_topology_dirty:{graph_state.get('reason') or 'unknown'}")
    elif not graph_state:
        warnings.append("graph_state_missing")
    detail["graph_insight_count"] = len((index_data or {}).get("graph_insights") or [])

    pending_rows = conn.execute(
        "SELECT json_extract(data_json, '$.type') AS item_type, COUNT(*) AS count "
        "FROM governance_queue WHERE json_extract(data_json, '$.status') = 'pending' "
        "GROUP BY item_type"
    ).fetchall()
    pending_by_type = {
        str(row["item_type"] or "unknown"): int(row["count"] or 0)
        for row in pending_rows
    }
    pending_total = sum(pending_by_type.values())
    critical_types = {"contradiction", "evidence-gap", "publish-candidate"}
    critical_pending = sum(pending_by_type.get(item_type, 0) for item_type in critical_types)
    detail["pending_governance_by_type"] = pending_by_type
    detail["pending_governance_total"] = pending_total
    detail["critical_pending_governance"] = critical_pending
    max_pending = max(
        0,
        int(os.environ.get("VECTOR_LAKE_MAX_PENDING_GOVERNANCE_ITEMS", "500")),
    )
    if critical_pending:
        issues.append(f"critical_governance_pending:{critical_pending}")
    if pending_total > max_pending:
        issues.append(f"governance_backlog:{pending_total}>{max_pending}")
    elif pending_total:
        warnings.append(f"governance_pending:{pending_total}")

    validity_counts = {
        str(row["validity_state"] or "unknown"): int(row["count"] or 0)
        for row in conn.execute(
            "SELECT json_extract(data_json, '$.validity_state') AS validity_state, "
            "COUNT(*) AS count FROM operational_memory GROUP BY validity_state"
        )
    }
    detail["runtime_validity_state_counts"] = validity_counts
    unsupported = validity_counts.get("unsupported", 0)
    if unsupported:
        unsupported_governance = _runtime_unsupported_governance(conn)
        detail["runtime_unsupported_governance"] = unsupported_governance
        max_unmanaged = max(
            0,
            int(
                os.environ.get(
                    "VECTOR_LAKE_MAX_UNMANAGED_UNSUPPORTED_RUNTIME_CLAIMS",
                    os.environ.get("VECTOR_LAKE_MAX_UNSUPPORTED_RUNTIME_CLAIMS", "0"),
                )
            ),
        )
        unmanaged = unsupported_governance["unmanaged"]
        if unmanaged > max_unmanaged:
            issues.append(
                f"unmanaged_unsupported_runtime_claims:{unmanaged}>{max_unmanaged}"
            )
        managed = unsupported_governance["managed"]
        if managed:
            warnings.append(f"managed_unsupported_runtime_claims:{managed}")
    if validity_counts.get("provisional", 0):
        warnings.append(f"provisional_runtime_claims:{validity_counts['provisional']}")
    if validity_counts.get("expired", 0):
        warnings.append(f"expired_runtime_claims:{validity_counts['expired']}")

    foundation = _evidence_foundation_metrics(conn)
    detail["evidence_foundation"] = foundation
    coverage_thresholds = {
        "claim_evidence_coverage": "VECTOR_LAKE_MIN_CLAIM_EVIDENCE_COVERAGE",
        "claim_extraction_coverage": "VECTOR_LAKE_MIN_CLAIM_EXTRACTION_COVERAGE",
        "claim_assessment_coverage": "VECTOR_LAKE_MIN_CLAIM_ASSESSMENT_COVERAGE",
        "evidence_raw_locator_coverage": "VECTOR_LAKE_MIN_EVIDENCE_LOCATOR_COVERAGE",
        "evidence_lineage_coverage": "VECTOR_LAKE_MIN_EVIDENCE_LINEAGE_COVERAGE",
        "source_integrity_coverage": "VECTOR_LAKE_MIN_SOURCE_INTEGRITY_COVERAGE",
    }
    denominator_by_metric = {
        "claim_evidence_coverage": foundation["claim_total"],
        "claim_extraction_coverage": foundation["claim_total"],
        "claim_assessment_coverage": foundation["claim_total"],
        "evidence_raw_locator_coverage": foundation["evidence_total"],
        "evidence_lineage_coverage": foundation["evidence_total"],
        "source_integrity_coverage": foundation["source_total"],
    }
    for metric, env_name in coverage_thresholds.items():
        threshold = min(1.0, max(0.0, float(os.environ.get(env_name, "0.95"))))
        coverage = float(foundation[metric])
        if denominator_by_metric[metric] and coverage < threshold:
            warnings.append(f"{metric}_low:{coverage:.4f}<{threshold:.4f}")

    awaiting_row = conn.execute(
        "SELECT COUNT(*) AS count, COALESCE(MIN(updated_at), '') AS oldest "
        "FROM jobs WHERE status = 'awaiting_subagent'"
    ).fetchone()
    awaiting_count = int(awaiting_row["count"] or 0)
    detail["awaiting_subagent_jobs"] = awaiting_count
    oldest_awaiting = _parse_dt(awaiting_row["oldest"])
    if oldest_awaiting is not None:
        awaiting_age = max(
            0,
            int((datetime.now(timezone.utc) - oldest_awaiting).total_seconds()),
        )
        detail["oldest_awaiting_subagent_age_seconds"] = awaiting_age
        max_age = max(
            60,
            int(os.environ.get("VECTOR_LAKE_MAX_AWAITING_SUBAGENT_AGE_SECONDS", "86400")),
        )
        if awaiting_age > max_age:
            issues.append(f"semantic_ingest_backlog:oldest={awaiting_age}s>{max_age}s")

    status = "not_ready" if issues else ("degraded" if warnings else "ready")
    if owns_connection:
        conn.close()
    return {
        "ready": status == "ready",
        "status": status,
        "issues": issues,
        "warnings": warnings,
        "detail": detail,
    }


def enforce_runtime_write_health(validation_mode: str = "full"):
    if os.environ.get("VECTOR_LAKE_DISABLE_WRITE_HEALTH_GATE") == "1":
        return
    if validation_mode == "schema":
        return

    # A full write is an explicitly authorized mutation path. It may bootstrap
    # or migrate storage before the read-only health assessment; diagnostic
    # callers never cross this boundary.
    from vector_lake.db_store import init_db

    init_db()
    try:
        cache_seconds = float(
            os.environ.get("VECTOR_LAKE_WRITE_HEALTH_CACHE_SECONDS", "0")
        )
    except (TypeError, ValueError):
        cache_seconds = 0.0
    cache_seconds = max(0.0, min(30.0, cache_seconds))
    token = _write_health_surface_token()
    health = _recent_write_gate_health(token, cache_seconds)
    if health is None:
        with _WRITE_GATE_DEEP_LOCK:
            health = _recent_write_gate_health(token, cache_seconds)
            if health is None:
                for attempt in range(2):
                    health = assess_runtime_health(deep_projection_checks=True)
                    token_after = _write_health_surface_token()
                    if token_after == token:
                        if health.get("ok") and cache_seconds > 0:
                            with _CACHE_LOCK:
                                _WRITE_GATE_CACHE.update({
                                    "token": token,
                                    "checked_at": time.monotonic(),
                                    "health": health,
                                })
                        break
                    if attempt == 1:
                        raise RuntimeError(
                            "Vector Lake write gate could not validate a stable runtime "
                            "snapshot because projections changed during validation."
                        )
                    token = token_after
                    health = _recent_write_gate_health(token, cache_seconds)
                    if health is not None:
                        break
    if not health["ok"]:
        raise RuntimeError(
            "Vector Lake write gate blocked this mutation because runtime health is not clean. "
            "Use validation_mode='schema' for bounded repairs or set VECTOR_LAKE_DISABLE_WRITE_HEALTH_GATE=1 after explicit approval. "
            f"Issues: {'; '.join(health['issues'])}"
        )
