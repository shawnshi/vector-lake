import os
import json
import hashlib
import hmac
import logging
import re
import sqlite3
import time
from collections import Counter
from pathlib import Path
from datetime import datetime, timezone

from vector_lake import governance_store
from vector_lake.merge_analysis import normalize_source_identity
from vector_lake.wiki_utils import (
    get_raw_dir,
    get_wiki_dir,
    iter_markdown_files,
    validate_wiki_filename,
)
from vector_lake.purpose_contract import (
    load_purpose_contract,
)
from vector_lake.raw_revision import (
    RawRevisionFormatError,
    RawSourceContainmentError,
    RawSourceUnstableError,
    parse_revision,
    stable_raw_revision,
)

# The ingest engine lives in the orchestration layer; these names keep the
# remaining handler-tier code here working unchanged.
from vector_lake.ingest_engine import (
    INGEST_CONTRACT_VERSION,
    IngestBaselineConflict,
    _EXACT_REVIEWED_INGEST_CONTRACT,
    _HASH_SUFFIX,
    _LazyIngestInstructionContext,
    _ReviewedIngestFinalizerContext,
    _build_ingest_instructions,
    _finalize_ingest_impl,
    _is_allowed_ingest_source_identity,
    _normalize_codex_output_pages,
    _prepare_final_ingest_files,
    _prepare_source_link_precondition,
    _projection_hash_for_canonical_version,
    _source_identity_index,
    _stable_current_raw_revision,
    _strip_markdown_suffix,
    _validate_final_ingest_files,
    _verify_ingest_source_baseline,
    canonical_source_name,
    finalize_ingest_strict,
    is_private_diary_path,
    process_ingest_task_cleanup,
    requeue_legacy_ingest_jobs,
)

log = logging.getLogger("vector-lake-ingest")
_INGEST_DEBT_APPLY_DEFAULT_LIMIT = 100
_INGEST_DEBT_APPLY_MAX_LIMIT = 100
_TERMINAL_RETRY_CONTRACT = "vector-lake-terminal-ingest-retry/v1"
_INGEST_DEBT_EXACT_CONTRACT = "vector-lake-ingest-debt-exact/v1"
_INGEST_DEBT_EXACT_ACTIONS = {"supersede_duplicate"}
_LEGACY_RETRYABLE_GENERATOR_REASON = "codex_event_log_type_is_not_allowed:error"
# Marker written by ``_auto_source_page``; a terminal job whose canonical page
# still carries it never received enrichment content and can be re-armed
# deliberately.  This page evidence is the only gate on reopen; the failure text
# is not consulted (see ``_provenance_only_terminal_job_ids``).
_PROVENANCE_ONLY_SOURCE_MARKER = "该页面为摄入引擎自动生成的 provenance-only Source 记录"
# Public alias: lint and other read-only inspectors must not depend on a
# private name to recognize this class.
PROVENANCE_ONLY_SOURCE_MARKER = _PROVENANCE_ONLY_SOURCE_MARKER


