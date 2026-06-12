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
    """Native Antigravity Agentic subagent orchestration."""
    config_path = get_extension_root() / "config.json"
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
    except Exception:
        config = {}
        
    target_dirs = [str((get_extension_root() / d).resolve()) for d in config.get("target_directories", [])]
    exclude_paths = config.get("exclude_paths", [])
    supported_exts = set(config.get("supported_extensions", [".md", ".txt"]))
    
    files_to_process = []
    for target_dir in target_dirs:
        folder = Path(target_dir)
        if not folder.exists(): continue
        for root, dirs, files in os.walk(folder):
            dirs[:] = [d for d in dirs if not d.startswith('.')]
            for file in files:
                if file.startswith('~') or file.startswith('.'): continue
                
                filepath = os.path.join(root, file)
                path_str = filepath.replace("\\", "/")
                if any(exclude in path_str for exclude in exclude_paths):
                    continue

                if os.path.splitext(file)[1].lower() in supported_exts:
                    files_to_process.append(filepath)
                    
    processed = get_processed_files()
    pending_files = []
    
    for filepath in files_to_process:
        file_hash = calculate_hash(filepath)
        if not file_hash: continue
        if filepath in processed and processed[filepath].get("hash") == file_hash:
            continue
        pending_files.append((filepath, file_hash))
        
    if not pending_files:
        return "No new files to ingest. System is fully synced."

    pending_files = pending_files[:batch_size]
    
    tmp_dir = get_extension_root() / "tmp"
    tmp_dir.mkdir(exist_ok=True)
    
    schema_content = ""
    try:
        schema_content = (get_extension_root() / "schema.md").read_text(encoding="utf-8")
    except Exception: pass
    
    index_summary = _read_index_summary()
    
    subagent_prompts = []
    
    for i, (filepath, file_hash) in enumerate(pending_files):
        task_id = uuid.uuid4().hex[:8]
        task_file = tmp_dir / f"ingest_task_{task_id}.md"
        
        canonical_name = canonical_source_name(filepath)
        
        instructions = f"""You are the Vector Lake Ingestion Engine.
Your task is to ingest a raw source file into the Knowledge Graph (Wiki).

Source Path: {filepath}
File Hash: {file_hash}
Canonical Name: {canonical_name}

Wiki Rules & Schema:
{schema_content}

Existing Index Summary:
{index_summary}

Task:
1. Read the Source Path content using `view_file`.
2. Extract the core entities, concepts, and tensions based on the Schema.
3. Call the lazy MCP tool using `call_mcp_tool` (ServerName="vector-lake-mcp", ToolName="finalize_ingest"). Pass the formatted JSON array of new wiki nodes to `files_written_str`, and `{"filepath": "<filepath>", "hash": "<file_hash>"}` to `raw_files_processed_json`.
"""
        task_file.write_text(instructions, encoding="utf-8")
        
        subagent_prompts.append(f"""{{
    "TypeName": "vector-lake-ingestor",
    "Role": "Vector Lake Ingestor Task {i+1}",
    "Prompt": "Read instructions from `{task_file}` and execute the ingestion. This file hash is {file_hash} and path is {filepath}."
}}""")
        
    response = "I have found pending files to ingest. Please use the `invoke_subagent` tool with the following Subagents array to execute them natively in parallel:\\n\\n[\\n"
    response += ",\\n".join(subagent_prompts)
    response += "\\n]\\n\\n"
    response += "After invoking, you must STOP CALLING TOOLS and wait for them to finish."
    return response

def finalize_ingest(files_written_str: str, raw_files_processed_json: str) -> str:
    """Finalizes an ingest operation from a subagent."""
    try:
        files = json.loads(files_written_str)
        wiki_dir = get_wiki_dir()
        
        files_written = []
        for item in files:
            fname = item["filename"]
            fcontent = item["content"]
            out_path = wiki_dir / fname
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(fcontent)
            files_written.append(str(out_path))
            
        if files_written:
            governance_store.sync_pages_to_canonical(
                files_written,
                origin="ingest-subagent",
                auto_approve=True,
                summary=f"Subagent ingest sync for {len(files_written)} page(s)"
            )
            
        processed_data = json.loads(raw_files_processed_json)
        filepath = processed_data["filepath"]
        file_hash = processed_data["hash"]
        
        now = datetime.now(timezone.utc).isoformat()
        mark_file_processed(filepath, file_hash, now)
        
        return f"Successfully finalized ingestion for {filepath}."
    except Exception as e:
        return f"Error finalizing ingestion: {e}"
