import os
import json
import hashlib
import logging
import re
import sqlite3
import traceback
from collections import Counter
from pathlib import Path
from datetime import datetime, timezone

from vector_lake import get_extension_root
from vector_lake.db_store import mark_file_processed
from vector_lake import governance_store
from vector_lake.merge_analysis import normalize_source_identity
from vector_lake.skeleton_parser import parse_static_skeleton
from vector_lake.wiki_utils import (
    get_meta_dir,
    get_raw_dir,
    get_wiki_dir,
    get_index_path,
    validate_wiki_filename,
)
from vector_lake.purpose_contract import (
    PurposeContractError,
    build_synthesis_proposals,
    load_purpose_contract,
    render_strategy_directive,
    validate_ingest_payload,
)

log = logging.getLogger("vector-lake-ingest")

def list_ingest_tasks(limit: int = 20, include_queued: bool = True) -> str:
    """List ingest jobs that require operator or host-subagent action."""
    from vector_lake.db_store import get_jobs_by_status

    statuses = ["awaiting_subagent"]
    if include_queued:
        statuses.insert(0, "queued")
    rows = get_jobs_by_status(statuses, limit=limit)
    if not rows:
        return "No queued or awaiting-subagent ingest jobs."
    lines = ["=== Ingest Task Queue ==="]
    for row in rows:
        payload = {}
        try:
            payload = json.loads(row.get("payload") or "{}")
        except Exception:
            payload = {}
        lines.append(
            "- "
            f"{row.get('job_id')} "
            f"status={row.get('status')} retries={row.get('retries')} "
            f"file={payload.get('filepath', '<unknown>')} "
            f"task_packet={row.get('task_packet_path') or '<not-created>'}"
        )
    return "\n".join(lines)


