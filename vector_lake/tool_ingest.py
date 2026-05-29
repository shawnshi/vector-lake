import os
import json
import hashlib
import logging
import uuid
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

def _normalize_raw_ref(raw_ref: str) -> str:
    return normalize_raw_ref(raw_ref)

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
    purpose_path = get_memory_dir() / "purpose.md"
    try:
        return purpose_path.read_text(encoding="utf-8")
    except Exception:
        return ""

def _read_overview() -> str:
    overview_path = get_wiki_dir() / "overview.md"
    try:
        return overview_path.read_text(encoding="utf-8")
    except Exception:
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
    try:
        from vector_lake import governance_store
        entities = governance_store.load_entities().get("items", {})
        if not entities:
            return ""
        lines = []
        for entity in list(entities.values())[:50]:
            name = entity.get("canonical_name")
            aliases = entity.get("aliases", [])
            if name and aliases:
                lines.append(f"- {name} (Aliases: {', '.join(aliases)})")
            elif name:
                lines.append(f"- {name}")
        return "\n".join(lines)
    except Exception:
        return ""

def prepare_ingest_batch(batch_size: int = 5) -> str:
    """Scan for unprocessed raw sources and prepare subagent ingestion instructions.
    
    Args:
        batch_size: Number of files to process in this batch.
    """
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
        if len(pending_files) >= batch_size:
            break
            
    if not pending_files:
        return "No new files to ingest. System is fully synced."
        
    # Prepare Context
    root_dir = str(get_extension_root().parent.parent.resolve())
    schema_content = ""
    try:
        schema_content = (get_extension_root() / "schema.md").read_text(encoding="utf-8")
    except Exception: pass
    
    categories_content = ""
    try:
        categories_content = (get_extension_root() / "SCHEMA_CATEGORIES.md").read_text(encoding="utf-8")
    except Exception: pass
    
    shared_context = {
        'schema_content': schema_content,
        'categories_content': categories_content,
        'purpose_content': _read_purpose(),
        'index_summary': _read_index_summary(),
        'entity_dict': _read_entity_dictionary(),
        'overview_content': _read_overview(),
    }
    
    tmp_dir = get_extension_root() / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    
    instructions = []
    
    for filepath, file_hash in pending_files:
        try:
            with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
        except Exception:
            continue
            
        try:
            rel_p = os.path.relpath(filepath, root_dir)
        except ValueError:
            rel_p = filepath
            
        try:
            raw_ref = os.path.relpath(filepath, str(get_memory_dir())).replace("\\", "/")
        except ValueError:
            raw_ref = rel_p.replace("\\", "/")
            
        canonical_name = canonical_source_name(filepath)
        action = "CREATE or UPDATE"
        
        file_list_str = (
            f"- Source: `{rel_p}`\n"
            f"  Target Source Page: `{canonical_name}` ({action})\n"
            f"  YAML sources field: [\"{raw_ref}\"]"
        )
        
        instruction_md = f"""# Ingestion Task
You have been invoked to ingest the following raw source file into the Vector Lake Wiki.

## Target
{file_list_str}

## Source Content
{content}

## Wiki Rules & Context
- Schema:
{shared_context['schema_content']}

- Existing Entities Dictionary:
{shared_context['entity_dict']}

- Index Summary:
{shared_context['index_summary']}

## Strict Two-Step CoT Ingestion (Phase 2)
You MUST execute this task in exactly two distinct steps. DO NOT write Wiki pages before completing Step 1.

### Step 1: Tension Parser & Analysis (Analysis Buffer)
1. Deeply analyze the Source Content against the existing Context (Entities Dictionary & Index Summary).
2. Explicitly write down your analysis to a temporary file `{tmp_dir.as_posix()}/analysis_{task_id}.json`. This file must contain:
   - `tensions`: Array of contradictions with existing knowledge.
   - `consensus`: Array of points that align perfectly.
   - `unknowns`: Array of new concepts/entities that need to be created.
3. BEFORE proceeding, you MUST call the `check_duplicate_entity` MCP tool for ANY new entities you identified.

### Step 2: Canonical Writer
1. Based strictly on the `analysis_{task_id}.json` you just created, write the `{canonical_name}` source page.
2. Write or update the related concept/entity pages natively using your file writing tools in `MEMORY/wiki/`.
3. Terminate only when files are successfully written.

File Hash tracking: {file_hash}
Raw Path: {filepath}
"""
        task_id = uuid.uuid4().hex[:8]
        tmp_file = tmp_dir / f"ingest_task_{task_id}.md"
        tmp_file.write_text(instruction_md, encoding="utf-8")
        
        instructions.append(
            f"Subagent Name: vector-lake-ingestor\n"
            f"Task: Read instructions from `{tmp_file}` and execute the ingestion. "
            f"This file hash is {file_hash} and path is {filepath}."
        )
        
    out = (
        f"Prepared {len(instructions)} files for ingestion.\n\n"
        "**ACTION REQUIRED:**\n"
        "Please use the `invoke_subagent` tool to spawn a `vector-lake-ingestor` subagent for each task below.\n"
        "When ALL subagents have completed their work, collect the list of all absolute paths they modified, "
        "and pass them, along with the raw file paths and hashes, to the `finalize_ingest` tool to sync the governance store and mark them as processed.\n\n"
    )
    for i, inst in enumerate(instructions):
        out += f"### Task {i+1}\n{inst}\n\n"
        
    return out

def finalize_ingest(files_written_str: str, raw_files_processed_json: str) -> str:
    """Finalize ingestion batch.
    
    Args:
        files_written_str: Comma-separated list of modified wiki files.
        raw_files_processed_json: JSON string mapping raw file paths to their hashes, e.g. '{"/path/to/raw.md": "hash123"}'.
    """
    files_written = [f.strip() for f in files_written_str.split(",") if f.strip()]
    if files_written:
        governance_store.sync_pages_to_canonical(
            files_written,
            origin="ingest-subagent",
            auto_approve=True,
            summary=f"Subagent ingest sync for {len(files_written)} page(s)",
        )
        
    try:
        processed_map = json.loads(raw_files_processed_json)
        now = datetime.now(timezone.utc).isoformat()
        for filepath, file_hash in processed_map.items():
            mark_file_processed(filepath, file_hash, now)
    except Exception as e:
        log.error(f"Failed to mark files as processed: {e}")
        
    return f"Ingestion finalized. Synced {len(files_written)} pages and marked {len(processed_map) if 'processed_map' in locals() else 0} raw files as processed."