def _provenance_only_terminal_job_ids(conn) -> list[str]:
    """Return terminal ingest jobs whose canonical page is still a seed stub.

    A provenance-only Source page means the enrichment generator never delivered
    content (empty file list, dead runner, or an unfinished relay answer).  Those
    jobs are otherwise terminal, so re-arming them is an explicit operator choice
    rather than an automatic retry.

    Selection is gated on the *page evidence*, not on the failure text: a
    ``finalized`` job and a ``failed`` job whose page is still a seed stub both
    prove enrichment never landed, so both are recoverable.  The previous rule
    additionally required a failed job's error to start with
    ``serialized_prompt_and_schema_exceed_token_budget``, which left
    ``intelligence_20260701_briefing`` unrecoverable by any entry point: its page
    carried the seed marker but its error was ``model attempt budget exhausted``,
    and the single-job terminal retry covers only the legacy generator error.
    """
    from vector_lake.wiki_utils import get_wiki_dir

    wiki_dir = Path(get_wiki_dir())
    selected: list[str] = []
    for row in conn.execute(
        "SELECT job_id, status, error_msg, payload FROM jobs "
        "WHERE task_type = 'ingest' AND status IN ('finalized', 'failed')"
    ):
        try:
            payload = json.loads(row["payload"] or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        canonical_name = str(payload.get("canonical_name") or "")
        if not canonical_name.casefold().endswith(".md"):
            continue
        candidate = wiki_dir / Path(canonical_name).name
        try:
            text = candidate.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        if _PROVENANCE_ONLY_SOURCE_MARKER in text:
            selected.append(str(row["job_id"]))
    return sorted(selected)








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
    "source_observed_at",
    "attempt_id",
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
        "source_observed_at": str(payload.get("source_observed_at") or ""),
        "attempt_id": str(payload.get("attempt_id") or ""),
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
    if action not in {"complete_already_processed", "requeue_current"}:
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

    marker_matches = any(marker_matches_current(row) for row in rows)
    if action == "requeue_current":
        if item.get("reopen_provenance_only"):
            # Reopening a provenance-only Source page is an explicit operator
            # action.  The processed marker records that the raw bytes were
            # ingested, not that the page ever received enrichment content, so it
            # must not fence this requeue.
            return ""
        return (
            "processed_files now proves the current raw revision"
            if marker_matches
            else ""
        )
    if not marker_matches:
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


def _job_is_non_retryable_policy_failure(record: dict) -> bool:
    """Return True for deterministic model-capability failures.

    These failure classes mean the model cannot produce schema-conformant output
    for this input at its current capability level.  Requeuing them (and paying
    the 3.5GB maintenance backup each round) cannot succeed, so debt
    reconciliation must leave them terminal instead of re-arming them.
    """
    try:
        result = json.loads(str(record.get("result_json") or "{}"))
    except (TypeError, json.JSONDecodeError):
        return False
    if not isinstance(result, dict):
        return False
    failure_class = str(result.get("failure_class") or "")
    if failure_class in {"input_policy", "output_policy", "generator_policy"}:
        return True
    return False


def _is_legacy_retryable_generator_failure(record: dict) -> bool:
    """Recognize the one runner error previously misclassified as model policy."""
    try:
        result = json.loads(str(record.get("result_json") or "{}"))
    except (TypeError, json.JSONDecodeError):
        return False
    return bool(
        isinstance(result, dict)
        and result.get("maintenance") == "auto_ingest_controller"
        and result.get("state") == "quarantined"
        and result.get("failure_class") == "generator_policy"
        and result.get("reason") == _LEGACY_RETRYABLE_GENERATOR_REASON
    )


def _terminal_retry_row_guard(record: dict, raw_revision: str) -> str:
    guarded = {
        key: record.get(key)
        for key in (
            "job_id",
            "task_type",
            "status",
            "payload",
            "retries",
            "error_msg",
            "result_json",
            "completed_at",
            "updated_at",
            "available_at",
            "lease_until",
            "lease_owner",
            "lease_token",
            "lease_generation",
            "idempotency_key",
            "task_packet_path",
        )
    }
    guarded["raw_revision"] = raw_revision
    return hashlib.sha256(
        json.dumps(
            guarded,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _ingest_debt_exact_row_guard(expected_state: dict) -> str:
    return "sha256:" + hashlib.sha256(
        json.dumps(
            expected_state,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _ingest_debt_exact_fingerprint(plan: dict) -> str:
    fingerprint_material = dict(plan)
    fingerprint_material.pop("fingerprint", None)
    return "sha256:" + hashlib.sha256(
        json.dumps(
            fingerprint_material,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _ingest_debt_exact_supersede_plan(conn, item: dict) -> dict:
    if item.get("action") != "supersede_duplicate":
        raise IngestBaselineConflict(
            "exact ingest debt target no longer resolves to supersede_duplicate"
        )
    job_id = str(item.get("job_id") or "")
    owner_job_id = str(item.get("owner_job_id") or "")
    target_key = str(item.get("target_key") or "")
    raw_value = str(item.get("raw_path") or "")
    current_hash = str(item.get("expected_raw_hash") or "")
    candidate_payload = item.get("payload")
    if (
        not job_id
        or not owner_job_id
        or not target_key
        or not raw_value
        or not current_hash
        or not isinstance(candidate_payload, dict)
    ):
        raise IngestBaselineConflict(
            "exact ingest debt supersede plan is missing owner or raw evidence"
        )
    try:
        raw_path = Path(raw_value).resolve()
    except (OSError, RuntimeError) as exc:
        raise IngestBaselineConflict(
            "exact ingest debt raw source path cannot be resolved"
        ) from exc
    revision_owners, revision_conflicts = _ingest_debt_effective_revision_owners(
        conn,
        candidate_job_id=job_id,
        raw_path=raw_path,
        current_hash=current_hash,
        candidate_payload=candidate_payload,
    )
    owner_is_unique = (
        not revision_conflicts
        and len(revision_owners) == 1
        and revision_owners[0]["job_id"] == owner_job_id
        and revision_owners[0]["target_key"] == target_key
    )
    if not owner_is_unique:
        raise IngestBaselineConflict(
            "exact ingest debt target does not have one confirmed current owner"
        )
    owner_record = conn.execute(
        "SELECT * FROM jobs WHERE job_id = ?",
        (owner_job_id,),
    ).fetchone()
    if owner_record is None:
        raise IngestBaselineConflict(
            "exact ingest debt current owner disappeared before confirmation"
        )
    owner_snapshot = {
        "owners": [
            {
                "job_id": str(revision_owners[0]["job_id"]),
                "target_key": str(revision_owners[0]["target_key"]),
                "owner_row_guard": _ingest_debt_exact_row_guard(dict(owner_record)),
            }
        ],
        "conflicts": [],
    }
    exact_plan = {
        "contract": _INGEST_DEBT_EXACT_CONTRACT,
        "version": 1,
        "job_id": job_id,
        "action": "supersede_duplicate",
        "raw_path": str(raw_path),
        "raw_revision": current_hash,
        "owner_job_id": owner_job_id,
        "target_key": target_key,
        "target_row_guard": _ingest_debt_exact_row_guard(
            item.get("expected_state") or {}
        ),
        "current_owner_snapshot": owner_snapshot,
    }
    exact_plan["fingerprint"] = _ingest_debt_exact_fingerprint(exact_plan)
    return exact_plan


def _ingest_debt_exact_revalidate_postbackup(conn, exact_plan: dict) -> None:
    target_job_id = str(exact_plan.get("job_id") or "")
    target_guard = str(exact_plan.get("target_row_guard") or "")
    owner_snapshot = exact_plan.get("current_owner_snapshot") or {}
    owners = owner_snapshot.get("owners") if isinstance(owner_snapshot, dict) else None
    if (
        not target_job_id
        or not target_guard
        or not isinstance(owners, list)
        or len(owners) != 1
    ):
        raise IngestBaselineConflict("exact ingest debt guard snapshot is invalid")
    owner_snapshot_row = owners[0]
    if not isinstance(owner_snapshot_row, dict):
        raise IngestBaselineConflict("exact ingest debt owner guard snapshot is invalid")
    owner_job_id = str(owner_snapshot_row.get("job_id") or "")
    owner_guard = str(owner_snapshot_row.get("owner_row_guard") or "")
    if not owner_job_id or not owner_guard:
        raise IngestBaselineConflict("exact ingest debt owner guard snapshot is incomplete")
    target_row = conn.execute(
        "SELECT * FROM jobs WHERE job_id = ?",
        (target_job_id,),
    ).fetchone()
    if target_row is None:
        raise IngestBaselineConflict("exact ingest debt target disappeared after backup")
    current_target_guard = _ingest_debt_exact_row_guard(dict(target_row))
    if not hmac.compare_digest(current_target_guard, target_guard):
        raise IngestBaselineConflict("exact ingest debt target row changed after backup")
    owner_row = conn.execute(
        "SELECT * FROM jobs WHERE job_id = ?",
        (owner_job_id,),
    ).fetchone()
    if owner_row is None:
        raise IngestBaselineConflict("exact ingest debt current owner disappeared after backup")
    current_owner_guard = _ingest_debt_exact_row_guard(dict(owner_row))
    if not hmac.compare_digest(current_owner_guard, owner_guard):
        raise IngestBaselineConflict("exact ingest debt current owner row changed after backup")


def _terminal_ingest_retry_plan(job_id: str) -> tuple[dict, str]:
    normalized_job_id = str(job_id or "").strip()
    if re.fullmatch(r"[0-9a-f]{32}", normalized_job_id) is None:
        raise ValueError("terminal ingest retry job_id is invalid")
    conn, preview_error = _open_ingest_debt_preview_connection()
    if conn is None:
        raise RuntimeError(preview_error or "terminal ingest retry state is unavailable")
    try:
        row = conn.execute(
            "SELECT * FROM jobs WHERE job_id = ?",
            (normalized_job_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"Unknown ingest job: {normalized_job_id}")
        record = dict(row)
        if record.get("task_type") != "ingest":
            raise ValueError(f"Job {normalized_job_id} is not an ingest job")
        if record.get("status") != "failed" or int(record.get("retries") or 0) < 3:
            raise ValueError(f"Job {normalized_job_id} is not terminal failed")
        if not _is_legacy_retryable_generator_failure(record):
            raise ValueError(
                f"Job {normalized_job_id} is not an approved legacy runner-error retry"
            )
        lease_until = str(record.get("lease_until") or "")
        if lease_until:
            parsed_lease = datetime.fromisoformat(lease_until.replace("Z", "+00:00"))
            if parsed_lease.tzinfo is None:
                parsed_lease = parsed_lease.replace(tzinfo=timezone.utc)
            if parsed_lease > datetime.now(timezone.utc):
                raise ValueError(f"Job {normalized_job_id} still has an active lease")
        try:
            payload = json.loads(str(record.get("payload") or "{}"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"Job {normalized_job_id} has invalid payload") from exc
        if not isinstance(payload, dict):
            raise ValueError(f"Job {normalized_job_id} payload is not an object")
        filepath = str(payload.get("filepath") or "")
        queued_revision = str(payload.get("hash") or "")
        current_raw = _stable_current_raw_revision(filepath)
        if not current_raw.matches(queued_revision):
            raise IngestBaselineConflict(
                f"Raw source changed after terminal job {normalized_job_id} failed"
            )
        processed = conn.execute(
            "SELECT file_hash FROM processed_files WHERE filepath = ?",
            (filepath,),
        ).fetchone()
        if processed is not None and current_raw.matches(
            str(processed["file_hash"] or "")
        ):
            raise ValueError(f"Job {normalized_job_id} revision is already processed")
        row_guard = _terminal_retry_row_guard(record, current_raw.canonical_revision)
        plan = {
            "contract": _TERMINAL_RETRY_CONTRACT,
            "job_id": normalized_job_id,
            "action": "requeue_current",
            "reason": "legacy runner error event was misclassified as generator policy",
            "raw_revision": current_raw.canonical_revision,
            "row_guard": row_guard,
            "can_apply": True,
        }
        fingerprint = "sha256:" + hashlib.sha256(
            json.dumps(
                plan,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        plan["fingerprint"] = fingerprint
        return plan, row_guard
    finally:
        conn.close()


def retry_terminal_ingest_job(
    job_id: str,
    *,
    dry_run: bool = True,
    confirmation: str = "",
) -> str:
    """Preview or requeue one exact legacy runner-error ingest job."""
    if not isinstance(dry_run, bool):
        raise ValueError("dry_run must be boolean")
    plan, row_guard = _terminal_ingest_retry_plan(job_id)
    if dry_run:
        return json.dumps(
            {"ok": True, "committed": False, "dry_run": True, "plan": plan},
            ensure_ascii=False,
            indent=2,
        )
    fingerprint = str(plan["fingerprint"])
    if not hmac.compare_digest(str(confirmation or ""), fingerprint):
        raise PermissionError("terminal ingest retry confirmation mismatch")
    reconciliation = json.loads(
        reconcile_ingest_job_debt(
            dry_run=False,
            limit=1,
            _operator_retry_job_id=str(job_id),
            _expected_operator_retry_guard=row_guard,
            _expected_operator_retry_revision=str(plan["raw_revision"]),
        )
    )
    if reconciliation.get("applied_counts", {}).get("requeue_current") != 1:
        raise RuntimeError("terminal ingest retry did not requeue the exact job")
    return json.dumps(
        {
            "ok": True,
            "committed": True,
            "dry_run": False,
            "state": "queued",
            "job_id": str(job_id),
            "confirmation_fingerprint": fingerprint,
            "reconciliation": reconciliation,
        },
        ensure_ascii=False,
        indent=2,
    )


def reconcile_ingest_job_debt(
    dry_run: bool = True,
    limit: int = 0,
    *,
    reopen_provenance_only: bool = False,
    job_id: str = "",
    expected_action: str = "",
    confirmation: str = "",
    _operator_retry_job_id: str = "",
    _expected_operator_retry_guard: str = "",
    _expected_operator_retry_revision: str = "",
) -> str:
    """Classify and safely recover abandoned ingest jobs without discarding valid work."""
    from vector_lake import db_store

    exact_job_id = str(job_id or "").strip()
    exact_expected_action = str(expected_action or "").strip()
    exact_confirmation = str(confirmation or "").strip()
    exact_arguments = (exact_job_id, exact_expected_action, exact_confirmation)
    private_operator_arguments = (
        str(_operator_retry_job_id or ""),
        str(_expected_operator_retry_guard or ""),
        str(_expected_operator_retry_revision or ""),
    )
    if any(exact_arguments) and any(private_operator_arguments):
        raise ValueError(
            "exact ingest debt arguments cannot be mixed with private operator retry arguments"
        )
    if any(exact_arguments):
        if not exact_job_id or not exact_expected_action:
            raise ValueError(
                "exact ingest debt job_id and expected_action are required together"
            )
        if re.fullmatch(r"[0-9a-f]{32}", exact_job_id) is None:
            raise ValueError("exact ingest debt job_id is invalid")
        if exact_expected_action not in _INGEST_DEBT_EXACT_ACTIONS:
            raise ValueError("exact ingest debt expected_action is unsupported")
        if not dry_run and not exact_confirmation:
            raise PermissionError("exact ingest debt confirmation is required")

    preview_error = ""
    if dry_run:
        conn, preview_error = _open_ingest_debt_preview_connection()
        if conn is None:
            if exact_job_id:
                raise RuntimeError(
                    preview_error or "exact ingest debt state is unavailable"
                )
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
    if reopen_provenance_only:
        reopen_ids = _provenance_only_terminal_job_ids(conn)
        if not reopen_ids:
            conn.close()
            return json.dumps(
                {
                    "dry_run": bool(dry_run),
                    "selected_jobs": 0,
                    "available_jobs": 0,
                    "counts": {},
                    "backup": "",
                    "preview_error": "",
                    "reopen_provenance_only": True,
                    "reopen_candidates": 0,
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
        placeholders = ", ".join("?" for _ in reopen_ids)
        debt_predicate = (
            "task_type = 'ingest' AND status IN ('finalized', 'failed') "
            f"AND job_id IN ({placeholders})"
        )
        predicate_params = tuple(reopen_ids)
    operator_retry_job_id = str(_operator_retry_job_id or "").strip()
    operator_retry_arguments = (
        operator_retry_job_id,
        str(_expected_operator_retry_guard or ""),
        str(_expected_operator_retry_revision or ""),
    )
    if any(operator_retry_arguments) and not all(operator_retry_arguments):
        raise ValueError(
            "operator retry job, row guard, and revision must be supplied together"
        )
    filtered_predicate = debt_predicate
    predicate_params: tuple[object, ...] = ()
    if operator_retry_job_id:
        if re.fullmatch(r"[0-9a-f]{32}", operator_retry_job_id) is None:
            raise ValueError("operator retry job_id is invalid")
        filtered_predicate += " AND job_id = ?"
        predicate_params = (operator_retry_job_id,)
    elif exact_job_id:
        filtered_predicate += " AND job_id = ?"
        predicate_params = (exact_job_id,)
    if reopen_provenance_only:
        # The reopen predicate carries one placeholder per candidate job id.
        predicate_params = tuple(reopen_ids) + tuple(predicate_params)
    available_jobs = int(
        conn.execute(
            f"SELECT COUNT(*) FROM jobs WHERE {filtered_predicate}",
            predicate_params,
        ).fetchone()[0]
    )
    selected_limit = max(0, int(limit))
    if exact_job_id:
        selected_limit = 1
    elif not dry_run:
        selected_limit = min(
            _INGEST_DEBT_APPLY_MAX_LIMIT,
            selected_limit or _INGEST_DEBT_APPLY_DEFAULT_LIMIT,
        )
    select_sql = (
        f"SELECT * FROM jobs WHERE {filtered_predicate} ORDER BY created_at, job_id"
    )
    select_params = predicate_params
    if selected_limit:
        select_sql += " LIMIT ?"
        select_params = (*select_params, selected_limit)
    rows = conn.execute(select_sql, select_params).fetchall()

    plans: list[dict] = []
    requeue_groups: dict[str, list[dict]] = {}
    reconcile_target_dirs = get_ingest_target_directories() if rows else []
    reconcile_source_identity_index = None
    instruction_context = None if dry_run else _LazyIngestInstructionContext()

    for row in rows:
        record = dict(row)
        if operator_retry_job_id:
            if not _is_legacy_retryable_generator_failure(record):
                raise IngestBaselineConflict(
                    "terminal ingest retry failure classification changed after confirmation"
                )
            confirmed_guard = _terminal_retry_row_guard(
                record,
                str(_expected_operator_retry_revision),
            )
            if not hmac.compare_digest(
                confirmed_guard,
                str(_expected_operator_retry_guard),
            ):
                raise IngestBaselineConflict(
                    "terminal ingest retry state changed after confirmation"
                )
        expected_state = {
            "job_id": record.get("job_id"),
            "task_type": record.get("task_type"),
            "created_at": record.get("created_at"),
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
            if operator_retry_job_id:
                raise IngestBaselineConflict(
                    "terminal ingest retry payload changed after confirmation"
                )
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
            if operator_retry_job_id:
                raise IngestBaselineConflict(
                    "terminal ingest retry filepath changed after confirmation"
                )
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
            if operator_retry_job_id:
                raise IngestBaselineConflict(
                    "terminal ingest retry raw source changed after confirmation"
                )
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
            if operator_retry_job_id:
                raise IngestBaselineConflict(
                    "terminal ingest retry raw source changed after confirmation"
                )
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
        if operator_retry_job_id and not hmac.compare_digest(
            current_hash,
            str(_expected_operator_retry_revision),
        ):
            raise IngestBaselineConflict(
                "terminal ingest retry raw revision changed after confirmation"
            )

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
        if processed_matches_current and not reopen_provenance_only:
            if operator_retry_job_id:
                raise IngestBaselineConflict(
                    "terminal ingest retry revision became processed after confirmation"
                )
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
        # Model-capability failures are deterministic and retrying them burns
        # model tokens plus a full 3.5GB backup per round with no benefit.
        # Keep them terminal so a reconcile pass does not re-arm an endless
        # quarantine -> requeue storm. The only override is an exact,
        # fingerprint-bound retry of the historical top-level runner error.
        operator_retry = bool(
            operator_retry_job_id == str(record.get("job_id") or "")
        )
        non_retryable_policy = (
            _job_is_non_retryable_policy_failure(record) and not operator_retry
        )
        needs_requeue = (
            reopen_provenance_only
            or (record.get("status") == "failed" and not non_retryable_policy)
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
        local_publication = payload.get("local_publication")
        has_local_source_identity = bool(
            isinstance(local_publication, dict)
            and local_publication.get("contract") == "deterministic-source/v1"
            and canonical_name
        )
        if (
            record.get("status") == "failed" and not has_local_source_identity
        ) or not canonical_name:
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
            "source_observed_at": datetime.fromtimestamp(
                current_snapshot.observed_mtime_ns / 1_000_000_000,
                tz=timezone.utc,
            ).isoformat(),
            "attempt_id": hashlib.sha256(
                (
                    f"{record['job_id']}\0{current_hash}\0"
                    f"{record.get('updated_at') or ''}\0reconcile-v6"
                ).encode("utf-8")
            ).hexdigest()[:32],
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
                "provenance-only Source page reopened for enrichment"
                if reopen_provenance_only
                else "operator-authorized legacy runner-error retry"
                if operator_retry
                else "terminal failure"
                if record.get("status") == "failed"
                else "raw hash or ingest contract changed"
            ),
            "packet_path": packet_path,
            "payload": refreshed,
            "raw_path": str(raw_path),
            "expected_raw_hash": current_hash,
            "processed_lookup_paths": processed_lookup_paths,
            "target_key": target_key,
            "created_at": str(record.get("created_at") or ""),
            "reopen_provenance_only": reopen_provenance_only,
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
    exact_plan = None
    if exact_job_id:
        if len(rows) != 1 or available_jobs != 1:
            raise ValueError(f"Unknown or ineligible ingest job: {exact_job_id}")
        if (
            len(plans) != 1
            or str(plans[0].get("job_id") or "") != exact_job_id
            or str(plans[0].get("action") or "") != exact_expected_action
        ):
            raise IngestBaselineConflict(
                "exact ingest debt target no longer resolves to the expected action"
            )
        exact_plan = _ingest_debt_exact_supersede_plan(conn, plans[0])
        if not dry_run and not hmac.compare_digest(
            exact_confirmation,
            str(exact_plan.get("fingerprint") or ""),
        ):
            raise PermissionError("exact ingest debt confirmation mismatch")
    if operator_retry_job_id and (
        len(plans) != 1
        or plans[0].get("job_id") != operator_retry_job_id
        or plans[0].get("action") != "requeue_current"
    ):
        raise IngestBaselineConflict(
            "terminal ingest retry no longer resolves to the confirmed requeue action"
        )
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
    if exact_plan is not None:
        result["exact"] = {
            "contract": _INGEST_DEBT_EXACT_CONTRACT,
            "fingerprint": exact_plan["fingerprint"],
            "plan": exact_plan,
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
                if operator_retry_job_id:
                    raise IngestBaselineConflict(
                        "terminal ingest retry precondition changed before mutation: "
                        + raw_precondition_error
                    )
                result["concurrent_skips"].append(
                    {
                        "job_id": job_id,
                        "reason": raw_precondition_error,
                    }
                )
                continue
            if exact_job_id and action == "supersede_duplicate":
                _ingest_debt_exact_revalidate_postbackup(conn, exact_plan or {})
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
            if action == "requeue_current":
                conn.execute(
                    "DELETE FROM ingest_outbox_links WHERE job_id = ?",
                    (job_id,),
                )
                refreshed_payload = item["payload"]
                refreshed_revision = str(refreshed_payload.get("hash") or "")
                refreshed_attempt_id = str(refreshed_payload.get("attempt_id") or "")
                latest_source_outbox = conn.execute(
                    "SELECT id, status FROM mutation_outbox WHERE filename = ? "
                    "ORDER BY id DESC LIMIT 1",
                    (str(refreshed_payload.get("canonical_name") or ""),),
                ).fetchone()
                if latest_source_outbox is not None:
                    source_outbox_id = int(latest_source_outbox["id"])
                    source_outbox_status = str(latest_source_outbox["status"])
                    if source_outbox_status != "completed":
                        db_store.link_ingest_outbox_events(
                            outbox_ids=[source_outbox_id],
                            job_id=job_id,
                            revision=refreshed_revision,
                            attempt_id=refreshed_attempt_id,
                            connection=conn,
                        )
                        db_store.record_ingest_stage_event(
                            job_id=job_id,
                            revision=refreshed_revision,
                            attempt_id=refreshed_attempt_id,
                            stage="outbox",
                            transition="completed",
                            metadata={
                                "outbox_ids": [source_outbox_id],
                                "state": "reconciled_projection_barrier",
                                "status": source_outbox_status,
                            },
                            connection=conn,
                        )
                    else:
                        db_store.record_ingest_stage_event(
                            job_id=job_id,
                            revision=refreshed_revision,
                            attempt_id=refreshed_attempt_id,
                            stage="index_visible",
                            transition="completed",
                            ordinal=max(1, source_outbox_id),
                            metadata={
                                "outbox_id": source_outbox_id,
                                "state": "visible_before_requeue",
                            },
                            connection=conn,
                        )
            applied_counts[action] += 1
            if packet_path:
                db_store.enqueue_ingest_task_cleanup(job_id, packet_path)

    result["applied_counts"] = dict(sorted(applied_counts.items()))
    if exact_job_id:
        exact_mutations = int(applied_counts.get(exact_expected_action, 0))
        if (
            result["concurrent_skips"]
            or exact_mutations != 1
            or sum(applied_counts.values()) != 1
        ):
            raise IngestBaselineConflict(
                "exact ingest debt apply conflicted before mutating the requested job"
            )
        result["exact"]["committed"] = True
        result["exact"]["mutation_count"] = exact_mutations
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












def calculate_hash(filepath: str) -> str:
    try:
        return stable_raw_revision(
            filepath,
            allowed_roots=get_ingest_target_directories(),
            include_legacy_md5=False,
        ).canonical_revision
    except (OSError, RawSourceContainmentError, RawSourceUnstableError) as exc:
        log.error("Error calculating stable hash for %s: %s", filepath, exc)
        return ""






























































































# Ingest root/config resolution moved to the base layer so that storage callers
# (db_store) no longer import a handler. ``_load_ingest_config`` keeps its local
# name for this module's call sites and for its historical importers.
from vector_lake.ingest_paths import (  # noqa: E402
    get_ingest_target_directories,
)



















def _canonical_json_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _validate_exact_review_metadata(review: object) -> dict[str, str]:
    allowed_fields = {"review_id", "approved_at", "approver", "scope", "reason"}
    if not isinstance(review, dict) or set(review) != allowed_fields:
        raise ValueError("exact reviewed ingest review fields are not exact")
    normalized = {key: str(review.get(key) or "").strip() for key in allowed_fields}
    if not 1 <= len(normalized["review_id"]) <= 128:
        raise ValueError("exact reviewed ingest review_id is invalid")
    if not 1 <= len(normalized["approved_at"]) <= 128:
        raise ValueError("exact reviewed ingest approved_at is invalid")
    if not 1 <= len(normalized["approver"]) <= 128:
        raise ValueError("exact reviewed ingest approver is invalid")
    if normalized["scope"] != "exact-ingest-finalization":
        raise ValueError("exact reviewed ingest review scope is unsupported")
    if not 12 <= len(normalized["reason"]) <= 500:
        raise ValueError("exact reviewed ingest review reason must be 12 to 500 characters")
    return normalized


def _exact_reviewed_selection_digest(
    *,
    job_id: str,
    filepath: str,
    raw_hash: str,
    canonical_name: str,
    source_hash: str,
    source_projection_hash: str,
    ingest_contract_version: int,
    integration_candidates_sha256: str,
    output_sha256: str,
    review_sha256: str,
    disposition: str,
) -> str:
    selection_identity = {
        "contract": _EXACT_REVIEWED_INGEST_CONTRACT,
        "job_id": job_id,
        "filepath": filepath,
        "hash": raw_hash,
        "canonical_name": canonical_name,
        "source_hash": source_hash,
        "source_projection_hash": source_projection_hash,
        "ingest_contract_version": ingest_contract_version,
        "integration_candidates_sha256": integration_candidates_sha256,
        "output_sha256": output_sha256,
        "review_sha256": review_sha256,
        "disposition": disposition,
    }
    return _canonical_json_sha256(selection_identity)




def _exact_reviewed_ingest_plan(selections: list[dict]) -> tuple[dict, list[dict]]:
    """Build a one-job, exact, human-reviewed ingest plan without leasing."""
    from vector_lake import db_store
    from vector_lake.auto_ingest_worker import (
        _validate_generator_output,
        load_auto_ingest_config,
    )

    if not isinstance(selections, list) or len(selections) != 1:
        raise ValueError("exact reviewed ingest finalization requires exactly one selection")
    config = load_auto_ingest_config()
    if config.enabled:
        raise RuntimeError(
            "automatic ingest is enabled; task claims are controller-exclusive"
        )
    contract = load_purpose_contract()
    selection = selections[0]
    allowed_fields = {
        "job_id",
        "filepath",
        "hash",
        "canonical_name",
        "source_hash",
        "source_projection_hash",
        "ingest_contract_version",
        "integration_candidates_sha256",
        "review",
        "output",
    }
    if not isinstance(selection, dict) or set(selection) != allowed_fields:
        raise ValueError("exact reviewed ingest selection fields are not exact")
    job_id = str(selection.get("job_id") or "")
    if re.fullmatch(r"[0-9a-f]{32}", job_id) is None:
        raise ValueError("exact reviewed ingest job_id is invalid")
    filepath = str(selection.get("filepath") or "")
    raw_hash = str(selection.get("hash") or "")
    parse_revision(raw_hash)
    canonical_name = str(selection.get("canonical_name") or "")
    validate_wiki_filename(canonical_name)
    source_hash = str(selection.get("source_hash") or "")
    source_projection_hash = str(selection.get("source_projection_hash") or "")
    raw_contract_version = selection.get("ingest_contract_version")
    if isinstance(raw_contract_version, bool) or not isinstance(raw_contract_version, int):
        raise ValueError("exact reviewed ingest_contract_version must be an integer")
    ingest_contract_version = raw_contract_version
    integration_candidates_sha256 = str(
        selection.get("integration_candidates_sha256") or ""
    )
    if re.fullmatch(r"[0-9a-f]{64}", integration_candidates_sha256) is None:
        raise ValueError("integration_candidates_sha256 must be a lowercase SHA-256 digest")
    review = _validate_exact_review_metadata(selection.get("review"))
    output = selection.get("output")
    if not isinstance(output, dict):
        raise ValueError("exact reviewed ingest output must be an object")
    review_sha256 = _canonical_json_sha256(review)
    output_sha256 = _canonical_json_sha256(output)
    snapshot = db_store.inspect_exact_reviewed_ingest_candidate(job_id)
    payload = dict(snapshot["payload"])
    identity_checks = {
        "filepath": filepath,
        "hash": raw_hash,
        "canonical_name": canonical_name,
        "source_hash": source_hash,
        "source_projection_hash": source_projection_hash,
        "ingest_contract_version": str(ingest_contract_version),
    }
    for key, expected in identity_checks.items():
        queued = payload.get(key)
        if key == "ingest_contract_version":
            queued_value = str(queued or "")
        else:
            queued_value = str(queued or "")
        if str(expected) != queued_value:
            raise ValueError(f"exact reviewed ingest {key} does not match queued payload")
    queued_candidates = payload.get("integration_candidates")
    if not isinstance(queued_candidates, list):
        raise ValueError("exact reviewed ingest queued integration_candidates missing")
    if not hmac.compare_digest(
        integration_candidates_sha256,
        _canonical_json_sha256(queued_candidates),
    ):
        raise ValueError("exact reviewed ingest integration candidates digest mismatch")
    if is_private_diary_path(filepath):
        raise ValueError("private sources cannot use exact reviewed ingest finalization")
    try:
        current_raw = _stable_current_raw_revision(filepath)
    except (OSError, RawRevisionFormatError, RawSourceContainmentError, RawSourceUnstableError, ValueError):
        raise IngestBaselineConflict(
            "Raw source is not available for exact reviewed ingest finalization"
        ) from None
    if not current_raw.matches(raw_hash):
        raise IngestBaselineConflict(
            f"Raw source changed after exact reviewed ingest job {job_id} was produced"
        )
    job_record = snapshot["job"]
    reviewed_updated_at = str(
        job_record.get("updated_at") or job_record.get("created_at") or ""
    )
    if not reviewed_updated_at:
        raise ValueError(f"Job {job_id} has no stable reviewed ingest timestamp")
    processed_data = dict(payload)
    processed_data.update(
        {
            "job_id": job_id,
            "_reviewed_updated_at": reviewed_updated_at,
        }
    )
    processed_data["_queued_integration_candidates"] = list(queued_candidates)
    already_reviewed = bool(snapshot.get("already_reviewed"))
    stored_reviewed = snapshot.get("stored_reviewed_ingest")
    raw_integration = output.get("integration")
    reviewed_rejected_requested = bool(
        isinstance(raw_integration, dict)
        and str(raw_integration.get("disposition") or "").lower() == "rejected"
    )
    files, integration = _validate_generator_output(
        output,
        job_id,
        processed_data,
        config,
        human_reviewed_rejected=reviewed_rejected_requested,
    )
    disposition = str(integration.get("disposition") or "")
    if str(job_record.get("status") or "") == "failed" and disposition != "rejected":
        raise ValueError(
            "terminal failed exact reviewed ingest only supports reviewed rejected output"
        )
    selection_digest = _exact_reviewed_selection_digest(
        job_id=job_id,
        filepath=filepath,
        raw_hash=raw_hash,
        canonical_name=canonical_name,
        source_hash=source_hash,
        source_projection_hash=source_projection_hash,
        ingest_contract_version=ingest_contract_version,
        integration_candidates_sha256=integration_candidates_sha256,
        output_sha256=output_sha256,
        review_sha256=review_sha256,
        disposition=disposition,
    )
    if already_reviewed:
        if not isinstance(stored_reviewed, dict):
            raise ValueError(f"Job {job_id} has no stored reviewed ingest provenance")
        stored_selection_digest = str(stored_reviewed.get("selection_digest") or "")
        if not hmac.compare_digest(stored_selection_digest, selection_digest):
            raise ValueError(
                f"Job {job_id} reviewed ingest selection does not match stored provenance"
            )
    else:
        _verify_ingest_source_baseline(processed_data)
    processed_data["integration"] = integration
    if not already_reviewed:
        (
            planned_output_files,
            _planned_disposition,
            planned_target_names,
        ) = _prepare_final_ingest_files(files, processed_data, contract)
        _prepare_source_link_precondition(planned_output_files)()
    else:
        planned_output_files = _normalize_codex_output_pages(
            files,
            contract,
            default_updated_at=reviewed_updated_at,
        )
        planned_target_names = set()
    _validate_final_ingest_files(
        planned_output_files,
        planned_target_names,
        contract,
    )
    planned_files = [
        {
            "filename": str(item["filename"]),
            "content_sha256": hashlib.sha256(
                str(item["content"]).encode("utf-8")
            ).hexdigest(),
        }
        for item in planned_output_files
    ]
    item = {
        "job_id": job_id,
        "output_sha256": output_sha256,
        "review_sha256": review_sha256,
        "selection_digest": selection_digest,
        "stored_reviewed_fingerprint": (
            str(stored_reviewed.get("fingerprint") or "")
            if isinstance(stored_reviewed, dict)
            else ""
        ),
        "row_guard": snapshot["row_guard"],
        "effective_updated_at": reviewed_updated_at,
        "state": "already_finalized" if already_reviewed else "finalizable",
        "original_status": str(job_record.get("status") or ""),
        "disposition": disposition,
        "raw_revision": current_raw.canonical_revision,
        "source_hash": source_hash,
        "source_projection_hash": source_projection_hash,
        "integration_candidates_sha256": integration_candidates_sha256,
        "planned_files": planned_files,
    }
    plan = {
        "contract": _EXACT_REVIEWED_INGEST_CONTRACT,
        "requested": 1,
        "items": [item],
        "can_apply": True,
    }
    fingerprint = "sha256:" + _canonical_json_sha256(plan)
    plan["fingerprint"] = fingerprint
    material = {
        "job_id": job_id,
        "row_guard": snapshot["row_guard"],
        "original_status": str(job_record.get("status") or ""),
        "already_reviewed": already_reviewed,
        "files": files,
        "processed_data": processed_data,
        "selection_digest": selection_digest,
        "stored_reviewed_fingerprint": item["stored_reviewed_fingerprint"],
        "review": review,
        "output_sha256": output_sha256,
        "review_sha256": review_sha256,
    }
    return plan, [material]


def _verify_exact_reviewed_claim(material: dict, claim: dict) -> None:
    from vector_lake import db_store

    original = claim.get("exact_reviewed_snapshot")
    if not isinstance(original, dict):
        raise RuntimeError("exact reviewed ingest claim lacks original snapshot")
    row = db_store.get_connection().execute(
        "SELECT * FROM jobs WHERE job_id = ?", (str(material.get("job_id") or ""),)
    ).fetchone()
    if row is None:
        raise RuntimeError("exact reviewed ingest claimed row disappeared")
    current = dict(row)
    for key in (
        "job_id",
        "task_type",
        "payload",
        "retries",
        "error_msg",
        "result_json",
        "completed_at",
        "created_at",
        "available_at",
        "idempotency_key",
        "task_packet_path",
    ):
        if str(claim.get(key) or "") != str(current.get(key) or ""):
            raise RuntimeError("exact reviewed ingest claimed row changed")
    if str(original.get("status") or "") != str(material.get("original_status") or ""):
        raise RuntimeError("exact reviewed ingest claimed original status changed")
    if str(current.get("status") or "") != "subagent_processing":
        raise RuntimeError("exact reviewed ingest claim did not enter processing state")
    if str(current.get("lease_owner") or "") != str(claim.get("lease_owner") or ""):
        raise RuntimeError("exact reviewed ingest claim owner changed")
    if str(current.get("lease_token") or "") != str(claim.get("lease_token") or ""):
        raise RuntimeError("exact reviewed ingest claim token changed")
    if not str(claim.get("lease_owner") or "").startswith("exact-reviewed-ingest:"):
        raise RuntimeError("exact reviewed ingest claim owner is invalid")
    if not str(claim.get("lease_token") or ""):
        raise RuntimeError("exact reviewed ingest claim token is missing")
    if int(claim.get("lease_generation") or 0) <= int(original.get("lease_generation") or 0):
        raise RuntimeError("exact reviewed ingest claim generation did not advance")
    if str(claim.get("payload") or "") != str(original.get("payload") or ""):
        raise RuntimeError("exact reviewed ingest payload changed during claim")


def _projection_pair_settled_now() -> bool:
    try:
        from vector_lake import indexer

        return bool(indexer.projection_pair_matches_current_generation())
    except Exception:
        return False


def finalize_exact_reviewed_ingest_outputs(
    selections: list[dict],
    *,
    dry_run: bool = True,
    confirmation: str = "",
) -> str:
    """Preview or apply one exact HUMAN-reviewed ingest finalization."""
    from vector_lake import db_store

    if not isinstance(dry_run, bool):
        raise ValueError("dry_run must be boolean")
    plan, materials = _exact_reviewed_ingest_plan(selections)
    fingerprint = str(plan["fingerprint"])
    if dry_run:
        return json.dumps(
            {"ok": True, "committed": False, "dry_run": True, "plan": plan},
            ensure_ascii=False,
            indent=2,
        )
    if not hmac.compare_digest(str(confirmation or ""), fingerprint):
        raise PermissionError("exact reviewed ingest confirmation mismatch")
    pending_materials = [item for item in materials if not item.get("already_reviewed")]
    claims = (
        db_store.claim_exact_reviewed_ingest_jobs(
            pending_materials,
            lease_seconds=1800,
        )
        if pending_materials
        else []
    )
    claim_by_job = {str(item["job_id"]): item for item in claims}
    committed: list[str] = [
        str(item["job_id"]) for item in materials if item.get("already_reviewed")
    ]
    already_finalized = list(committed)
    restored: list[str] = []
    indeterminate: list[str] = []
    errors: list[dict] = []
    for position, material in enumerate(pending_materials):
        job_id = str(material["job_id"])
        claim = claim_by_job[job_id]
        try:
            _verify_exact_reviewed_claim(material, claim)
            processed_data = dict(material["processed_data"])
            processed_data.update(
                {
                    "lease_owner": claim["lease_owner"],
                    "lease_token": claim["lease_token"],
                    "lease_generation": claim["lease_generation"],
                }
            )
            reviewed_ingest_context = _ReviewedIngestFinalizerContext(
                job_id=job_id,
                lease_owner=str(claim["lease_owner"]),
                lease_generation=int(claim["lease_generation"]),
                raw_revision=str(material["processed_data"].get("hash") or ""),
                selection_digest=str(material["selection_digest"]),
                review_sha256=str(material["review_sha256"]),
                output_sha256=str(material["output_sha256"]),
                provenance={
                    "contract": str(plan["contract"]),
                    "fingerprint": fingerprint,
                    "selection_digest": str(material["selection_digest"]),
                    "review_sha256": str(material["review_sha256"]),
                    "output_sha256": str(material["output_sha256"]),
                },
            )
            _finalize_exact_reviewed_ingest_strict(
                material["files"],
                processed_data,
                reviewed_ingest_context=reviewed_ingest_context,
            )
            row = db_store.get_connection().execute(
                "SELECT status FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            if row is None or str(row["status"] or "") != "finalized":
                raise RuntimeError("reviewed finalizer returned without durable finalization")
            committed.append(job_id)
        except Exception as exc:
            errors.append(
                {
                    "job_id": job_id,
                    "error_type": type(exc).__name__,
                    "error": "exact reviewed ingest finalization failed",
                }
            )
            for pending in pending_materials[position:]:
                pending_id = str(pending["job_id"])
                pending_claim = claim_by_job[pending_id]
                try:
                    if db_store.restore_exact_reviewed_ingest_claim(
                        pending_claim,
                        reason=f"exact reviewed ingest stopped after {job_id}",
                    ):
                        restored.append(pending_id)
                    else:
                        row = db_store.get_connection().execute(
                            "SELECT status FROM jobs WHERE job_id = ?", (pending_id,)
                        ).fetchone()
                        if row is not None and str(row["status"] or "") == "finalized":
                            committed.append(pending_id)
                        else:
                            indeterminate.append(pending_id)
                except Exception:
                    indeterminate.append(pending_id)
            break
    requested_count = len(materials)
    unattempted = [
        str(item["job_id"])
        for item in materials
        if str(item["job_id"]) not in set(committed) | set(restored) | set(indeterminate)
    ]
    fully_committed = len(set(committed)) == requested_count
    projection_settled = _projection_pair_settled_now() if fully_committed else False
    state = (
        "completed"
        if fully_committed and projection_settled
        else "committed_projection_pending"
        if fully_committed
        else "partial"
    )
    reviewed_fingerprints = {
        str(item["job_id"]): (
            str(item["stored_reviewed_fingerprint"])
            if item.get("already_reviewed")
            else fingerprint
        )
        for item in materials
    }
    receipt = {
        "ok": state == "completed",
        "committed": fully_committed,
        "projection_settled": projection_settled,
        "dry_run": False,
        "state": state,
        "fingerprint": fingerprint,
        "confirmation_fingerprint": fingerprint,
        "reviewed_ingest_fingerprints": reviewed_fingerprints,
        "requested": requested_count,
        "committed_job_ids": sorted(set(committed)),
        "already_finalized_job_ids": sorted(set(already_finalized)),
        "restored_job_ids": sorted(set(restored)),
        "unattempted_job_ids": unattempted,
        "indeterminate_job_ids": sorted(set(indeterminate)),
        "selection_digests": {
            str(item["job_id"]): str(item["selection_digest"])
            for item in materials
        },
        "errors": errors,
    }
    return json.dumps(receipt, ensure_ascii=False, indent=2)


def _terminal_recovery_selection_digest(
    *,
    job_id: str,
    attempt_id: str,
    artifact_generation: int,
    events_sha256: str,
    operator_adjustment: str,
    output_sha256: str,
) -> str:
    selection_identity = {
        "contract": "vector-lake-terminal-ingest-output-recovery/v1",
        "job_id": job_id,
        "attempt_id": attempt_id,
        "artifact_generation": artifact_generation,
        "events_sha256": events_sha256,
        "operator_adjustment": operator_adjustment,
        "output_sha256": output_sha256,
    }
    return hashlib.sha256(
        json.dumps(
            selection_identity,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _terminal_ingest_recovery_plan(selections: list[dict]) -> tuple[dict, list[dict]]:
    """Build a content-bound recovery plan without leasing or mutating jobs."""
    from vector_lake import db_store
    from vector_lake.auto_ingest_worker import (
        _validate_generator_output,
        load_auto_ingest_config,
    )

    if not isinstance(selections, list) or not 1 <= len(selections) <= 3:
        raise ValueError("terminal ingest recovery requires between one and three selections")
    requested_count = len(selections)
    allowed_fields = {
        "job_id",
        "attempt_id",
        "artifact_generation",
        "events_sha256",
        "operator_adjustment",
        "output",
    }
    job_ids: list[str] = []
    plan_items: list[dict] = []
    materials: list[dict] = []
    config = load_auto_ingest_config()
    contract = load_purpose_contract()
    for selection in selections:
        if not isinstance(selection, dict) or set(selection) != allowed_fields:
            raise ValueError("terminal ingest recovery selection fields are not exact")
        job_id = str(selection.get("job_id") or "")
        attempt_id = str(selection.get("attempt_id") or "")
        if re.fullmatch(r"[0-9a-f]{32}", job_id) is None:
            raise ValueError("terminal ingest recovery job_id is invalid")
        if re.fullmatch(r"[0-9a-f]{32}", attempt_id) is None:
            raise ValueError("terminal ingest recovery attempt_id is invalid")
        if job_id in job_ids:
            raise ValueError("terminal ingest recovery job_ids must be unique")
        job_ids.append(job_id)
        raw_artifact_generation = selection.get("artifact_generation")
        if isinstance(raw_artifact_generation, bool) or not isinstance(
            raw_artifact_generation, int
        ):
            raise ValueError("artifact_generation must be a positive integer")
        artifact_generation = raw_artifact_generation
        if artifact_generation < 1:
            raise ValueError("artifact_generation must be a positive integer")
        events_sha256 = str(selection.get("events_sha256") or "")
        if re.fullmatch(r"[0-9a-f]{64}", events_sha256) is None:
            raise ValueError("events_sha256 must be a lowercase SHA-256 digest")
        operator_adjustment = str(selection.get("operator_adjustment") or "").strip()
        if not 12 <= len(operator_adjustment) <= 500:
            raise ValueError("operator_adjustment must be 12 to 500 characters")
        output = selection.get("output")
        if not isinstance(output, dict):
            raise ValueError("terminal ingest recovery output must be an object")
        output_sha256 = hashlib.sha256(
            json.dumps(
                output,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        selection_digest = _terminal_recovery_selection_digest(
            job_id=job_id,
            attempt_id=attempt_id,
            artifact_generation=artifact_generation,
            events_sha256=events_sha256,
            operator_adjustment=operator_adjustment,
            output_sha256=output_sha256,
        )
        snapshot = db_store.inspect_terminal_ingest_recovery(
            job_id,
            attempt_id,
            artifact_generation,
        )
        payload = dict(snapshot["payload"])
        filepath = str(payload.get("filepath") or "")
        if is_private_diary_path(filepath):
            raise ValueError("private sources cannot use terminal ingest recovery")
        current_raw = _stable_current_raw_revision(filepath)
        queued_revision = str(payload.get("hash") or "")
        if not current_raw.matches(queued_revision):
            raise IngestBaselineConflict(
                f"Raw source changed after terminal job {job_id} was produced"
            )
        processed_data = dict(payload)
        recovery_updated_at = str(snapshot["job"].get("updated_at") or "")
        if not recovery_updated_at:
            raise ValueError(f"Job {job_id} has no stable recovery timestamp")
        processed_data.update(
            {
                "job_id": job_id,
                "attempt_id": attempt_id,
                "_recovery_updated_at": recovery_updated_at,
            }
        )
        processed_data["_queued_integration_candidates"] = list(
            payload.get("integration_candidates") or []
        )
        already_recovered = bool(snapshot.get("already_recovered"))
        stored_recovery = snapshot.get("stored_recovery")
        if already_recovered:
            if not isinstance(stored_recovery, dict):
                raise ValueError(f"Job {job_id} has no stored recovery provenance")
            stored_selection_digest = str(
                stored_recovery.get("selection_digest") or ""
            )
            if not hmac.compare_digest(stored_selection_digest, selection_digest):
                raise ValueError(
                    f"Job {job_id} recovery selection does not match stored provenance"
                )
        else:
            _verify_ingest_source_baseline(processed_data)
        files, integration = _validate_generator_output(
            output,
            job_id,
            processed_data,
            config,
        )
        processed_data["integration"] = integration
        if not already_recovered:
            (
                planned_output_files,
                _planned_disposition,
                planned_target_names,
            ) = _prepare_final_ingest_files(files, processed_data, contract)
            _prepare_source_link_precondition(planned_output_files)()
        else:
            planned_output_files = _normalize_codex_output_pages(
                files,
                contract,
                default_updated_at=recovery_updated_at,
            )
            planned_target_names = set()
        _validate_final_ingest_files(
            planned_output_files,
            planned_target_names,
            contract,
        )
        planned_files = [
            {
                "filename": str(item["filename"]),
                "content_sha256": hashlib.sha256(
                    str(item["content"]).encode("utf-8")
                ).hexdigest(),
            }
            for item in planned_output_files
        ]
        plan_items.append(
            {
                "job_id": job_id,
                "attempt_id": attempt_id,
                "artifact_generation": artifact_generation,
                "events_sha256": events_sha256,
                "operator_adjustment": operator_adjustment,
                "output_sha256": output_sha256,
                "selection_digest": selection_digest,
                "stored_recovery_fingerprint": (
                    str(stored_recovery.get("fingerprint") or "")
                    if isinstance(stored_recovery, dict)
                    else ""
                ),
                "row_guard": snapshot["row_guard"],
                "effective_updated_at": recovery_updated_at,
                "state": (
                    "already_recovered"
                    if already_recovered
                    else "recoverable"
                ),
                "raw_revision": current_raw.canonical_revision,
                "source_hash": str(payload.get("source_hash") or ""),
                "source_projection_hash": str(
                    payload.get("source_projection_hash") or ""
                ),
                "planned_files": planned_files,
            }
        )
        materials.append(
            {
                "job_id": job_id,
                "attempt_id": attempt_id,
                "artifact_generation": artifact_generation,
                "operator_adjustment": operator_adjustment,
                "selection_digest": selection_digest,
                "stored_recovery_fingerprint": (
                    str(stored_recovery.get("fingerprint") or "")
                    if isinstance(stored_recovery, dict)
                    else ""
                ),
                "row_guard": snapshot["row_guard"],
                "already_recovered": already_recovered,
                "files": files,
                "processed_data": processed_data,
            }
        )
    plan = {
        "contract": "vector-lake-terminal-ingest-output-recovery/v1",
        "requested": requested_count,
        "items": plan_items,
        "can_apply": True,
    }
    fingerprint = "sha256:" + hashlib.sha256(
        json.dumps(
            plan,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    plan["fingerprint"] = fingerprint
    return plan, materials


def _wait_for_terminal_recovery_projection(*, timeout_seconds: float = 90.0) -> None:
    """Wait boundedly for the watchdog to publish the preceding recovery commit."""
    from vector_lake import indexer

    deadline = time.monotonic() + max(1.0, float(timeout_seconds))
    while time.monotonic() < deadline:
        if indexer.projection_pair_matches_current_generation():
            return
        time.sleep(0.25)
    raise RuntimeError(
        "Timed out waiting for the preceding terminal recovery projection commit"
    )


def recover_terminal_ingest_outputs(
    selections: list[dict],
    *,
    dry_run: bool = True,
    confirmation: str = "",
) -> str:
    """Preview or apply one exact, bounded retained-output recovery batch."""
    from vector_lake import db_store

    if not isinstance(dry_run, bool):
        raise ValueError("dry_run must be boolean")
    plan, materials = _terminal_ingest_recovery_plan(selections)
    fingerprint = str(plan["fingerprint"])
    if dry_run:
        return json.dumps(
            {"ok": True, "committed": False, "dry_run": True, "plan": plan},
            ensure_ascii=False,
            indent=2,
        )
    if not hmac.compare_digest(str(confirmation or ""), fingerprint):
        raise PermissionError("terminal ingest recovery confirmation mismatch")
    pending_materials = [
        item for item in materials if not item.get("already_recovered")
    ]
    claims = (
        db_store.claim_terminal_ingest_recoveries(
            pending_materials,
            lease_seconds=1800,
        )
        if pending_materials
        else []
    )
    claim_by_job = {str(item["job_id"]): item for item in claims}
    committed: list[str] = [
        str(item["job_id"]) for item in materials if item.get("already_recovered")
    ]
    restored: list[str] = []
    indeterminate: list[str] = []
    errors: list[dict] = []
    for position, material in enumerate(pending_materials):
        job_id = str(material["job_id"])
        claim = claim_by_job[job_id]
        processed_data = dict(material["processed_data"])
        processed_data.update(
            {
                "lease_owner": claim["lease_owner"],
                "lease_token": claim["lease_token"],
                "lease_generation": claim["lease_generation"],
                "_recovery_provenance": {
                    "contract": plan["contract"],
                    "fingerprint": fingerprint,
                    "attempt_id": material["attempt_id"],
                    "artifact_generation": material["artifact_generation"],
                    "operator_adjustment": material["operator_adjustment"],
                    "selection_digest": material["selection_digest"],
                },
            }
        )
        try:
            finalize_ingest_strict(material["files"], processed_data)
            row = db_store.get_connection().execute(
                "SELECT status FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            if row is None or str(row["status"] or "") != "finalized":
                raise RuntimeError("recovery finalizer returned without durable finalization")
            committed.append(job_id)
        except Exception as exc:
            errors.append(
                {
                    "job_id": job_id,
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:500],
                }
            )
            for pending in pending_materials[position:]:
                pending_id = str(pending["job_id"])
                pending_claim = claim_by_job[pending_id]
                try:
                    if db_store.restore_terminal_ingest_recovery_claim(
                        pending_claim,
                        reason=f"recovery batch stopped after {job_id}: {type(exc).__name__}",
                    ):
                        restored.append(pending_id)
                    else:
                        row = db_store.get_connection().execute(
                            "SELECT status FROM jobs WHERE job_id = ?", (pending_id,)
                        ).fetchone()
                        if row is not None and str(row["status"] or "") == "finalized":
                            committed.append(pending_id)
                        else:
                            indeterminate.append(pending_id)
                except Exception:
                    indeterminate.append(pending_id)
            break
        if position + 1 < len(pending_materials):
            try:
                _wait_for_terminal_recovery_projection()
            except Exception as exc:
                errors.append(
                    {
                        "job_id": job_id,
                        "error_type": type(exc).__name__,
                        "error": str(exc)[:500],
                    }
                )
                for pending in pending_materials[position + 1 :]:
                    pending_id = str(pending["job_id"])
                    pending_claim = claim_by_job[pending_id]
                    try:
                        if db_store.restore_terminal_ingest_recovery_claim(
                            pending_claim,
                            reason=(
                                f"recovery projection did not settle after {job_id}: "
                                f"{type(exc).__name__}"
                            ),
                        ):
                            restored.append(pending_id)
                        else:
                            indeterminate.append(pending_id)
                    except Exception:
                        indeterminate.append(pending_id)
                break
    requested_count = len(materials)
    projection_settled = False
    if len(set(committed)) == requested_count:
        try:
            _wait_for_terminal_recovery_projection()
            projection_settled = True
        except Exception as exc:
            errors.append(
                {
                    "job_id": "",
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:500],
                }
            )
    unattempted = [
        str(item["job_id"])
        for item in materials
        if str(item["job_id"]) not in set(committed) | set(restored) | set(indeterminate)
    ]
    fully_committed = len(set(committed)) == requested_count
    state = (
        "completed"
        if fully_committed and projection_settled
        else "committed_projection_pending"
        if fully_committed
        else "partial"
    )
    recovery_fingerprints = {
        str(item["job_id"]): (
            str(item["stored_recovery_fingerprint"])
            if item.get("already_recovered")
            else fingerprint
        )
        for item in materials
    }
    receipt = {
        "ok": state == "completed",
        "committed": fully_committed,
        "projection_settled": projection_settled,
        "dry_run": False,
        "state": state,
        "fingerprint": fingerprint,
        "confirmation_fingerprint": fingerprint,
        "recovery_fingerprints": recovery_fingerprints,
        "requested": requested_count,
        "committed_job_ids": sorted(set(committed)),
        "restored_terminal_job_ids": sorted(set(restored)),
        "unattempted_job_ids": unattempted,
        "indeterminate_job_ids": sorted(set(indeterminate)),
        "errors": errors,
    }
    return json.dumps(receipt, ensure_ascii=False, indent=2)




def _finalize_exact_reviewed_ingest_strict(
    files_written: list,
    processed_data: dict,
    *,
    reviewed_ingest_context: _ReviewedIngestFinalizerContext,
) -> str:
    """Finalize exact reviewed ingest with a process-local attestation context."""
    return _finalize_ingest_impl(
        files_written,
        processed_data,
        propagate_errors=True,
        reviewed_ingest_context=reviewed_ingest_context,
    )


def finalize_ingest(files_written: list, processed_data: dict) -> str:
    """Finalize an ingest operation using the stable MCP string-return contract."""
    return _finalize_ingest_impl(
        files_written,
        processed_data,
        propagate_errors=False,
    )


