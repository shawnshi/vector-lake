import os
import json
import hashlib
import logging
import re
import sqlite3
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from datetime import datetime, timezone
from typing import Callable

from vector_lake import get_extension_root
from vector_lake.db_store import (
    mark_file_processed,
    update_processed_file_observations,
)
from vector_lake import governance_store
from vector_lake.merge_analysis import normalize_source_identity
from vector_lake.skeleton_parser import parse_static_skeleton
from vector_lake.wiki_utils import (
    get_raw_dir,
    get_wiki_dir,
    get_index_path,
    iter_markdown_files,
    iter_wiki_link_matches,
    normalize_semantic_text,
    split_frontmatter,
    validate_wiki_filename,
)
from vector_lake.purpose_contract import (
    PurposeContractError,
    build_synthesis_proposals,
    load_purpose_contract,
    render_strategy_directive,
    validate_ingest_payload,
)
from vector_lake.raw_revision import (
    RawRevisionFormatError,
    RawSourceContainmentError,
    RawSourceUnstableError,
    StableRawRevision,
    parse_revision,
    snapshot_still_current,
    stable_raw_revision,
)

log = logging.getLogger("vector-lake-ingest")
FULL_SCAN_COMPLETE_TOKEN = "VECTOR_LAKE_RAW_FULL_SCAN_COMPLETE_V1"
NO_NEW_REVISIONS_MESSAGE = (
    "No new source revisions to enqueue; existing job/debt state unchanged."
)
_INGEST_DEBT_APPLY_DEFAULT_LIMIT = 100
_INGEST_DEBT_APPLY_MAX_LIMIT = 100
_MAX_FINALIZE_INLINE_FILES = 64
_MAX_FINALIZE_INLINE_FILE_BYTES = 2 * 1024 * 1024
_MAX_FINALIZE_INLINE_BATCH_BYTES = 5 * 1024 * 1024


def _normalize_inline_files_written(files_written: list) -> list[dict]:
    """Accept only bounded inline content; never dereference caller paths."""
    if not isinstance(files_written, list):
        raise ValueError("files_written must be a list")
    if len(files_written) > _MAX_FINALIZE_INLINE_FILES:
        raise ValueError(
            f"files_written exceeds {_MAX_FINALIZE_INLINE_FILES} inline files"
        )
    normalized: list[dict] = []
    total_bytes = 0
    for index, item in enumerate(files_written):
        if not isinstance(item, dict):
            raise ValueError(f"files_written[{index}] must be an object")
        if "filepath" in item:
            raise ValueError(
                "files_written[].filepath is not supported; provide bounded inline content"
            )
        filename = str(item.get("filename") or "")
        if not filename or filename != os.path.basename(filename):
            raise ValueError(f"files_written[{index}].filename must be a basename")
        content = item.get("content")
        if not isinstance(content, str):
            raise ValueError(
                f"files_written[{index}] requires UTF-8 inline string content"
            )
        content_bytes = len(content.encode("utf-8"))
        if content_bytes > _MAX_FINALIZE_INLINE_FILE_BYTES:
            raise ValueError(
                f"files_written[{index}] exceeds the per-file inline byte limit"
            )
        total_bytes += content_bytes
        if total_bytes > _MAX_FINALIZE_INLINE_BATCH_BYTES:
            raise ValueError("files_written exceeds the inline batch byte limit")
        record = dict(item)
        record["filename"] = filename
        record["content"] = content
        normalized.append(record)
    return normalized


def _strip_markdown_suffix(value: str) -> str:
    text = str(value)
    return text[:-3] if text.casefold().endswith(".md") else text


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


_INGEST_PACKET_BINDING_FIELDS = (
    "filepath",
    "hash",
    "canonical_name",
    "source_hash",
    "source_projection_hash",
    "integration_candidates",
    "ingest_contract_version",
    "job_id",
)
_INGEST_PACKET_METADATA_FIELDS = frozenset(
    {"job_id", "processed_data", "finalize_tool"}
)
_INGEST_PACKET_EXPECTED_OUTPUT = (
    "JSON array consumable by finalize_ingest(files_written, processed_data)"
)


def _ingest_task_packet_contract(row: dict) -> tuple[dict, str]:
    """Derive the only packet payload and prompt allowed for a durable job row."""
    from vector_lake.ingest_worker import _subagent_ingest_prompt

    payload = json.loads(str(row.get("payload") or ""))
    if not isinstance(payload, dict):
        raise ValueError("claimed ingest payload is not an object")
    processed_data = {
        "filepath": str(payload.get("filepath") or ""),
        "hash": str(payload.get("hash") or ""),
        "canonical_name": str(payload.get("canonical_name") or ""),
        "source_hash": str(payload.get("source_hash") or ""),
        "source_projection_hash": str(payload.get("source_projection_hash") or ""),
        "integration_candidates": list(payload.get("integration_candidates") or []),
        "ingest_contract_version": payload.get("ingest_contract_version"),
        "job_id": str(row.get("job_id") or ""),
    }
    prompt = _subagent_ingest_prompt(str(payload.get("instructions") or ""))
    return processed_data, prompt


def _read_claimed_ingest_task_packet(row: dict) -> tuple[dict, str]:
    """Load a controlled packet only when its complete durable contract matches."""
    from vector_lake.native_llm import (
        SUBAGENT_TASK_COST_BOUNDARY,
        SUBAGENT_TASK_PACKET_FIELDS,
        SUBAGENT_TASK_RUNTIME,
        resolve_subagent_task_path,
    )

    packet_value = str(row.get("task_packet_path") or "").strip()
    if not packet_value:
        raise ValueError("task packet path is empty")
    packet_path = resolve_subagent_task_path(packet_value)
    try:
        task_packet = json.loads(packet_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"task packet is unreadable: {packet_path}: {exc}") from exc
    if not isinstance(task_packet, dict):
        raise ValueError(f"task packet payload is not an object: {packet_path}")
    if set(task_packet) != SUBAGENT_TASK_PACKET_FIELDS:
        raise ValueError(
            f"task packet fields do not match the runtime contract: {packet_path}"
        )
    if (
        not isinstance(task_packet["task_id"], str)
        or task_packet["task_id"] != packet_path.stem
    ):
        raise ValueError(
            f"task packet identity does not match its filename: {packet_path}"
        )
    if task_packet["task_type"] != "ingest":
        raise ValueError(f"task packet type is not ingest: {packet_path}")
    created_at = task_packet["created_at"]
    if not isinstance(created_at, str) or not created_at.strip():
        raise ValueError(f"task packet created_at is empty: {packet_path}")
    if task_packet["runtime"] != SUBAGENT_TASK_RUNTIME:
        raise ValueError(f"task packet runtime does not match: {packet_path}")
    if task_packet["cost_boundary"] != SUBAGENT_TASK_COST_BOUNDARY:
        raise ValueError(f"task packet cost boundary does not match: {packet_path}")
    if task_packet["expected_output"] != _INGEST_PACKET_EXPECTED_OUTPUT:
        raise ValueError(f"task packet expected output does not match: {packet_path}")
    metadata = task_packet["metadata"]
    if not isinstance(metadata, dict):
        raise ValueError(f"task packet metadata is not an object: {packet_path}")
    if set(metadata) != _INGEST_PACKET_METADATA_FIELDS:
        raise ValueError(f"task packet metadata fields do not match: {packet_path}")
    expected_processed, expected_prompt = _ingest_task_packet_contract(row)
    if metadata["job_id"] != expected_processed["job_id"]:
        raise ValueError(
            f"task packet job identity does not match its lease: {packet_path}"
        )
    if metadata["finalize_tool"] != "finalize_ingest":
        raise ValueError(f"task packet finalize tool does not match: {packet_path}")
    processed = metadata["processed_data"]
    if not isinstance(processed, dict):
        raise ValueError(f"task packet processed_data is not an object: {packet_path}")
    if set(processed) != set(_INGEST_PACKET_BINDING_FIELDS):
        raise ValueError(
            f"task packet processed_data fields do not match: {packet_path}"
        )
    for binding_field in _INGEST_PACKET_BINDING_FIELDS:
        if processed[binding_field] != expected_processed[binding_field]:
            raise ValueError(
                f"task packet {binding_field} does not match its durable payload: {packet_path}"
            )
    if task_packet["prompt"] != expected_prompt:
        raise ValueError(
            f"task packet prompt does not match its durable payload: {packet_path}"
        )
    return task_packet, str(packet_path)


def _persist_rejected_ingest_packet(job_id: str, task_path: Path) -> None:
    from vector_lake import db_store

    with db_store.transaction():
        db_store.enqueue_ingest_task_cleanup(str(job_id), str(task_path))


def _rebuild_claimed_ingest_task_packet(row: dict) -> tuple[dict, str]:
    """Rebuild a missing or invalid packet from its lease-bound durable payload."""
    from vector_lake import db_store, native_llm

    processed_data, prompt = _ingest_task_packet_contract(row)
    job_id = processed_data["job_id"]
    task_path = native_llm.create_subagent_task(
        "ingest",
        prompt,
        "JSON array consumable by finalize_ingest(files_written, processed_data)",
        {
            "job_id": job_id,
            "processed_data": processed_data,
            "finalize_tool": "finalize_ingest",
        },
    )
    try:
        replaced = db_store.replace_ingest_subagent_task_packet(
            job_id,
            str(task_path),
            str(row.get("lease_owner") or ""),
            str(row.get("lease_token") or ""),
            int(row.get("lease_generation") or 0),
        )
    except Exception:
        _persist_rejected_ingest_packet(job_id, task_path)
        raise
    if not replaced:
        _persist_rejected_ingest_packet(job_id, task_path)
        raise RuntimeError("subagent lease changed before packet repair")
    repaired_row = dict(row)
    repaired_row["task_packet_path"] = str(task_path)
    return _read_claimed_ingest_task_packet(repaired_row)


def claim_ingest_tasks(limit: int = 5, lease_seconds: int = 3600) -> str:
    """Lease valid task packets, repairing bad pointers without spending a retry."""
    from vector_lake import db_store
    from vector_lake.auto_ingest_worker import load_auto_ingest_config

    if load_auto_ingest_config().enabled:
        raise RuntimeError(
            "automatic ingest is enabled; task claims are controller-exclusive"
        )

    requeue_legacy_ingest_jobs()
    claimed = db_store.claim_subagent_jobs(
        limit=limit,
        lease_seconds=lease_seconds,
        required_ingest_contract_version=INGEST_CONTRACT_VERSION,
        forbid_live_owner_prefix="auto-ingest:",
    )
    tasks = []
    for row in claimed:
        try:
            task_packet, packet_path = _read_claimed_ingest_task_packet(row)
        except (OSError, RuntimeError, TypeError, ValueError) as packet_error:
            try:
                task_packet, packet_path = _rebuild_claimed_ingest_task_packet(row)
            except Exception as repair_error:
                reason = (
                    f"Task packet repair failed after {packet_error}: {repair_error}"
                )
                db_store.fail_ingest_subagent_task_claim(
                    str(row.get("job_id") or ""),
                    str(row.get("lease_owner") or ""),
                    str(row.get("lease_token") or ""),
                    int(row.get("lease_generation") or 0),
                    reason,
                )
                log.warning("%s", reason)
                continue
        metadata = task_packet.setdefault("metadata", {})
        processed = metadata.setdefault("processed_data", {})
        processed.update(
            {
                "job_id": row.get("job_id"),
                "lease_owner": row.get("lease_owner"),
                "lease_token": row.get("lease_token"),
                "lease_generation": row.get("lease_generation"),
            }
        )
        tasks.append(
            {
                "job_id": row.get("job_id"),
                "status": row.get("status"),
                "lease_until": row.get("lease_until"),
                "lease_owner": row.get("lease_owner"),
                "lease_token": row.get("lease_token"),
                "lease_generation": row.get("lease_generation"),
                "task_packet_path": packet_path,
                "task_packet": task_packet,
            }
        )
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


def _resolved_task_packet_path(value: str | Path) -> str:
    return str(Path(value).resolve())


