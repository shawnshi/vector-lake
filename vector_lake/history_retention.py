"""_HISTORY_RETENTION_REQUIRED_TABLES _history_retention_plan _public_history_retention_result _utc_now _validate_history_retention_options history_retention_maintenance

Split out of tool_governance_maintenance so that a lower layer can use it without importing a
handler. The cluster is closed under intra-module references; tool_governance_maintenance
re-imports these names for its remaining code.
"""

from __future__ import annotations

from __future__ import annotations
import hmac
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from filelock import FileLock, Timeout as FileLockTimeout
from vector_lake import db_store, governance_store


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()

_HISTORY_RETENTION_REQUIRED_TABLES = frozenset(
    {
        "change_sets",
        "change_set_idempotency",
        "governance_queue",
        "jobs",
        "ingest_task_cleanup",
        "mutation_outbox",
        "claim_versions",
        "evidence_versions",
        "claims",
        "evidence",
        "runtime_generations",
        "change_set_payloads",
        "change_set_payload_refs",
        "change_set_lifecycle_v6",
        "history_retention_runs_v6",
    }
)

def _validate_history_retention_options(
    *,
    ttl_days: int,
    batch_size: int,
    max_delete_bytes: int,
    keep_change_sets: int,
    keep_terminal_jobs: int,
    keep_terminal_outbox: int,
    keep_versions_per_family: int,
    claim_version_cursor: str,
    evidence_version_cursor: str,
    version_cursor_receipt: str,
) -> dict[str, int | str]:
    options = {
        "ttl_days": int(ttl_days),
        "batch_size": int(batch_size),
        "max_delete_bytes": int(max_delete_bytes),
        "keep_change_sets": int(keep_change_sets),
        "keep_terminal_jobs": int(keep_terminal_jobs),
        "keep_terminal_outbox": int(keep_terminal_outbox),
        "keep_versions_per_family": int(keep_versions_per_family),
        "claim_version_cursor": str(claim_version_cursor or ""),
        "evidence_version_cursor": str(evidence_version_cursor or ""),
        "version_cursor_receipt": str(version_cursor_receipt or ""),
    }
    if options["ttl_days"] < 1:
        raise ValueError("ttl_days must be positive")
    if not 1 <= options["batch_size"] <= 500:
        raise ValueError("batch_size must be between 1 and 500")
    if not (
        1
        <= options["max_delete_bytes"]
        <= governance_store._HISTORY_RETENTION_MAX_DELETE_BYTES
    ):
        raise ValueError("max_delete_bytes must be between 1 byte and 128 MiB")
    for name in (
        "keep_change_sets",
        "keep_terminal_jobs",
        "keep_terminal_outbox",
    ):
        if options[name] < 0:
            raise ValueError(f"{name} must be zero or positive")
    if not (
        1
        <= options["keep_versions_per_family"]
        <= governance_store._HISTORY_VERSION_MAX_KEEP_PER_FAMILY
    ):
        raise ValueError(
            "keep_versions_per_family must be between 1 and "
            f"{governance_store._HISTORY_VERSION_MAX_KEEP_PER_FAMILY}"
        )
    return options

