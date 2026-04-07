import os
import re
import json
import hashlib
import logging
import subprocess
from pathlib import Path
from datetime import datetime, timezone

from db import get_processed_files, mark_file_processed

# Setup logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("vector-lake-ingest")

# Load config
CONFIG_PATH = Path(__file__).parent / "config.json"
with open(CONFIG_PATH, "r", encoding="utf-8") as f:
    config = json.load(f)

# Resolve paths
EXTENSION_ROOT = Path(__file__).parent
TARGET_DIRS = [str((EXTENSION_ROOT / d).resolve()) for d in config.get("target_directories", [])]
WIKI_DIR = (EXTENSION_ROOT / "../../MEMORY/wiki").resolve()
SCHEMA_PATH = EXTENSION_ROOT / "schema.md"
MEMORY_DIR = WIKI_DIR.parent  # .gemini/MEMORY/

SUPPORTED_EXTS = { ".md", ".txt", ".png", ".jpeg", ".pdf" }

def canonical_source_name(raw_path: str) -> str:
    """Deterministically derive a Source wiki filename from a raw file path.
    Example: 'raw/article/白皮书20260404.md' -> 'Source_白皮书20260404.md'
    """
    basename = Path(raw_path).stem
    return f"Source_{basename}.md"

def _normalize_raw_ref(raw_ref: str) -> str:
    """Normalize a raw reference path for consistent matching.
    Strips leading MEMORY/ prefix, normalizes slashes.
    """
    normalized = raw_ref.replace("\\", "/").strip()
    # Remove leading MEMORY/ if present
    if normalized.startswith("MEMORY/"):
        normalized = normalized[len("MEMORY/"):]
    return normalized

def scan_existing_sources(wiki_dir) -> dict:
    """Scan wiki/ for all Source_*.md files and build raw_path -> source_filename mapping.
    Returns: { 'raw/article/file.md': 'Source_File.md', ... }
    """
    import re
    mapping = {}
    wiki_path = Path(wiki_dir)
    if not wiki_path.exists():
        return mapping
    for entry in wiki_path.iterdir():
        if not entry.is_file() or not entry.name.startswith("Source_") or not entry.name.endswith(".md"):
            continue
        try:
            content = entry.read_text(encoding="utf-8")
        except Exception:
            continue
        # Parse YAML frontmatter
        match = re.search(r"^---\s*\n(.*?)\n---", content, re.DOTALL)
        if not match:
            continue
        yaml_block = match.group(1)
        src_match = re.search(r"sources:\s*\[(.*?)\]", yaml_block, re.DOTALL)
        if not src_match:
            continue
        sources_str = src_match.group(1)
        sources = [s.strip().strip('"').strip("'") for s in sources_str.split(",")]
        for src in sources:
            if src:
                normalized = _normalize_raw_ref(src)
                if normalized not in mapping:
                    mapping[normalized] = entry.name
    return mapping

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


def _sanitize_for_prompt(text: str) -> str:
    """Sanitize text before embedding in LLM prompt to prevent injection."""
    # Strip characters that could be used for prompt injection
    text = text.replace('`', '')       # Prevent code block escapes
    text = text.replace('@', '_at_')   # Prevent @mention hijacking
    text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', text)  # Strip control chars
    # Truncate excessively long paths
    if len(text) > 500:
        text = text[:500] + '...'
    return text


def _backup_wiki_targets(wiki_dir, file_entries: list):
    """Create .bak snapshots of existing wiki files that may be modified.
    Enables rollback if the Ingestor Agent produces bad writes.
    """
    import shutil
    backup_count = 0
    for entry in file_entries:
        if entry.get("action") == "UPDATE":
            target = os.path.join(str(wiki_dir), entry["target_source_file"])
            if os.path.exists(target):
                bak_path = target + ".bak"
                try:
                    shutil.copy2(target, bak_path)
                    backup_count += 1
                except Exception as e:
                    log.warning(f"Failed to backup {target}: {e}")
    if backup_count > 0:
        log.info(f"Created {backup_count} .bak snapshots before agent write.")