def claim_ingest_tasks(limit: int = 5, lease_seconds: int = 3600) -> str:
    """Lease task packets to the current host runtime and return structured work."""
    from vector_lake.db_store import claim_subagent_jobs

    claimed = claim_subagent_jobs(limit=limit, lease_seconds=lease_seconds)
    tasks = []
    for row in claimed:
        task_packet = None
        packet_path = row.get("task_packet_path")
        if packet_path:
            try:
                task_packet = json.loads(Path(packet_path).read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                task_packet = {"error": f"Unreadable task packet: {exc}"}
        if isinstance(task_packet, dict) and "error" not in task_packet:
            metadata = task_packet.setdefault("metadata", {})
            processed = metadata.setdefault("processed_data", {})
            processed.update({
                "job_id": row.get("job_id"),
                "lease_owner": row.get("lease_owner"),
                "lease_token": row.get("lease_token"),
                "lease_generation": row.get("lease_generation"),
            })
        tasks.append({
            "job_id": row.get("job_id"),
            "status": row.get("status"),
            "lease_until": row.get("lease_until"),
            "lease_owner": row.get("lease_owner"),
            "lease_token": row.get("lease_token"),
            "lease_generation": row.get("lease_generation"),
            "task_packet_path": packet_path,
            "task_packet": task_packet,
        })
    return json.dumps(tasks, ensure_ascii=False, indent=2)


def expire_ingest_tasks(max_age_seconds: int = 86400) -> str:
    """Mark stale awaiting-subagent ingest jobs as failed so they can be retried explicitly."""
    from vector_lake.db_store import expire_stale_subagent_jobs

    expired = expire_stale_subagent_jobs(max_age_seconds=max_age_seconds)
    return f"Expired {expired} awaiting-subagent ingest job(s)."


def process_ingest_task_cleanup(limit: int = 20) -> dict:
    """Replay durable task-packet cleanup without clearing unverified pointers."""
    from vector_lake import db_store
    from vector_lake.native_llm import remove_subagent_task

    result = {"claimed": 0, "completed": 0, "failed": 0, "errors": []}
    rows = db_store.claim_ingest_task_cleanup(limit=max(1, int(limit)))
    result["claimed"] = len(rows)
    for row in rows:
        cleanup_id = int(row["cleanup_id"])
        lease_args = (
            str(row["lease_owner"]),
            str(row["lease_token"]),
            int(row["lease_generation"]),
        )
        try:
            remove_subagent_task(
                str(row["task_packet_path"]),
                expected_job_id=str(row["job_id"]),
                expected_task_type="ingest",
                expected_task_id=str(row["expected_task_id"]),
            )
            if not db_store.complete_ingest_task_cleanup(cleanup_id, *lease_args):
                raise RuntimeError("cleanup lease changed before completion")
            result["completed"] += 1
        except (OSError, RuntimeError, ValueError) as exc:
            db_store.fail_ingest_task_cleanup(
                cleanup_id,
                *lease_args,
                error=str(exc),
            )
            result["failed"] += 1
            result["errors"].append(
                f"cleanup_id={cleanup_id} path={row['task_packet_path']}: {exc}"
            )
    return result


def _open_ingest_debt_preview_connection():
    """Open the existing state read-only; previews never initialize or migrate it."""
    from vector_lake import db_store

    db_path = db_store.get_db_path().resolve()
    if not db_path.is_file():
        return None, f"database_missing:{db_path}"
    connection = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    required = {
        "jobs": {
            "job_id",
            "task_type",
            "payload",
            "status",
            "retries",
            "created_at",
            "updated_at",
            "available_at",
            "lease_until",
            "lease_owner",
            "lease_token",
            "lease_generation",
            "idempotency_key",
            "task_packet_path",
        },
        "processed_files": {"filepath", "file_hash"},
        "entities": {"data_json"},
    }
    for table, columns in required.items():
        exists = connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        if exists is None:
            connection.close()
            return None, f"schema_not_ready:missing_table:{table}"
        available = {
            str(row["name"])
            for row in connection.execute(f"PRAGMA table_info({table})")
        }
        missing = sorted(columns - available)
        if missing:
            connection.close()
            return None, f"schema_not_ready:{table}:missing_columns:{','.join(missing)}"
    return connection, ""


def _source_identity_index_from_connection(connection) -> dict[str, str]:
    """Build the source identity map from an already-open read-only connection."""
    items = []
    for row in connection.execute(
        "SELECT data_json FROM entities ORDER BY entity_id"
    ):
        try:
            item = json.loads(str(row["data_json"] or "{}"))
        except (TypeError, json.JSONDecodeError):
            continue
        if (
            isinstance(item, dict)
            and str(item.get("type") or "").casefold() == "source"
            and str(item.get("status") or "").casefold() != "merged"
        ):
            items.append(item)
    wiki_dir = get_wiki_dir()
    selected: dict[str, tuple[tuple[int, int, int, str], str]] = {}
    for entity in items:
        page_key = str(entity.get("page_key") or "").strip().removesuffix(".md")
        if not page_key or not (wiki_dir / f"{page_key}.md").is_file():
            continue
        categories = {
            str(value).casefold()
            for value in (
                entity.get("categories")
                if isinstance(entity.get("categories"), list)
                else [entity.get("categories")]
            )
            if value
        }
        is_backlog = (
            str(entity.get("topic_cluster") or "").casefold() == "raw_ingest_backlog"
            or "raw_ingest_backlog" in categories
        )
        rank = (
            int(not is_backlog),
            int(not _HASH_SUFFIX.search(page_key)),
            int(str(entity.get("status") or "").casefold() == "active"),
            page_key.casefold(),
        )
        sources = entity.get("sources") or []
        if not isinstance(sources, list):
            sources = [sources]
        for source in sources:
            identity = normalize_source_identity(source)
            if not identity.startswith("raw/"):
                continue
            current = selected.get(identity)
            if current is None or rank[:3] > current[0][:3] or (
                rank[:3] == current[0][:3] and rank[3] < current[0][3]
            ):
                selected[identity] = (rank, f"{page_key}.md")
    return {
        identity: filename
        for identity, (_rank, filename) in selected.items()
    }


def reconcile_ingest_job_debt(dry_run: bool = True, limit: int = 0) -> str:
    """Classify and safely recover abandoned ingest jobs without discarding valid work."""
    from vector_lake import db_store

    preview_error = ""
    if dry_run:
        conn, preview_error = _open_ingest_debt_preview_connection()
        if conn is None:
            return json.dumps(
                {
                    "dry_run": True,
                    "selected_jobs": 0,
                    "counts": {},
                    "backup": "",
                    "preview_error": preview_error,
                    "cleanup": {
                        "claimed": 0,
                        "completed": 0,
                        "failed": 0,
                        "errors": [],
                    },
                    "concurrent_skips": [],
                    "samples": [],
                },
                ensure_ascii=False,
                indent=2,
            )
    else:
        db_store.init_db()
        conn = db_store.get_connection()
    rows = conn.execute(
        "SELECT * FROM jobs WHERE task_type = 'ingest' AND "
        "(status = 'awaiting_subagent' OR (status = 'failed' AND retries >= 3)) "
        "ORDER BY created_at, job_id"
    ).fetchall()
    selected_limit = max(0, int(limit))
    if selected_limit:
        rows = rows[:selected_limit]

    processed = {
        str(row["filepath"]): str(row["file_hash"] or "")
        for row in conn.execute("SELECT filepath, file_hash FROM processed_files")
    }
    existing_by_key = {
        str(row["idempotency_key"]): dict(row)
        for row in conn.execute(
            "SELECT job_id, status, retries, created_at, idempotency_key FROM jobs "
            "WHERE idempotency_key IS NOT NULL"
        )
    }
    plans: list[dict] = []
    requeue_groups: dict[str, list[dict]] = {}
    preview_source_identity_index = None

    for row in rows:
        record = dict(row)
        expected_state = {
            "status": record.get("status"),
            "retries": int(record.get("retries") or 0),
            "lease_generation": int(record.get("lease_generation") or 0),
            "task_packet_path": record.get("task_packet_path"),
            "lease_until": record.get("lease_until"),
            "lease_owner": record.get("lease_owner"),
            "lease_token": record.get("lease_token"),
            "updated_at": record.get("updated_at"),
            "payload": record.get("payload"),
            "idempotency_key": record.get("idempotency_key"),
        }
        try:
            payload = json.loads(record.get("payload") or "{}")
        except json.JSONDecodeError:
            payload = None
        if not isinstance(payload, dict):
            plans.append({
                "job_id": record["job_id"],
                "action": "blocked_invalid_payload",
                "reason": "payload is not a JSON object",
                "expected_state": expected_state,
            })
            continue

        raw_value = str(payload.get("filepath") or "").strip()
        if not raw_value:
            plans.append({
                "job_id": record["job_id"],
                "action": "blocked_invalid_payload",
                "reason": "filepath is missing",
                "expected_state": expected_state,
            })
            continue
        raw_path = Path(raw_value)
        if not raw_path.is_absolute():
            raw_path = get_raw_dir().parent / raw_path
        raw_path = raw_path.resolve()
        packet_path = str(record.get("task_packet_path") or "")

        if not raw_path.is_file():
            plans.append({
                "job_id": record["job_id"],
                "action": "cancel_missing_raw",
                "reason": f"raw source is missing: {raw_path}",
                "packet_path": packet_path,
                "expected_state": expected_state,
            })
            continue

        current_hash = calculate_hash(str(raw_path))
        if not current_hash:
            plans.append({
                "job_id": record["job_id"],
                "action": "blocked_unreadable_raw",
                "reason": f"raw source cannot be hashed: {raw_path}",
                "expected_state": expected_state,
            })
            continue

        processed_hash = processed.get(str(raw_path)) or processed.get(raw_value)
        if processed_hash == current_hash:
            plans.append({
                "job_id": record["job_id"],
                "action": "complete_already_processed",
                "reason": "processed_files already records the current raw hash",
                "packet_path": packet_path,
                "expected_state": expected_state,
            })
            continue

        payload_hash = str(payload.get("hash") or "")
        contract_current = (
            str(payload.get("ingest_contract_version") or "")
            == str(INGEST_CONTRACT_VERSION)
        )
        needs_requeue = (
            record.get("status") == "failed"
            or payload_hash != current_hash
            or not contract_current
        )
        if not needs_requeue:
            plans.append({
                "job_id": record["job_id"],
                "action": "leave_awaiting",
                "reason": "current task packet still matches the raw source",
                "expected_state": expected_state,
            })
            continue

        canonical_name = str(payload.get("canonical_name") or "").strip()
        if not canonical_name:
            if dry_run:
                if preview_source_identity_index is None:
                    preview_source_identity_index = (
                        _source_identity_index_from_connection(conn)
                    )
                canonical_name = canonical_source_name(
                    str(raw_path),
                    source_identity_index=preview_source_identity_index,
                )
            else:
                canonical_name = canonical_source_name(str(raw_path))
        canonical_key = canonical_name[:-3] if canonical_name.endswith(".md") else canonical_name
        refreshed_fields = {
            "filepath": str(raw_path),
            "hash": current_hash,
            "canonical_name": canonical_name,
            "ingest_contract_version": INGEST_CONTRACT_VERSION,
        }
        if dry_run:
            refreshed_fields.update({
                "source_hash": str(payload.get("source_hash") or ""),
                "instructions": str(payload.get("instructions") or ""),
            })
        else:
            refreshed_fields.update({
                "source_hash": governance_store.canonical_page_versions(
                    {canonical_key}
                ).get(canonical_key, ""),
                "instructions": _build_ingest_instructions(
                    str(raw_path),
                    current_hash,
                    canonical_name,
                ),
            })
        refreshed = dict(payload)
        refreshed.update(refreshed_fields)
        target_key = db_store._job_idempotency_key("ingest", refreshed)
        candidate = {
            "job_id": record["job_id"],
            "action": "requeue_current",
            "reason": (
                "terminal failure"
                if record.get("status") == "failed"
                else "raw hash or ingest contract changed"
            ),
            "packet_path": packet_path,
            "payload": refreshed,
            "target_key": target_key,
            "created_at": str(record.get("created_at") or ""),
            "expected_state": expected_state,
        }
        requeue_groups.setdefault(str(target_key), []).append(candidate)

    for target_key, candidates in requeue_groups.items():
        existing = existing_by_key.get(target_key)
        candidate_ids = {item["job_id"] for item in candidates}
        if existing and existing["job_id"] not in candidate_ids:
            owner_id = str(existing["job_id"])
            for item in candidates:
                item.update({
                    "action": "supersede_duplicate",
                    "reason": f"current ingest identity is already owned by {owner_id}",
                    "owner_job_id": owner_id,
                })
                plans.append(item)
            continue

        owner = None
        if existing and existing["job_id"] in candidate_ids:
            owner = next(
                item for item in candidates if item["job_id"] == existing["job_id"]
            )
        if owner is None:
            owner = max(candidates, key=lambda item: (item["created_at"], item["job_id"]))
        plans.append(owner)
        for item in candidates:
            if item["job_id"] == owner["job_id"]:
                continue
            item.update({
                "action": "supersede_duplicate",
                "reason": f"duplicate current ingest identity; owner={owner['job_id']}",
                "owner_job_id": owner["job_id"],
            })
            plans.append(item)

    plans.sort(key=lambda item: str(item["job_id"]))
    counts = Counter(item["action"] for item in plans)
    result = {
        "dry_run": bool(dry_run),
        "selected_jobs": len(rows),
        "counts": dict(sorted(counts.items())),
        "backup": "",
        "preview_error": preview_error,
        "cleanup": {
            "claimed": 0,
            "completed": 0,
            "failed": 0,
            "errors": [],
        },
        "concurrent_skips": [],
        "samples": [
            {
                "job_id": item["job_id"],
                "action": item["action"],
                "reason": item["reason"],
            }
            for item in plans[:20]
        ],
    }
    if dry_run:
        conn.close()
        return json.dumps(result, ensure_ascii=False, indent=2)

    mutating_actions = {
        "cancel_missing_raw",
        "complete_already_processed",
        "requeue_current",
        "supersede_duplicate",
    }
    if not any(item["action"] in mutating_actions for item in plans):
        return json.dumps(result, ensure_ascii=False, indent=2)

    from vector_lake.tool_projection import create_maintenance_backup

    result["backup"] = create_maintenance_backup("ingest_job_debt")
    now = datetime.now(timezone.utc).isoformat()
    applied_counts: Counter = Counter()
    with db_store.transaction():
        for item in plans:
            action = item["action"]
            job_id = item["job_id"]
            packet_path = str(item.get("packet_path") or "")
            expected = item.get("expected_state") or {}
            cas_where = (
                "job_id = ? AND status IS ? AND retries = ? "
                "AND COALESCE(lease_generation, 0) = ? "
                "AND task_packet_path IS ? AND lease_until IS ? "
                "AND lease_owner IS ? AND lease_token IS ? "
                "AND updated_at IS ? AND payload IS ? AND idempotency_key IS ?"
            )
            cas_values = (
                job_id,
                expected.get("status"),
                int(expected.get("retries") or 0),
                int(expected.get("lease_generation") or 0),
                expected.get("task_packet_path"),
                expected.get("lease_until"),
                expected.get("lease_owner"),
                expected.get("lease_token"),
                expected.get("updated_at"),
                expected.get("payload"),
                expected.get("idempotency_key"),
            )
            cursor = None
            if action == "cancel_missing_raw":
                cursor = conn.execute(
                    "UPDATE jobs SET status = 'cancelled', retries = 0, error_msg = ?, "
                    "updated_at = ?, completed_at = ?, available_at = ?, "
                    "lease_until = NULL, lease_owner = NULL, lease_token = NULL, "
                    "idempotency_key = NULL, result_json = ? "
                    f"WHERE {cas_where}",
                    (
                        item["reason"],
                        now,
                        now,
                        now,
                        json.dumps({"maintenance": action}, sort_keys=True),
                        *cas_values,
                    ),
                )
            elif action == "complete_already_processed":
                cursor = conn.execute(
                    "UPDATE jobs SET status = 'completed', retries = 0, error_msg = '', "
                    "updated_at = ?, completed_at = ?, available_at = ?, "
                    "lease_until = NULL, lease_owner = NULL, lease_token = NULL, "
                    f"result_json = ? WHERE {cas_where}",
                    (
                        now,
                        now,
                        now,
                        json.dumps({"maintenance": action}, sort_keys=True),
                        *cas_values,
                    ),
                )
            elif action == "supersede_duplicate":
                cursor = conn.execute(
                    "UPDATE jobs SET status = 'superseded', retries = 0, error_msg = ?, "
                    "updated_at = ?, completed_at = ?, available_at = ?, "
                    "lease_until = NULL, lease_owner = NULL, lease_token = NULL, "
                    "idempotency_key = NULL, result_json = ? "
                    f"WHERE {cas_where}",
                    (
                        item["reason"],
                        now,
                        now,
                        now,
                        json.dumps(
                            {
                                "maintenance": action,
                                "owner_job_id": item.get("owner_job_id"),
                            },
                            sort_keys=True,
                        ),
                        *cas_values,
                    ),
                )
            elif action == "requeue_current":
                owner = conn.execute(
                    "SELECT job_id FROM jobs WHERE idempotency_key = ? AND job_id <> ?",
                    (item["target_key"], job_id),
                ).fetchone()
                if owner is not None:
                    result["concurrent_skips"].append({
                        "job_id": job_id,
                        "reason": (
                            "current ingest identity acquired concurrently by "
                            f"{owner['job_id']}"
                        ),
                    })
                    continue
                cursor = conn.execute(
                    "UPDATE jobs SET payload = ?, status = 'queued', retries = 0, "
                    "error_msg = ?, updated_at = ?, completed_at = NULL, available_at = ?, "
                    "lease_until = NULL, lease_owner = NULL, lease_token = NULL, "
                    "idempotency_key = ?, result_json = NULL "
                    f"WHERE {cas_where}",
                    (
                        json.dumps(item["payload"], ensure_ascii=False, sort_keys=True),
                        f"Recovered by ingest debt reconciliation: {item['reason']}",
                        now,
                        now,
                        item["target_key"],
                        *cas_values,
                    ),
                )
            if cursor is None or cursor.rowcount != 1:
                if action in mutating_actions:
                    result["concurrent_skips"].append({
                        "job_id": job_id,
                        "reason": "job state changed after preview; no mutation applied",
                    })
                continue
            applied_counts[action] += 1
            if packet_path:
                db_store.enqueue_ingest_task_cleanup(job_id, packet_path)

    result["applied_counts"] = dict(sorted(applied_counts.items()))
    result["cleanup"] = process_ingest_task_cleanup(
        limit=max(20, sum(applied_counts.values()))
    )

    result["terminal_failed_after"] = int(
        conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE status = 'failed' AND retries >= 3"
        ).fetchone()[0]
    )
    result["awaiting_after"] = int(
        conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE status = 'awaiting_subagent'"
        ).fetchone()[0]
    )
    result["queued_after"] = int(
        conn.execute("SELECT COUNT(*) FROM jobs WHERE status = 'queued'").fetchone()[0]
    )
    return json.dumps(result, ensure_ascii=False, indent=2)

