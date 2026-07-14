"""Runtime health checks used by write gates and doctor surfaces."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


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


def assess_runtime_health(
    max_watchdog_age_seconds: int = 120,
    deep_projection_checks: bool = False,
) -> dict[str, Any]:
    from vector_lake.db_store import get_connection, get_db_path, init_db
    from vector_lake.wiki_utils import get_index_path, get_meta_dir, get_wiki_dir

    issues: list[str] = []
    warnings: list[str] = []
    detail: dict[str, Any] = {}

    try:
        db_path = get_db_path()
        if not db_path.exists():
            init_db()
        conn = get_connection()
    except Exception as exc:
        return {"ok": False, "issues": [f"database_unavailable:{exc}"], "warnings": [], "detail": {}}

    detail["db_path"] = str(db_path)

    outbox_counts = {
        row["status"]: row["count"]
        for row in conn.execute("SELECT status, COUNT(*) AS count FROM mutation_outbox GROUP BY status")
    }
    detail["outbox_counts"] = outbox_counts
    if outbox_counts.get("failed", 0):
        issues.append(f"mutation_outbox_failed:{outbox_counts.get('failed', 0)}")
    oldest_pending = conn.execute(
        "SELECT MIN(COALESCE(available_at, created_at)) FROM mutation_outbox "
        "WHERE status IN ('pending', 'processing')"
    ).fetchone()[0]
    oldest_pending_dt = _parse_dt(oldest_pending)
    if oldest_pending_dt is not None:
        pending_age = max(0, int((datetime.now(timezone.utc) - oldest_pending_dt).total_seconds()))
        detail["oldest_pending_outbox_age_seconds"] = pending_age
        max_pending_age = max(1, int(os.environ.get("VECTOR_LAKE_OUTBOX_MAX_PENDING_AGE_SECONDS", "300")))
        if pending_age > max_pending_age:
            issues.append(f"mutation_outbox_stalled:{pending_age}s")

    terminal_jobs = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE status = 'failed' AND retries >= 3"
    ).fetchone()[0]
    detail["terminal_failed_jobs"] = int(terminal_jobs)
    if terminal_jobs:
        issues.append(f"terminal_failed_jobs:{terminal_jobs}")
    awaiting_row = conn.execute(
        "SELECT COUNT(*) AS count, MIN(updated_at) AS oldest FROM jobs WHERE status = 'awaiting_subagent'"
    ).fetchone()
    awaiting_count = int(awaiting_row["count"] or 0)
    detail["awaiting_subagent_jobs"] = awaiting_count
    backlog_messages = []
    max_awaiting = max(1, int(os.environ.get("VECTOR_LAKE_MAX_AWAITING_SUBAGENT_JOBS", "500")))
    if awaiting_count > max_awaiting:
        backlog_messages.append(f"count={awaiting_count}")
    oldest_awaiting = _parse_dt(awaiting_row["oldest"])
    if oldest_awaiting is not None:
        awaiting_age = max(0, int((datetime.now(timezone.utc) - oldest_awaiting).total_seconds()))
        detail["oldest_awaiting_subagent_age_seconds"] = awaiting_age
        max_age = max(60, int(os.environ.get("VECTOR_LAKE_MAX_AWAITING_SUBAGENT_AGE_SECONDS", "86400")))
        if awaiting_age > max_age:
            backlog_messages.append(f"oldest={awaiting_age}s")
    if backlog_messages:
        message = "subagent_backlog:" + ",".join(backlog_messages)
        if os.environ.get("VECTOR_LAKE_SUBAGENT_BACKLOG_BLOCKING") == "1":
            issues.append(message)
        else:
            warnings.append(message)

    status_path = get_meta_dir() / ".watchdog_status.json"
    if status_path.exists():
        try:
            status = json.loads(status_path.read_text(encoding="utf-8"))
            updated_at = _parse_dt(status.get("updated_at"))
            age = None
            if updated_at is not None:
                age = max(0, int((datetime.now(timezone.utc) - updated_at).total_seconds()))
            detail["watchdog_age_seconds"] = age
            detail["watchdog_status"] = status.get("status")
            if age is None or age > max_watchdog_age_seconds:
                issues.append(f"watchdog_stale:{age if age is not None else 'unknown'}")
            unhealthy_components = [
                name
                for name, component in (status.get("components") or {}).items()
                if str(component.get("status", "")).lower() in {"error", "halted"}
            ]
            if str(status.get("status", "")).lower() in {"error", "halted"} or unhealthy_components:
                issues.append(
                    "watchdog_unhealthy:"
                    + (",".join(sorted(unhealthy_components)) or str(status.get("status")))
                )
        except Exception as exc:
            issues.append(f"watchdog_status_unreadable:{exc}")
    else:
        warnings.append("watchdog_status_missing")

    excluded = {"index.md", "log.md", "overview.md", "orphan_pages.md", "wiki_link_stats.md", "Synthesis_log.md"}
    wiki_dir = get_wiki_dir()
    wiki_keys = {
        path.stem for path in wiki_dir.glob("*.md")
        if path.is_file() and path.name not in excluded and not path.name.startswith("System_")
    } if wiki_dir.exists() else set()
    canonical_keys = {
        row["page_key"] for row in conn.execute(
            "SELECT json_extract(data_json, '$.page_key') AS page_key FROM entities "
            "WHERE json_extract(data_json, '$.page_key') IS NOT NULL"
        )
        if row["page_key"] and not str(row["page_key"]).startswith("System_")
    }
    index_path = get_index_path()
    index_available = index_path.exists()
    if index_path.exists():
        try:
            index_keys = {
                key for key in json.loads(index_path.read_text(encoding="utf-8")).get("nodes", {})
                if not str(key).startswith("System_")
            }
        except Exception as exc:
            index_keys = set()
            issues.append(f"index_unreadable:{exc}")
    else:
        index_keys = set()
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

    strict_timeline_parity = os.environ.get("VECTOR_LAKE_TIMELINE_PARITY_BLOCKING") == "1"
    if deep_projection_checks or strict_timeline_parity:
        from vector_lake.tool_timeline import timeline_projection_parity

        timeline_drift = timeline_projection_parity()
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

    return {"ok": not issues, "issues": issues, "warnings": warnings, "detail": detail}


def enforce_runtime_write_health(validation_mode: str = "full"):
    if os.environ.get("VECTOR_LAKE_DISABLE_WRITE_HEALTH_GATE") == "1":
        return
    if validation_mode == "schema":
        return
    health = assess_runtime_health()
    if not health["ok"]:
        raise RuntimeError(
            "Vector Lake write gate blocked this mutation because runtime health is not clean. "
            "Use validation_mode='schema' for bounded repairs or set VECTOR_LAKE_DISABLE_WRITE_HEALTH_GATE=1 after explicit approval. "
            f"Issues: {'; '.join(health['issues'])}"
        )