def process_file_batch(filepaths: list, existing_source_map: dict = None):
    if not filepaths: return False

    log.info(f"Delegating ingestion of a batch of {len(filepaths)} files to Vector Lake Ingestor Agent...")
    
    before_mtimes = {}
    if os.path.exists(WIKI_DIR):
        for entry in os.scandir(WIKI_DIR):
            if entry.is_file() and entry.name.endswith('.md'):
                before_mtimes[entry.path] = entry.stat().st_mtime

    # Use relative paths to avoid encoding issues with absolute paths in the prompt
    root_dir = str(EXTENSION_ROOT.parent.parent.resolve())
    rel_filepaths = []
    for p in filepaths:
        try:
            rel_filepaths.append(os.path.relpath(p, root_dir))
        except ValueError:
            rel_filepaths.append(p)
            
    # --- DEDUP: Plan A - Scan existing Source pages ---
    if existing_source_map is None:
        existing_source_map = scan_existing_sources(WIKI_DIR)
        log.info(f"Scanned {len(existing_source_map)} existing Source page mappings for dedup.")

    # --- DEDUP: Plan B - Compute canonical target filenames ---
    file_entries = []
    for abs_p, rel_p in zip(filepaths, rel_filepaths):
        # Compute the raw ref path as it appears in YAML sources: field (relative to MEMORY/)
        try:
            raw_ref = os.path.relpath(abs_p, str(MEMORY_DIR)).replace("\\", "/")
        except ValueError:
            raw_ref = rel_p.replace("\\", "/")
        
        normalized_ref = _normalize_raw_ref(raw_ref)
        canonical_name = canonical_source_name(abs_p)
        
        # Check if an existing Source page already covers this raw file
        existing_name = existing_source_map.get(normalized_ref)
        target_name = existing_name if existing_name else canonical_name
        action = "UPDATE" if existing_name else "CREATE"
        
        file_entries.append({
            "rel_path": rel_p,
            "raw_ref": raw_ref,
            "target_source_file": target_name,
            "action": action,
        })
    
    # Build structured file list with explicit target filenames (sanitized)
    file_list_lines = []
    for entry in file_entries:
        safe_rel = _sanitize_for_prompt(entry['rel_path'])
        safe_target = _sanitize_for_prompt(entry['target_source_file'])
        safe_ref = _sanitize_for_prompt(entry['raw_ref'])
        file_list_lines.append(
            f"- Source: `{safe_rel}`\n"
            f"  Target Source Page: `{safe_target}` ({entry['action']})\n"
            f"  YAML sources field: [\"{safe_ref}\"]"
        )
    file_list_str = "\n".join(file_list_lines)

    # P3-13: Backup wiki targets before agent write
    _backup_wiki_targets(WIKI_DIR, file_entries)

    schema_path = EXTENSION_ROOT / "schema.md"
    try:
        with open(str(schema_path), "r", encoding="utf-8") as f:
            schema_content = f.read()
    except Exception:
        schema_content = ""
    categories_path = EXTENSION_ROOT / "SCHEMA_CATEGORIES.md"
    try:
        with open(str(categories_path), "r", encoding="utf-8") as f:
            categories_content = f.read()
    except Exception:
        categories_content = ""

    prompt = f"""
@vector-lake-ingestor
[BATCH INGEST PROCESS EXECUTED]
Please compile the following raw source files into the Wiki directory (`{WIKI_DIR}`):

Source Files (with MANDATORY target filenames):
{file_list_str}

--- CRITICAL DEDUP RULES ---
1. You MUST use the exact "Target Source Page" filename specified above for each Source page. DO NOT invent your own filename.
2. If the action is "UPDATE", the file already exists. Read it first, then update its content with new insights while preserving existing links.
3. If the action is "CREATE", create a new file with the exact specified filename.
4. NEVER create multiple Source pages for the same raw file.
5. Use the exact "YAML sources field" value provided above in the frontmatter `sources:` array.

Please strictly adhere to the following schemas and categories when extracting and node weaving:

--- SCHEMA ---
{schema_content}

--- CATEGORIES ---
{categories_content}

Please begin extraction and node weaving.
"""

    gemini_exec = "gemini.cmd" if os.name == "nt" else "gemini"
    try:
        cmd = [gemini_exec, "--prompt", "", "--approval-mode", "yolo"]
        print("Waiting for Ingestor Agent to process the batch (this may take 1~2 minutes)...", flush=True)
        result = subprocess.run(cmd, input=prompt, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', timeout=300)
        
        if result.stdout: print(result.stdout)
        success = (result.returncode == 0)
    except Exception as e:
        log.error(f"Gemini CLI failed for batch: {e}")
        success = False

    if not success:
        log.error("Ingestor Subagent failed or crashed during batch processing.")
        return False
            
    changed_files = []
    if os.path.exists(WIKI_DIR):
        for entry in os.scandir(WIKI_DIR):
            if entry.is_file() and entry.name.endswith('.md'):
                p = entry.path
                mtime = entry.stat().st_mtime
                if p not in before_mtimes or mtime > before_mtimes[p]:
                    changed_files.append(p)
                    
    if changed_files:
        import tools
        for p in changed_files:
            tools.sanitize_wiki_node(p)
        log.info(f"Agent modified and sanitized {len(changed_files)} wiki files.")
    else:
        log.warning("Agent ran for batch but no wiki files were modified.")

    now = datetime.now(timezone.utc).isoformat()
    for fp in filepaths:
        f_hash = calculate_hash(fp)
        if f_hash: mark_file_processed(fp, f_hash, now)
        
    return True

def sync_all():
    log.info("Starting Native Agent Ingest Sync...")
    
    files_to_process = []
    for target_dir in TARGET_DIRS:
        folder = Path(target_dir)
        if not folder.exists(): continue
        for root, dirs, files in os.walk(folder):
            dirs[:] = [d for d in dirs if not d.startswith('.')] # Ignore hidden directories
            for file in files:
                if file.startswith('~') or file.startswith('.'): continue # Ignore temporary/hidden files
                if os.path.splitext(file)[1].lower() in SUPPORTED_EXTS:
                    files_to_process.append(os.path.join(root, file))

    log.info(f"Scanned {len(files_to_process)} candidate raw sources.")

    processed = get_processed_files()
    existing_source_map = scan_existing_sources(WIKI_DIR)
    log.info(f"Cached {len(existing_source_map)} existing Source page mappings for dedup.")
    
    batch = []
    batch_size = 15 # Increased batch size
    
    for filepath in files_to_process:
        file_hash = calculate_hash(filepath)
        if not file_hash: continue
        if filepath in processed and processed[filepath].get("hash") == file_hash:
            continue
            
        batch.append(filepath)
        if len(batch) >= batch_size:
            process_file_batch(batch, existing_source_map)
            batch = []
            
    if batch:
        process_file_batch(batch, existing_source_map)
                    
    log.info("Ingest sync completed.")

if __name__ == "__main__":
    sync_all()
