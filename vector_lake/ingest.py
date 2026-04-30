import os
import re
import json
import hashlib
import logging
import subprocess
from pathlib import Path
from datetime import datetime, timezone

from vector_lake import get_extension_root
from vector_lake.db import get_processed_files, mark_file_processed
from vector_lake import governance_store
from vector_lake.wiki_utils import backup_file, get_memory_dir, get_wiki_dir, normalize_raw_ref, normalize_sources, read_markdown_file, sanitize_wiki_node
import time

# Setup logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("vector-lake-ingest")

# Load config
CONFIG_PATH = get_extension_root() / "config.json"
with open(CONFIG_PATH, "r", encoding="utf-8") as f:
    config = json.load(f)

# Resolve paths
EXTENSION_ROOT = get_extension_root()
TARGET_DIRS = [str((EXTENSION_ROOT / d).resolve()) for d in config.get("target_directories", [])]
EXCLUDE_PATHS = config.get("exclude_paths", [])
WIKI_DIR = get_wiki_dir()
SCHEMA_PATH = EXTENSION_ROOT / "schema.md"
MEMORY_DIR = get_memory_dir()

SUPPORTED_EXTS = set(config.get("supported_extensions", [".md", ".txt"]))

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
    return normalize_raw_ref(raw_ref)

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
            frontmatter, _, _ = read_markdown_file(entry)
        except Exception:
            continue
        for src in normalize_sources(frontmatter.get("sources", [])):
            if src:
                if src not in mapping:
                    mapping[src] = entry.name
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
    backup_count = 0
    for entry in file_entries:
        if entry.get("action") == "UPDATE":
            target = os.path.join(str(wiki_dir), entry["target_source_file"])
            if os.path.exists(target):
                try:
                    backup_file(target)
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