def _history_retention_plan(
    conn: sqlite3.Connection,
    *,
    cutoff: str,
    plan_as_of: str,
    options: dict[str, int | str],
) -> dict:
    tables = {
        str(row["name"])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    missing = sorted(_HISTORY_RETENTION_REQUIRED_TABLES - tables)
    if missing:
        raise RuntimeError(f"schema_not_ready:missing_tables:{','.join(missing)}")
    return governance_store.plan_history_retention(
        conn,
        cutoff=cutoff,
        batch_size=options["batch_size"],
        max_delete_bytes=options["max_delete_bytes"],
        keep_change_sets=options["keep_change_sets"],
        keep_terminal_jobs=options["keep_terminal_jobs"],
        keep_terminal_outbox=options["keep_terminal_outbox"],
        keep_versions_per_family=options["keep_versions_per_family"],
        claim_version_cursor=options["claim_version_cursor"],
        evidence_version_cursor=options["evidence_version_cursor"],
        version_cursor_receipt=options["version_cursor_receipt"],
        plan_as_of=plan_as_of,
    )

def _public_history_retention_result(
    *,
    dry_run: bool,
    schema_state: dict,
    plan: dict | None = None,
    deleted_counts: dict[str, int] | None = None,
    preview_error: str | None = None,
) -> str:
    result = {
        "dry_run": bool(dry_run),
        "applied": not dry_run and preview_error is None,
        "schema_state": schema_state,
    }
    if plan is not None:
        selected = plan.get("selected_ids") or {}
        result.update(
            {
                key: value
                for key, value in plan.items()
                if key not in {"selected_ids", "candidates"}
            }
        )
        result["selected_samples"] = {
            table_name: list(values)[:20] for table_name, values in selected.items()
        }
    if deleted_counts is not None:
        result["deleted_counts"] = deleted_counts
    if preview_error is not None:
        result["preview_error"] = preview_error
    return json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)

