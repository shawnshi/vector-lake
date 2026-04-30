import importlib
import os
import shutil
import sys

from vector_lake import governance_store
from vector_lake.wiki_utils import get_index_path, get_memory_dir, get_raw_dir, get_wiki_dir
from vector_lake.wiki_utils import (
    get_claims_path,
    get_entities_path,
    get_evidence_path,
    get_memory_objects_path,
    get_meta_dir,
    get_sources_path,
)


def doctor_vector_lake() -> str:
    governance_store.initialize_meta_store()
    checks = []

    python_ok = sys.version_info >= (3, 10)
    checks.append(("Python", python_ok, f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"))

    gemini_exec = shutil.which("gemini.cmd") or shutil.which("gemini")
    checks.append(("Gemini CLI", bool(gemini_exec), gemini_exec or "not found"))

    for label, path in [("MEMORY", get_memory_dir()), ("Raw", get_raw_dir()), ("Wiki", get_wiki_dir())]:
        checks.append((label, path.exists(), str(path)))

    index_exists = get_index_path().exists()
    checks.append(("Index", True, str(get_index_path()) if index_exists else "Lake is drying (Empty)"))
    for label, path in [
        ("Meta", get_meta_dir()),
        ("Entities", get_entities_path()),
        ("Claims", get_claims_path()),
        ("Evidence", get_evidence_path()),
        ("Sources", get_sources_path()),
        ("Operational Memory", get_memory_objects_path()),
    ]:
        checks.append((label, path.exists(), str(path)))

    dependencies = {
        "filelock": "filelock",
        "yaml": "PyYAML",
        "watchdog": "watchdog",
        "networkx": "networkx",
        "community": "python-louvain",
        "dotenv": "python-dotenv",
    }
    for module_name, package_name in dependencies.items():
        try:
            importlib.import_module(module_name)
            checks.append((package_name, True, "installed"))
        except ImportError:
            checks.append((package_name, False, "missing"))

    lines = ["=== Vector Lake Doctor ==="]
    all_ok = True
    for label, ok, detail in checks:
        lines.append(f"[{'OK' if ok else 'FAIL'}] {label}: {detail}")
        all_ok = all_ok and ok
    lines.append("")
    lines.append("Summary: healthy" if all_ok else "Summary: issues detected")
    lines.append(f"VECTOR_LAKE_MEMORY_DIR={os.environ.get('VECTOR_LAKE_MEMORY_DIR', '<default>')}")
    return "\n".join(lines)