_HASH_SUFFIX = re.compile(r"-[0-9a-f]{8}$", re.IGNORECASE)


def _raw_identity_for_path(raw_path: str) -> str:
    path = Path(raw_path).resolve()
    try:
        relative = path.relative_to(get_raw_dir().resolve())
        return normalize_source_identity(f"raw/{relative.as_posix()}")
    except ValueError:
        return normalize_source_identity(str(path))


def _source_identity_index() -> dict[str, str]:
    """Map exact raw identities to deterministic existing Source filenames."""
    items = governance_store.query_entities(
        {"status!=": "Merged", "type": "source"}
    )["items"].values()
    wiki_dir = get_wiki_dir()
    selected: dict[str, tuple[tuple[int, int, int, str], str]] = {}
    for entity in items:
        page_key = str(entity.get("page_key") or "").strip().removesuffix(".md")
        if not page_key or not (wiki_dir / f"{page_key}.md").is_file():
            continue
        categories = {
            str(value).casefold()
            for value in (
                entity.get("categories")
                if isinstance(entity.get("categories"), list)
                else [entity.get("categories")]
            )
            if value
        }
        is_backlog = (
            str(entity.get("topic_cluster") or "").casefold() == "raw_ingest_backlog"
            or "raw_ingest_backlog" in categories
        )
        rank = (
            int(not is_backlog),
            int(not _HASH_SUFFIX.search(page_key)),
            int(str(entity.get("status") or "").casefold() == "active"),
            page_key.casefold(),
        )
        sources = entity.get("sources") or []
        if not isinstance(sources, list):
            sources = [sources]
        for source in sources:
            identity = normalize_source_identity(source)
            if not identity.startswith("raw/"):
                continue
            current = selected.get(identity)
            if current is None or rank[:3] > current[0][:3] or (
                rank[:3] == current[0][:3] and rank[3] < current[0][3]
            ):
                selected[identity] = (rank, f"{page_key}.md")
    return {identity: filename for identity, (_rank, filename) in selected.items()}


