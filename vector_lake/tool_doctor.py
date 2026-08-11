import importlib
import sqlite3
import os
import sys
import ast
import json
from datetime import datetime, timezone
from pathlib import Path

from vector_lake.wiki_utils import (
    get_index_path,
    get_memory_dir,
    get_raw_dir,
    get_wiki_dir,
    iter_markdown_files,
    peek_meta_dir,
)
from vector_lake.db_store import inspect_schema_migration_state, peek_db_path
from vector_lake import db_store, get_extension_root
from vector_lake.native_llm import native_llm_ready
from vector_lake.runtime_health import (
    _open_runtime_database_read_only,
    assess_runtime_health,
    assess_semantic_readiness,
)


def _dependency_available(module_name: str) -> bool:
    """Check installation without executing dependency import side effects."""
    return importlib.util.find_spec(module_name) is not None

def _check_ast(module_path: Path) -> tuple[bool, str]:
    if not module_path.exists():
        return False, "file not found"
    try:
        with open(module_path, "r", encoding="utf-8") as f:
            ast.parse(f.read(), filename=module_path.name)
        return True, "AST OK"
    except SyntaxError as e:
        return False, f"SyntaxError: {e}"
    except Exception as e:
        return False, f"Error: {e}"



def _read_database_state_from_connection(conn: sqlite3.Connection) -> dict:
    """Read Doctor counters from a caller-owned query-only connection."""
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    canonical_keys = {
        row["page_key"]
        for row in conn.execute(
            "SELECT json_extract(data_json, '$.page_key') AS page_key "
            "FROM entities WHERE json_extract(data_json, '$.page_key') "
            "IS NOT NULL"
        )
        if not str(row["page_key"]).startswith("System_")
    }
    outbox_counts = {
        row["status"]: row["count"]
        for row in conn.execute(
            "SELECT status, COUNT(*) AS count FROM mutation_outbox "
            "GROUP BY status"
        )
    }
    terminal_jobs = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE status = 'failed' AND retries >= 3"
    ).fetchone()[0]
    queued_jobs = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE status = 'queued'"
    ).fetchone()[0]
    awaiting_jobs = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE status = 'awaiting_subagent'"
    ).fetchone()[0]
    return {
        "canonical_keys": canonical_keys,
        "outbox_counts": outbox_counts,
        "terminal_jobs": terminal_jobs,
        "queued_jobs": queued_jobs,
        "awaiting_jobs": awaiting_jobs,
    }


def _read_database_state(db_path: Path) -> dict:
    """Read Doctor counters without creating, migrating, or retaining a connection."""
    with db_store.read_only_transaction_snapshot(db_path.resolve()) as conn:
        return _read_database_state_from_connection(conn)


def _schema_readiness_reasons(schema_state: dict) -> list[str]:
    """Explain a non-ready schema even when a valid older ledger is migratable."""
    reasons = [str(item) for item in (schema_state.get("issues") or []) if item]
    user_version = schema_state.get("user_version")
    supported_version = schema_state.get("supported_version")
    if user_version is not None and supported_version is not None:
        current = int(user_version)
        supported = int(supported_version)
        if current < supported:
            reasons.append(f"database_schema_upgrade_required:{current}->{supported}")
        elif current > supported:
            reasons.append(f"database_schema_newer_than_runtime:{current}>{supported}")
    if not schema_state.get("ready") and not reasons:
        reasons.append(f"database_schema_not_ready:{schema_state.get('status', 'unknown')}")
    return list(dict.fromkeys(reasons))