def reconcile_orphan_ingest_task_packets(
    dry_run: bool = True,
    min_age_seconds: int = 86400,
    limit: int = 0,
) -> str:
    """Preview or remove old ingest packets that no durable row references.

    Only structurally valid ingest packets for known jobs are candidates. Current
    job pointers, pending cleanup intents, recent packets, unknown jobs, and all
    non-ingest task types remain untouched.
    """
    from vector_lake import db_store

    try:
        age_floor = int(min_age_seconds)
        selected_limit = int(limit)
    except (TypeError, ValueError) as exc:
        raise ValueError("cleanup age and limit must be integers") from exc
    if age_floor < 0:
        raise ValueError("min_age_seconds must be zero or greater")
    if selected_limit < 0:
        raise ValueError("limit must be zero or greater")

    preview_error = ""
    if dry_run:
        conn, preview_error = _open_ingest_debt_preview_connection()
        if conn is None:
            return json.dumps(
                {
                    "dry_run": True,
                    "scanned": 0,
                    "candidate_count": 0,
                    "selected_count": 0,
                    "removed": 0,
                    "protected": {},
                    "errors": [],
                    "samples": [],
                    "preview_error": preview_error,
                },
                ensure_ascii=False,
                indent=2,
            )
    else:
        schema_connection, preview_error = _open_ingest_debt_preview_connection()
        if schema_connection is None:
            return json.dumps(
                {
                    "dry_run": False,
                    "scanned": 0,
                    "candidate_count": 0,
                    "selected_count": 0,
                    "removed": 0,
                    "protected": {},
                    "errors": [],
                    "samples": [],
                    "preview_error": preview_error,
                },
                ensure_ascii=False,
                indent=2,
            )
        schema_connection.close()
        conn = db_store.get_connection()

    jobs = {
        str(row["job_id"]): dict(row)
        for row in conn.execute("SELECT job_id, status, task_packet_path FROM jobs")
    }
    current_paths = {
        _resolved_task_packet_path(row["task_packet_path"])
        for row in jobs.values()
        if row.get("task_packet_path")
    }
    pending_cleanup_paths = {
        _resolved_task_packet_path(row["task_packet_path"])
        for row in conn.execute(
            "SELECT task_packet_path FROM ingest_task_cleanup "
            "WHERE status <> 'completed'"
        )
        if row["task_packet_path"]
    }
    protected: Counter = Counter()
    errors: list[str] = []
    candidates: list[dict] = []
    scanned = 0
    now = datetime.now(timezone.utc)
    from vector_lake.native_llm import peek_subagent_task_root

    task_root = peek_subagent_task_root()
    for packet_path in sorted(
        task_root.glob("*/*.json"),
        key=lambda path: str(path).casefold(),
    ):
        if not packet_path.is_file():
            continue
        scanned += 1
        resolved = _resolved_task_packet_path(packet_path)
        if resolved in current_paths:
            protected["current_job_pointer"] += 1
            continue
        if resolved in pending_cleanup_paths:
            protected["pending_cleanup"] += 1
            continue
        try:
            packet = json.loads(packet_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            protected["unreadable"] += 1
            errors.append(f"{packet_path}: {exc}")
            continue
        if not isinstance(packet, dict):
            protected["invalid_payload"] += 1
            continue
        task_id = str(packet.get("task_id") or "")
        if not task_id or task_id != packet_path.stem:
            protected["invalid_identity"] += 1
            continue
        if str(packet.get("task_type") or "") != "ingest":
            protected["non_ingest"] += 1
            continue
        metadata = packet.get("metadata")
        job_id = str(metadata.get("job_id") or "") if isinstance(metadata, dict) else ""
        job = jobs.get(job_id)
        if not job_id or job is None:
            protected["unknown_job"] += 1
            continue
        try:
            created_at = datetime.fromisoformat(
                str(packet.get("created_at") or "").replace("Z", "+00:00")
            )
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            protected["invalid_created_at"] += 1
            continue
        age_seconds = max(0, int((now - created_at).total_seconds()))
        if age_seconds < age_floor:
            protected["recent"] += 1
            continue
        candidates.append(
            {
                "path": resolved,
                "task_id": task_id,
                "job_id": job_id,
                "job_status": str(job.get("status") or ""),
                "age_seconds": age_seconds,
            }
        )

    candidates.sort(key=lambda item: (item["age_seconds"], item["path"]), reverse=True)
    candidate_count = len(candidates)
    selected = candidates[:selected_limit] if selected_limit else candidates
    result = {
        "dry_run": bool(dry_run),
        "scanned": scanned,
        "candidate_count": candidate_count,
        "selected_count": len(selected),
        "removed": 0,
        "protected": dict(sorted(protected.items())),
        "errors": errors[:100],
        "samples": [
            {
                "job_id": item["job_id"],
                "job_status": item["job_status"],
                "age_seconds": item["age_seconds"],
                "path": item["path"],
            }
            for item in selected[:20]
        ],
        "preview_error": preview_error,
    }
    if dry_run:
        conn.close()
        return json.dumps(result, ensure_ascii=False, indent=2)

    from vector_lake.native_llm import remove_subagent_task

    for item in selected:
        with db_store.transaction(max_wait_seconds=5):
            current = conn.execute(
                "SELECT task_packet_path FROM jobs WHERE job_id = ?",
                (item["job_id"],),
            ).fetchone()
            current_path = (
                _resolved_task_packet_path(current["task_packet_path"])
                if current is not None and current["task_packet_path"]
                else ""
            )
            pending = conn.execute(
                "SELECT 1 FROM ingest_task_cleanup "
                "WHERE task_packet_path = ? AND status <> 'completed' LIMIT 1",
                (item["path"],),
            ).fetchone()
            if current is None or current_path == item["path"] or pending is not None:
                protected["changed_after_scan"] += 1
                continue
            try:
                removed = remove_subagent_task(
                    item["path"],
                    expected_job_id=item["job_id"],
                    expected_task_type="ingest",
                    expected_task_id=item["task_id"],
                )
                result["removed"] += int(bool(removed))
                if not removed:
                    protected["missing_after_scan"] += 1
            except (OSError, RuntimeError, ValueError) as exc:
                errors.append(f"{item['path']}: {exc}")
    result["protected"] = dict(sorted(protected.items()))
    result["errors"] = errors[:100]
    return json.dumps(result, ensure_ascii=False, indent=2)


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
        "ingest_task_cleanup": {"task_packet_path", "status"},
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


def _source_identity_index_from_connection(
    connection,
    target_dirs: list[Path] | None = None,
) -> dict[str, str]:
    """Build the source identity map from an already-open read-only connection."""
    resolved_target_dirs = (
        get_ingest_target_directories() if target_dirs is None else target_dirs
    )
    items = []
    for row in connection.execute("SELECT data_json FROM entities ORDER BY entity_id"):
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
    wiki_paths = {path.stem: path for path in iter_markdown_files(wiki_dir)}
    selected: dict[str, tuple[tuple[int, int, int, str], str]] = {}
    for entity in items:
        page_key = _strip_markdown_suffix(str(entity.get("page_key") or "").strip())
        wiki_path = wiki_paths.get(page_key)
        if not page_key or wiki_path is None:
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
            if not _is_allowed_ingest_source_identity(
                identity,
                resolved_target_dirs,
            ):
                continue
            current = selected.get(identity)
            if (
                current is None
                or rank[:3] > current[0][:3]
                or (rank[:3] == current[0][:3] and rank[3] < current[0][3])
            ):
                selected[identity] = (rank, wiki_path.name)
    return {identity: filename for identity, (_rank, filename) in selected.items()}


def _ingest_debt_raw_precondition_failure(conn, item: dict) -> str:
    """Revalidate raw/processed evidence immediately before debt mutation."""
    action = str(item.get("action") or "")
    if action not in {
        "blocked_projection_drift",
        "blocked_revision_identity_conflict",
        "blocked_unreadable_raw",
        "cancel_missing_raw",
        "complete_already_processed",
        "requeue_current",
        "supersede_duplicate",
    }:
        return ""
    raw_value = str(item.get("raw_path") or "").strip()
    if not raw_value:
        return "raw source precondition is missing from the maintenance plan"
    raw_path = Path(raw_value)
    if action == "cancel_missing_raw":
        try:
            raw_path.stat()
        except FileNotFoundError:
            return ""
        except OSError as exc:
            return f"raw source absence cannot be revalidated: {exc}"
        return f"raw source reappeared after preview: {raw_path}"

    try:
        current_snapshot = _stable_current_raw_revision(str(raw_path))
        current_hash = current_snapshot.canonical_revision
    except (OSError, ValueError) as exc:
        if action == "blocked_unreadable_raw":
            return ""
        return f"raw source cannot be stably revalidated: {exc}"
    if action == "blocked_unreadable_raw":
        return f"raw source became readable after preview: {raw_path}"
    expected_hash = str(item.get("expected_raw_hash") or "")
    if not expected_hash or current_hash != expected_hash:
        return (
            "raw source changed after preview: "
            f"expected={expected_hash or '<missing>'} current={current_hash}"
        )
    if action == "blocked_projection_drift":
        try:
            _projection_hash_for_canonical_version(
                str(item.get("canonical_name") or ""),
                str(item.get("source_hash") or ""),
            )
        except (OSError, UnicodeError, ValueError):
            return ""
        return "projection baseline became valid after preview"
    if action != "complete_already_processed":
        return ""

    lookup_paths = [
        str(value) for value in item.get("processed_lookup_paths") or () if str(value)
    ]
    lookup_paths = list(dict.fromkeys(lookup_paths))
    if not lookup_paths:
        return "processed-file precondition is missing from the maintenance plan"
    placeholders = ", ".join("?" for _ in lookup_paths)
    rows = conn.execute(
        f"SELECT file_hash FROM processed_files WHERE filepath IN ({placeholders})",
        tuple(lookup_paths),
    ).fetchall()
    def marker_matches_current(row) -> bool:
        try:
            return current_snapshot.matches(str(row["file_hash"] or ""))
        except RawRevisionFormatError:
            return False

    if not any(marker_matches_current(row) for row in rows):
        return "processed_files no longer proves the current raw revision"
    return ""



def _ingest_debt_effective_revision_owners(
    conn,
    *,
    candidate_job_id: str,
    raw_path: Path,
    current_hash: str,
    candidate_payload: dict,
) -> tuple[list[dict], list[dict]]:
    """Find current ingest owners for one normalized raw-file revision."""
    from vector_lake import db_store

    snapshot = _stable_current_raw_revision(str(raw_path))
    if snapshot.canonical_revision != current_hash:
        raise ValueError("current raw revision changed during owner lookup")
    rows = conn.execute(
        "SELECT job_id, task_type, status, retries, payload, updated_at, "
        "idempotency_key FROM jobs WHERE task_type = 'ingest' AND job_id <> ? "
        "AND idempotency_key IS NOT NULL AND json_valid(payload) = 1 "
        "AND json_extract(payload, '$.hash') IN (?, ?)",
        (
            str(candidate_job_id),
            snapshot.canonical_revision,
            snapshot.legacy_md5,
        ),
    ).fetchall()
    raw_identity = os.path.normcase(str(raw_path.resolve()))
    owners: list[dict] = []
    conflicts: list[dict] = []
    for row in rows:
        record = dict(row)
        try:
            owner_payload = json.loads(str(record.get("payload") or "{}"))
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(owner_payload, dict):
            continue
        owner_raw_value = str(owner_payload.get("filepath") or "").strip()
        if not owner_raw_value:
            continue
        owner_raw_path = Path(owner_raw_value)
        if not owner_raw_path.is_absolute():
            owner_raw_path = get_raw_dir().parent / owner_raw_path
        try:
            owner_identity = os.path.normcase(str(owner_raw_path.resolve()))
        except (OSError, RuntimeError):
            continue
        if owner_identity != raw_identity:
            continue

        canonical_name = str(owner_payload.get("canonical_name") or "").strip()
        rebound_payload = dict(candidate_payload)
        rebound_payload.update(
            {
                "filepath": owner_raw_value,
                "hash": str(owner_payload.get("hash") or ""),
                "canonical_name": canonical_name,
            }
        )
        target_key = db_store._job_idempotency_key("ingest", rebound_payload)
        stored_key = str(record.get("idempotency_key") or "")
        if db_store._ingest_identity_owner_is_releasable(
            conn,
            record,
            rebound_payload,
        ):
            continue
        if not canonical_name or not target_key or target_key != stored_key:
            if not canonical_name:
                conflict_reason = "missing_canonical_name"
            elif not target_key:
                conflict_reason = "missing_computed_idempotency_key"
            else:
                conflict_reason = "idempotency_key_mismatch"
            conflicts.append(
                {
                    "job_id": str(record.get("job_id") or ""),
                    "stored_key": stored_key,
                    "computed_key": str(target_key or ""),
                    "canonical_name": canonical_name,
                    "reason": conflict_reason,
                }
            )
            continue
        owners.append(
            {
                "job_id": str(record.get("job_id") or ""),
                "target_key": stored_key,
                "payload": rebound_payload,
            }
        )
    return owners, conflicts

def _ingest_debt_revision_owner_signature(
    owners: list[dict],
    conflicts: list[dict],
):
    """Freeze the complete effective-owner and invalid-owner evidence set."""
    owner_signature = tuple(
        sorted(
            {
                (
                    str(owner.get("job_id") or ""),
                    str(owner.get("target_key") or ""),
                )
                for owner in owners
            }
        )
    )
    conflict_signature = tuple(
        sorted(
            {
                (
                    str(conflict.get("job_id") or ""),
                    str(conflict.get("stored_key") or ""),
                    str(conflict.get("computed_key") or ""),
                    str(conflict.get("canonical_name") or ""),
                    str(conflict.get("reason") or ""),
                )
                for conflict in conflicts
            }
        )
    )
    return owner_signature, conflict_signature

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
    debt_predicate = (
        "task_type = 'ingest' AND "
        "(status = 'awaiting_subagent' OR (status = 'failed' AND COALESCE(retries, 0) >= 3)) "
        "AND CASE WHEN json_valid(result_json) = 0 THEN 1 "
        "WHEN json_extract(result_json, '$.maintenance') = 'ingest_job_debt' "
        "AND json_extract(result_json, '$.state') = 'blocked' THEN 0 "
        "ELSE 1 END = 1"
    )
    available_jobs = int(
        conn.execute(f"SELECT COUNT(*) FROM jobs WHERE {debt_predicate}").fetchone()[0]
    )
    selected_limit = max(0, int(limit))
    if not dry_run:
        selected_limit = min(
            _INGEST_DEBT_APPLY_MAX_LIMIT,
            selected_limit or _INGEST_DEBT_APPLY_DEFAULT_LIMIT,
        )
    select_sql = (
        f"SELECT * FROM jobs WHERE {debt_predicate} ORDER BY created_at, job_id"
    )
    select_params: tuple[int, ...] = ()
    if selected_limit:
        select_sql += " LIMIT ?"
        select_params = (selected_limit,)
    rows = conn.execute(select_sql, select_params).fetchall()

    plans: list[dict] = []
    requeue_groups: dict[str, list[dict]] = {}
    reconcile_target_dirs = get_ingest_target_directories() if rows else []
    reconcile_source_identity_index = None
    instruction_context = None if dry_run else _LazyIngestInstructionContext()

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
            "available_at": record.get("available_at"),
            "completed_at": record.get("completed_at"),
            "result_json": record.get("result_json"),
            "error_msg": record.get("error_msg"),
        }
        packet_path = str(record.get("task_packet_path") or "")
        try:
            payload = json.loads(record.get("payload") or "{}")
        except json.JSONDecodeError:
            payload = None
        if not isinstance(payload, dict):
            plans.append(
                {
                    "job_id": record["job_id"],
                    "action": "blocked_invalid_payload",
                    "reason": "payload is not a JSON object",
                    "packet_path": packet_path,
                    "expected_state": expected_state,
                }
            )
            continue

        raw_value = str(payload.get("filepath") or "").strip()
        if not raw_value:
            plans.append(
                {
                    "job_id": record["job_id"],
                    "action": "blocked_invalid_payload",
                    "reason": "filepath is missing",
                    "packet_path": packet_path,
                    "expected_state": expected_state,
                }
            )
            continue
        raw_path = Path(raw_value)
        if not raw_path.is_absolute():
            raw_path = get_raw_dir().parent / raw_path
        raw_path = raw_path.resolve()
        if not raw_path.is_file():
            plans.append(
                {
                    "job_id": record["job_id"],
                    "action": "cancel_missing_raw",
                    "reason": f"raw source is missing: {raw_path}",
                    "packet_path": packet_path,
                    "raw_path": str(raw_path),
                    "expected_state": expected_state,
                }
            )
            continue

        try:
            current_snapshot = _stable_current_raw_revision(
                str(raw_path),
                allowed_roots=reconcile_target_dirs,
            )
        except (OSError, RawSourceUnstableError):
            plans.append(
                {
                    "job_id": record["job_id"],
                    "action": "blocked_unreadable_raw",
                    "reason": f"raw source cannot be hashed: {raw_path}",
                    "packet_path": packet_path,
                    "raw_path": str(raw_path),
                    "expected_state": expected_state,
                }
            )
            continue
        current_hash = current_snapshot.canonical_revision

        processed_lookup_paths = list(dict.fromkeys([str(raw_path), raw_value]))
        placeholders = ", ".join("?" for _ in processed_lookup_paths)
        processed_rows = conn.execute(
            "SELECT filepath, file_hash FROM processed_files "
            f"WHERE filepath IN ({placeholders})",
            tuple(processed_lookup_paths),
        ).fetchall()
        processed = {
            str(processed_row["filepath"]): str(processed_row["file_hash"] or "")
            for processed_row in processed_rows
        }
        processed_hash = processed.get(str(raw_path)) or processed.get(raw_value)
        try:
            processed_matches_current = bool(
                processed_hash and current_snapshot.matches(processed_hash)
            )
        except RawRevisionFormatError:
            processed_matches_current = False
        if processed_matches_current:
            plans.append(
                {
                    "job_id": record["job_id"],
                    "action": "complete_already_processed",
                    "reason": "processed_files already records the current raw hash",
                    "packet_path": packet_path,
                    "raw_path": str(raw_path),
                    "expected_raw_hash": current_hash,
                    "processed_lookup_paths": processed_lookup_paths,
                    "expected_state": expected_state,
                }
            )
            continue

        payload_hash = str(payload.get("hash") or "")
        contract_current = (
            str(payload.get("ingest_contract_version") or "")
            == str(INGEST_CONTRACT_VERSION)
            and "source_projection_hash" in payload
            and isinstance(payload.get("integration_candidates"), list)
        )
        needs_requeue = (
            record.get("status") == "failed"
            or payload_hash != current_hash
            or not contract_current
        )
        if not needs_requeue:
            plans.append(
                {
                    "job_id": record["job_id"],
                    "action": "leave_awaiting",
                    "reason": "current task packet still matches the raw source",
                    "expected_state": expected_state,
                }
            )
            continue

        if record.get("status") == "failed":
            revision_owners, revision_conflicts = (
                _ingest_debt_effective_revision_owners(
                    conn,
                    candidate_job_id=str(record["job_id"]),
                    raw_path=raw_path,
                    current_hash=current_hash,
                    candidate_payload=payload,
                )
            )
            if revision_conflicts or len(revision_owners) > 1:
                conflict_ids = sorted(
                    {
                        *(conflict["job_id"] for conflict in revision_conflicts),
                        *(owner["job_id"] for owner in revision_owners),
                    }
                )
                plans.append(
                    {
                        "job_id": record["job_id"],
                        "action": "blocked_revision_identity_conflict",
                        "reason": (
                            "multiple or invalid effective ingest owners for current "
                            f"raw revision: {', '.join(conflict_ids)}"
                        ),
                        "packet_path": packet_path,
                        "payload": payload,
                        "raw_path": str(raw_path),
                        "expected_raw_hash": current_hash,
                        "owner_set_signature": _ingest_debt_revision_owner_signature(
                            revision_owners,
                            revision_conflicts,
                        ),
                        "expected_state": expected_state,
                    }
                )
                continue
            if revision_owners:
                owner = revision_owners[0]
                plans.append(
                    {
                        "job_id": record["job_id"],
                        "action": "supersede_duplicate",
                        "reason": (
                            "current raw revision is already owned by "
                            f"{owner['job_id']}"
                        ),
                        "packet_path": packet_path,
                        "payload": owner["payload"],
                        "raw_path": str(raw_path),
                        "expected_raw_hash": current_hash,
                        "target_key": owner["target_key"],
                        "owner_job_id": owner["job_id"],
                        "expected_state": expected_state,
                    }
                )
                continue

        canonical_name = str(payload.get("canonical_name") or "").strip()
        if record.get("status") == "failed" or not canonical_name:
            if reconcile_source_identity_index is None:
                if dry_run:
                    reconcile_source_identity_index = (
                        _source_identity_index_from_connection(
                            conn,
                            reconcile_target_dirs,
                        )
                    )
                else:
                    reconcile_source_identity_index = _source_identity_index(
                        reconcile_target_dirs
                    )
            canonical_name = canonical_source_name(
                str(raw_path),
                source_identity_index=reconcile_source_identity_index,
                target_dirs=reconcile_target_dirs,
            )
        canonical_key = _strip_markdown_suffix(canonical_name)
        refreshed_fields = {
            "filepath": str(raw_path),
            "hash": current_hash,
            "canonical_name": canonical_name,
            "ingest_contract_version": INGEST_CONTRACT_VERSION,
        }
        if dry_run:
            refreshed_fields.update(
                {
                    "source_hash": str(payload.get("source_hash") or ""),
                    "source_projection_hash": str(
                        payload.get("source_projection_hash") or ""
                    ),
                    "integration_candidates": list(
                        payload.get("integration_candidates") or []
                    ),
                    "instructions": str(payload.get("instructions") or ""),
                }
            )
        else:
            source_hash = governance_store.canonical_page_versions({canonical_key}).get(
                canonical_key, ""
            )
            try:
                source_projection_hash = _projection_hash_for_canonical_version(
                    canonical_name,
                    source_hash,
                )
            except (OSError, UnicodeError, ValueError) as exc:
                plans.append(
                    {
                        "job_id": record["job_id"],
                        "action": "blocked_projection_drift",
                        "reason": f"cannot establish source projection baseline: {exc}",
                        "packet_path": packet_path,
                        "raw_path": str(raw_path),
                        "expected_raw_hash": current_hash,
                        "canonical_name": canonical_name,
                        "source_hash": source_hash,
                        "expected_state": expected_state,
                    }
                )
                continue
            integration_candidates: list[dict] = []
            instructions = _build_ingest_instructions(
                str(raw_path),
                current_hash,
                canonical_name,
                instruction_context,
                integration_candidates,
            )
            refreshed_fields.update(
                {
                    "source_hash": source_hash,
                    "source_projection_hash": source_projection_hash,
                    "integration_candidates": integration_candidates,
                    "instructions": instructions,
                }
            )
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
            "raw_path": str(raw_path),
            "expected_raw_hash": current_hash,
            "target_key": target_key,
            "created_at": str(record.get("created_at") or ""),
            "expected_state": expected_state,
        }
        requeue_groups.setdefault(str(target_key), []).append(candidate)

    existing_by_key = {}
    if requeue_groups:
        target_keys = sorted(requeue_groups)
        for offset in range(0, len(target_keys), 400):
            key_batch = target_keys[offset : offset + 400]
            placeholders = ", ".join("?" for _ in key_batch)
            rows_by_key = conn.execute(
                "SELECT job_id, task_type, status, retries, created_at, payload, "
                "updated_at, idempotency_key "
                f"FROM jobs WHERE idempotency_key IN ({placeholders})",
                tuple(key_batch),
            )
            existing_by_key.update(
                (str(row["idempotency_key"]), dict(row)) for row in rows_by_key
            )

    for target_key, candidates in requeue_groups.items():
        existing = existing_by_key.get(target_key)
        candidate_ids = {item["job_id"] for item in candidates}
        if (
            existing
            and existing["job_id"] not in candidate_ids
            and db_store._ingest_identity_owner_is_releasable(
                conn,
                existing,
                candidates[0]["payload"],
            )
        ):
            existing = None
        if existing and existing["job_id"] not in candidate_ids:
            owner_id = str(existing["job_id"])
            for item in candidates:
                item.update(
                    {
                        "action": "supersede_duplicate",
                        "reason": f"current ingest identity is already owned by {owner_id}",
                        "owner_job_id": owner_id,
                    }
                )
                plans.append(item)
            continue

        owner = None
        if existing and existing["job_id"] in candidate_ids:
            owner = next(
                item for item in candidates if item["job_id"] == existing["job_id"]
            )
        if owner is None:
            owner = max(
                candidates, key=lambda item: (item["created_at"], item["job_id"])
            )
        plans.append(owner)
        for item in candidates:
            if item["job_id"] == owner["job_id"]:
                continue
            item.update(
                {
                    "action": "supersede_duplicate",
                    "reason": f"duplicate current ingest identity; owner={owner['job_id']}",
                    "owner_job_id": owner["job_id"],
                }
            )
            plans.append(item)

    plans.sort(key=lambda item: str(item["job_id"]))
    counts = Counter(item["action"] for item in plans)
    result = {
        "dry_run": bool(dry_run),
        "selected_jobs": len(rows),
        "available_jobs": available_jobs,
        "effective_limit": selected_limit,
        "remaining_unselected": max(0, available_jobs - len(rows)),
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
        "blocked_invalid_payload",
        "blocked_projection_drift",
        "blocked_revision_identity_conflict",
        "blocked_unreadable_raw",
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
    apply_plans = sorted(
        plans,
        key=lambda item: (
            item["action"] == "supersede_duplicate",
            str(item["job_id"]),
        ),
    )
    with db_store.transaction():
        for item in apply_plans:
            action = item["action"]
            job_id = item["job_id"]
            packet_path = str(item.get("packet_path") or "")
            expected = item.get("expected_state") or {}
            cas_where = (
                "job_id = ? AND status IS ? AND COALESCE(retries, 0) = ? "
                "AND COALESCE(lease_generation, 0) = ? "
                "AND task_packet_path IS ? AND lease_until IS ? "
                "AND lease_owner IS ? AND lease_token IS ? "
                "AND updated_at IS ? AND payload IS ? AND idempotency_key IS ? "
                "AND available_at IS ? AND completed_at IS ? "
                "AND result_json IS ? AND error_msg IS ?"
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
                expected.get("available_at"),
                expected.get("completed_at"),
                expected.get("result_json"),
                expected.get("error_msg"),
            )
            raw_precondition_error = _ingest_debt_raw_precondition_failure(
                conn,
                item,
            )
            if raw_precondition_error:
                result["concurrent_skips"].append(
                    {
                        "job_id": job_id,
                        "reason": raw_precondition_error,
                    }
                )
                continue
            owner_sensitive_actions = {
                "blocked_revision_identity_conflict",
                "requeue_current",
                "supersede_duplicate",
            }
            if action in owner_sensitive_actions:
                candidate_payload = item.get("payload")
                raw_value = str(item.get("raw_path") or "").strip()
                current_hash = str(item.get("expected_raw_hash") or "").strip()
                try:
                    apply_raw_path = Path(raw_value).resolve()
                except (OSError, RuntimeError):
                    apply_raw_path = None
                if (
                    not isinstance(candidate_payload, dict)
                    or apply_raw_path is None
                    or not current_hash
                ):
                    result["concurrent_skips"].append(
                        {
                            "job_id": job_id,
                            "reason": (
                                "owner-set precondition is missing from the maintenance plan"
                            ),
                        }
                    )
                    continue
                revision_owners, revision_conflicts = (
                    _ingest_debt_effective_revision_owners(
                        conn,
                        candidate_job_id=str(job_id),
                        raw_path=apply_raw_path,
                        current_hash=current_hash,
                        candidate_payload=candidate_payload,
                    )
                )
                if action == "requeue_current" and (
                    revision_conflicts or revision_owners
                ):
                    result["concurrent_skips"].append(
                        {
                            "job_id": job_id,
                            "reason": "effective owner set changed before debt requeue",
                        }
                    )
                    continue
                if action == "blocked_revision_identity_conflict":
                    current_signature = _ingest_debt_revision_owner_signature(
                        revision_owners,
                        revision_conflicts,
                    )
                    if item.get("owner_set_signature") != current_signature:
                        result["concurrent_skips"].append(
                            {
                                "job_id": job_id,
                                "reason": (
                                    "revision identity conflict changed before debt mutation"
                                ),
                            }
                        )
                        continue
                if action == "supersede_duplicate":
                    owner_job_id = str(item.get("owner_job_id") or "")
                    target_key = str(item.get("target_key") or "")
                    owner_is_unique = (
                        not revision_conflicts
                        and len(revision_owners) == 1
                        and revision_owners[0]["job_id"] == owner_job_id
                        and revision_owners[0]["target_key"] == target_key
                    )
                    if not owner_is_unique:
                        result["concurrent_skips"].append(
                            {
                                "job_id": job_id,
                                "reason": (
                                    "duplicate owner no longer holds the unique "
                                    "effective ingest identity"
                                ),
                            }
                        )
                        continue
            cursor = None
            if action in {
                "blocked_invalid_payload",
                "blocked_projection_drift",
                "blocked_revision_identity_conflict",
                "blocked_unreadable_raw",
            }:
                cursor = conn.execute(
                    "UPDATE jobs SET status = 'failed', retries = MAX(3, COALESCE(retries, 0)), "
                    "error_msg = ?, updated_at = ?, completed_at = ?, available_at = ?, "
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
                                "maintenance": "ingest_job_debt",
                                "state": "blocked",
                                "action": action,
                                "reason": item["reason"],
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        *cas_values,
                    ),
                )
            elif action == "cancel_missing_raw":
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
                candidate_current = conn.execute(
                    f"SELECT 1 FROM jobs WHERE {cas_where}",
                    cas_values,
                ).fetchone()
                if candidate_current is None:
                    result["concurrent_skips"].append(
                        {
                            "job_id": job_id,
                            "reason": "job state changed before identity transfer",
                        }
                    )
                    continue
                owner = conn.execute(
                    "SELECT job_id, task_type, status, retries, payload, updated_at, "
                    "idempotency_key FROM jobs WHERE idempotency_key = ? "
                    "AND job_id <> ?",
                    (item["target_key"], job_id),
                ).fetchone()
                released_owner = bool(
                    owner
                    and db_store._release_releasable_ingest_identity_owner(
                        conn,
                        owner,
                        item["target_key"],
                        item["payload"],
                        now,
                    )
                )
                if owner is not None and not released_owner:
                    result["concurrent_skips"].append(
                        {
                            "job_id": job_id,
                            "reason": (
                                "current ingest identity acquired concurrently by "
                                f"{owner['job_id']}"
                            ),
                        }
                    )
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
                if released_owner and cursor.rowcount != 1:
                    raise RuntimeError(
                        "Debt identity transfer lost its candidate after owner release"
                    )
            if cursor is None or cursor.rowcount != 1:
                if action in mutating_actions:
                    result["concurrent_skips"].append(
                        {
                            "job_id": job_id,
                            "reason": "job state changed after preview; no mutation applied",
                        }
                    )
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
            "SELECT COUNT(*) FROM jobs WHERE status = 'failed' AND COALESCE(retries, 0) >= 3"
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