def canonical_source_name(
    raw_path: str,
    source_identity_index: dict[str, str] | None = None,
) -> str:
    path = Path(raw_path).resolve()
    identity_index = _source_identity_index() if source_identity_index is None else source_identity_index
    existing = identity_index.get(_raw_identity_for_path(raw_path))
    if existing:
        return existing
    try:
        relative = path.relative_to(get_raw_dir().resolve()).with_suffix("")
        parts = relative.parts
    except ValueError:
        parts = (path.stem,)
    safe_parts = []
    for part in parts:
        safe = re.sub(r"[^0-9A-Za-z_\-\u3400-\u9fff]+", "-", str(part)).strip("-_")
        safe_parts.append(safe or "source")
    return f"Source_{'__'.join(safe_parts)}.md"

def calculate_hash(filepath: str) -> str:
    hasher = hashlib.md5()
    try:
        with open(filepath, "rb") as f:
            for chunk in iter(lambda: f.read(4096), b""):
                hasher.update(chunk)
        return hasher.hexdigest()
    except Exception as e:
        log.error(f"Error calculating hash for {filepath}: {e}")
        return ""

def _read_purpose() -> str:
    try:
        return render_strategy_directive()
    except PurposeContractError as exc:
        log.error("Strategic purpose contract is unavailable: %s", exc)
        return "[STRATEGIC PURPOSE CONTRACT UNAVAILABLE: halt and repair purpose.md before ingesting.]"

INTEGRATION_DISPOSITIONS = {"integrated", "standalone", "rejected"}
INGEST_CONTRACT_VERSION = 2
INTEGRATION_PREDICATES = {"validates", "falsifies", "depends-on", "mentions", "related_to"}
INTEGRATION_EVENT_TAGS = {
    "Release", "Pivot", "Conflict", "Validation", "Observation", "Decision", "Execution", "Outcome"
}
INGEST_CANDIDATE_TYPES = {
    "concept", "vendor", "institution", "product", "person", "event", "policy", "standard",
    "synthesis",
}
INGEST_SEARCH_STOP_WORDS = {
    "about", "after", "ai", "also", "an", "and", "api", "are", "as", "at", "based", "be", "been", "before",
    "between", "by", "can", "compiled", "consensus", "directive", "for", "from", "generated", "has",
    "have", "here", "if", "in", "into", "is", "it", "its", "latest", "model", "more", "no", "not",
    "of", "on", "only", "or", "other", "our", "read", "should", "source", "sources", "system", "than",
    "that", "the", "their", "then", "there", "this", "through", "to", "truth", "using", "was", "were",
    "which", "while", "who", "will", "with",
}
INGEST_CHINESE_STOP_TERMS = {
    "体系", "机制", "医疗", "系统", "平台", "医院", "数据", "管理", "治理", "国家", "智能", "人工智能",
    "评估", "实验", "资本", "架构", "推理", "物理", "生成式", "临床", "模型", "技术", "应用", "方案",
    "服务", "项目", "流程",
}


def _normalise_search_text(value: str) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", str(value or "").lower())


