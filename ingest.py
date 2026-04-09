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


def _read_purpose() -> str:
    """Read purpose.md for LLM context injection."""
    purpose_path = MEMORY_DIR / "purpose.md"
    try:
        return purpose_path.read_text(encoding="utf-8")
    except Exception:
        return ""


def _read_overview() -> str:
    """Read current wiki overview for context."""
    overview_path = WIKI_DIR / "overview.md"
    try:
        return overview_path.read_text(encoding="utf-8")
    except Exception:
        return ""


def _read_index_summary() -> str:
    """Read a compact summary of index.json for existing content awareness."""
    index_path = WIKI_DIR / "index.json"
    try:
        with open(index_path, "r", encoding="utf-8") as f:
            index_data = json.load(f)
        nodes = index_data.get("nodes", {})
        if not nodes:
            return ""
        lines = []
        for key, node in list(nodes.items())[:100]:  # Cap at 100 entries
            title = node.get("title", key)
            ntype = node.get("type", "?")
            summary = (node.get("summary", "") or "")[:80]
            lines.append(f"- [{ntype}] {title}: {summary}")
        return "\n".join(lines)
    except Exception:
        return ""


def process_file_batch(filepaths: list, existing_source_map: dict = None):
    """Two-step Chain-of-Thought ingest pipeline.
    
    Step 1 (Analysis): LLM reads source → structured analysis of entities, concepts,
                       arguments, connections, contradictions.
    Step 2 (Generation): LLM takes analysis → generates wiki files + review items.
    """
    if not filepaths: return False

    log.info(f"[2-Step CoT] Ingesting batch of {len(filepaths)} files...")
    
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

    # Backup wiki targets before agent write
    _backup_wiki_targets(WIKI_DIR, file_entries)

    # Load schema, categories, purpose, index summary, overview
    schema_content = ""
    try:
        schema_content = (EXTENSION_ROOT / "schema.md").read_text(encoding="utf-8")
    except Exception:
        pass
    categories_content = ""
    try:
        categories_content = (EXTENSION_ROOT / "SCHEMA_CATEGORIES.md").read_text(encoding="utf-8")
    except Exception:
        pass
    
    purpose_content = _read_purpose()
    index_summary = _read_index_summary()
    overview_content = _read_overview()

    # ── Step 1: Analysis ──────────────────────────────────────────
    # LLM reads sources and produces structured analysis:
    # key entities, concepts, arguments, connections to existing wiki, contradictions
    log.info("[Step 1/2] Running analysis pass...")

    analysis_prompt = f"""@vector-lake-ingestor
[STEP 1 OF 2 — ANALYSIS ONLY. DO NOT WRITE ANY FILES.]

Read the following source files and produce a **structured analysis** in Chinese.
Do NOT create or modify any wiki files in this step. Only output your analysis text.

Source Files:
{file_list_str}

Your analysis MUST cover these sections:

## 关键实体 (Key Entities)
List people, organizations, products, datasets, tools mentioned. For each:
- Name and type (Entity/Person/System)
- Role in the source (central vs. peripheral)
- Whether it likely already exists in the wiki (check the index below)

## 关键概念 (Key Concepts)
List theories, methods, techniques, phenomena. For each:
- Name and brief definition
- Why it matters in this source

## 核心论点与发现 (Main Arguments & Findings)
- What are the core claims or results?
- What evidence supports them?

## 与现有知识库的联系 (Connections to Existing Wiki)
- What existing pages does this source relate to?
- Does it strengthen, challenge, or extend existing knowledge?

## 矛盾与张力 (Contradictions & Tensions)
- Does anything conflict with existing wiki content?
- Are there internal tensions or caveats?

## 建议 (Recommendations)
- What wiki pages should be created or updated?
- Any open questions worth flagging for the user?

Be thorough but concise. Focus on what's genuinely important.

{f"--- PURPOSE (Wiki 目标) ---{chr(10)}{purpose_content}" if purpose_content else ""}

{f"--- EXISTING WIKI INDEX (检查现有内容) ---{chr(10)}{index_summary}" if index_summary else ""}
"""

    gemini_exec = "gemini.cmd" if os.name == "nt" else "gemini"
    analysis_result = ""
    
    try:
        log.info("Waiting for Analysis Agent (Step 1)...")
        result = subprocess.run(
            [gemini_exec, "--prompt", "", "--approval-mode", "yolo"],
            input=analysis_prompt.encode('utf-8'),
            capture_output=True, timeout=900
        )
        analysis_result = result.stdout.decode('utf-8', errors='replace')
        if result.returncode != 0:
            log.warning("Analysis step returned non-zero, proceeding with available output.")
    except Exception as e:
        log.error(f"Analysis step failed: {e}")
        # Fall back to single-step if analysis fails
        analysis_result = "(Analysis unavailable — falling back to direct generation)"

    log.info(f"[Step 1/2] Analysis complete ({len(analysis_result)} chars).")

    # ── Step 2: Generation ────────────────────────────────────────
    # LLM takes analysis → generates wiki files + review items + overview update
    log.info("[Step 2/2] Running generation pass...")

    generation_prompt = f"""@vector-lake-ingestor
[STEP 2 OF 2 — GENERATION. NOW WRITE FILES.]

Based on the following analysis, compile the source files into the Wiki directory (`{WIKI_DIR}`).

Source Files (with MANDATORY target filenames):
{file_list_str}

--- SOURCE ANALYSIS (from Step 1) ---
{analysis_result[:20000]}

--- CRITICAL DEDUP RULES ---
1. You MUST use the exact "Target Source Page" filename specified above for each Source page. DO NOT invent your own filename.
2. If the action is "UPDATE", the file already exists. Read it first, then update its content with new insights while preserving existing links.
3. If the action is "CREATE", create a new file with the exact specified filename.
4. NEVER create multiple Source pages for the same raw file.
5. Use the exact "YAML sources field" value provided above in the frontmatter `sources:` array.

--- SCHEMA ---
{schema_content}

--- CATEGORIES ---
{categories_content}

{f"--- PURPOSE (对齐目标) ---{chr(10)}{purpose_content}" if purpose_content else ""}

--- ADDITIONAL REQUIREMENTS ---

### overview.md 更新（必须）
After writing entity/concept/source pages, you MUST also update `wiki/overview.md`.
This file is a 2-5 paragraph high-level summary of ALL topics in the wiki (not just this batch).
{f"Current overview:{chr(10)}{overview_content}" if overview_content else "Create a new overview.md if it does not exist."}

### Review Items（矛盾/空白/建议）
After writing wiki files, if you identified contradictions, duplicates, knowledge gaps, or
research suggestions in the analysis, output REVIEW blocks in this exact format:

---REVIEW: type | Title---
Description of what needs the user's attention.
SEARCH: search query 1 | search query 2
PAGES: wiki/page1.md, wiki/page2.md
---END REVIEW---

Valid types: contradiction, duplicate, missing-page, suggestion
Only create reviews for things that genuinely need human input.

Please begin extraction and node weaving.
"""

    try:
        log.info("Waiting for Generation Agent (Step 2)...")
        cmd = [gemini_exec, "--prompt", "", "--approval-mode", "yolo"]
        result = subprocess.run(cmd, input=generation_prompt.encode('utf-8'), capture_output=True, timeout=1800)
        
        stdout_str = result.stdout.decode('utf-8', errors='replace')
        if stdout_str: print(stdout_str)
        success = (result.returncode == 0)
    except Exception as e:
        log.error(f"Gemini CLI failed for generation step: {e}")
        success = False

    if not success:
        log.error("Generation Agent failed or crashed.")
        return False
    
    # ── Step 3: Parse review items from agent output ──────────────
    if stdout_str:
        try:
            import review
            review_items = review.parse_review_blocks(stdout_str, str(filepaths))
            if review_items:
                review.add_items(review_items)
                log.info(f"Captured {len(review_items)} review item(s) from agent output.")
        except Exception as e:
            log.warning(f"Failed to parse review items: {e}")
            
    # ── Step 4: Post-processing ───────────────────────────────────
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
    log.info("Starting Native Agent Ingest Sync (2-Step CoT)...")
    
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
    batch_size = 5 # Decreased batch size
    
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
