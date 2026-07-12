import importlib
import os
import shutil
import sys
import ast
import json
from pathlib import Path

from vector_lake import governance_store
from vector_lake.wiki_utils import get_index_path, get_memory_dir, get_raw_dir, get_wiki_dir, get_meta_dir
from vector_lake.db_store import get_db_path, get_connection
from vector_lake import get_extension_root

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

    # 1. Environment & Config
    python_ok = sys.version_info >= (3, 10)
    checks.append(("Python", python_ok, f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"))

    has_api_key = bool(os.environ.get("GEMINI_API_KEY"))
    checks.append(("GEMINI_API_KEY", has_api_key, "Set" if has_api_key else "Missing"))

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
        "jieba": "jieba",
        "mistune": "mistune"
    }
    for module_name, package_name in dependencies.items():
        try:
            importlib.import_module(module_name)
            checks.append((package_name, True, "installed"))
        except ImportError:
            checks.append((package_name, False, "missing"))
            
    # Check agy CLI
    agy_ok = shutil.which("agy") is not None
    checks.append(("agy CLI", agy_ok, "available" if agy_ok else "missing in PATH"))

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
        tools_count = len(mcp._tools) if hasattr(mcp, "_tools") else 0
        checks.append(("MCP Server", True, f"Import OK, {tools_count} tools exposed"))
    except Exception as e:
        checks.append(("MCP Server", False, f"Startup Exception: {e}"))

    # 6. Watchdog Heartbeat
    status_path = get_extension_root() / "tmp" / "watchdog_status.json"
    if status_path.exists():
        try:
            with open(status_path, "r", encoding="utf-8") as f:
                status = json.load(f)
            checks.append(("Watchdog Status", True, f"[{status.get('state', 'unknown')}] {status.get('message', '')}"))
        except Exception as e:
            checks.append(("Watchdog Status", False, f"Parse error: {e}"))
    else:
        checks.append(("Watchdog Status", False, "No status file found (not running?)"))

    # 7. State Projection Consistency
    try:
        # Wiki Files
        wiki_files_count = len([f for f in get_wiki_dir().glob("*.md") if f.is_file()])
        # JSON Index
        with open(get_index_path(), "r", encoding="utf-8") as f:
            index_nodes = len(json.load(f).get("nodes", {}))
        # SQLite
        conn = get_connection()
        sqlite_entities = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
        
        # Simple heuristic: if difference > 50, flag as inconsistent
        diff = max(abs(wiki_files_count - index_nodes), abs(wiki_files_count - sqlite_entities), abs(index_nodes - sqlite_entities))
        consistent = diff <= 50
        checks.append(("State Consistency", consistent, f"Wiki:{wiki_files_count} JSON:{index_nodes} SQLite:{sqlite_entities}"))
    except Exception as e:
        checks.append(("State Consistency", False, f"Check failed: {e}"))

    lines = ["=== Vector Lake Doctor ==="]
    all_ok = True
    for label, ok, detail in checks:
        lines.append(f"[{'OK' if ok else 'FAIL'}] {label}: {detail}")
        all_ok = all_ok and ok
    lines.append("")
    lines.append("Summary: healthy" if all_ok else "Summary: issues detected")
    lines.append(f"VECTOR_LAKE_MEMORY_DIR={os.environ.get('VECTOR_LAKE_MEMORY_DIR', '<default>')}")
    return "\n".join(lines)