def _read_relevant_index_context(filepath: str, max_nodes: int = 40) -> str:
    """Return deterministic source-relevant candidates from the complete index."""
    index_path = get_index_path()
    if not index_path.exists():
        return ""
    try:
        source_text = Path(filepath).read_text(encoding="utf-8", errors="replace")[:200_000]
        source_norm = _normalise_search_text(f"{Path(filepath).stem} {source_text}")
        source_words = Counter(
            word for word in re.findall(r"\b[a-z0-9]{2,}\b", source_text.lower())
            if word not in INGEST_SEARCH_STOP_WORDS and not word.isdigit()
        )
        source_acronyms = {
            token.lower() for token in re.findall(r"\b[A-Z][A-Z0-9]{1,7}\b", source_text)
        }
        index_data = json.loads(index_path.read_text(encoding="utf-8"))
        nodes = index_data.get("nodes", {})
        if not nodes:
            return ""
        canonical_versions = governance_store.canonical_page_versions(set(nodes))
        scored = []
        wiki_dir = get_wiki_dir()
        for key, node in nodes.items():
            if str(node.get("type") or "").lower() not in INGEST_CANDIDATE_TYPES:
                continue
            filename = f"{key}.md"
            try:
                validate_wiki_filename(filename)
            except ValueError:
                continue
            target_path = wiki_dir / filename
            target_hash = canonical_versions.get(key)
            if not target_path.exists() or not target_hash:
                continue
            aliases = node.get("aliases") or []
            if isinstance(aliases, str):
                aliases = [aliases]
            labels = [key.split("_", 1)[-1], node.get("title", ""), *aliases]
            score = 0
            match_reasons = set()
            for label in labels:
                chinese_label = "".join(re.findall(r"[\u4e00-\u9fff]", str(label)))
                if (
                    len(chinese_label) >= 3
                    and chinese_label not in INGEST_CHINESE_STOP_TERMS
                    and chinese_label in source_norm
                ):
                    score = max(score, 180 + len(chinese_label))
                    match_reasons.add(f"exact_chinese:{chinese_label}")
                label_words = [
                    word for word in re.findall(r"\b[a-z0-9]{2,}\b", str(label).lower())
                    if word not in INGEST_SEARCH_STOP_WORDS and word not in {"concept", "vendor", "institution"}
                ]
                if not chinese_label and label_words and all(word in source_words for word in label_words):
                    score = max(
                        score,
                        160 + sum(min(len(word), 12) for word in label_words)
                        + sum(min(source_words[word], 5) for word in label_words),
                    )
                    match_reasons.add(f"exact_terms:{'+'.join(label_words)}")
            candidate_words = {
                word for word in re.findall(
                    r"\b[a-z0-9]{2,}\b",
                    f"{node.get('title', '')} {(node.get('summary', '') or '')[:500]}".lower(),
                )
                if word not in INGEST_SEARCH_STOP_WORDS and not word.isdigit()
            }
            for word in source_words.keys() & candidate_words:
                score += 45 if word in source_acronyms else min(len(word), 12)
                match_reasons.add(f"overlap:{word}")
            if score <= 0:
                continue
            scored.append((score, key, node, target_hash, sorted(match_reasons)))
        scored.sort(key=lambda item: (-item[0], item[1]))
        lines = []
        candidate_limit = max(1, int(max_nodes))
        scan_limit = max(100, candidate_limit * 5)
        for score, key, node, target_hash, match_reasons in scored[:scan_limit]:
            try:
                _read_canonical_target_content(f"{key}.md", target_hash)
            except ValueError:
                continue
            lines.append(json.dumps({
                "target": f"{key}.md",
                "target_hash": target_hash,
                "type": node.get("type", "unknown"),
                "title": node.get("title", key),
                "summary": (node.get("summary", "") or "")[:160],
                "match_score": score,
                "match_reasons": match_reasons[:8],
            }, ensure_ascii=False))
            if len(lines) >= candidate_limit:
                break
        return "\n".join(f"- {line}" for line in lines)
    except Exception as exc:
        raise RuntimeError(
            f"Could not build source-relevant ingest context for {filepath}: {exc}"
        ) from exc


def _updated_now(content: str) -> str:
    updated = datetime.now(timezone.utc).isoformat()
    return re.sub(r"(?m)^updated:\s*.*$", f"updated: {updated}", content, count=1)


def _read_canonical_target_content(filename: str, expected_version: str) -> str:
    """Read Markdown whose extracted entity state matches the canonical version."""
    from vector_lake.db_store import get_connection, init_db

    candidates = []
    target_path = get_wiki_dir() / filename
    if target_path.exists():
        candidates.append(("markdown projection", target_path.read_text(encoding="utf-8"), "full"))
    init_db()
    rows = get_connection().execute(
        "SELECT payload_text, validation_mode FROM mutation_outbox "
        "WHERE filename = ? AND mutation_type = 'update' AND payload_text IS NOT NULL "
        "ORDER BY id DESC",
        (filename,),
    )
    candidates.extend(
        (
            "mutation outbox",
            str(row["payload_text"]),
            str(row["validation_mode"] or "full"),
        )
        for row in rows
    )
    seen = set()
    for origin, content, validation_mode in candidates:
        fingerprint = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        try:
            content_version = governance_store.canonical_page_version_from_content(filename, content)
        except Exception:
            continue
        if content_version == expected_version:
            if not target_path.exists() and origin == "mutation outbox":
                from vector_lake.mutation_coordinator import materialize_markdown_projection

                materialize_markdown_projection(
                    filename,
                    "update",
                    content,
                    validation_mode=validation_mode if validation_mode in {"full", "schema"} else "full",
                )
            return content
    raise ValueError(
        f"No canonical-aligned Markdown snapshot is available for {filename}; "
        "replay or repair its latest mutation outbox projection before integrating"
    )


def _upsert_section_relation(
    content: str,
    heading: str,
    marker: str,
    line: str,
    legacy_tokens: tuple[str, ...] = (),
) -> str:
    start = content.find(heading)
    if start < 0:
        raise ValueError(f"Integration target is missing the required section: {heading}")
    section_start = start + len(heading)
    next_heading = re.search(r"(?m)^##\s+", content[section_start:])
    section_end = section_start + (next_heading.start() if next_heading else len(content[section_start:]))
    section = content[section_start:section_end]
    matches = []
    for relation_match in re.finditer(r"(?m)^-[^\n]*$", section):
        relation_line = relation_match.group(0)
        if marker in relation_line or (
            legacy_tokens and all(token in relation_line for token in legacy_tokens)
        ):
            matches.append(relation_match)
    if matches:
        chunks = [section[:matches[0].start()], line]
        cursor = matches[0].end()
        for duplicate in matches[1:]:
            chunks.append(section[cursor:duplicate.start()])
            cursor = duplicate.end()
        chunks.append(section[cursor:])
        merged = "".join(chunks)
        return _updated_now(content[:section_start] + merged + content[section_end:])
    insert_at = section_end
    prefix = content[:insert_at].rstrip()
    suffix = content[insert_at:].lstrip("\n")
    merged = f"{prefix}\n\n{line}\n"
    if suffix:
        merged += f"\n{suffix}"
    return _updated_now(merged)


