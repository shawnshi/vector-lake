import os
import json
import hashlib
import logging
import uuid
import subprocess
import sys
from pathlib import Path
from datetime import datetime, timezone

from vector_lake import get_extension_root
from vector_lake.db import get_processed_files, mark_file_processed
from vector_lake import governance_store
from vector_lake.wiki_utils import (
    get_memory_dir,
    get_wiki_dir,
    get_index_path,
    normalize_raw_ref
)

log = logging.getLogger("vector-lake-ingest")

def canonical_source_name(raw_path: str) -> str:
    basename = Path(raw_path).stem
    return f"Source_{basename}.md"

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
    return ""

def _read_overview() -> str:
    return ""

def _read_index_summary() -> str:
    index_path = get_index_path()
    try:
        with open(index_path, "r", encoding="utf-8") as f:
            index_data = json.load(f)
        nodes = index_data.get("nodes", {})
        if not nodes:
            return ""
        lines = []
        for key, node in list(nodes.items())[:100]:
            title = node.get("title", key)
            ntype = node.get("type", "?")
            summary = (node.get("summary", "") or "")[:80]
            lines.append(f"- [{ntype}] {title}: {summary}")
        return "\n".join(lines)
    except Exception:
        return ""

def _read_entity_dictionary() -> str:
    return ""

def prepare_ingest_batch(batch_size: int = 5) -> str:
    """Async Queue approach to ingestion to prevent API EOF.
    This replaces the old subagent storm approach.
    """
    daemon_script = get_extension_root() / "scripts" / "ingest_daemon.py"
    
    if not daemon_script.exists():
        return "Error: ingest_daemon.py not found. Cannot launch async queue."
        
    try:
        # Launch asynchronously
        creation_flags = 0
        if sys.platform == 'win32':
            creation_flags = subprocess.CREATE_NO_WINDOW
        
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        
        subprocess.Popen(
            [sys.executable, str(daemon_script)], 
            env=env,
            creationflags=creation_flags
        )
        return "[Queue Task Accepted] Vector Lake Ingestion Daemon has been triggered in the background. It will process files sequentially to prevent API EOF crashes. You do NOT need to spawn any subagents. Execution will continue asynchronously."
    except Exception as e:
        return f"Failed to trigger daemon: {e}"

def finalize_ingest(files_written_str: str, raw_files_processed_json: str) -> str:
    """Legacy finalized method, kept for backwards compatibility if needed."""
    return "Ingestion is now managed completely by the background ingest daemon asynchronously."