def _read_entity_dictionary() -> str:
    """Read canonical identities and aliases to prevent entity drift."""
    try:
        from vector_lake import governance_store
        entities = governance_store.load_entities().get("items", {})
        if not entities:
            return ""
        lines = []
        for entity in entities.values():
            name = entity.get("canonical_name")
            aliases = entity.get("aliases", [])
            if name and aliases:
                lines.append(f"- {name} (Aliases: {', '.join(aliases)})")
            elif name:
                lines.append(f"- {name}")
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
    file_contents_for_prompt = []
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
        
        # Read file content to pass to agent
        content = ""
        try:
            with open(abs_p, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            # Add to content block for prompt
            file_contents_for_prompt.append(
                f"--- SOURCE FILE: {rel_p} ---\n"
                f"{content}\n"
                f"--- END SOURCE FILE ---\n"
            )
        except Exception as e:
            log.error(f"Could not read file {abs_p} for ingest: {e}")

        file_entries.append({
            "rel_path": rel_p,
            "raw_ref": raw_ref,
            "target_source_file": target_name,
            "action": action,
            "content": content
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
            f"  YAML sources field: [\"{safe_ref.replace('\"', '\\\"')}\"]"
        )
    file_list_str = "\n".join(file_list_lines)
    file_content_str = "\n".join(file_contents_for_prompt)

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
    entity_dict = _read_entity_dictionary()
    overview_content = _read_overview()

    # ── Step 1: Analysis ──────────────────────────────────────────
    # LLM reads sources and produces structured analysis:
    # key entities, concepts, arguments, connections to existing wiki, contradictions
    log.info("[Step 1/2] Running analysis pass...")

    analysis_prompt = f"""@vector-lake-ingestor
[STEP 1 OF 2 — ANALYSIS ONLY. DO NOT WRITE ANY FILES.]

Analyze the content of the source files provided below and produce a **structured analysis** in Chinese.
Do NOT create or modify any wiki files in this step. Only output your analysis text.

Source Files Information:
{file_list_str}

--- SOURCE FILE CONTENTS ---
{file_content_str}
--- END SOURCE FILE CONTENTS ---

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

{f"--- EXISTING ENTITY DICTIONARY (强制实体对齐) ---{chr(10)}{entity_dict}" if entity_dict else ""}
"""

    gemini_exec = "gemini.cmd" if os.name == "nt" else "gemini"
    analysis_result = ""
    
    llm_config = config.get("llm", {})
    model_cascade = llm_config.get("model_cascade", ["default", "gemini-3.1-flash"])
    timeout_analysis = llm_config.get("timeout_analysis", 120)
    retries = len(model_cascade)

    for attempt in range(retries):
        try:
            current_model = model_cascade[attempt]
            model_flag = [] if current_model in ("", "default") else ["-m", current_model]
            model_disp = current_model if current_model not in ("", "default") else "default"
            log.info(f"Waiting for Analysis Agent (Step 1) - Attempt {attempt+1}/{retries}... (Model: {model_disp})")
            cmd = [gemini_exec] + model_flag + ["-p", "You are an analysis agent.", "--approval-mode", "yolo"]
            result = subprocess.run(
                cmd,
                input=analysis_prompt.encode('utf-8'),
                capture_output=True, timeout=timeout_analysis
            )
            if result.returncode == 0:
                analysis_result = result.stdout.decode('utf-8', errors='replace')
                stderr_str = result.stderr.decode('utf-8', errors='replace')
                if "GaxiosError" in stderr_str or "RESOURCE_EXHAUSTED" in stderr_str or "rateLimitExceeded" in stderr_str:
                    log.warning(f"Analysis step attempt {attempt+1} encountered API error despite exit code 0.")
                elif len(analysis_result.strip()) > 50:
                    break
                else:
                    log.warning(f"Analysis step attempt {attempt+1} returned suspiciously empty output.")
            else:
                stderr = result.stderr.decode('utf-8', errors='replace').strip()
                log.warning(f"Analysis step attempt {attempt+1} returned non-zero: {stderr}")
        except subprocess.TimeoutExpired as e:
            log.warning(f"Analysis step attempt {attempt+1} timed out.")
        except Exception as e:
            log.error(f"Analysis step attempt {attempt+1} failed: {e}")
            
        if attempt < retries - 1:
            time.sleep(3)
            
    if not analysis_result:
        log.warning("Analysis step failed after retries, proceeding with available output.")
        analysis_result = "(Analysis unavailable — falling back to direct generation)"

    log.info(f"[Step 1/2] Analysis complete ({len(analysis_result)} chars).")

    # ── Step 2: Generation ────────────────────────────────────────
    # LLM takes analysis → generates wiki files + review items + overview update
    log.info("[Step 2/2] Running generation pass...")

    generation_prompt = f"""@vector-lake-ingestor
[STEP 2 OF 2 — GENERATION. NOW WRITE FILES.]

Based on the following analysis and original source content, compile the source files into the Wiki directory (`{WIKI_DIR}`).

Source Files (with MANDATORY target filenames):
{file_list_str}

--- SOURCE ANALYSIS (from Step 1) ---
{analysis_result[:20000]}

--- ORIGINAL SOURCE FILE CONTENTS ---
{file_content_str}
--- END ORIGINAL SOURCE FILE CONTENTS ---

--- CRITICAL DEDUP RULES ---
1. You MUST use the exact "Target Source Page" filename specified above for each Source page. DO NOT invent your own filename.
2. If the action is "UPDATE", the file already exists. Read it first, then update its content with new insights while preserving existing links.
3. If the action is "CREATE", create a new file with the exact specified filename.
4. NEVER create multiple Source pages for the same raw file.
5. Use the exact "YAML sources field" value provided above in the frontmatter `sources:` array.
6. ALWAYS check the EXISTING ENTITY DICTIONARY below. If an entity you are about to extract matches any Alias, you MUST normalize it to its Canonical Name. Do NOT create new pages for existing aliases.

--- SCHEMA ---
{schema_content}

--- CATEGORIES ---
{categories_content}

{f"--- PURPOSE (对齐目标) ---{chr(10)}{purpose_content}" if purpose_content else ""}

{f"--- EXISTING ENTITY DICTIONARY (强制实体对齐) ---{chr(10)}{entity_dict}" if entity_dict else ""}

--- ADDITIONAL REQUIREMENTS ---

[SYSTEM DIRECTIVE: PYTHON-LED I/O]
You are running in a restricted sandbox. DO NOT use the `write_file`, `replace`, or `run_shell_command` tools.
You MUST output the generated pages in the following plain text format exactly. My Python wrapper will handle the disk I/O.

---FILE: filename.md---
(yaml frontmatter)
(body content)
---END FILE---

### Anti-Drift Alignment Scoring (反漂移验证)
You MUST evaluate how closely each generated node (Entity, Concept, Source, Synthesis) aligns with `PURPOSE`.
1. Calculate an `alignment_score` from 0 to 100.
2. Add `alignment_score: [score]` to the YAML frontmatter.
3. If `alignment_score` < 60, you MUST set `status: "Contested"`. Do not set it to "Active".

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

    success = False
    stdout_str = ""
    llm_config = config.get("llm", {})
    model_cascade = llm_config.get("model_cascade", ["default", "gemini-3.1-flash", "gemini-3.1-8b"])
    timeout_generation = llm_config.get("timeout_generation", 180)
    retries = len(model_cascade)

    for attempt in range(retries):
        try:
            current_model = model_cascade[attempt]
            model_flag = [] if current_model in ("", "default") else ["-m", current_model]
            model_disp = current_model if current_model not in ("", "default") else "default"
            log.info(f"Waiting for Generation Agent (Step 2) - Attempt {attempt+1}/{retries}... (Model: {model_disp})")
            cmd = [gemini_exec] + model_flag + ["-p", "You are a generation agent.", "--approval-mode", "yolo"]
            result = subprocess.run(cmd, input=generation_prompt.encode('utf-8'), capture_output=True, timeout=timeout_generation)
            
            if result.returncode == 0:
                stdout_str = result.stdout.decode('utf-8', errors='replace')
                stderr_str = result.stderr.decode('utf-8', errors='replace')
                if "GaxiosError" in stderr_str or "RESOURCE_EXHAUSTED" in stderr_str or "rateLimitExceeded" in stderr_str:
                    log.warning(f"Generation Agent attempt {attempt+1} encountered API error despite exit code 0.")
                elif "---FILE:" in stdout_str or "---REVIEW:" in stdout_str or len(stdout_str.strip()) > 100:
                    if stdout_str: print(stdout_str)
                    success = True
                    break
                else:
                    log.warning(f"Generation Agent attempt {attempt+1} returned suspiciously empty output without valid structures.")
            else:
                stderr = result.stderr.decode('utf-8', errors='replace').strip()
                log.warning(f"Generation Agent attempt {attempt+1} failed: {stderr}")
        except subprocess.TimeoutExpired as e:
            log.warning(f"Generation Agent attempt {attempt+1} timed out.")
        except Exception as e:
            log.error(f"Gemini CLI failed for generation step: {e}")
            
        if attempt < retries - 1:
            time.sleep(3)

    if not success:
        log.error("Generation Agent failed or crashed after retries.")
        return False
    
    # ── Step 3: Parse review items from agent output ──────────────
    if stdout_str:
        try:
            import re
            from vector_lake import governance_store
            
            REVIEW_BLOCK_REGEX = re.compile(r'---REVIEW:\s*(\w[\w-]*)\s*\|\s*(.+?)\s*---\n([\s\S]*?)---END REVIEW---')
            VALID_TYPES = {"contradiction", "duplicate", "missing-page", "suggestion"}
            
            review_items = []
            for match in REVIEW_BLOCK_REGEX.finditer(stdout_str):
                raw_type = match.group(1).strip().lower()
                title = match.group(2).strip()
                body = match.group(3).strip()

                review_type = raw_type if raw_type in VALID_TYPES else "suggestion"

                search_match = re.search(r'^SEARCH:\s*(.+)$', body, re.MULTILINE)
                search_queries = [q.strip() for q in search_match.group(1).split("|") if q.strip()] if search_match else []

                pages_match = re.search(r'^PAGES:\s*(.+)$', body, re.MULTILINE)
                affected_pages = [p.strip() for p in pages_match.group(1).split(",") if p.strip()] if pages_match else []

                description = body
                description = re.sub(r'^SEARCH:.*$', '', description, flags=re.MULTILINE)
                description = re.sub(r'^PAGES:.*$', '', description, flags=re.MULTILINE)
                description = description.strip()

                review_items.append({
                    "item_type": review_type,
                    "title": title,
                    "description": description,
                    "source": str(filepaths),
                    "search_queries": search_queries,
                    "affected_pages": affected_pages
                })
                
            if review_items:
                for item in review_items:
                    governance_store.enqueue_governance_item(**item)
                log.info(f"Captured {len(review_items)} review item(s) from agent output.")
        except Exception as e:
            log.warning(f"Failed to parse review items: {e}")
            
    # ── Step 4: Post-processing (Python-Led I/O) ──────────────────
    changed_files = []
    if stdout_str:
        from vector_lake.wiki_utils import atomic_write_text
        file_blocks = re.finditer(r"---FILE:\s*([^\n]+)---\n(.*?)\n---END FILE---", stdout_str, re.DOTALL)
        for match in file_blocks:
            filename = match.group(1).strip()
            content = match.group(2).strip()
            
            if not filename.startswith(("Source_", "Entity_", "Concept_", "Synthesis_")) and filename != "overview.md":
                log.warning(f"Intercepted illegal write attempt to {filename}")
                continue
                
            file_path = os.path.join(WIKI_DIR, filename)
            try:
                atomic_write_text(Path(file_path), content)
                changed_files.append(file_path)
            except Exception as e:
                log.error(f"Failed to write {filename}: {e}")
                    
    if changed_files:
        for p in changed_files:
            sanitize_wiki_node(p)
        governance_store.sync_pages_to_canonical(
            changed_files,
            origin="ingest",
            auto_approve=True,
            summary=f"Ingest sync for {len(changed_files)} page(s)",
        )
        log.info(f"Agent modified and sanitized {len(changed_files)} wiki files.")
    else:
        log.warning("Agent ran for batch but no wiki files were modified.")

    now = datetime.now(timezone.utc).isoformat()
    for fp in filepaths:
        if os.path.exists(fp):
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
                
                filepath = os.path.join(root, file)
                path_str = filepath.replace("\\", "/")
                if any(exclude in path_str for exclude in EXCLUDE_PATHS):
                    continue

                if os.path.splitext(file)[1].lower() in SUPPORTED_EXTS:
                    files_to_process.append(filepath)

    log.info(f"Scanned {len(files_to_process)} candidate raw sources.")

    processed = get_processed_files()
    existing_source_map = scan_existing_sources(WIKI_DIR)
    log.info(f"Cached {len(existing_source_map)} existing Source page mappings for dedup.")
    
    batch = []
    llm_config = config.get("llm", {})
    batch_size = llm_config.get("batch_size", 20)
    
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