def doctor_vector_lake() -> str:
    checks = []
    semantic_readiness = {
        "ready": False,
        "status": "not_ready",
        "issues": ["semantic_readiness_not_assessed"],
        "warnings": [],
        "detail": {},
    }

    # 1. Environment & Config
    python_ok = sys.version_info >= (3, 10)
    checks.append(("Python", python_ok, f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"))

    has_api_key = bool(os.environ.get("GEMINI_API_KEY"))
    checks.append((
        "Gemini Embedding",
        True if has_api_key else None,
        (
            "GEMINI_API_KEY is available"
            if has_api_key
            else "Unavailable: GEMINI_API_KEY is not inherited by this process"
        ),
    ))

    # 2. Dependencies
    dependencies = {
        "google.genai": "google-genai",
        "filelock": "filelock",
        "yaml": "PyYAML",
        "watchdog": "watchdog",
        "networkx": "networkx",
        "community": "python-louvain",
        "dotenv": "python-dotenv",
        "mcp": "mcp",
        "sqlite_vec": "sqlite-vec",
        "rjieba": "rjieba",
        "mistune": "mistune"
    }
    for module_name, package_name in dependencies.items():
        try:
            if not _dependency_available(module_name):
                raise ImportError(module_name)
            checks.append(
                (package_name, True, "discoverable; runtime import deferred")
            )
        except ImportError:
            checks.append((package_name, False, "missing"))
            
    llm_ok, llm_detail = native_llm_ready()
    checks.append(("Subagent Text Runtime", True if llm_ok else None, llm_detail))

    # 3. Paths & Basic Files
    meta_path = peek_meta_dir()
    db_path = peek_db_path()
    schema_state = inspect_schema_migration_state(db_path)
    schema_reasons = _schema_readiness_reasons(schema_state)
    for label, path in [("MEMORY", get_memory_dir()), ("Raw", get_raw_dir()), ("Wiki", get_wiki_dir())]:
        checks.append((label, path.exists(), str(path)))

    index_exists = get_index_path().exists()
    checks.append(("Index", index_exists, str(get_index_path()) if index_exists else "Lake is drying (Empty)"))
    for label, path in [("Meta", meta_path), ("SQLite DB", db_path)]:
        checks.append((label, path.exists(), str(path)))
    migration_detail = (
        f"user_version={schema_state['user_version']}; "
        f"supported={schema_state['supported_version']}; "
        f"ledger_entries={len(schema_state['ledger'])}"
    )
    if schema_reasons:
        migration_detail += "; " + "; ".join(schema_reasons)
    checks.append((
        "Schema Migrations",
        bool(schema_state["ready"]),
        migration_detail,
    ))

    committed_index_data = None
    projection_connection = None
    if index_exists and schema_state["ready"]:
        try:
            from vector_lake.indexer import read_committed_index_snapshot

            projection_connection, _projection_db_path = (
                _open_runtime_database_read_only()
            )
            committed_index_data = read_committed_index_snapshot(
                get_index_path(),
                lock_timeout=1.0,
                connection=projection_connection,
                _acquire_lock=False,
            )
            projection_manifest = committed_index_data.get(
                "projection_manifest"
            ) or {}
            checks.append((
                "Projection Pair",
                True,
                "committed generation="
                + str(projection_manifest.get("generation") or "unknown"),
            ))
        except Exception as exc:
            checks.append(("Projection Pair", False, f"not committed/current: {exc}"))
        finally:
            if projection_connection is not None:
                projection_connection.close()
    else:
        reason = "index missing" if not index_exists else "database schema not ready"
        checks.append(("Projection Pair", False, reason))

    # 4. AST Compilation Checks
    ext_root = get_extension_root()
    for mod in ["mcp_server.py", "watchdog_app.py", "tool_ingest.py"]:
        mod_path = ext_root / "vector_lake" / mod
        ok, detail = _check_ast(mod_path)
        checks.append((f"AST Compile {mod}", ok, detail))

    # 5. MCP Discovery / Import check
    try:
        from vector_lake.mcp_server import mcp
        manager = getattr(mcp, "_tool_manager", None)
        registered = getattr(manager, "_tools", {}) if manager is not None else {}
        tools_count = len(registered) if registered is not None else 0
        checks.append(("MCP Server", tools_count > 0, f"Import OK, {tools_count} tools exposed"))
    except Exception as e:
        checks.append(("MCP Server", False, f"Startup Exception: {e}"))

    # 6. Watchdog Heartbeat
    status_path = meta_path / ".watchdog_status.json"
    if status_path.exists():
        try:
            with open(status_path, "r", encoding="utf-8") as f:
                status = json.load(f)
            updated_at = status.get("updated_at")
            age_seconds = None
            if updated_at:
                updated_dt = datetime.fromisoformat(str(updated_at).replace("Z", "+00:00"))
                if updated_dt.tzinfo is None:
                    updated_dt = updated_dt.replace(tzinfo=timezone.utc)
                age_seconds = max(0, int((datetime.now(timezone.utc) - updated_dt).total_seconds()))
            unhealthy_states = {"error", "halted", "stopped"}
            try:
                component_max_age = max(
                    5,
                    int(
                        os.environ.get(
                            "VECTOR_LAKE_WATCHDOG_COMPONENT_MAX_AGE_SECONDS",
                            "120",
                        )
                    ),
                )
            except (TypeError, ValueError):
                component_max_age = 120
            now_utc = datetime.now(timezone.utc)
            unhealthy_components = []
            stale_components = []
            for name, raw_component in (status.get("components") or {}).items():
                component = (
                    raw_component if isinstance(raw_component, dict) else {}
                )
                component_state = str(component.get("status", "")).lower()
                if component_state in unhealthy_states:
                    unhealthy_components.append(str(name))
                component_updated = component.get("heartbeat_at") or component.get(
                    "updated_at"
                )
                component_dt = (
                    datetime.fromisoformat(
                        str(component_updated).replace("Z", "+00:00")
                    )
                    if component_updated
                    else None
                )
                if component_dt is not None and component_dt.tzinfo is None:
                    component_dt = component_dt.replace(tzinfo=timezone.utc)
                component_age = (
                    max(0, int((now_utc - component_dt).total_seconds()))
                    if component_dt is not None
                    else None
                )
                if component_age is None or component_age > component_max_age:
                    stale_components.append(str(name))
            heartbeat_ok = (
                age_seconds is not None
                and age_seconds <= 120
                and str(status.get("status", "")).lower() not in unhealthy_states
                and not unhealthy_components
                and not stale_components
            )
            detail = (
                f"[{status.get('status', 'unknown')}] "
                f"{status.get('current_action', '')}; "
                f"age={age_seconds if age_seconds is not None else 'unknown'}s; "
                f"unhealthy={','.join(sorted(unhealthy_components)) or 'none'}; "
                f"stale={','.join(sorted(stale_components)) or 'none'}"
            )
            checks.append(("Watchdog Status", heartbeat_ok, detail))
        except Exception as e:
            checks.append(("Watchdog Status", False, f"Parse error: {e}"))
    else:
        checks.append(("Watchdog Status", False, "No status file found (not running?)"))

    # 7. State Projection Consistency
    try:
        excluded = {"index.md", "log.md", "overview.md", "orphan_pages.md", "wiki_link_stats.md", "synthesis_log.md"}
        wiki_keys = {
            path.stem
            for path in iter_markdown_files(get_wiki_dir())
            if path.name.casefold() not in excluded
            and not path.name.casefold().startswith("system_")
        }
        if committed_index_data is None:
            raise RuntimeError("projection pair is not committed and current")
        index_data = committed_index_data
        index_keys = {
            key for key in index_data.get("nodes", {})
            if not str(key).startswith("System_")
        }
        if not schema_state["ready"]:
            raise RuntimeError(
                "database schema is not ready: "
                + "; ".join(schema_reasons)
            )
        db_state = _read_database_state(db_path)
        canonical_keys = db_state["canonical_keys"]
        missing_index = canonical_keys - index_keys
        extra_index = index_keys - canonical_keys
        missing_canonical = wiki_keys - canonical_keys
        extra_canonical = canonical_keys - wiki_keys
        consistent = not (missing_index or extra_index or missing_canonical or extra_canonical)
        checks.append((
            "State Consistency",
            consistent,
            f"Wiki:{len(wiki_keys)} JSON:{len(index_keys)} SQLite:{len(canonical_keys)} "
            f"missing_index:{len(missing_index)} extra_index:{len(extra_index)} "
            f"missing_canonical:{len(missing_canonical)} extra_canonical:{len(extra_canonical)}",
        ))

        outbox_counts = db_state["outbox_counts"]
        outbox_ok = outbox_counts.get("failed", 0) == 0
        checks.append(("Mutation Outbox", outbox_ok, json.dumps(outbox_counts, ensure_ascii=False, sort_keys=True)))

        terminal_jobs = db_state["terminal_jobs"]
        queued_jobs = db_state["queued_jobs"]
        awaiting_jobs = db_state["awaiting_jobs"]
        checks.append((
            "Ingest Jobs",
            terminal_jobs == 0,
            f"queued:{queued_jobs} awaiting_subagent:{awaiting_jobs} terminal_failed:{terminal_jobs}",
        ))

        health = assess_runtime_health(deep_projection_checks=True)
        checks.append((
            "Write Gate",
            health["ok"],
            (
                "clean"
                + (f"; warnings: {'; '.join(health['warnings'])}" if health["warnings"] else "")
                if health["ok"]
                else "; ".join(health["issues"])
            ),
        ))
        semantic_readiness = assess_semantic_readiness(index_data=index_data)
    except Exception as e:
        checks.append(("State Consistency", False, f"Check failed: {e}"))
        semantic_readiness = {
            "ready": False,
            "status": "not_ready",
            "issues": [f"semantic_readiness_check_failed:{e}"],
            "warnings": [],
            "detail": {},
        }

    lines = ["=== Vector Lake Doctor ==="]
    all_ok = True
    has_warnings = False
    for label, ok, detail in checks:
        if ok is None:
            state = "WARN"
            has_warnings = True
        elif ok:
            state = "OK"
        else:
            state = "FAIL"
            all_ok = False
        lines.append(f"[{state}] {label}: {detail}")
    lines.append("")
    infrastructure_status = (
        "issues detected"
        if not all_ok
        else ("healthy with warnings" if has_warnings else "healthy")
    )
    lines.append(f"Infrastructure Summary: {infrastructure_status}")
    lines.append(f"Semantic Readiness: {semantic_readiness['status']}")
    if semantic_readiness["issues"]:
        lines.append("Semantic issues: " + "; ".join(semantic_readiness["issues"]))
    if semantic_readiness["warnings"]:
        lines.append("Semantic warnings: " + "; ".join(semantic_readiness["warnings"]))
    lines.append(
        f"Summary: infrastructure {infrastructure_status}; "
        f"semantic readiness {semantic_readiness['status']}"
    )
    lines.append(f"VECTOR_LAKE_MEMORY_DIR={os.environ.get('VECTOR_LAKE_MEMORY_DIR', '<default>')}")
    return "\n".join(lines)


def semantic_readiness_vector_lake(decision_id: str | None = None) -> str:
    """Return the machine-readable semantic readiness report."""
    return json.dumps(
        assess_semantic_readiness(decision_id=decision_id),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )

