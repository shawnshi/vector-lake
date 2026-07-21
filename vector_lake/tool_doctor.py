import importlib
import os
import sys
import ast
import json
from datetime import datetime, timezone
from pathlib import Path

from vector_lake.wiki_utils import get_index_path, get_memory_dir, get_raw_dir, get_wiki_dir, get_meta_dir
from vector_lake.db_store import get_db_path, get_connection
from vector_lake import get_extension_root
from vector_lake.native_llm import native_llm_ready
from vector_lake.runtime_health import assess_runtime_health, assess_semantic_readiness
from vector_lake.tokenizer_runtime import load_tokenizer

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
    checks.append(("GEMINI_API_KEY", True, "Set" if has_api_key else "Not set (optional; embeddings disabled)"))

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
            if module_name == "rjieba":
                load_tokenizer()
            elif module_name == "google.genai":
                if importlib.util.find_spec(module_name) is None:
                    raise ImportError(module_name)
            else:
                importlib.import_module(module_name)
            checks.append((package_name, True, "installed"))
        except ImportError:
            checks.append((package_name, False, "missing"))
            
    llm_ok, llm_detail = native_llm_ready()
    checks.append(("Subagent Text Runtime", True, llm_detail if llm_ok else llm_detail))

    # 3. Paths & Basic Files
    for label, path in [("MEMORY", get_memory_dir()), ("Raw", get_raw_dir()), ("Wiki", get_wiki_dir())]:
        checks.append((label, path.exists(), str(path)))

    index_exists = get_index_path().exists()
    checks.append(("Index", index_exists, str(get_index_path()) if index_exists else "Lake is drying (Empty)"))
    for label, path in [("Meta", get_meta_dir()), ("SQLite DB", get_db_path())]:
        checks.append((label, path.exists(), str(path)))

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
    status_path = get_meta_dir() / ".watchdog_status.json"
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
            unhealthy_components = [
                name
                for name, component in (status.get("components") or {}).items()
                if str(component.get("status", "")).lower() in {"error", "halted"}
            ]
            heartbeat_ok = (
                age_seconds is not None
                and age_seconds <= 120
                and str(status.get("status", "")).lower() not in {"error", "halted"}
                and not unhealthy_components
            )
            detail = f"[{status.get('status', 'unknown')}] {status.get('current_action', '')}; age={age_seconds if age_seconds is not None else 'unknown'}s"
            checks.append(("Watchdog Status", heartbeat_ok, detail))
        except Exception as e:
            checks.append(("Watchdog Status", False, f"Parse error: {e}"))
    else:
        checks.append(("Watchdog Status", False, "No status file found (not running?)"))

    # 7. State Projection Consistency
    try:
        excluded = {"index.md", "log.md", "overview.md", "orphan_pages.md", "wiki_link_stats.md", "Synthesis_log.md"}
        wiki_keys = {
            path.stem for path in get_wiki_dir().glob("*.md")
            if path.is_file() and path.name not in excluded and not path.name.startswith("System_")
        }
        with open(get_index_path(), "r", encoding="utf-8") as f:
            index_data = json.load(f)
            index_keys = {
                key for key in index_data.get("nodes", {})
                if not str(key).startswith("System_")
            }
        conn = get_connection()
        canonical_keys = {
            row["page_key"] for row in conn.execute(
                "SELECT json_extract(data_json, '$.page_key') AS page_key FROM entities "
                "WHERE json_extract(data_json, '$.page_key') IS NOT NULL"
            )
            if not str(row["page_key"]).startswith("System_")
        }
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

        outbox_counts = {
            row["status"]: row["count"]
            for row in conn.execute("SELECT status, COUNT(*) AS count FROM mutation_outbox GROUP BY status")
        }
        outbox_ok = outbox_counts.get("failed", 0) == 0
        checks.append(("Mutation Outbox", outbox_ok, json.dumps(outbox_counts, ensure_ascii=False, sort_keys=True)))

        terminal_jobs = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE status = 'failed' AND retries >= 3"
        ).fetchone()[0]
        queued_jobs = conn.execute("SELECT COUNT(*) FROM jobs WHERE status = 'queued'").fetchone()[0]
        awaiting_jobs = conn.execute("SELECT COUNT(*) FROM jobs WHERE status = 'awaiting_subagent'").fetchone()[0]
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
    for label, ok, detail in checks:
        lines.append(f"[{'OK' if ok else 'FAIL'}] {label}: {detail}")
        all_ok = all_ok and ok
    lines.append("")
    lines.append("Infrastructure Summary: healthy" if all_ok else "Infrastructure Summary: issues detected")
    lines.append(f"Semantic Readiness: {semantic_readiness['status']}")
    if semantic_readiness["issues"]:
        lines.append("Semantic issues: " + "; ".join(semantic_readiness["issues"]))
    if semantic_readiness["warnings"]:
        lines.append("Semantic warnings: " + "; ".join(semantic_readiness["warnings"]))
    lines.append(
        "Summary: infrastructure healthy; semantic readiness " + semantic_readiness["status"]
        if all_ok
        else "Summary: infrastructure issues detected; semantic readiness " + semantic_readiness["status"]
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