def _is_allowed_ingest_source_identity(
    identity: str,
    target_dirs: list[Path] | None = None,
) -> bool:
    normalized = normalize_source_identity(identity)
    if normalized.startswith("raw/"):
        return True
    path = Path(normalized)
    if not path.is_absolute():
        return False
    try:
        resolved = path.resolve()
    except OSError:
        return False
    roots = get_ingest_target_directories() if target_dirs is None else target_dirs
    return any(resolved.is_relative_to(root.resolve()) for root in roots)


def _source_identity_index(
    target_dirs: list[Path] | None = None,
    *,
    source_identities: set[str] | None = None,
    connection=None,
) -> dict[str, str]:
    """Map exact raw identities to deterministic existing Source filenames."""
    scoped_identities = None
    if source_identities is not None:
        scoped_identities = {
            normalized
            for value in source_identities
            if (normalized := normalize_source_identity(value))
        }
    if scoped_identities is None:
        items = governance_store.query_entities(
            {"status!=": "Merged", "type": "source"}
        )["items"].values()
        wiki_dir = get_wiki_dir()
        wiki_paths = {path.stem: path for path in iter_markdown_files(wiki_dir)}
    else:
        if connection is None:
            from vector_lake.db_store import get_connection, init_db

            init_db()
            connection = get_connection()
        items = _candidate_source_entities(connection, scoped_identities)
        wiki_dir = get_wiki_dir()
        page_keys = {
            _strip_markdown_suffix(str(item.get("page_key") or "").strip())
            for item in items
        }
        scoped_page_keys = {
            page_key
            for page_key in page_keys
            if page_key and Path(page_key).name == page_key
        }
        wiki_paths = {
            candidate.stem: candidate
            for candidate in iter_markdown_files(wiki_dir)
            if candidate.stem in scoped_page_keys
        }
    selected: dict[str, tuple[tuple[int, int, int, str], str]] = {}
    for entity in items:
        page_key = _strip_markdown_suffix(str(entity.get("page_key") or "").strip())
        wiki_path = wiki_paths.get(page_key)
        if not page_key or wiki_path is None:
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
            if scoped_identities is not None and identity not in scoped_identities:
                continue
            if not _is_allowed_ingest_source_identity(
                identity,
                target_dirs,
            ):
                continue
            current = selected.get(identity)
            if (
                current is None
                or rank[:3] > current[0][:3]
                or (rank[:3] == current[0][:3] and rank[3] < current[0][3])
            ):
                selected[identity] = (rank, wiki_path.name)
    return {identity: filename for identity, (_rank, filename) in selected.items()}


def canonical_source_name(
    raw_path: str,
    source_identity_index: dict[str, str] | None = None,
    target_dirs: list[Path] | None = None,
) -> str:
    path = Path(raw_path).resolve()
    resolved_targets = (
        get_ingest_target_directories() if target_dirs is None else target_dirs
    )
    identity_index = (
        _source_identity_index(resolved_targets)
        if source_identity_index is None
        else source_identity_index
    )
    source_identity = _raw_identity_for_path(raw_path)
    existing = identity_index.get(source_identity)
    if existing:
        return existing
    try:
        relative = path.relative_to(get_raw_dir().resolve()).with_suffix("")
        parts = relative.parts
    except ValueError:
        matching_roots = [
            root.resolve()
            for root in resolved_targets
            if path.is_relative_to(root.resolve())
        ]
        root = (
            min(
                matching_roots,
                key=lambda candidate: (
                    len(candidate.parts),
                    os.path.normcase(str(candidate)),
                ),
            )
            if matching_roots
            else path.parent
        )
        relative = path.relative_to(root).with_suffix("")
        root_label = root.name or root.drive.replace(":", "") or "external"
        parts = (root_label, *relative.parts)
    safe_parts = []
    for part in parts:
        safe = re.sub(
            r"[^0-9A-Za-z\u4e00-\u9fa5]+",
            "-",
            str(part),
        ).strip("-")
        safe_parts.append(safe or "source")
    identity_hash = hashlib.sha256(
        os.path.normcase(source_identity).encode("utf-8")
    ).hexdigest()[:8]
    identity_suffix = f"-{identity_hash}"
    core = "-".join(safe_parts)
    max_core_length = 120 - len("Source_") - len(".md") - len(identity_suffix)
    core = core[:max_core_length].rstrip("-") or "source"
    canonical_name = f"Source_{core}{identity_suffix}.md"
    validate_wiki_filename(canonical_name)
    return canonical_name


def calculate_hash(filepath: str) -> str:
    try:
        return stable_raw_revision(
            filepath,
            allowed_roots=get_ingest_target_directories(),
        ).canonical_revision
    except (OSError, RawSourceContainmentError, RawSourceUnstableError) as exc:
        log.error("Error calculating stable hash for %s: %s", filepath, exc)
        return ""


def _stable_current_raw_revision(
    filepath: str,
    *,
    allowed_roots: list[Path] | None = None,
) -> StableRawRevision:
    """Read one supported raw path through the shared stable revision layer."""
    path = Path(filepath)
    if not path.is_absolute():
        path = get_raw_dir().parent / path
    return stable_raw_revision(
        path,
        allowed_roots=(
            allowed_roots
            if allowed_roots is not None
            else get_ingest_target_directories()
        ),
    )


def _stable_current_raw_hash(filepath: str) -> str:
    return _stable_current_raw_revision(filepath).canonical_revision


def _read_purpose() -> str:
    try:
        return render_strategy_directive()
    except PurposeContractError as exc:
        log.error("Strategic purpose contract is unavailable: %s", exc)
        return "[STRATEGIC PURPOSE CONTRACT UNAVAILABLE: halt and repair purpose.md before ingesting.]"


INTEGRATION_DISPOSITIONS = {"integrated", "standalone", "rejected"}
INGEST_CONTRACT_VERSION = 5


class IngestBaselineConflict(ValueError):
    """A source or integration baseline changed after durable dispatch."""


class IngestFinalizationInfrastructureError(RuntimeError):
    """A retryable runtime failure prevented durable ingest finalization."""


INTEGRATION_PREDICATES = {
    "validates",
    "falsifies",
    "depends-on",
    "mentions",
    "related_to",
}
INTEGRATION_EVENT_TAGS = {
    "Release",
    "Pivot",
    "Conflict",
    "Validation",
    "Observation",
    "Decision",
    "Execution",
    "Outcome",
}
INGEST_CANDIDATE_TYPES = {
    "concept",
    "vendor",
    "institution",
    "product",
    "person",
    "event",
    "policy",
    "standard",
    "synthesis",
}
INGEST_SEARCH_STOP_WORDS = {
    "about",
    "after",
    "ai",
    "also",
    "an",
    "and",
    "api",
    "are",
    "as",
    "at",
    "based",
    "be",
    "been",
    "before",
    "between",
    "by",
    "can",
    "compiled",
    "consensus",
    "directive",
    "for",
    "from",
    "generated",
    "has",
    "have",
    "here",
    "if",
    "in",
    "into",
    "is",
    "it",
    "its",
    "latest",
    "model",
    "more",
    "no",
    "not",
    "of",
    "on",
    "only",
    "or",
    "other",
    "our",
    "read",
    "should",
    "source",
    "sources",
    "system",
    "than",
    "that",
    "the",
    "their",
    "then",
    "there",
    "this",
    "through",
    "to",
    "truth",
    "using",
    "was",
    "were",
    "which",
    "while",
    "who",
    "will",
    "with",
}
INGEST_CHINESE_STOP_TERMS = {
    "体系",
    "机制",
    "医疗",
    "系统",
    "平台",
    "医院",
    "数据",
    "管理",
    "治理",
    "国家",
    "智能",
    "人工智能",
    "评估",
    "实验",
    "资本",
    "架构",
    "推理",
    "物理",
    "生成式",
    "临床",
    "模型",
    "技术",
    "应用",
    "方案",
    "服务",
    "项目",
    "流程",
}


@dataclass(frozen=True)
class _PreparedIndexCandidate:
    key: str
    target_hash: str
    node_type: str
    title: str
    summary: str
    labels: tuple[str, ...]
    candidate_words: frozenset[str]


@dataclass(frozen=True)
class _PreparedIndexContext:
    candidates: tuple[_PreparedIndexCandidate, ...]


@dataclass
class _IngestInstructionContext:
    schema_content: str
    prompt_template: str
    purpose_content: str
    index_context: _PreparedIndexContext
    target_validity: dict[tuple[str, str], str | None] = field(default_factory=dict)


class _LazyIngestInstructionContext:
    """Build immutable batch inputs only if the active instruction builder needs them."""

    def __init__(self):
        self._value: _IngestInstructionContext | None = None

    def get(self) -> _IngestInstructionContext:
        if self._value is None:
            self._value = _prepare_ingest_instruction_context()
        return self._value


def _normalise_search_text(value: str) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", str(value or "").lower())


def _projection_hash_for_canonical_version(
    filename: str,
    expected_version: str,
) -> str:
    """Capture an exact Markdown baseline only when it matches canonical state."""
    if Path(filename).name != filename or not filename.casefold().endswith(".md"):
        raise ValueError(f"Unsafe wiki projection filename: {filename}")
    target_path = get_wiki_dir() / filename
    if not target_path.exists():
        return ""
    raw_content = target_path.read_bytes()
    content = normalize_semantic_text(raw_content.decode("utf-8"))
    actual_version = governance_store.canonical_page_version_from_content(
        filename, content
    )
    if not expected_version or actual_version != expected_version:
        raise ValueError(
            f"Markdown projection is not aligned with canonical state for {filename}"
        )
    return hashlib.sha256(raw_content).hexdigest()


def _prepare_relevant_index_context() -> _PreparedIndexContext:
    """Load and normalize the canonical index once for one ingest batch."""
    index_path = get_index_path()
    if not index_path.exists():
        return _PreparedIndexContext(candidates=())
    from vector_lake.indexer import read_committed_index_snapshot

    index_data = read_committed_index_snapshot(index_path)
    nodes = index_data.get("nodes", {})
    if not isinstance(nodes, dict) or not nodes:
        return _PreparedIndexContext(candidates=())
    canonical_versions = governance_store.canonical_page_versions(set(nodes))
    wiki_dir = get_wiki_dir()
    candidates = []
    for raw_key, node in nodes.items():
        if not isinstance(node, dict):
            continue
        key = str(raw_key)
        node_type = str(node.get("type") or "").lower()
        if node_type not in INGEST_CANDIDATE_TYPES:
            continue
        filename = f"{key}.md"
        try:
            validate_wiki_filename(filename)
        except ValueError:
            continue
        target_hash = str(canonical_versions.get(key) or "")
        if not (wiki_dir / filename).exists() or not target_hash:
            continue
        aliases = node.get("aliases") or []
        if isinstance(aliases, str):
            aliases = [aliases]
        elif not isinstance(aliases, list):
            aliases = []
        labels = tuple(
            str(label)
            for label in (key.split("_", 1)[-1], node.get("title", ""), *aliases)
        )
        summary = str(node.get("summary", "") or "")
        candidate_words = frozenset(
            word
            for word in re.findall(
                r"\b[a-z0-9]{2,}\b",
                f"{node.get('title', '')} {summary[:500]}".lower(),
            )
            if word not in INGEST_SEARCH_STOP_WORDS and not word.isdigit()
        )
        candidates.append(
            _PreparedIndexCandidate(
                key=key,
                target_hash=target_hash,
                node_type=node_type,
                title=str(node.get("title", key)),
                summary=summary[:160],
                labels=labels,
                candidate_words=candidate_words,
            )
        )
    return _PreparedIndexContext(candidates=tuple(candidates))


def _read_relevant_index_context(
    filepath: str,
    max_nodes: int = 40,
    *,
    prepared_context: _PreparedIndexContext | None = None,
    target_validity: dict[tuple[str, str], str | None] | None = None,
    candidate_manifest: list[dict] | None = None,
) -> str:
    """Return deterministic source-relevant candidates from a batch snapshot."""
    try:
        if candidate_manifest is not None:
            candidate_manifest.clear()
        source_text = Path(filepath).read_text(encoding="utf-8", errors="replace")[
            :200_000
        ]
        source_norm = _normalise_search_text(f"{Path(filepath).stem} {source_text}")
        source_words = Counter(
            word
            for word in re.findall(r"\b[a-z0-9]{2,}\b", source_text.lower())
            if word not in INGEST_SEARCH_STOP_WORDS and not word.isdigit()
        )
        source_acronyms = {
            token.lower()
            for token in re.findall(r"\b[A-Z][A-Z0-9]{1,7}\b", source_text)
        }
        context = prepared_context or _prepare_relevant_index_context()
        if not context.candidates:
            return ""
        scored = []
        for candidate in context.candidates:
            score = 0
            match_reasons = set()
            for label in candidate.labels:
                chinese_label = "".join(re.findall(r"[\u4e00-\u9fff]", str(label)))
                if (
                    len(chinese_label) >= 3
                    and chinese_label not in INGEST_CHINESE_STOP_TERMS
                    and chinese_label in source_norm
                ):
                    score = max(score, 180 + len(chinese_label))
                    match_reasons.add(f"exact_chinese:{chinese_label}")
                label_words = [
                    word
                    for word in re.findall(r"\b[a-z0-9]{2,}\b", str(label).lower())
                    if word not in INGEST_SEARCH_STOP_WORDS
                    and word not in {"concept", "vendor", "institution"}
                ]
                if (
                    not chinese_label
                    and label_words
                    and all(word in source_words for word in label_words)
                ):
                    score = max(
                        score,
                        160
                        + sum(min(len(word), 12) for word in label_words)
                        + sum(min(source_words[word], 5) for word in label_words),
                    )
                    match_reasons.add(f"exact_terms:{'+'.join(label_words)}")
            for word in source_words.keys() & candidate.candidate_words:
                score += 45 if word in source_acronyms else min(len(word), 12)
                match_reasons.add(f"overlap:{word}")
            if score <= 0:
                continue
            scored.append((score, candidate, sorted(match_reasons)))
        scored.sort(key=lambda item: (-item[0], item[1].key))
        lines = []
        candidate_limit = max(1, int(max_nodes))
        scan_limit = max(100, candidate_limit * 5)
        validity = target_validity if target_validity is not None else {}
        for score, candidate, match_reasons in scored[:scan_limit]:
            cache_key = (candidate.key, candidate.target_hash)
            if cache_key not in validity:
                try:
                    validity[cache_key] = _projection_hash_for_canonical_version(
                        f"{candidate.key}.md",
                        candidate.target_hash,
                    )
                except ValueError:
                    validity[cache_key] = None
            target_projection_hash = validity[cache_key]
            if not target_projection_hash:
                continue
            target = f"{candidate.key}.md"
            if candidate_manifest is not None:
                candidate_manifest.append(
                    {
                        "target": target,
                        "target_hash": candidate.target_hash,
                        "target_projection_hash": target_projection_hash.casefold(),
                    }
                )
            lines.append(
                json.dumps(
                    {
                        "target": target,
                        "target_hash": candidate.target_hash,
                        "target_projection_hash": target_projection_hash,
                        "type": candidate.node_type,
                        "title": candidate.title,
                        "summary": candidate.summary,
                        "match_score": score,
                        "match_reasons": match_reasons[:8],
                    },
                    ensure_ascii=False,
                )
            )
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