def history_retention_maintenance(
    dry_run: bool = True,
    ttl_days: int = 30,
    batch_size: int = 500,
    max_delete_bytes: int = 128 * 1024 * 1024,
    keep_change_sets: int = 1000,
    keep_terminal_jobs: int = 1000,
    keep_terminal_outbox: int = 1000,
    keep_versions_per_family: int = 2,
    claim_version_cursor: str = "",
    evidence_version_cursor: str = "",
    version_cursor_receipt: str = "",
    plan_as_of: str = "",
    confirmation: str = "",
) -> str:
    """Preview or apply one exact, globally bounded retention batch."""
    options = _validate_history_retention_options(
        ttl_days=ttl_days,
        batch_size=batch_size,
        max_delete_bytes=max_delete_bytes,
        keep_change_sets=keep_change_sets,
        keep_terminal_jobs=keep_terminal_jobs,
        keep_terminal_outbox=keep_terminal_outbox,
        keep_versions_per_family=keep_versions_per_family,
        claim_version_cursor=claim_version_cursor,
        evidence_version_cursor=evidence_version_cursor,
        version_cursor_receipt=version_cursor_receipt,
    )
    normalized_as_of = governance_store._strict_utc_instant(
        plan_as_of or _utc_now()
    )
    if normalized_as_of is None:
        raise ValueError("plan_as_of must be a timezone-aware ISO-8601 instant")
    as_of_dt = datetime.fromisoformat(normalized_as_of)
    cutoff = (as_of_dt - timedelta(days=options["ttl_days"])).isoformat()
    path = db_store.peek_db_path() if dry_run else db_store.get_db_path()

    if not dry_run and (not plan_as_of or not confirmation):
        raise RuntimeError(
            "History retention apply requires plan_as_of and the exact preview fingerprint"
        )

    if dry_run:
        schema_state = db_store.inspect_schema_migration_state(path)
        if not schema_state["ready"]:
            return _public_history_retention_result(
                dry_run=True,
                schema_state=schema_state,
                preview_error=f"schema_not_ready:{schema_state['status']}",
            )
        conn = None
        try:
            conn = sqlite3.connect(
                f"{path.resolve().as_uri()}?mode=ro",
                uri=True,
                timeout=5.0,
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            plan = _history_retention_plan(
                conn,
                cutoff=cutoff,
                plan_as_of=normalized_as_of,
                options=options,
            )
        except (OSError, sqlite3.Error, RuntimeError, ValueError) as exc:
            return _public_history_retention_result(
                dry_run=True,
                schema_state=schema_state,
                preview_error=str(exc),
            )
        finally:
            if conn is not None:
                conn.close()
        return _public_history_retention_result(
            dry_run=True,
            schema_state=schema_state,
            plan=plan,
        )

    lock_path = path.parent / ".history-retention.lock"
    try:
        maintenance_lock = FileLock(str(lock_path), timeout=5)
        maintenance_lock.acquire()
    except FileLockTimeout as exc:
        raise RuntimeError("History retention maintenance window is busy") from exc
    try:
        db_store.init_db()
        schema_state = db_store.inspect_schema_migration_state(path)
        if not schema_state["ready"]:
            raise RuntimeError(f"schema_not_ready:{schema_state['status']}")
        with db_store.transaction():
            conn = db_store.get_connection()
            prior = conn.execute(
                "SELECT plan_as_of, options_json, plan_sha256, receipt_json "
                "FROM history_retention_runs_v6 "
                "WHERE fingerprint = ?",
                (confirmation,),
            ).fetchone()
            if prior is not None:
                try:
                    receipt = json.loads(prior["receipt_json"])
                    stored_rules = json.loads(prior["options_json"])
                except (TypeError, ValueError) as exc:
                    raise RuntimeError(
                        "History retention receipt metadata is malformed"
                    ) from exc
                expected_rules = {
                    "max_delete_rows": options["batch_size"],
                    "max_delete_bytes": options["max_delete_bytes"],
                    "keep_change_sets": options["keep_change_sets"],
                    "keep_terminal_jobs": options["keep_terminal_jobs"],
                    "keep_terminal_outbox": options["keep_terminal_outbox"],
                    "keep_versions_per_family": options[
                        "keep_versions_per_family"
                    ],
                    "claim_version_cursor": options["claim_version_cursor"],
                    "evidence_version_cursor": options[
                        "evidence_version_cursor"
                    ],
                    "version_cursor_receipt": options[
                        "version_cursor_receipt"
                    ],
                    "scan_version_history": True,
                }
                if stored_rules != expected_rules:
                    raise RuntimeError(
                        "History retention receipt options do not match the request"
                    )
                if (
                    prior["plan_as_of"] != normalized_as_of
                    or receipt.get("plan_as_of") != normalized_as_of
                ):
                    raise RuntimeError(
                        "History retention receipt plan_as_of does not match"
                    )
                if governance_store._strict_utc_instant(receipt.get("cutoff")) != cutoff:
                    raise RuntimeError(
                        "History retention receipt cutoff does not match the request"
                    )
                if (
                    receipt.get("fingerprint") != confirmation
                    or not confirmation.startswith("sha256:")
                    or not hmac.compare_digest(
                        str(prior["plan_sha256"] or ""),
                        confirmation[7:],
                    )
                ):
                    raise RuntimeError(
                        "History retention receipt fingerprint is invalid"
                    )
                deleted_counts = {
                    str(key): int(value)
                    for key, value in (receipt.get("deleted_counts") or {}).items()
                }
                plan = {
                    "contract": "history-retention-plan-v2",
                    "plan_as_of": normalized_as_of,
                    "cutoff": receipt.get("cutoff"),
                    "rules": expected_rules,
                    "fingerprint": confirmation,
                    "selected_ids": {},
                    "selected_counts": {},
                    "selected_count_total": 0,
                    "selected_bytes_total": int(
                        receipt.get("selected_bytes_total") or 0
                    ),
                    "version_resume_cursors": receipt.get(
                        "version_resume_cursors"
                    )
                    or {},
                    "replayed_receipt": True,
                }
            else:
                plan = _history_retention_plan(
                    conn,
                    cutoff=cutoff,
                    plan_as_of=normalized_as_of,
                    options=options,
                )
                if plan["fingerprint"] != confirmation:
                    raise RuntimeError(
                        "History retention candidate set changed after preview"
                    )
                deleted_counts = governance_store.apply_history_retention_plan(
                    conn,
                    plan,
                    confirmation=confirmation,
                    plan_as_of=normalized_as_of,
                )
    finally:
        maintenance_lock.release()
    schema_state = db_store.inspect_schema_migration_state(path)
    return _public_history_retention_result(
        dry_run=False,
        schema_state=schema_state,
        plan=plan,
        deleted_counts=deleted_counts,
    )
