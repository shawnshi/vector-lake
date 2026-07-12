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
from vector_lake.db_store import get_processed_files, mark_file_processed
from vector_lake import governance_store
from vector_lake.skeleton_parser import parse_static_skeleton
from vector_lake.wiki_utils import (
    get_memory_dir,
    get_wiki_dir,
    get_index_path,
    normalize_raw_ref
)
from vector_lake.purpose_contract import (
    PurposeContractError,
    build_synthesis_proposals,
    load_purpose_contract,
    render_strategy_directive,
    validate_ingest_payload,
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
    try:
        return render_strategy_directive()
    except PurposeContractError as exc:
        log.error("Strategic purpose contract is unavailable: %s", exc)
        return "[STRATEGIC PURPOSE CONTRACT UNAVAILABLE: halt and repair purpose.md before ingesting.]"

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
                    
    from vector_lake.db_store import get_connection
    conn = get_connection()
    cur = conn.execute("SELECT filepath, file_hash, processed_at FROM processed_files")
    processed = {row["filepath"]: {"hash": row["file_hash"], "processed_at": row["processed_at"]} for row in cur.fetchall()}
    
    tmp_dir = get_extension_root() / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    processing_file = tmp_dir / "processing_files.json"
    from filelock import FileLock
    
    with FileLock(str(processing_file) + ".lock", timeout=10):
        try:
            with open(processing_file, "r") as f:
                currently_processing = json.load(f)
        except Exception:
            currently_processing = {}
            
        now_ts = datetime.now(timezone.utc).timestamp()
        currently_processing = {k: v for k, v in currently_processing.items() if now_ts - v < 3600}
        
        pending_files = []
        
        for filepath in files_to_process:
            try:
                stat = os.stat(filepath)
                mtime = stat.st_mtime
                
                if filepath in processed:
                    processed_at_str = processed[filepath]["processed_at"]
                    if processed_at_str:
                        processed_at_dt = datetime.fromisoformat(processed_at_str.replace("Z", "+00:00"))
                        if processed_at_dt.tzinfo is None:
                            processed_at_dt = processed_at_dt.replace(tzinfo=timezone.utc)
                        if mtime <= processed_at_dt.timestamp():
                            continue
                            
                    file_hash = calculate_hash(filepath)
                    if file_hash == processed[filepath]["hash"]:
                        continue
                else:
                    file_hash = calculate_hash(filepath)
                    
                if file_hash and file_hash not in currently_processing:
                    pending_files.append((filepath, file_hash))
            except OSError:
                pass
        
        if not pending_files:
                return "No new files to ingest. System is fully synced."

        pending_files = pending_files[:batch_size]
        for _, fh in pending_files:
                currently_processing[fh] = now_ts
                
        with open(processing_file, "w") as f:
                json.dump(currently_processing, f)
    
    schema_content = ""
    try:
        schema_content = (get_extension_root() / "schema.md").read_text(encoding="utf-8")
        cat_path = get_extension_root() / "SCHEMA_CATEGORIES.md"
        if cat_path.exists():
            schema_content += "\n\n" + cat_path.read_text(encoding="utf-8")
    except Exception: pass
    
    index_summary = _read_index_summary()
    purpose_content = _read_purpose()
    
    subagent_prompts = []
    
    from vector_lake.db_store import enqueue_job
    enqueued_count = 0
    
    for i, (filepath, file_hash) in enumerate(pending_files):
        canonical_name = canonical_source_name(filepath)
        skeleton_block = parse_static_skeleton(filepath)
        
        templates_dir = get_extension_root() / "templates"
        prompt_path = templates_dir / "ingest_prompt.md"
        if prompt_path.exists():
            prompt_template = prompt_path.read_text(encoding="utf-8")
        else:
            prompt_template = "Error: templates/ingest_prompt.md not found."
            
        instructions = prompt_template.replace("{{filepath}}", str(filepath)) \
            .replace("{{file_hash}}", file_hash) \
            .replace("{{canonical_name}}", canonical_name) \
            .replace("{{skeleton_block}}", skeleton_block) \
            .replace("{{schema_content}}", schema_content) \
            .replace("{{index_summary}}", index_summary) \
            .replace("{{purpose_content}}", purpose_content)

        payload = {
            "filepath": filepath,
            "hash": file_hash,
            "canonical_name": canonical_name,
            "instructions": instructions
        }
        
        enqueue_job("ingest", payload)
        enqueued_count += 1
        
    return f"Successfully enqueued {enqueued_count} files for ingestion."

def finalize_ingest(files_written: list, processed_data: dict) -> str:
    """Finalizes an ingest operation from a subagent using direct data."""
    try:
        from vector_lake.wiki_utils import safe_write_markdown, SafeWriteError
        files = files_written
        contract = load_purpose_contract()
        node_records = validate_ingest_payload(files, contract)
                
        wiki_dir = get_wiki_dir()
        
        files_written = []
        from vector_lake.mutation_coordinator import execute_mutation_plan
        for item in files:
            fname = os.path.basename(item["filename"])
            fcontent = item["content"]
            
            if "Concept_Decision_" in fname:
                lower_content = fcontent.lower()
                if not all(k in lower_content for k in ["context", "alternatives", "justification"]):
                    raise SafeWriteError(f"Decision nodes like {fname} MUST contain 'context', 'alternatives', and 'justification'.")
                    
            execute_mutation_plan(fname, content=fcontent, is_delete=False)
            files_written.append(str(wiki_dir / fname))
            
        from vector_lake.db_store import transaction
        
        try:
            with transaction():
                filepath = processed_data["filepath"]
                file_hash = processed_data["hash"]
                mark_file_processed(filepath, file_hash)
        except Exception as e:
            raise Exception(f"Ingest aborted during mark_file_processed. Error: {e}")
        
        from filelock import FileLock
        processing_file = get_extension_root() / "tmp" / "processing_files.json"
        try:
            with FileLock(str(processing_file) + ".lock", timeout=10):
                with open(processing_file, "r") as f:
                    currently_processing = json.load(f)
                if file_hash in currently_processing:
                    del currently_processing[file_hash]
                with open(processing_file, "w") as f:
                    json.dump(currently_processing, f)
        except Exception:
            pass
        
        proposal_count = 0
        if files_written:
            try:
                # Include existing nodes sharing the newly observed tension target,
                # so independently ingested sources can converge on one proposal.
                candidate_records = list(node_records)
                target_names = {
                    str(edge.get("target", "")).strip()
                    for record in node_records
                    for edge in record.get("tension_edges", [])
                    if isinstance(edge, dict) and str(edge.get("target", "")).strip()
                }
                if target_names:
                    with open(get_index_path(), "r", encoding="utf-8") as handle:
                        index_nodes = json.load(handle).get("nodes", {}).values()
                    for node in index_nodes:
                        edges = node.get("tension_edges", [])
                        if any(isinstance(edge, dict) and edge.get("target") in target_names for edge in edges):
                            candidate_records.append({
                                "filename": node.get("id", ""),
                                "sources": node.get("sources", []),
                                "tension_edges": edges,
                            })
                for proposal in build_synthesis_proposals(candidate_records, contract):
                    governance_store.enqueue_governance_item(
                        proposal["type"], proposal["title"], proposal["description"],
                        ", ".join(proposal["sources"]), proposal["search_queries"], proposal["affected_pages"],
                    )
                    proposal_count += 1
            except Exception as exc:
                log.warning("Ingest completed, but Synthesis-Proposal evaluation failed: %s", exc)

        suffix = f" Queued {proposal_count} Synthesis-Proposal(s)." if proposal_count else ""
        return f"Successfully finalized ingestion for {filepath}.{suffix}"
    except Exception as e:
        import traceback; return f"Error finalizing ingestion: {e}\n{traceback.format_exc()}"