def _read_canonical_target_content(
    filename: str,
    expected_version: str,
    *,
    expected_projection_hash: str | None = None,
    materialize_missing: bool = True,
) -> str:
    """Read Markdown whose extracted entity state matches the canonical version."""
    from vector_lake.db_store import get_connection, init_db

    candidates = []
    target_path = get_wiki_dir() / filename
    if target_path.exists():
        target_bytes = target_path.read_bytes()
        current_projection_hash = hashlib.sha256(target_bytes).hexdigest()
        if (
            expected_projection_hash is not None
            and current_projection_hash != expected_projection_hash.casefold()
        ):
            raise IngestBaselineConflict(
                f"integration target_projection_hash is stale for {filename}"
            )
        candidates.append(
            (
                "markdown projection",
                normalize_semantic_text(target_bytes.decode("utf-8")),
                "full",
            )
        )
    elif expected_projection_hash:
        raise IngestBaselineConflict(
            f"integration target projection is missing for {filename}"
        )
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
            content_version = governance_store.canonical_page_version_from_content(
                filename, content
            )
        except Exception:
            continue
        if content_version == expected_version:
            if (
                materialize_missing
                and not target_path.exists()
                and origin == "mutation outbox"
            ):
                from vector_lake.mutation_coordinator import (
                    materialize_markdown_projection,
                )

                materialize_markdown_projection(
                    filename,
                    "update",
                    content,
                    validation_mode=validation_mode
                    if validation_mode in {"full", "schema"}
                    else "full",
                )
            return content
    raise IngestBaselineConflict(
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
        raise ValueError(
            f"Integration target is missing the required section: {heading}"
        )
    section_start = start + len(heading)
    next_heading = re.search(r"(?m)^##\s+", content[section_start:])
    section_end = section_start + (
        next_heading.start() if next_heading else len(content[section_start:])
    )
    section = content[section_start:section_end]
    matches = []
    for relation_match in re.finditer(r"(?m)^-[^\n]*$", section):
        relation_line = relation_match.group(0)
        if marker in relation_line or (
            legacy_tokens and all(token in relation_line for token in legacy_tokens)
        ):
            matches.append(relation_match)
    if matches:
        chunks = [section[: matches[0].start()], line]
        cursor = matches[0].end()
        for duplicate in matches[1:]:
            chunks.append(section[cursor : duplicate.start()])
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


def _verify_ingest_source_baseline(processed_data: dict) -> None:
    """Fail closed when the canonical Source changed after dispatch."""
    canonical_name = str(processed_data.get("canonical_name") or "").strip()
    validate_wiki_filename(canonical_name)
    canonical_key = _strip_markdown_suffix(canonical_name)
    expected_version = str(processed_data.get("source_hash") or "")
    actual_version = str(
        governance_store.canonical_page_versions({canonical_key}).get(
            canonical_key,
            "",
        )
        or ""
    )
    if actual_version != expected_version:
        raise IngestBaselineConflict(
            f"Source canonical baseline changed for {canonical_name}: "
            f"expected {expected_version or '<absent>'}, "
            f"current {actual_version or '<absent>'}"
        )
    try:
        actual_projection_hash = _projection_hash_for_canonical_version(
            canonical_name,
            actual_version,
        )
    except (OSError, UnicodeError, ValueError) as exc:
        raise IngestBaselineConflict(
            f"Source projection baseline changed for {canonical_name}: {exc}"
        ) from exc
    expected_projection_hash = str(
        processed_data.get("source_projection_hash") or ""
    ).casefold()
    if actual_projection_hash.casefold() != expected_projection_hash:
        raise IngestBaselineConflict(
            f"Source projection baseline changed for {canonical_name}: "
            f"expected {expected_projection_hash or '<absent>'}, "
            f"current {actual_projection_hash or '<absent>'}"
        )

def _apply_integration_disposition(
    files_written: list, processed_data: dict
) -> tuple[list, str, set[str]]:
    """Validate semantic completion and materialize bounded source/target updates."""
    integration = processed_data.get("integration")
    if not isinstance(integration, dict):
        raise ValueError("finalize_ingest requires an integration disposition")
    disposition = str(integration.get("disposition") or "").strip().lower()
    if disposition not in INTEGRATION_DISPOSITIONS:
        raise ValueError(
            f"integration disposition must be one of {sorted(INTEGRATION_DISPOSITIONS)}"
        )

    files = _normalize_inline_files_written(files_written)

    reason = str(integration.get("reason") or "").strip()
    if disposition == "rejected":
        if files:
            raise ValueError("rejected ingest disposition must not include wiki files")
        if len(reason) < 12:
            raise ValueError("rejected ingest disposition requires an auditable reason")
        return [], disposition, set()

    canonical_name = str(processed_data.get("canonical_name") or "").strip()
    source_items = [
        item
        for item in files
        if os.path.basename(str(item.get("filename", ""))) == canonical_name
    ]
    if len(source_items) != 1:
        raise ValueError(
            f"{disposition} ingest disposition requires exactly one canonical source page: {canonical_name}"
        )
    source_item = source_items[0]
    source_item["expected_version"] = str(processed_data.get("source_hash") or "")
    source_item["expected_projection_hash"] = str(
        processed_data.get("source_projection_hash") or ""
    )
    for item in files:
        if item is not source_item:
            item.setdefault("expected_version", "")
            item.setdefault("expected_projection_hash", "")
    relations = integration.get("relations") or []
    if disposition == "standalone":
        if relations:
            raise ValueError(
                "standalone ingest disposition cannot include integration relations"
            )
        if len(reason) < 12:
            raise ValueError(
                "standalone ingest disposition requires an auditable reason"
            )
        return files, disposition, set()
    if not isinstance(relations, list) or not relations:
        raise ValueError("integrated ingest disposition requires at least one relation")

    submitted_names = {
        os.path.basename(str(item.get("filename", ""))) for item in files
    }
    source_content = str(source_item.get("content") or "").rstrip()
    source_key = _strip_markdown_suffix(canonical_name)
    graph_heading = "## Graph Integration"
    if graph_heading not in source_content:
        source_content += f"\n\n{graph_heading}\n"
    queued_candidates = processed_data.get("_queued_integration_candidates")
    if not isinstance(queued_candidates, list):
        raise ValueError("integrated ingest requires queued integration candidates")
    allowed_candidates = {}
    for candidate in queued_candidates:
        if not isinstance(candidate, dict):
            raise ValueError("queued integration candidates must be objects")
        candidate_target = str(candidate.get("target") or "")
        if not candidate_target or candidate_target in allowed_candidates:
            raise ValueError("queued integration candidates contain an invalid target")
        allowed_candidates[candidate_target] = (
            str(candidate.get("target_hash") or ""),
            str(candidate.get("target_projection_hash") or "").casefold(),
        )
    target_mutations = []
    seen_targets = set()
    for relation in relations:
        if not isinstance(relation, dict):
            raise ValueError("integration relations must be objects")
        target = os.path.basename(str(relation.get("target") or ""))
        if target != str(relation.get("target") or ""):
            raise ValueError("integration relation target must be a wiki basename")
        validate_wiki_filename(target)
        if (
            target == canonical_name
            or target in submitted_names
            or target in seen_targets
        ):
            raise ValueError(
                f"integration target is duplicated or conflicts with submitted files: {target}"
            )
        seen_targets.add(target)
        queued_candidate = allowed_candidates.get(target)
        if queued_candidate is None:
            raise ValueError(
                f"integration target was not dispatched as a candidate: {target}"
            )
        target_key = target[:-3]
        actual_hash = governance_store.canonical_page_versions({target_key}).get(
            target_key
        )
        expected_hash = str(relation.get("target_hash") or "")
        if expected_hash != queued_candidate[0]:
            raise ValueError(
                f"integration target_hash does not match the dispatched candidate for {target}"
            )
        if not expected_hash or expected_hash != actual_hash:
            raise IngestBaselineConflict(
                f"integration target_hash is stale or missing for {target}"
            )
        target_projection_hash = str(
            relation.get("target_projection_hash") or ""
        ).casefold()
        if "target_projection_hash" not in relation or (
            target_projection_hash
            and not re.fullmatch(r"[0-9a-f]{64}", target_projection_hash)
        ):
            raise ValueError(
                f"integration target_projection_hash is stale or missing for {target}"
            )
        if target_projection_hash != queued_candidate[1]:
            raise ValueError(
                "integration target_projection_hash does not match the dispatched "
                f"candidate for {target}"
            )
        target_content = _read_canonical_target_content(
            target,
            expected_hash,
            expected_projection_hash=target_projection_hash,
            materialize_missing=False,
        )
        predicate = str(relation.get("predicate") or "").strip()
        if predicate not in INTEGRATION_PREDICATES:
            raise ValueError(f"unsupported integration predicate: {predicate}")
        evidence = " ".join(str(relation.get("evidence") or "").split())
        if len(evidence) < 12:
            raise ValueError(f"integration evidence is too short for {target}")
        try:
            confidence = float(relation.get("confidence"))
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"integration confidence must be numeric for {target}"
            ) from exc
        if not 0.0 <= confidence <= 1.0:
            raise ValueError(f"integration confidence must be in [0, 1] for {target}")
        event_date = str(relation.get("event_date") or "")
        try:
            datetime.strptime(event_date, "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError(
                f"integration event_date must be YYYY-MM-DD for {target}"
            ) from exc
        event_tag = str(relation.get("event_tag") or "").strip().strip("[]")
        if event_tag not in INTEGRATION_EVENT_TAGS:
            raise ValueError(
                f"unsupported integration event_tag for {target}: {event_tag}"
            )
        relation_id = hashlib.sha256(
            f"{source_key}\x00{target_key}".encode("utf-8")
        ).hexdigest()[:16]
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
        target_mutations.append(
            {
                "filename": target,
                "content": target_content,
                "expected_version": expected_hash,
                "expected_projection_hash": target_projection_hash,
            }
        )

    source_item["content"] = _updated_now(source_content)
    return files + target_mutations, disposition, seen_targets


_TEMPORAL_WIKI_LINK = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class SourceLinkClosureError(ValueError):
    pass


@dataclass(frozen=True)
class _PreparedSourceLinkClosure:
    proposed_page_keys: frozenset[str]
    proposed_labels: tuple[tuple[str, str], ...]
    source_links: tuple[tuple[str, str], ...]


def _strict_link_identity(value: object) -> str:
    """Normalize identity syntax only; preserve prefix, punctuation and spacing."""
    cleaned = _strip_markdown_suffix(str(value or "").strip())
    return unicodedata.normalize("NFKC", cleaned).casefold()


def _prepare_source_link_closure(files: list[dict]) -> _PreparedSourceLinkClosure:
    proposed_page_keys = set()
    proposed_labels = []
    source_links = []
    for item in files:
        filename = os.path.basename(str(item.get("filename") or ""))
        content = str(item.get("content") or "")
        frontmatter, _ = split_frontmatter(content)
        page_key = _strip_markdown_suffix(filename)
        if page_key:
            proposed_page_keys.add(_strict_link_identity(page_key))
            proposed_labels.append((page_key, page_key))
            title = str(frontmatter.get("title") or "").strip()
            if title:
                proposed_labels.append((title, page_key))
            aliases = frontmatter.get("aliases") or []
            if isinstance(aliases, str):
                aliases = [aliases]
            if isinstance(aliases, list):
                proposed_labels.extend(
                    (str(alias).strip(), page_key)
                    for alias in aliases
                    if str(alias).strip()
                )
        if str(frontmatter.get("type") or "").casefold() != "source":
            continue
        for match in iter_wiki_link_matches(content):
            target = _strip_markdown_suffix(match.group(1).strip())
            if target and not _TEMPORAL_WIKI_LINK.fullmatch(target):
                source_links.append((filename, target))
    return _PreparedSourceLinkClosure(
        proposed_page_keys=frozenset(proposed_page_keys),
        proposed_labels=tuple(proposed_labels),
        source_links=tuple(source_links),
    )


def _decode_link_aliases(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    if not value:
        return []
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError):
        return []
    if isinstance(parsed, list):
        return [str(item) for item in parsed]
    if isinstance(parsed, str):
        return [parsed]
    return []


def _canonical_source_link_label_rows() -> list[tuple[str, str, object]]:
    """Read the narrow canonical label projection on the caller's transaction."""
    from vector_lake import db_store

    return [
        (str(row[0] or ""), str(row[1] or ""), row[2])
        for row in db_store.get_connection()
        .execute(
            "SELECT "
            "json_extract(data_json, '$.page_key'), "
            "json_extract(data_json, '$.title'), "
            "json_extract(data_json, '$.aliases') "
            "FROM entities"
        )
        .fetchall()
    ]


def _assert_source_link_closure(
    prepared: _PreparedSourceLinkClosure,
    canonical_rows: list[tuple[str, str, object]],
) -> None:
    labels: dict[str, set[str]] = {}

    def register(label: object, page_key: str) -> None:
        identity = _strict_link_identity(label)
        if identity:
            labels.setdefault(identity, set()).add(page_key)

    for page_key, title, raw_aliases in canonical_rows:
        if not page_key or _strict_link_identity(page_key) in prepared.proposed_page_keys:
            continue
        register(page_key, page_key)
        register(title, page_key)
        for alias in _decode_link_aliases(raw_aliases):
            register(alias, page_key)
    for label, page_key in prepared.proposed_labels:
        register(label, page_key)

    failures = []
    for filename, target in prepared.source_links:
        matches = sorted(labels.get(_strict_link_identity(target), set()))
        if len(matches) == 1:
            continue
        status = "missing" if not matches else f"ambiguous: {', '.join(matches)}"
        failures.append((filename, target, status))
    if not failures:
        return
    unique_failures = sorted(set(failures))
    samples = "; ".join(
        f"{filename} -> [[{target}]] ({status})"
        for filename, target, status in unique_failures[:20]
    )
    raise SourceLinkClosureError(
        "Source Wiki link closure failed: "
        f"{len(unique_failures)} unresolved/ambiguous target(s); {samples}"
    )


def _prepare_source_link_precondition(
    files: list[dict],
) -> Callable[[], None]:
    """Prepare immutable batch labels; query canonical state inside the write lock."""
    prepared = _prepare_source_link_closure(files)

    def verify() -> None:
        _assert_source_link_closure(
            prepared,
            _canonical_source_link_label_rows(),
        )

    return verify


def _validate_final_ingest_files(
    files: list[dict],
    integration_target_names: set[str],
    contract: dict,
) -> tuple[list[dict], set[str]]:
    """Fully validate submitted files and narrowly tolerate legacy targets."""
    from vector_lake.defense_hook import DefenseHookException, verify_asset
    from vector_lake.schema_validator import validate_schema
    from vector_lake.wiki_utils import split_frontmatter

    if not files:
        return validate_ingest_payload([], contract), set()
    permitted_tiers = (
        set(contract["evidence_tiers"]) if integration_target_names else set()
    )
    node_records = []
    schema_maintenance_names = set()
    file_names = {
        os.path.basename(str(item.get("filename") or "")) for item in files
    }
    if not integration_target_names <= file_names:
        raise ValueError("Integration target mutations are incomplete.")
    for item in files:
        filename = os.path.basename(str(item.get("filename") or ""))
        content = str(item.get("content") or "")
        frontmatter, _body = split_frontmatter(content)
        if filename not in integration_target_names:
            verify_asset(content, filename, frontmatter, get_index_path())
            node_records.extend(validate_ingest_payload([item], contract))
            continue
        try:
            verify_asset(content, filename, frontmatter, get_index_path())
            continue
        except DefenseHookException:
            # Match mutation_coordinator's fenced schema-maintenance path:
            # preserve historical dynamic tag debt while validating structure.
            validate_schema(frontmatter, content, filename)
            strategic_scope = str(
                frontmatter.get("strategic_scope") or ""
            ).strip().lower()
            evidence_tier = str(
                frontmatter.get("evidence_tier") or ""
            ).strip()
            has_legacy_purpose_metadata = (
                strategic_scope not in {"core", "edge"}
                or evidence_tier not in permitted_tiers
            )
            if not has_legacy_purpose_metadata:
                raise
            aliases = frontmatter.get("aliases")
            if aliases is not None and not isinstance(aliases, list):
                raise PurposeContractError(
                    f"{filename}: aliases must be a list."
                )
            categories = frontmatter.get("categories")
            if not isinstance(categories, list) or len(categories) != 1:
                raise PurposeContractError(
                    f"{filename}: categories must be a list with exactly one domain."
                )
            schema_maintenance_names.add(filename)
    return node_records, schema_maintenance_names


def _prepare_ingest_instruction_context() -> _IngestInstructionContext:
    schema_content = ""
    try:
        schema_content = (get_extension_root() / "schema.md").read_text(
            encoding="utf-8"
        )
        category_path = get_extension_root() / "SCHEMA_CATEGORIES.md"
        if category_path.exists():
            schema_content += "\n\n" + category_path.read_text(encoding="utf-8")
    except OSError:
        pass
    prompt_path = get_extension_root() / "templates" / "ingest_prompt.md"
    if not prompt_path.exists():
        raise FileNotFoundError("templates/ingest_prompt.md not found")
    return _IngestInstructionContext(
        schema_content=schema_content,
        prompt_template=prompt_path.read_text(encoding="utf-8"),
        purpose_content=_read_purpose(),
        index_context=_prepare_relevant_index_context(),
    )


def _build_ingest_instructions(
    filepath: str,
    file_hash: str,
    canonical_name: str,
    batch_context: _LazyIngestInstructionContext | None = None,
    candidate_manifest: list[dict] | None = None,
) -> str:
    context = (
        batch_context.get()
        if batch_context is not None
        else _prepare_ingest_instruction_context()
    )
    return (
        context.prompt_template.replace("{{filepath}}", str(filepath))
        .replace("{{file_hash}}", file_hash)
        .replace("{{canonical_name}}", canonical_name)
        .replace("{{skeleton_block}}", parse_static_skeleton(filepath))
        .replace("{{schema_content}}", context.schema_content)
        .replace(
            "{{index_summary}}",
            _read_relevant_index_context(
                filepath,
                prepared_context=context.index_context,
                target_validity=context.target_validity,
                candidate_manifest=candidate_manifest,
            ),
        )
        .replace("{{purpose_content}}", context.purpose_content)
    )


def requeue_legacy_ingest_jobs() -> int:
    """Make bounded, CAS-fenced progress on every claimable legacy ingest job."""
    from vector_lake import db_store

    db_store.init_db()
    conn = db_store.get_connection()
    migration_limit = 100
    now = datetime.now(timezone.utc).isoformat()
    contract_sql = db_store._current_ingest_contract_sql()
    rows = conn.execute(
        "SELECT job_id, payload, task_packet_path, status, retries, available_at, "
        "lease_generation, lease_until, lease_owner, lease_token, updated_at, "
        "idempotency_key, completed_at, result_json, error_msg, created_at "
        "FROM jobs WHERE task_type = 'ingest' "
        f"AND (COALESCE(({contract_sql}), 0) = 0 OR (json_valid(payload) "
        "AND json_type(payload, '$.hash') = 'text' "
        "AND length(json_extract(payload, '$.hash')) = 32)) AND ("
        "status = 'awaiting_subagent' OR "
        "(status IN ('queued', 'failed') AND COALESCE(retries, 0) < 3 "
        "AND COALESCE(available_at, created_at, '') <= ?) OR "
        "(status IN ('dispatched', 'subagent_processing') "
        "AND COALESCE(lease_until, '') <= ?)) "
        "ORDER BY created_at, job_id LIMIT ?",
        (str(INGEST_CONTRACT_VERSION), now, now, migration_limit),
    ).fetchall()
    if not rows:
        return 0

    instruction_context = _LazyIngestInstructionContext()
    target_dirs = None
    source_identity_index = None
    plans = []
    for row in rows:
        record = dict(row)
        expected = {
            key: record.get(key)
            for key in (
                "status",
                "available_at",
                "task_packet_path",
                "lease_until",
                "lease_owner",
                "lease_token",
                "updated_at",
                "payload",
                "idempotency_key",
                "completed_at",
                "result_json",
                "error_msg",
            )
        }
        expected["retries"] = int(record.get("retries") or 0)
        expected["lease_generation"] = int(record.get("lease_generation") or 0)
        plan = {
            "job_id": str(record["job_id"]),
            "packet_path": str(record.get("task_packet_path") or ""),
            "expected": expected,
            "action": "fail",
            "reason": "Legacy ingest payload is invalid",
        }
        try:
            payload = json.loads(str(record.get("payload") or ""))
        except (TypeError, json.JSONDecodeError) as exc:
            plan["reason"] = f"Legacy ingest payload is not valid JSON: {exc}"
            plans.append(plan)
            continue
        if not isinstance(payload, dict):
            plan["reason"] = "Legacy ingest payload root must be an object"
            plans.append(plan)
            continue

        raw_value = str(payload.get("filepath") or "").strip()
        if not raw_value:
            plan["reason"] = "Legacy ingest filepath is missing"
            plans.append(plan)
            continue
        raw_path = Path(raw_value)
        if not raw_path.is_absolute():
            raw_path = get_raw_dir().parent / raw_path
        try:
            raw_path = raw_path.resolve()
        except OSError as exc:
            plan["reason"] = f"Legacy ingest filepath cannot be resolved: {exc}"
            plans.append(plan)
            continue
        plan["raw_path"] = str(raw_path)
        if not raw_path.is_file():
            plan.update(
                action="cancel",
                reason=f"Legacy ingest raw source is missing: {raw_path}",
            )
            plans.append(plan)
            continue
        try:
            current_hash = _stable_current_raw_hash(str(raw_path))
        except (OSError, ValueError) as exc:
            plan["reason"] = f"Legacy ingest raw source is unstable: {exc}"
            plans.append(plan)
            continue

        try:
            canonical_name = str(payload.get("canonical_name") or "").strip()
            try:
                validate_wiki_filename(canonical_name)
            except ValueError:
                if target_dirs is None:
                    target_dirs = get_ingest_target_directories()
                if source_identity_index is None:
                    source_identity_index = _source_identity_index(target_dirs)
                canonical_name = canonical_source_name(
                    str(raw_path),
                    source_identity_index=source_identity_index,
                    target_dirs=target_dirs,
                )
                validate_wiki_filename(canonical_name)
            canonical_key = _strip_markdown_suffix(canonical_name)
            source_hash = governance_store.canonical_page_versions({canonical_key}).get(
                canonical_key,
                "",
            )
            source_projection_hash = _projection_hash_for_canonical_version(
                canonical_name,
                source_hash,
            )
            integration_candidates: list[dict] = []
            instructions = _build_ingest_instructions(
                str(raw_path),
                current_hash,
                canonical_name,
                instruction_context,
                integration_candidates,
            )
        except (
            OSError,
            TypeError,
            UnicodeError,
            ValueError,
            RuntimeError,
            sqlite3.Error,
        ) as exc:
            plan["reason"] = (
                f"Legacy ingest projection or prompt rebuild is unsafe: {exc}"
            )
            plans.append(plan)
            continue

        refreshed = dict(payload)
        refreshed.update(
            {
                "filepath": str(raw_path),
                "hash": current_hash,
                "canonical_name": canonical_name,
                "source_hash": source_hash,
                "source_projection_hash": source_projection_hash,
                "integration_candidates": integration_candidates,
                "ingest_contract_version": INGEST_CONTRACT_VERSION,
                "instructions": instructions,
            }
        )
        plan.update(
            action="migrate",
            reason="Legacy ingest packet rebuilt for the current integration contract",
            payload=refreshed,
            expected_raw_hash=current_hash,
            target_key=db_store._job_idempotency_key("ingest", refreshed),
        )
        plans.append(plan)

    now = datetime.now(timezone.utc).isoformat()
    migrated = 0
    cleanup_count = 0
    cas_where = (
        "job_id = ? AND task_type = 'ingest' AND status IS ? "
        "AND COALESCE(retries, 0) = ? AND available_at IS ? "
        "AND task_packet_path IS ? AND COALESCE(lease_generation, 0) = ? "
        "AND lease_until IS ? AND lease_owner IS ? AND lease_token IS ? "
        "AND updated_at IS ? AND payload IS ? AND idempotency_key IS ? "
        "AND completed_at IS ? AND result_json IS ? AND error_msg IS ?"
    )
    for plan in plans:
        expected = plan["expected"]
        cas_values = (
            plan["job_id"],
            expected.get("status"),
            int(expected.get("retries") or 0),
            expected.get("available_at"),
            expected.get("task_packet_path"),
            int(expected.get("lease_generation") or 0),
            expected.get("lease_until"),
            expected.get("lease_owner"),
            expected.get("lease_token"),
            expected.get("updated_at"),
            expected.get("payload"),
            expected.get("idempotency_key"),
            expected.get("completed_at"),
            expected.get("result_json"),
            expected.get("error_msg"),
        )
        with db_store.transaction():
            action = plan["action"]
            if action == "migrate":
                try:
                    current_hash = _stable_current_raw_hash(plan["raw_path"])
                except (OSError, ValueError):
                    continue
                if current_hash != plan["expected_raw_hash"]:
                    continue
                candidate_current = conn.execute(
                    f"SELECT 1 FROM jobs WHERE {cas_where}",
                    cas_values,
                ).fetchone()
                if candidate_current is None:
                    continue
                owner = conn.execute(
                    "SELECT job_id, task_type, status, retries, payload, updated_at, "
                    "idempotency_key FROM jobs WHERE idempotency_key = ? "
                    "AND job_id <> ?",
                    (plan["target_key"], plan["job_id"]),
                ).fetchone()
                released_owner = bool(
                    owner
                    and db_store._release_releasable_ingest_identity_owner(
                        conn,
                        owner,
                        plan["target_key"],
                        plan["payload"],
                        now,
                    )
                )
                if owner is not None and not released_owner:
                    cursor = conn.execute(
                        "UPDATE jobs SET status = 'superseded', retries = 0, "
                        "idempotency_key = NULL, error_msg = ?, result_json = ?, "
                        "updated_at = ?, completed_at = ?, available_at = NULL, "
                        "lease_until = NULL, lease_owner = NULL, lease_token = NULL "
                        f"WHERE {cas_where}",
                        (
                            f"Legacy ingest identity is owned by {owner['job_id']}",
                            json.dumps(
                                {
                                    "maintenance": "legacy_ingest_migration",
                                    "owner_job_id": str(owner["job_id"]),
                                },
                                sort_keys=True,
                            ),
                            now,
                            now,
                            *cas_values,
                        ),
                    )
                else:
                    cursor = conn.execute(
                        "UPDATE jobs SET payload = ?, status = 'queued', retries = 0, "
                        "idempotency_key = ?, error_msg = ?, result_json = NULL, "
                        "available_at = ?, updated_at = ?, completed_at = NULL, "
                        "lease_until = NULL, lease_owner = NULL, lease_token = NULL "
                        f"WHERE {cas_where}",
                        (
                            json.dumps(
                                plan["payload"],
                                ensure_ascii=False,
                                sort_keys=True,
                            ),
                            plan["target_key"],
                            plan["reason"],
                            now,
                            now,
                            *cas_values,
                        ),
                    )
                    if released_owner and cursor.rowcount != 1:
                        raise RuntimeError(
                            "Legacy identity transfer lost its candidate after owner release"
                        )
                    if cursor.rowcount == 1:
                        migrated += 1
            elif action == "cancel":
                raw_path = Path(plan["raw_path"])
                if raw_path.exists():
                    continue
                cursor = conn.execute(
                    "UPDATE jobs SET status = 'cancelled', retries = 0, "
                    "idempotency_key = NULL, error_msg = ?, result_json = ?, "
                    "updated_at = ?, completed_at = ?, available_at = NULL, "
                    "lease_until = NULL, lease_owner = NULL, lease_token = NULL "
                    f"WHERE {cas_where}",
                    (
                        plan["reason"],
                        json.dumps(
                            {
                                "maintenance": "legacy_ingest_migration",
                                "state": "cancelled",
                            },
                            sort_keys=True,
                        ),
                        now,
                        now,
                        *cas_values,
                    ),
                )
            else:
                cursor = conn.execute(
                    "UPDATE jobs SET status = 'failed', retries = 3, error_msg = ?, "
                    "result_json = ?, updated_at = ?, completed_at = ?, "
                    "available_at = ?, lease_until = NULL, lease_owner = NULL, "
                    "lease_token = NULL, idempotency_key = NULL "
                    f"WHERE {cas_where}",
                    (
                        plan["reason"],
                        json.dumps(
                            {
                                "maintenance": "legacy_ingest_migration",
                                "state": "blocked",
                            },
                            sort_keys=True,
                        ),
                        now,
                        now,
                        now,
                        *cas_values,
                    ),
                )
            if cursor.rowcount == 1 and plan["packet_path"]:
                db_store.enqueue_ingest_task_cleanup(
                    plan["job_id"],
                    plan["packet_path"],
                )
                cleanup_count += 1

    if cleanup_count:
        cleanup = process_ingest_task_cleanup(limit=max(20, cleanup_count))
        for error in cleanup["errors"]:
            log.warning("Could not remove superseded ingest packet: %s", error)
    return migrated


def _existing_durable_ingest_keys(
    conn,
    candidate_keys,
    *,
    chunk_size: int = 400,
) -> set[str]:
    """Read only durable identities relevant to this inventory."""
    keys = sorted({str(key) for key in candidate_keys if key})
    bounded_chunk_size = max(1, min(400, int(chunk_size)))
    existing: set[str] = set()
    for offset in range(0, len(keys), bounded_chunk_size):
        chunk = keys[offset : offset + bounded_chunk_size]
        placeholders = ", ".join("?" for _ in chunk)
        rows = conn.execute(
            "SELECT idempotency_key FROM jobs "
            "WHERE task_type = 'ingest' AND idempotency_key IS NOT NULL "
            "AND COALESCE(status, '') NOT IN ('cancelled', 'superseded') "
            "AND NOT (COALESCE(status, '') = 'failed' "
            "AND COALESCE(retries, 0) >= 3) "
            "AND (COALESCE(status, '') NOT IN ('completed', 'finalized') "
            "OR NOT json_valid(payload) "
            "OR CASE WHEN json_valid(payload) "
            "THEN json_extract(payload, '$.filepath') END IS NULL "
            "OR CASE WHEN json_valid(payload) "
            "THEN json_extract(payload, '$.hash') END IS NULL "
            "OR EXISTS ("
            "SELECT 1 FROM processed_files AS processed "
            "WHERE processed.filepath = CASE WHEN json_valid(payload) "
            "THEN json_extract(payload, '$.filepath') END "
            "AND processed.file_hash = CASE WHEN json_valid(payload) "
            "THEN json_extract(payload, '$.hash') END"
            ")) "
            f"AND idempotency_key IN ({placeholders})",
            tuple(chunk),
        )
        existing.update(str(row["idempotency_key"]) for row in rows)
    return existing


def _sql_parameter_chunks(values, *, chunk_size: int = 400):
    """Yield deterministic SQL parameter chunks within the local safety bound."""
    unique = sorted(
        {str(value) for value in values if value is not None and str(value)}
    )
    bounded_chunk_size = max(1, min(400, int(chunk_size)))
    for offset in range(0, len(unique), bounded_chunk_size):
        yield unique[offset : offset + bounded_chunk_size]


def _candidate_processed_files(conn, filepaths) -> dict[str, tuple]:
    """Read processed-file state only for paths in a candidate event batch."""
    processed = {}
    for chunk in _sql_parameter_chunks(filepaths):
        placeholders = ", ".join("?" for _ in chunk)
        rows = conn.execute(
            "SELECT filepath, file_hash, observed_mtime_ns, observed_size "
            f"FROM processed_files WHERE filepath IN ({placeholders})",
            tuple(chunk),
        )
        processed.update(
            {
                row["filepath"]: (
                    row["file_hash"],
                    row["observed_mtime_ns"],
                    row["observed_size"],
                )
                for row in rows
            }
        )
    return processed


def _processed_file_row(conn, filepath: str) -> tuple | None:
    row = conn.execute(
        "SELECT file_hash, observed_mtime_ns, observed_size "
        "FROM processed_files WHERE filepath = ?",
        (str(filepath),),
    ).fetchone()
    if row is None:
        return None
    return (
        str(row["file_hash"] or ""),
        row["observed_mtime_ns"],
        row["observed_size"],
    )


def _processed_revision_state(
    conn,
    filepath: str,
    initial_row: tuple,
    snapshot: StableRawRevision,
) -> tuple[str, tuple | None]:
    """Compare one marker without silently trusting a legacy MD5 collision."""
    row = initial_row
    for _attempt in range(2):
        if row is None:
            return "drifted", None
        stored_hash, observed_mtime_ns, observed_size = row
        try:
            kind, _digest = parse_revision(stored_hash)
        except RawRevisionFormatError:
            return "invalid", row
        if not snapshot.matches(stored_hash):
            if not snapshot_still_current(snapshot):
                return "retry", row
            return "drifted", row
        if kind == "sha256":
            if not snapshot_still_current(snapshot):
                return "retry", row
            return "matched", row
        # An MD5 match cannot prove the current bytes are the bytes that were
        # ingested. Keep the legacy marker and enqueue a canonical SHA-256 job.
        # An explicit operator migration may establish a trusted baseline
        # before scanning, but the runtime must never make that trust decision.
        if not snapshot_still_current(snapshot):
            return "retry", row
        return "legacy", row
    return "retry", row


def _candidate_legacy_ingest_identities(conn, filepaths) -> set[tuple[str, str, str]]:
    """Read active legacy identities only for paths in a candidate event batch."""
    identities = set()
    for chunk in _sql_parameter_chunks(filepaths):
        placeholders = ", ".join("?" for _ in chunk)
        rows = conn.execute(
            "SELECT CASE WHEN json_valid(payload) "
            "THEN json_extract(payload, '$.filepath') END AS filepath, "
            "CASE WHEN json_valid(payload) "
            "THEN json_extract(payload, '$.hash') END AS file_hash, "
            "CASE WHEN json_valid(payload) "
            "THEN json_extract(payload, '$.canonical_name') "
            "END AS canonical_name "
            "FROM jobs WHERE task_type = 'ingest' AND idempotency_key IS NULL "
            "AND (status IN ('queued', 'dispatched', 'awaiting_subagent', "
            "'subagent_processing') OR (status = 'failed' AND COALESCE(retries, 0) < 3)) "
            "AND CASE WHEN json_valid(payload) "
            "THEN json_extract(payload, '$.filepath') END "
            f"IN ({placeholders})",
            tuple(chunk),
        )
        identities.update(
            (
                str(row["filepath"]),
                str(row["file_hash"]),
                str(row["canonical_name"]),
            )
            for row in rows
            if row["filepath"] and row["file_hash"] and row["canonical_name"]
        )
    return identities


def _candidate_source_entities(conn, source_identities) -> list[dict]:
    """Read candidate-linked Source entities with one JSON table scan."""
    candidates = frozenset(
        normalized
        for value in source_identities
        if (normalized := normalize_source_identity(value))
    )
    if not candidates:
        return []

    def is_candidate(value) -> int:
        return int(normalize_source_identity(value) in candidates)

    conn.create_function(
        "vector_lake_source_identity_is_candidate",
        1,
        is_candidate,
        deterministic=True,
    )
    try:
        rows = conn.execute(
            "SELECT DISTINCT entities.entity_id, entities.data_json "
            "FROM entities JOIN json_each("
            "CASE WHEN json_valid(entities.data_json) "
            "THEN entities.data_json ELSE '{}' END, '$.sources'"
            ") AS source_identity "
            "WHERE entities.type = 'source' AND entities.status != 'Merged' "
            "AND vector_lake_source_identity_is_candidate(source_identity.value) = 1 "
            "ORDER BY entities.entity_id"
        ).fetchall()
    finally:
        conn.create_function(
            "vector_lake_source_identity_is_candidate",
            1,
            None,
            deterministic=True,
        )
    items = {}
    for row in rows:
        try:
            item = json.loads(str(row["data_json"] or "{}"))
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(item, dict):
            entity_id = str(item.get("entity_id") or row["entity_id"])
            items[entity_id] = item
    return [items[entity_id] for entity_id in sorted(items)]


def _load_ingest_config() -> dict:
    config_path = get_extension_root() / "config.json"
    try:
        with config_path.open("r", encoding="utf-8") as handle:
            loaded = json.load(handle)
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"ingest_config_invalid:{config_path}:{type(exc).__name__}:{exc}"
        ) from exc
    if not isinstance(loaded, dict):
        raise RuntimeError(f"ingest_config_invalid:{config_path}:root_must_be_object")
    for field_name in (
        "target_directories",
        "exclude_paths",
        "supported_extensions",
    ):
        value = loaded.get(field_name)
        if value is not None and (
            not isinstance(value, list)
            or any(not isinstance(item, str) or not item.strip() for item in value)
        ):
            raise RuntimeError(
                f"ingest_config_invalid:{config_path}:{field_name}_must_be_string_list"
            )
    return loaded


def get_ingest_target_directories(
    config: dict | None = None,
    *,
    collapse_nested: bool = False,
) -> list[Path]:
    """Resolve every ingest root and optionally collapse nested watch trees."""
    loaded = _load_ingest_config() if config is None else config
    candidates = []
    for configured in loaded.get("target_directories", []):
        configured_path = Path(configured)
        candidates.append(
            (
                configured_path
                if configured_path.is_absolute()
                else get_extension_root() / configured_path
            ).resolve()
        )
    candidates.append(get_raw_dir().resolve())
    unique = []
    seen = set()
    for candidate in candidates:
        identity = os.path.normcase(str(candidate))
        if identity not in seen:
            seen.add(identity)
            unique.append(candidate)
    if not collapse_nested:
        return unique
    collapsed = []
    for candidate in sorted(
        unique,
        key=lambda value: (len(value.parts), os.path.normcase(str(value))),
    ):
        if any(
            candidate == parent or candidate.is_relative_to(parent)
            for parent in collapsed
        ):
            continue
        collapsed.append(candidate)
    return collapsed


def is_private_diary_path(path: str | Path) -> bool:
    """Check lexical and resolved ancestry for the reserved privacy/Diary subtree."""
    candidate = Path(path)
    variants = [candidate.absolute()]
    try:
        variants.append(candidate.resolve())
    except OSError:
        pass
    for variant in variants:
        parts = tuple(part.casefold() for part in variant.parts)
        if any(
            parts[index] == "privacy" and parts[index + 1] == "diary"
            for index in range(len(parts) - 1)
        ):
            return True
    return False