def _apply_integration_disposition(files_written: list, processed_data: dict) -> tuple[list, str]:
    """Validate semantic completion and materialize bounded source/target updates."""
    integration = processed_data.get("integration")
    if not isinstance(integration, dict):
        raise ValueError("finalize_ingest requires an integration disposition")
    disposition = str(integration.get("disposition") or "").strip().lower()
    if disposition not in INTEGRATION_DISPOSITIONS:
        raise ValueError(f"integration disposition must be one of {sorted(INTEGRATION_DISPOSITIONS)}")

    files = []
    for item in files_written:
        record = dict(item)
        if "filepath" in record and not record.get("content"):
            record["content"] = Path(record["filepath"]).read_text(encoding="utf-8")
        files.append(record)

    reason = str(integration.get("reason") or "").strip()
    if disposition == "rejected":
        if files:
            raise ValueError("rejected ingest disposition must not include wiki files")
        if len(reason) < 12:
            raise ValueError("rejected ingest disposition requires an auditable reason")
        return [], disposition

    canonical_name = str(processed_data.get("canonical_name") or "").strip()
    source_items = [item for item in files if os.path.basename(str(item.get("filename", ""))) == canonical_name]
    if len(source_items) != 1:
        raise ValueError(f"{disposition} ingest disposition requires exactly one canonical source page: {canonical_name}")
    source_item = source_items[0]
    source_item["expected_version"] = str(processed_data.get("source_hash") or "")
    for item in files:
        if item is not source_item:
            item.setdefault("expected_version", "")
    relations = integration.get("relations") or []
    if disposition == "standalone":
        if relations:
            raise ValueError("standalone ingest disposition cannot include integration relations")
        if len(reason) < 12:
            raise ValueError("standalone ingest disposition requires an auditable reason")
        return files, disposition
    if not isinstance(relations, list) or not relations:
        raise ValueError("integrated ingest disposition requires at least one relation")

    submitted_names = {os.path.basename(str(item.get("filename", ""))) for item in files}
    source_content = str(source_item.get("content") or "").rstrip()
    source_key = canonical_name[:-3] if canonical_name.endswith(".md") else canonical_name
    graph_heading = "## Graph Integration"
    if graph_heading not in source_content:
        source_content += f"\n\n{graph_heading}\n"
    target_mutations = []
    seen_targets = set()
    for relation in relations:
        if not isinstance(relation, dict):
            raise ValueError("integration relations must be objects")
        target = os.path.basename(str(relation.get("target") or ""))
        if target != str(relation.get("target") or ""):
            raise ValueError("integration relation target must be a wiki basename")
        validate_wiki_filename(target)
        if target == canonical_name or target in submitted_names or target in seen_targets:
            raise ValueError(f"integration target is duplicated or conflicts with submitted files: {target}")
        seen_targets.add(target)
        target_key = target[:-3]
        actual_hash = governance_store.canonical_page_versions({target_key}).get(target_key)
        expected_hash = str(relation.get("target_hash") or "")
        if not expected_hash or expected_hash != actual_hash:
            raise ValueError(f"integration target_hash is stale or missing for {target}")
        target_content = _read_canonical_target_content(target, expected_hash)
        predicate = str(relation.get("predicate") or "").strip()
        if predicate not in INTEGRATION_PREDICATES:
            raise ValueError(f"unsupported integration predicate: {predicate}")
        evidence = " ".join(str(relation.get("evidence") or "").split())
        if len(evidence) < 12:
            raise ValueError(f"integration evidence is too short for {target}")
        try:
            confidence = float(relation.get("confidence"))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"integration confidence must be numeric for {target}") from exc
        if not 0.0 <= confidence <= 1.0:
            raise ValueError(f"integration confidence must be in [0, 1] for {target}")
        event_date = str(relation.get("event_date") or "")
        try:
            datetime.strptime(event_date, "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError(f"integration event_date must be YYYY-MM-DD for {target}") from exc
        event_tag = str(relation.get("event_tag") or "").strip().strip("[]")
        if event_tag not in INTEGRATION_EVENT_TAGS:
            raise ValueError(f"unsupported integration event_tag for {target}: {event_tag}")
        relation_id = hashlib.sha256(f"{source_key}\x00{target_key}".encode("utf-8")).hexdigest()[:16]
        marker = f"<!-- vector-lake-relation:{relation_id} -->"
        source_content = _upsert_section_relation(
            source_content,
            graph_heading,
            marker,
            f"- [{predicate}:: [[{target_key}]]] {evidence} "
            f"(confidence: {confidence:.2f}) {marker}",
            legacy_tokens=(f"[[{target_key}]]",),
        )
        source_anchor = f"(Source: [[{source_key}]])"
        target_heading = (
            "## 支撑拓扑 (Supporting Topology)"
            if target.startswith("Synthesis_")
            else "## 2. 证据时间线"
        )
        target_line = (
            f"- [depends-on:: [[{source_key}]]] {evidence} {source_anchor} {marker}"
            if target.startswith("Synthesis_")
            else f"- [{event_date}] [{event_tag}] {evidence} {source_anchor} {marker}"
        )
        target_content = _upsert_section_relation(
            target_content,
            target_heading,
            marker,
            target_line,
            legacy_tokens=(source_anchor,),
        )
        target_mutations.append({
            "filename": target,
            "content": target_content,
            "expected_version": expected_hash,
        })

    source_item["content"] = _updated_now(source_content)
    return files + target_mutations, disposition


def _build_ingest_instructions(filepath: str, file_hash: str, canonical_name: str) -> str:
    schema_content = ""
    try:
        schema_content = (get_extension_root() / "schema.md").read_text(encoding="utf-8")
        category_path = get_extension_root() / "SCHEMA_CATEGORIES.md"
        if category_path.exists():
            schema_content += "\n\n" + category_path.read_text(encoding="utf-8")
    except OSError:
        pass
    prompt_path = get_extension_root() / "templates" / "ingest_prompt.md"
    if not prompt_path.exists():
        raise FileNotFoundError("templates/ingest_prompt.md not found")
    return (
        prompt_path.read_text(encoding="utf-8")
        .replace("{{filepath}}", str(filepath))
        .replace("{{file_hash}}", file_hash)
        .replace("{{canonical_name}}", canonical_name)
        .replace("{{skeleton_block}}", parse_static_skeleton(filepath))
        .replace("{{schema_content}}", schema_content)
        .replace("{{index_summary}}", _read_relevant_index_context(filepath))
        .replace("{{purpose_content}}", _read_purpose())
    )


def requeue_legacy_ingest_jobs() -> int:
    """Rebuild pre-integration-contract awaiting packets before they are claimed."""
    from vector_lake import db_store

    db_store.init_db()
    conn = db_store.get_connection()
    rows = conn.execute(
        "SELECT job_id, payload, task_packet_path FROM jobs "
        "WHERE task_type = 'ingest' AND status = 'awaiting_subagent'"
    ).fetchall()
    migrations = []
    for row in rows:
        try:
            payload = json.loads(row["payload"] or "{}")
        except json.JSONDecodeError:
            continue
        if (
            "source_hash" in payload
            and str(payload.get("ingest_contract_version") or "") == str(INGEST_CONTRACT_VERSION)
        ):
            continue
        filepath = str(payload.get("filepath") or "")
        file_hash = str(payload.get("hash") or "")
        canonical_name = str(payload.get("canonical_name") or "")
        if not filepath or not file_hash or not canonical_name or not Path(filepath).exists():
            continue
        canonical_key = canonical_name[:-3] if canonical_name.endswith(".md") else canonical_name
        payload["source_hash"] = governance_store.canonical_page_versions({canonical_key}).get(
            canonical_key,
            "",
        )
        payload["ingest_contract_version"] = INGEST_CONTRACT_VERSION
        payload["instructions"] = _build_ingest_instructions(filepath, file_hash, canonical_name)
        migrations.append((str(row["job_id"]), payload, str(row["task_packet_path"] or "")))

    if not migrations:
        return 0
    now = datetime.now(timezone.utc).isoformat()
    migrated = 0
    with db_store.transaction():
        for job_id, payload, packet_path in migrations:
            cursor = conn.execute(
                "UPDATE jobs SET payload = ?, status = 'queued', retries = 0, "
                "error_msg = 'Legacy ingest packet rebuilt for the integration contract', "
                "available_at = ?, updated_at = ?, "
                "lease_until = NULL, lease_owner = NULL, lease_token = NULL "
                "WHERE job_id = ? AND status = 'awaiting_subagent' "
                "AND task_packet_path IS ?",
                (
                    json.dumps(payload, ensure_ascii=False),
                    now,
                    now,
                    job_id,
                    packet_path or None,
                ),
            )
            if cursor.rowcount != 1:
                continue
            migrated += 1
            if packet_path:
                db_store.enqueue_ingest_task_cleanup(job_id, packet_path)
    if migrated:
        cleanup = process_ingest_task_cleanup(limit=max(20, migrated))
        for error in cleanup["errors"]:
            log.warning("Could not remove superseded ingest packet: %s", error)
    return migrated

def prepare_ingest_batch(batch_size: int = 5, candidate_paths: list[str] | None = None) -> str:
    """Native Antigravity Agentic subagent orchestration."""
    config_path = get_extension_root() / "config.json"
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
    except Exception:
        config = {}
        
    target_dirs = []
    for configured in config.get("target_directories", []):
        configured_path = Path(configured)
        target_dirs.append(
            (configured_path if configured_path.is_absolute() else get_extension_root() / configured_path).resolve()
        )
    isolated_raw = get_raw_dir().resolve()
    if isolated_raw not in target_dirs:
        target_dirs.append(isolated_raw)
    exclude_paths = config.get("exclude_paths", [])
    supported_exts = set(config.get("supported_extensions", [".md", ".txt"]))
    
    def allowed_path(path: Path) -> bool:
        if not path.is_file() or path.name.startswith(("~", ".")):
            return False
        if path.suffix.lower() not in supported_exts:
            return False
        for target_dir in target_dirs:
            try:
                relative = path.resolve().relative_to(target_dir)
            except ValueError:
                continue
            relative_text = relative.as_posix()
            return not any(
                str(exclude).replace("\\", "/") in relative_text
                for exclude in exclude_paths
            )
        return False

    files_to_process = []
    if candidate_paths is not None:
        files_to_process = sorted({
            str(Path(candidate).resolve())
            for candidate in candidate_paths
            if allowed_path(Path(candidate))
        })
    else:
        for target_dir in target_dirs:
            if not target_dir.exists():
                continue
            for root, dirs, files in os.walk(target_dir):
                dirs[:] = [directory for directory in dirs if not directory.startswith('.')]
                for filename in files:
                    path = Path(root) / filename
                    if allowed_path(path):
                        files_to_process.append(str(path.resolve()))
                    
    from vector_lake.db_store import get_connection, init_db
    init_db()
    conn = get_connection()
    cur = conn.execute("SELECT filepath, file_hash, processed_at FROM processed_files")
    processed = {row["filepath"]: {"hash": row["file_hash"], "processed_at": row["processed_at"]} for row in cur.fetchall()}
    
    processing_file = get_meta_dir() / "processing_files.json"
    processing_file.parent.mkdir(parents=True, exist_ok=True)
    from filelock import FileLock
    
    with FileLock(str(processing_file) + ".lock", timeout=10):
        try:
            with open(processing_file, "r") as f:
                currently_processing = json.load(f)
        except Exception:
            currently_processing = {}
            
        now_ts = datetime.now(timezone.utc).timestamp()
        currently_processing = {k: v for k, v in currently_processing.items() if now_ts - v < 3600}
        
        pending_files = []
        
        for filepath in files_to_process:
            try:
                stat = os.stat(filepath)
                mtime = stat.st_mtime
                
                if filepath in processed:
                    processed_at_str = processed[filepath]["processed_at"]
                    if processed_at_str:
                        processed_at_dt = datetime.fromisoformat(processed_at_str.replace("Z", "+00:00"))
                        if processed_at_dt.tzinfo is None:
                            processed_at_dt = processed_at_dt.replace(tzinfo=timezone.utc)
                        if mtime <= processed_at_dt.timestamp():
                            continue
                            
                    file_hash = calculate_hash(filepath)
                    if file_hash == processed[filepath]["hash"]:
                        continue
                else:
                    file_hash = calculate_hash(filepath)
                    
                processing_key = hashlib.sha256(
                    f"{Path(filepath).resolve()}\0{file_hash}".encode("utf-8")
                ).hexdigest()
                if file_hash and processing_key not in currently_processing:
                    pending_files.append((filepath, file_hash, processing_key))
            except OSError:
                pass
        
        if not pending_files:
                return "No new files to ingest. System is fully synced."

        pending_files = pending_files[:batch_size]
        for _, _, processing_key in pending_files:
                currently_processing[processing_key] = now_ts
                
        with open(processing_file, "w") as f:
                json.dump(currently_processing, f)
    
    from vector_lake.db_store import enqueue_job
    enqueued_count = 0
    
    source_identity_index = _source_identity_index()
    for filepath, file_hash, _processing_key in pending_files:
        canonical_name = canonical_source_name(filepath, source_identity_index)
        canonical_key = canonical_name[:-3] if canonical_name.endswith(".md") else canonical_name
        source_hash = governance_store.canonical_page_versions({canonical_key}).get(canonical_key, "")
        instructions = _build_ingest_instructions(filepath, file_hash, canonical_name)

        payload = {
            "filepath": str(filepath),
            "hash": file_hash,
            "canonical_name": canonical_name,
            "source_hash": source_hash,
            "ingest_contract_version": INGEST_CONTRACT_VERSION,
            "instructions": instructions
        }
        
        enqueue_job("ingest", payload)
        enqueued_count += 1
        
    if batch_size == 1 and enqueued_count == 1:
        return json.dumps(payload)
        
    return f"Successfully enqueued {enqueued_count} files for ingestion."

def finalize_ingest(files_written: list, processed_data: dict) -> str:
    """Finalizes an ingest operation from a subagent using direct data."""
    try:
        from vector_lake.wiki_utils import SafeWriteError
        from vector_lake.db_store import finalize_ingest_job, validate_ingest_job_finalization

        files = files_written
        job_id = processed_data.get("job_id")
        if not job_id:
            raise ValueError("finalize_ingest requires a claimed job_id")
        job_row = validate_ingest_job_finalization(str(job_id), processed_data)
        lease_owner = str(processed_data.get("lease_owner") or "")
        lease_token = str(processed_data.get("lease_token") or "")
        lease_generation = int(processed_data.get("lease_generation"))
        files, integration_disposition = _apply_integration_disposition(files, processed_data)
        contract = load_purpose_contract()
        node_records = validate_ingest_payload(files, contract)
                
        wiki_dir = get_wiki_dir()
        
        written_paths = []
        mutations = []
        for item in files:
            fname = os.path.basename(item["filename"])
            if "filepath" in item and not item.get("content"):
                with open(item["filepath"], "r", encoding="utf-8") as f:
                    item["content"] = f.read()
            fcontent = item["content"]
            
            if "Concept_Decision_" in fname:
                lower_content = fcontent.lower()
                if not all(k in lower_content for k in ["context", "alternatives", "justification"]):
                    raise SafeWriteError(f"Decision nodes like {fname} MUST contain 'context', 'alternatives', and 'justification'.")

            mutation = {"filename": fname, "content": fcontent}
            if "expected_version" in item:
                mutation["expected_version"] = item["expected_version"]
            mutations.append(mutation)
            written_paths.append(str(wiki_dir / fname))

        filepath = processed_data["filepath"]
        file_hash = processed_data["hash"]
        if mutations:
            from vector_lake.mutation_coordinator import execute_mutation_batch

            def mark_ingest_processed():
                mark_file_processed(filepath, file_hash)
                finalize_ingest_job(
                    str(job_id),
                    lease_owner,
                    lease_token,
                    lease_generation,
                    result_data={"integration": processed_data.get("integration")},
                )
                if job_row.get("task_packet_path"):
                    from vector_lake import db_store

                    db_store.enqueue_ingest_task_cleanup(
                        str(job_id),
                        str(job_row["task_packet_path"]),
                    )

            execute_mutation_batch(
                mutations,
                canonical_callback=mark_ingest_processed,
            )
        else:
            from vector_lake.db_store import transaction

            with transaction():
                mark_file_processed(filepath, file_hash)
                finalize_ingest_job(
                    str(job_id),
                    lease_owner,
                    lease_token,
                    lease_generation,
                    result_data={"integration": processed_data.get("integration")},
                )
                if job_row.get("task_packet_path"):
                    from vector_lake import db_store

                    db_store.enqueue_ingest_task_cleanup(
                        str(job_id),
                        str(job_row["task_packet_path"]),
                    )

        cleanup = process_ingest_task_cleanup(limit=20)
        for error in cleanup["errors"]:
            log.warning("Ingest finalized, but task packet cleanup failed: %s", error)
        
        from filelock import FileLock
        import tempfile
        tmp_dir = Path(tempfile.gettempdir()) / "vector_lake_tmp"
        processing_file = tmp_dir / "processing_files.json"
        try:
            with FileLock(str(processing_file) + ".lock", timeout=10):
                with open(processing_file, "r") as f:
                    currently_processing = json.load(f)
                if file_hash in currently_processing:
                    del currently_processing[file_hash]
                with open(processing_file, "w") as f:
                    json.dump(currently_processing, f)
        except Exception:
            pass
        
        proposal_count = 0
        if written_paths:
            try:
                # Include existing nodes sharing the newly observed tension target,
                # so independently ingested sources can converge on one proposal.
                candidate_records = list(node_records)
                target_names = {
                    str(edge.get("target", "")).strip()
                    for record in node_records
                    for edge in record.get("tension_edges", [])
                    if isinstance(edge, dict) and str(edge.get("target", "")).strip()
                }
                if target_names:
                    with open(get_index_path(), "r", encoding="utf-8") as handle:
                        index_nodes = json.load(handle).get("nodes", {}).values()
                    for node in index_nodes:
                        edges = node.get("tension_edges", [])
                        if any(isinstance(edge, dict) and edge.get("target") in target_names for edge in edges):
                            candidate_records.append({
                                "filename": node.get("id", ""),
                                "sources": node.get("sources", []),
                                "tension_edges": edges,
                            })
                for proposal in build_synthesis_proposals(candidate_records, contract):
                    governance_store.enqueue_governance_item(
                        proposal["type"], proposal["title"], proposal["description"],
                        ", ".join(proposal["sources"]), proposal["search_queries"], proposal["affected_pages"],
                    )
                    proposal_count += 1
            except Exception as exc:
                log.warning("Ingest completed, but Synthesis-Proposal evaluation failed: %s", exc)

        suffix = f" Queued {proposal_count} Synthesis-Proposal(s)." if proposal_count else ""
        return (
            f"Successfully finalized ingestion for {filepath}. "
            f"Integration disposition: {integration_disposition}.{suffix}"
        )
    except Exception as e:
        return f"Error finalizing ingestion: {e}\n{traceback.format_exc()}"