def prepare_ingest_batch(
    batch_size: int = 5,
    candidate_paths: list[str] | None = None,
    *,
    _enqueue_all: bool = False,
) -> str:
    """Persist a bounded public batch or one private startup inventory."""
    config = _load_ingest_config()
    target_dirs = get_ingest_target_directories(config)
    exclude_paths = config.get("exclude_paths", [])
    exclude_part_patterns = [
        tuple(
            part.casefold()
            for part in str(exclude).replace("\\", "/").split("/")
            if part and part != "."
        )
        for exclude in exclude_paths
    ]
    exclude_part_patterns = [pattern for pattern in exclude_part_patterns if pattern]

    def relative_is_excluded(relative: Path) -> bool:
        relative_parts = tuple(part.casefold() for part in relative.parts)
        return any(
            any(
                relative_parts[offset : offset + len(pattern)] == pattern
                for offset in range(len(relative_parts) - len(pattern) + 1)
            )
            for pattern in exclude_part_patterns
            if len(pattern) <= len(relative_parts)
        )

    def path_is_excluded(path: Path, target_dir: Path) -> bool:
        variants = [path.absolute()]
        try:
            variants.append(path.resolve())
        except OSError:
            pass
        for variant in variants:
            try:
                relative = variant.relative_to(target_dir)
            except ValueError:
                continue
            if relative_is_excluded(relative):
                return True
        return False

    supported_exts = {
        (
            extension.casefold()
            if extension.startswith(".")
            else f".{extension.casefold()}"
        )
        for extension in config.get(
            "supported_extensions",
            [".md", ".txt"],
        )
    }

    def allowed_path(path: Path) -> bool:
        if is_private_diary_path(path):
            return False
        if not path.is_file() or path.name.startswith(("~", ".")):
            return False
        if path.suffix.lower() not in supported_exts:
            return False
        matched_target = False
        for target_dir in target_dirs:
            try:
                path.resolve().relative_to(target_dir)
            except ValueError:
                continue
            matched_target = True
            if path_is_excluded(path, target_dir):
                return False
        return matched_target

    files_to_process = set()
    inventory_errors = []
    if candidate_paths is not None:
        files_to_process = {
            str(Path(candidate).absolute())
            for candidate in candidate_paths
            if allowed_path(Path(candidate))
        }
    else:
        scan_target_dirs = get_ingest_target_directories(
            config,
            collapse_nested=True,
        )

        def record_walk_error(exc):
            inventory_errors.append(f"inventory_walk_error:{type(exc).__name__}:{exc}")

        for target_dir in scan_target_dirs:
            if is_private_diary_path(target_dir):
                continue
            if any(path_is_excluded(target_dir, root) for root in target_dirs):
                continue
            if not target_dir.exists() or not target_dir.is_dir():
                inventory_errors.append(f"ingest_target_unavailable:{target_dir}")
                continue
            for root, dirs, files in os.walk(
                target_dir,
                onerror=record_walk_error,
            ):
                root_path = Path(root)
                dirs[:] = [
                    directory
                    for directory in dirs
                    if not directory.startswith(".")
                    and not is_private_diary_path(root_path / directory)
                    and not any(
                        path_is_excluded(root_path / directory, target)
                        for target in target_dirs
                    )
                ]
                for filename in files:
                    path = root_path / filename
                    if allowed_path(path):
                        files_to_process.add(str(path.absolute()))

    # Configured roots may overlap. One recovery generation hashes each
    # physical path at most once before persisting the complete inventory.
    files_to_process = sorted(files_to_process)

    from vector_lake.db_store import _job_idempotency_key, get_connection, init_db

    init_db()
    conn = get_connection()
    if candidate_paths is None:
        cur = conn.execute(
            "SELECT filepath, file_hash, observed_mtime_ns, observed_size "
            "FROM processed_files"
        )
        processed = {
            row["filepath"]: (
                row["file_hash"],
                row["observed_mtime_ns"],
                row["observed_size"],
            )
            for row in cur.fetchall()
        }
        legacy_ingest_identities = {
            (
                str(row["filepath"]),
                str(row["file_hash"]),
                str(row["canonical_name"]),
            )
            for row in conn.execute(
                "SELECT CASE WHEN json_valid(payload) "
                "THEN json_extract(payload, '$.filepath') END AS filepath, "
                "CASE WHEN json_valid(payload) "
                "THEN json_extract(payload, '$.hash') END AS file_hash, "
                "CASE WHEN json_valid(payload) "
                "THEN json_extract(payload, '$.canonical_name') "
                "END AS canonical_name "
                "FROM jobs WHERE task_type = 'ingest' AND idempotency_key IS NULL "
                "AND (status IN ('queued', 'dispatched', 'awaiting_subagent', "
                "'subagent_processing') OR (status = 'failed' AND COALESCE(retries, 0) < 3))"
            )
            if row["filepath"] and row["file_hash"] and row["canonical_name"]
        }
        source_identity_index = _source_identity_index(target_dirs)
    else:
        cur = None
        processed = _candidate_processed_files(conn, files_to_process)
        legacy_ingest_identities = _candidate_legacy_ingest_identities(
            conn,
            files_to_process,
        )
        source_identity_index = _source_identity_index(
            target_dirs,
            source_identities={
                _raw_identity_for_path(filepath) for filepath in files_to_process
            },
            connection=conn,
        )

    ingest_candidates = []
    observations_to_update = []
    for filepath in files_to_process:
        try:
            processed_row = processed.get(filepath)
            state = "retry"
            classified_row = processed_row
            snapshot = None
            for _attempt in range(2):
                snapshot = stable_raw_revision(
                    filepath,
                    allowed_roots=target_dirs,
                )
                if processed_row is None:
                    state, classified_row = "drifted", None
                else:
                    state, classified_row = _processed_revision_state(
                        conn,
                        filepath,
                        processed_row,
                        snapshot,
                    )
                if state == "drifted" and not snapshot_still_current(snapshot):
                    state = "retry"
                if state != "retry":
                    break
                processed_row = _processed_file_row(conn, filepath)
            if snapshot is None or state == "retry":
                inventory_errors.append(f"hash_unstable:{filepath}")
                continue
            if state == "invalid":
                invalid_hash = str((classified_row or ("",))[0])
                inventory_errors.append(
                    f"processed_revision_invalid:{filepath}:{invalid_hash}"
                )
                continue
            if state == "cas_failed":
                inventory_errors.append(
                    f"processed_revision_upgrade_cas_failed:{filepath}"
                )
                continue
            if state in {"matched", "migrated"}:
                assert classified_row is not None
                _stored_hash, observed_mtime_ns, observed_size = classified_row
                if (
                    observed_mtime_ns != snapshot.observed_mtime_ns
                    or observed_size != snapshot.observed_size
                ):
                    observations_to_update.append(
                        (
                            filepath,
                            snapshot.canonical_revision,
                            snapshot.observed_mtime_ns,
                            snapshot.observed_size,
                        )
                    )
                continue

            file_hash = snapshot.canonical_revision
            canonical_name = canonical_source_name(
                filepath,
                source_identity_index,
                target_dirs,
            )
            identity = (filepath, file_hash, canonical_name)
            identity_key = _job_idempotency_key(
                "ingest",
                {
                    "filepath": filepath,
                    "hash": file_hash,
                    "canonical_name": canonical_name,
                },
            )
            ingest_candidates.append((identity_key, identity))
        except (OSError, RawSourceUnstableError) as exc:
            inventory_errors.append(
                f"source_stat_error:{filepath}:{type(exc).__name__}:{exc}"
            )

    if observations_to_update:
        update_processed_file_observations(observations_to_update)

    existing_ingest_keys = _existing_durable_ingest_keys(
        conn,
        (identity_key for identity_key, _identity in ingest_candidates),
    )
    pending_files = [
        identity
        for identity_key, identity in ingest_candidates
        if identity_key not in existing_ingest_keys
        and identity not in legacy_ingest_identities
    ]
    if not pending_files:
        if inventory_errors:
            samples = "; ".join(inventory_errors[:3])
            raise RuntimeError(
                f"Ingest inventory incomplete for {len(inventory_errors)} "
                f"path(s): {samples}"
            )
        if _enqueue_all:
            return f"{FULL_SCAN_COMPLETE_TOKEN}\n{NO_NEW_REVISIONS_MESSAGE}"
        return NO_NEW_REVISIONS_MESSAGE

    # Release full-inventory structures before instruction rendering and
    # job persistence amplify per-source payload memory.
    del files_to_process, ingest_candidates, existing_ingest_keys
    del legacy_ingest_identities, processed, source_identity_index, cur

    if not _enqueue_all:
        pending_files = pending_files[:batch_size]

    from vector_lake.db_store import enqueue_job

    enqueued_count = 0
    payload = None
    instruction_context = _LazyIngestInstructionContext()
    canonical_keys = {
        _strip_markdown_suffix(canonical_name)
        for _filepath, _file_hash, canonical_name in pending_files
    }
    source_hashes = governance_store.canonical_page_versions(canonical_keys)
    preparation_errors = []

    for filepath, file_hash, canonical_name in pending_files:
        try:
            canonical_key = _strip_markdown_suffix(canonical_name)
            source_hash = source_hashes.get(canonical_key, "")
            source_projection_hash = _projection_hash_for_canonical_version(
                canonical_name,
                source_hash,
            )
            integration_candidates: list[dict] = []
            instructions = _build_ingest_instructions(
                filepath,
                file_hash,
                canonical_name,
                instruction_context,
                integration_candidates,
            )
            payload = {
                "filepath": str(filepath),
                "hash": file_hash,
                "canonical_name": canonical_name,
                "source_hash": source_hash,
                "source_projection_hash": source_projection_hash,
                "integration_candidates": integration_candidates,
                "ingest_contract_version": INGEST_CONTRACT_VERSION,
                "instructions": instructions,
            }
            enqueue_job("ingest", payload)
            enqueued_count += 1
        except Exception as exc:
            preparation_errors.append(f"{filepath}: {type(exc).__name__}: {exc}")

    batch_errors = [*inventory_errors, *preparation_errors]
    if batch_errors:
        samples = "; ".join(batch_errors[:3])
        raise RuntimeError(
            "Ingest preparation failed for "
            f"{len(batch_errors)} file(s) after enqueueing "
            f"{enqueued_count} valid peer(s): {samples}"
        )

    if _enqueue_all:
        return (
            f"{FULL_SCAN_COMPLETE_TOKEN}\n"
            f"Successfully enqueued {enqueued_count} files for ingestion."
        )
    if batch_size == 1 and enqueued_count == 1:
        assert payload is not None
        return json.dumps(payload)

    return f"Successfully enqueued {enqueued_count} files for ingestion."


def _finalize_ingest_impl(
    files_written: list,
    processed_data: dict,
    *,
    propagate_errors: bool,
) -> str:
    """Finalize one ingest while preserving the public string-error contract."""
    try:
        from vector_lake.wiki_utils import SafeWriteError
        from vector_lake.db_store import (
            finalize_ingest_job,
            validate_ingest_job_finalization,
        )

        files = files_written
        job_id = processed_data.get("job_id")
        if not job_id:
            raise ValueError("finalize_ingest requires a claimed job_id")
        job_row = validate_ingest_job_finalization(str(job_id), processed_data)
        processed_data = dict(processed_data)
        processed_data["_queued_integration_candidates"] = list(
            job_row["parsed_payload"].get("integration_candidates") or []
        )
        queued_hash = str(processed_data.get("hash") or "")
        queued_filepath = str(processed_data.get("filepath") or "")

        try:
            queued_hash_kind, _queued_hash_digest = parse_revision(queued_hash)
        except RawRevisionFormatError as exc:
            raise IngestBaselineConflict(
                "Queued raw revision format is unsupported"
            ) from exc
        if queued_hash_kind != "sha256":
            raise IngestBaselineConflict(
                "Legacy queued raw revision must be rebuilt with canonical SHA-256"
            )

        def verify_raw_revision() -> StableRawRevision:
            try:
                snapshot = _stable_current_raw_revision(queued_filepath)
                matches = snapshot.matches(queued_hash)
            except RawRevisionFormatError as exc:
                raise IngestBaselineConflict(
                    "Queued raw revision format is unsupported"
                ) from exc
            if not matches:
                raise IngestBaselineConflict(
                    "Raw source changed after ingest dispatch; "
                    f"queued_hash={queued_hash} "
                    f"current_hash={snapshot.canonical_revision}"
                )
            return snapshot

        verify_raw_revision()
        _verify_ingest_source_baseline(processed_data)
        lease_owner = str(processed_data.get("lease_owner") or "")
        lease_token = str(processed_data.get("lease_token") or "")
        lease_generation = int(processed_data.get("lease_generation"))
        (
            files,
            integration_disposition,
            integration_target_names,
        ) = _apply_integration_disposition(files, processed_data)
        contract = load_purpose_contract()
        (
            node_records,
            schema_maintenance_filenames,
        ) = _validate_final_ingest_files(
            files,
            integration_target_names,
            contract,
        )
        verify_source_link_closure = _prepare_source_link_precondition(files)

        wiki_dir = get_wiki_dir()

        written_paths = []
        mutations = []
        for item in files:
            fname = os.path.basename(item["filename"])
            fcontent = item["content"]

            if "Concept_Decision_" in fname:
                lower_content = fcontent.lower()
                if not all(
                    k in lower_content
                    for k in ["context", "alternatives", "justification"]
                ):
                    raise SafeWriteError(
                        f"Decision nodes like {fname} MUST contain 'context', 'alternatives', and 'justification'."
                    )

            mutation = {"filename": fname, "content": fcontent}
            if "expected_version" in item:
                mutation["expected_version"] = item["expected_version"]
            if "expected_projection_hash" in item:
                mutation["expected_projection_hash"] = item["expected_projection_hash"]
            mutations.append(mutation)
            written_paths.append(str(wiki_dir / fname))

        filepath = processed_data["filepath"]
        from vector_lake.mutation_coordinator import execute_mutation_batch

        def mark_ingest_processed():
            final_snapshot = verify_raw_revision()
            mark_file_processed(
                filepath,
                final_snapshot.canonical_revision,
                observed_mtime_ns=final_snapshot.observed_mtime_ns,
                observed_size=final_snapshot.observed_size,
            )
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

        if mutations:
            execute_mutation_batch(
                mutations,
                canonical_callback=mark_ingest_processed,
                precondition_callback=verify_source_link_closure,
                origin="ingest_integration",
                schema_maintenance_filenames=schema_maintenance_filenames,
            )
        else:
            from vector_lake import db_store as ingest_db_store
            from vector_lake.runtime_health import enforce_runtime_write_health

            enforce_runtime_write_health(validation_mode="full")
            ingest_db_store.init_db()
            with ingest_db_store.transaction():
                verify_source_link_closure()
                mark_ingest_processed()

        try:
            cleanup = process_ingest_task_cleanup(limit=20)
        except Exception as cleanup_error:
            cleanup = {"errors": [f"{type(cleanup_error).__name__}:{cleanup_error}"]}
        for error in cleanup["errors"]:
            log.warning("Ingest finalized, but task packet cleanup failed: %s", error)

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
                        if any(
                            isinstance(edge, dict)
                            and edge.get("target") in target_names
                            for edge in edges
                        ):
                            candidate_records.append(
                                {
                                    "filename": node.get("id", ""),
                                    "sources": node.get("sources", []),
                                    "tension_edges": edges,
                                }
                            )
                for proposal in build_synthesis_proposals(candidate_records, contract):
                    governance_store.enqueue_governance_item(
                        proposal["type"],
                        proposal["title"],
                        proposal["description"],
                        ", ".join(proposal["sources"]),
                        proposal["search_queries"],
                        proposal["affected_pages"],
                    )
                    proposal_count += 1
            except Exception as exc:
                log.warning(
                    "Ingest completed, but Synthesis-Proposal evaluation failed: %s",
                    exc,
                )

        suffix = (
            f" Queued {proposal_count} Synthesis-Proposal(s)." if proposal_count else ""
        )
        return (
            f"Successfully finalized ingestion for {filepath}. "
            f"Integration disposition: {integration_disposition}.{suffix}"
        )
    except IngestBaselineConflict as exc:
        try:
            from vector_lake import db_store

            requeued = db_store.requeue_ingest_subagent_baseline_conflict(
                str(processed_data.get("job_id") or ""),
                str(processed_data.get("lease_owner") or ""),
                str(processed_data.get("lease_token") or ""),
                int(processed_data.get("lease_generation")),
                f"Ingest baseline changed after dispatch: {exc}",
                current_ingest_contract_version=INGEST_CONTRACT_VERSION,
            )
        except Exception as requeue_error:
            if propagate_errors:
                raise IngestFinalizationInfrastructureError(
                    "automatic baseline requeue failed: "
                    f"{type(requeue_error).__name__}:{requeue_error}"
                ) from requeue_error
            log.exception("Automatic baseline requeue failed")
            return (
                f"Error finalizing ingestion: {exc}; "
                f"automatic baseline requeue failed: {type(requeue_error).__name__}"
            )
        if requeued:
            return (
                "Ingest baseline changed; job requeued for a fresh dispatch: "
                f"{exc}"
            )
        if propagate_errors:
            raise IngestFinalizationInfrastructureError(
                "baseline changed, but the exact finalization lease was no longer current: "
                f"{exc}"
            ) from exc
        return (
            "Error finalizing ingestion: baseline changed, but the lease was "
            f"no longer current: {exc}"
        )
    except Exception as e:
        if propagate_errors:
            raise
        log.exception("Public ingest finalization failed")
        return f"Error finalizing ingestion: {e}"


def finalize_ingest(files_written: list, processed_data: dict) -> str:
    """Finalize an ingest operation using the stable MCP string-return contract."""
    return _finalize_ingest_impl(
        files_written,
        processed_data,
        propagate_errors=False,
    )


def finalize_ingest_strict(files_written: list, processed_data: dict) -> str:
    """Finalize for the automatic controller with typed retryable failures."""
    try:
        return _finalize_ingest_impl(
            files_written,
            processed_data,
            propagate_errors=True,
        )
    except IngestFinalizationInfrastructureError:
        raise
    except (OSError, sqlite3.Error, RuntimeError) as exc:
        raise IngestFinalizationInfrastructureError(
            f"{type(exc).__name__}:{exc}"
        ) from exc
