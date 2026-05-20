import os
import re
import json
import math
import hashlib
import logging
import subprocess
import asyncio
from pathlib import Path
from datetime import datetime, timezone
from collections import Counter

from vector_lake import get_extension_root
from vector_lake.db import get_processed_files, mark_file_processed
from vector_lake import governance_store
from vector_lake.wiki_utils import backup_file, get_memory_dir, get_wiki_dir, normalize_raw_ref, normalize_sources, read_markdown_file, sanitize_wiki_node, atomic_write_text, get_index_path, normalize_entity_name
import time
import random

CIRCUIT_BREAKER = {}

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
    text = text.replace('`', '')
    text = text.replace('@', '_at_')
    text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', text)
    if len(text) > 500:
        text = text[:500] + '...'
    return text


def _backup_wiki_targets(wiki_dir, file_entries: list):
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
    purpose_path = MEMORY_DIR / "purpose.md"
    try:
        return purpose_path.read_text(encoding="utf-8")
    except Exception:
        return ""


def _read_overview() -> str:
    overview_path = WIKI_DIR / "overview.md"
    try:
        return overview_path.read_text(encoding="utf-8")
    except Exception:
        return ""


def _read_index_summary() -> str:
    index_path = WIKI_DIR / "index.json"
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


def _calculate_cosine_similarity(text1: str, text2: str) -> float:
    def get_tokens(text):
        tokens = Counter()
        text = text.lower()
        cjk_chars = re.findall(r"[\u4e00-\u9fff\u3400-\u4dbf]", text)
        for char in cjk_chars:
            tokens[char] += 1
        for i in range(len(cjk_chars) - 1):
            tokens[cjk_chars[i] + cjk_chars[i+1]] += 1
        latin_words = re.findall(r"[a-z0-9]+", text)
        for word in latin_words:
            tokens[word] += 2
        return tokens

    vec1 = get_tokens(text1)
    vec2 = get_tokens(text2)
    intersection = set(vec1.keys()) & set(vec2.keys())
    numerator = sum([vec1[x] * vec2[x] for x in intersection])
    sum1 = sum([vec1[x] ** 2 for x in vec1.keys()])
    sum2 = sum([vec2[x] ** 2 for x in vec2.keys()])
    denominator = math.sqrt(sum1) * math.sqrt(sum2)
    if not denominator:
        return 0.0
    return float(numerator) / denominator


def _normalize_memory_key(value: str) -> str:
    """Normalize a string by converting to lowercase and replacing non-alphanumeric/CJK chars with underscores."""
    import re
    normalized = re.sub(r"\s+", " ", str(value or "").strip().lower())
    normalized = re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "_", normalized)
    normalized = re.sub(r"_+", "_", normalized).strip("_")
    return normalized[:96] or "general"


def _piea_intercept(filename: str, content: str, index_data: dict) -> dict | None:
    """PIEA Hook: Intercept duplicate nodes using hard normalization and cosine similarity before writing."""
    try:
        m_type = re.search(r"^type:\s*(\w+)", content, re.MULTILINE)
        m_title = re.search(r"^title:\s*([^\n]+)", content, re.MULTILINE)
        m_summary = re.search(r"^summary:\s*([^\n]+)", content, re.MULTILINE)

        if not m_type or not m_title:
            return None

        candidate_type = m_type.group(1).strip().lower()
        candidate_title = m_title.group(1).strip().strip('"').strip("'")
        candidate_summary = m_summary.group(1).strip() if m_summary else candidate_title

        if candidate_type not in ("entity", "concept"):
            return None

        nodes = index_data.get("nodes", {})
        threshold = config.get("piea", {}).get("threshold", 0.92)

        candidate_norm = _normalize_memory_key(candidate_title)

        for key, node in nodes.items():
            if node.get("type") != candidate_type:
                continue

            existing_title = node.get("title", "").strip('"').strip("'")
            existing_norm = _normalize_memory_key(existing_title)

            # 1. Hard Normalization Match (Strategy A)
            if candidate_norm == existing_norm and candidate_norm != "general":
                return {
                    "existing_key": key,
                    "existing_title": existing_title,
                    "similarity": 1.0,
                    "match_type": "hard_normalization"
                }

            # 2. Fallback to Cosine Similarity
            existing_summary = node.get("summary") or existing_title
            sim = _calculate_cosine_similarity(candidate_summary, existing_summary)

            if sim >= threshold:
                return {
                    "existing_key": key,
                    "existing_title": existing_title,
                    "similarity": sim,
                    "match_type": "cosine_similarity"
                }
    except Exception as e:
        log.warning(f"PIEA interception failed for {filename}: {e}")

    return None

async def _async_call_gemini_cli(prompt: str, role_prompt: str, model_cascade: list, timeout_sec: int, semaphore: asyncio.Semaphore, step_name: str) -> str:
    from google import genai
    from google.genai import types
    from google.genai.errors import APIError

    retries = len(model_cascade)
    client = genai.Client()
    
    async with semaphore:
        for attempt in range(retries):
            current_model = model_cascade[attempt]
            if current_model in ("", "default"):
                current_model = "gemini-2.5-pro"
            
            # Check Circuit Breaker
            if current_model in CIRCUIT_BREAKER:
                cooldown_until = CIRCUIT_BREAKER[current_model]
                if time.time() < cooldown_until:
                    log.warning(f"[{step_name}] Model '{current_model}' is in cooldown (circuit broken). Skipping...")
                    continue
                else:
                    del CIRCUIT_BREAKER[current_model] # Cooldown expired

            log.info(f"[{step_name}] Waiting for Agent - Attempt {attempt+1}/{retries}... (Model: {current_model})")
            
            try:
                # Use asyncio.wait_for to enforce timeout
                response = await asyncio.wait_for(
                    client.aio.models.generate_content(
                        model=current_model,
                        contents=prompt,
                        config=types.GenerateContentConfig(
                            system_instruction=role_prompt
                        )
                    ),
                    timeout=timeout_sec
                )
                stdout_str = response.text
                if stdout_str and len(stdout_str.strip()) > 50:
                    return stdout_str
                else:
                    log.warning(f"[{step_name}] attempt {attempt+1} returned suspiciously empty output.")
            except asyncio.TimeoutError:
                log.warning(f"[{step_name}] attempt {attempt+1} timed out after {timeout_sec}s.")
            except APIError as e:
                log.warning(f"[{step_name}] attempt {attempt+1} API Error: {e}")
                err_str = str(e)
                if "quotaExceeded" in err_str or "ModelNotFoundError" in err_str:
                    log.error(f"[{step_name}] Hard API Error detected for '{current_model}'. Tripping circuit breaker for 10 minutes.")
                    CIRCUIT_BREAKER[current_model] = time.time() + 600
            except Exception as e:
                log.error(f"[{step_name}] attempt {attempt+1} failed: {e}")
                
            if attempt < retries - 1:
                # Exponential Backoff with Jitter
                sleep_time = (2 ** attempt) + random.uniform(0, 1)
                log.info(f"[{step_name}] Backing off for {sleep_time:.2f}s before next attempt...")
                await asyncio.sleep(sleep_time)
                
        return ""

async def _process_single_file_async(abs_p: str, existing_source_map: dict, semaphore: asyncio.Semaphore, shared_context: dict) -> tuple[list, list]:
    root_dir = shared_context['root_dir']
    try:
        rel_p = os.path.relpath(abs_p, root_dir)
    except ValueError:
        rel_p = abs_p
        
    try:
        raw_ref = os.path.relpath(abs_p, str(MEMORY_DIR)).replace("\\", "/")
    except ValueError:
        raw_ref = rel_p.replace("\\", "/")
    
    normalized_ref = _normalize_raw_ref(raw_ref)
    canonical_name = canonical_source_name(abs_p)
    existing_name = existing_source_map.get(normalized_ref)
    target_name = existing_name if existing_name else canonical_name
    action = "UPDATE" if existing_name else "CREATE"
    
    try:
        with open(abs_p, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except Exception as e:
        log.error(f"Could not read file {abs_p} for ingest: {e}")
        return [], []

    safe_rel = _sanitize_for_prompt(rel_p)
    safe_target = _sanitize_for_prompt(target_name)
    safe_ref = _sanitize_for_prompt(raw_ref)
    
    file_list_str = (
        f"- Source: `{safe_rel}`\n"
        f"  Target Source Page: `{safe_target}` ({action})\n"
        f"  YAML sources field: [\"{safe_ref.replace('"', '\\"')}\"]"
    )
    
    file_content_str = (
        f"--- SOURCE FILE: {rel_p} ---\n"
        f"{content}\n"
        f"--- END SOURCE FILE ---"
    )

    _backup_wiki_targets(WIKI_DIR, [{"target_source_file": target_name, "action": action}])

    llm_config = config.get("llm", {})
    model_cascade = llm_config.get("model_cascade", ["default", "gemini-3.1-pro-preview", "gemini-3-flash-preview", "gemini-2.5-pro"])
    
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

{f"--- PURPOSE (Wiki 目标) ---\\n{shared_context['purpose_content']}" if shared_context['purpose_content'] else ""}

{f"--- EXISTING WIKI INDEX (检查现有内容) ---\\n{shared_context['index_summary']}" if shared_context['index_summary'] else ""}

{f"--- EXISTING ENTITY DICTIONARY (强制实体对齐) ---\\n{shared_context['entity_dict']}" if shared_context['entity_dict'] else ""}
"""
    
    timeout_analysis = llm_config.get("timeout_analysis", 120)
    analysis_result = await _async_call_gemini_cli(
        analysis_prompt, "You are an analysis agent.", model_cascade, timeout_analysis, semaphore, f"Step 1 | {safe_rel[-20:]}"
    )
    
    if not analysis_result:
        log.warning(f"Analysis step failed for {rel_p}, proceeding with direct generation")
        analysis_result = "(Analysis unavailable — falling back to direct generation)"

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
{shared_context['schema_content']}

--- CATEGORIES ---
{shared_context['categories_content']}

{f"--- PURPOSE (对齐目标) ---\\n{shared_context['purpose_content']}" if shared_context['purpose_content'] else ""}

{f"--- EXISTING ENTITY DICTIONARY (强制实体对齐) ---\\n{shared_context['entity_dict']}" if shared_context['entity_dict'] else ""}

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
{f"Current overview:{chr(10)}{shared_context['overview_content']}" if shared_context['overview_content'] else "Create a new overview.md if it does not exist."} 

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
    
    timeout_generation = llm_config.get("timeout_generation", 180)
    stdout_str = await _async_call_gemini_cli(
        generation_prompt, "You are a generation agent.", model_cascade, timeout_generation, semaphore, f"Step 2 | {safe_rel[-20:]}"
    )
    
    if not stdout_str:
        log.error(f"Generation Agent failed for {rel_p}")
        return [], []
        
    review_items = []
    try:
        import re
        # Fix unescaped pipe character and add fallback for optional groups
        REVIEW_BLOCK_REGEX = re.compile(r'---REVIEW:\s*(\w[\w-]*)\s*\|?\s*(.+?)\s*---\n([\s\S]*?)---END REVIEW---')
        VALID_TYPES = {"contradiction", "duplicate", "missing-page", "suggestion"}

        for match in REVIEW_BLOCK_REGEX.finditer(stdout_str):
            raw_type = (match.group(1) or "").strip().lower()
            title = (match.group(2) or "").strip()
            body = (match.group(3) or "").strip()
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
                "source": str([abs_p]),
                "search_queries": search_queries,
                "affected_pages": affected_pages
            })
    except Exception as e:
        log.warning(f"Failed to parse review items for {rel_p}: {e}")
        
    changed_files = []
    try:
        import re
        file_blocks = re.finditer(r"---FILE:\s*([^\n]+)---\n(.*?)\n---END FILE---", stdout_str, re.DOTALL)
        for match in file_blocks:
            filename = match.group(1).strip()
            filename = filename.replace("[[", "").replace("]]", "").replace("[", "")
            if filename != "overview.md":
                if filename.endswith(".md"):
                    filename = normalize_entity_name(filename[:-3]) + ".md"
                else:
                    filename = normalize_entity_name(filename)
            content = match.group(2).strip()
            
            if not filename.startswith(VALID_PREFIXES) and filename != "overview.md":
                log.warning(f"Intercepted illegal write attempt to {filename}")
                continue
                
            intercept_match = _piea_intercept(filename, content, shared_context['index_data'])
            if intercept_match:
                existing_key = intercept_match["existing_key"]
                sim = intercept_match["similarity"]
                match_type = intercept_match.get("match_type", "cosine_similarity")
                
                log.info(f"[PIEA] Intercepted {filename}: Aligned with {existing_key} (similarity: {sim:.3f}, type: {match_type})")
                
                # Strategy: Physical Append for ALL matched items
                log.info(f"[PIEA-Append] High similarity match. Appending to {existing_key}.md instead of creating {filename}.")
                existing_file_path = os.path.join(WIKI_DIR, f"{existing_key}.md")
                
                if os.path.exists(existing_file_path):
                    # Extract main body, ignoring YAML
                    body_content = re.sub(r'^---\n(.*?)\n---\n?', '', content, flags=re.DOTALL)
                    
                    try:
                        with open(existing_file_path, "a", encoding="utf-8") as ef:
                            ef.write(f"\n\n## Timeline (证据时间线)\n\n### [Auto-Append via PIEA: {now}]\n{body_content}")
                        changed_files.append(existing_file_path)
                        
                        review_items.append({
                            "item_type": "duplicate",
                            "title": f"PIEA Append: {filename} -> {existing_key}",
                            "description": f"Pre-Ingestion Entity Alignment intercepted a duplicate ({match_type}, sim: {sim:.3f}) and physically appended the content.",
                            "source": str([abs_p]),
                            "search_queries": [intercept_match["existing_title"], filename],
                            "affected_pages": [f"{existing_key}.md"]
                        })
                        continue # Skip writing the new file
                    except Exception as e:
                        log.error(f"Failed to append to {existing_file_path}: {e}")
                        # Fallback to Contested file if append fails
                
                # If append failed, write as Contested
                content = re.sub(r'^status:\s*"Active"', 'status: "Contested"', content, flags=re.MULTILINE)
                content = re.sub(r'^(alignment_score:\s*\d+)', r'\1\npiea_aligned_with: ' + existing_key, content, flags=re.MULTILINE)
                
                review_items.append({
                    "item_type": "duplicate",
                    "title": f"PIEA: {filename} <> {existing_key}",
                    "description": f"Pre-Ingestion Entity Alignment intercepted a high-similarity duplicate node (similarity: {sim:.3f}).",
                    "source": str([abs_p]),
                    "search_queries": [intercept_match["existing_title"], filename],
                    "affected_pages": [filename, f"{existing_key}.md"]
                })

            file_path = os.path.join(WIKI_DIR, filename)
            try:
                atomic_write_text(Path(file_path), content)
                changed_files.append(file_path)
            except Exception as e:
                log.error(f"Failed to write {filename}: {e}")
    except Exception as e:
        log.error(f"Failed in file writing for {rel_p}: {e}")
        
    if changed_files:
        for p in changed_files:
            sanitize_wiki_node(p)
            
    now = datetime.now(timezone.utc).isoformat()
    f_hash = calculate_hash(abs_p)
    if f_hash: 
        mark_file_processed(abs_p, f_hash, now)
        
    return changed_files, review_items

async def _run_batch_async(filepaths: list, existing_source_map: dict = None):
    log.info(f"[Async CoT] Ingesting batch of {len(filepaths)} files concurrently...")
    
    if existing_source_map is None:
        existing_source_map = scan_existing_sources(WIKI_DIR)
        
    root_dir = str(EXTENSION_ROOT.parent.parent.resolve())
    
    schema_content = ""
    try:
        schema_content = (EXTENSION_ROOT / "schema.md").read_text(encoding="utf-8")
    except Exception: pass
    
    categories_content = ""
    try:
        categories_content = (EXTENSION_ROOT / "SCHEMA_CATEGORIES.md").read_text(encoding="utf-8")
    except Exception: pass
    
    index_data = {}
    index_path = get_index_path()
    if index_path.exists():
        try:
            with open(index_path, "r", encoding="utf-8") as f:
                index_data = json.load(f)
        except Exception: pass
    
    shared_context = {
        'root_dir': root_dir,
        'schema_content': schema_content,
        'categories_content': categories_content,
        'purpose_content': _read_purpose(),
        'index_summary': _read_index_summary(),
        'entity_dict': _read_entity_dictionary(),
        'overview_content': _read_overview(),
        'index_data': index_data
    }
    
    max_concurrency = config.get("llm", {}).get("max_concurrent_tasks", 3)
    semaphore = asyncio.Semaphore(max_concurrency)
    
    tasks = []
    for fp in filepaths:
        tasks.append(asyncio.create_task(_process_single_file_async(fp, existing_source_map, semaphore, shared_context)))
        
    results = await asyncio.gather(*tasks, return_exceptions=True)
    
    all_changed_files = []
    all_review_items = []
    
    for res in results:
        if isinstance(res, Exception):
            log.error(f"File processing task raised exception: {res}")
        elif res:
            cf, ri = res
            all_changed_files.extend(cf)
            all_review_items.extend(ri)
            
    if all_review_items:
        for item in all_review_items:
            try:
                governance_store.enqueue_governance_item(**item)
            except Exception as e:
                log.warning(f"Failed to enqueue item: {e}")
                
    if all_changed_files:
        unique_changed = list(set(all_changed_files))
        governance_store.sync_pages_to_canonical(
            unique_changed,
            origin="ingest-async",
            auto_approve=True,
            summary=f"Async ingest sync for {len(unique_changed)} page(s)",
        )
        log.info(f"Agent modified and sanitized {len(unique_changed)} wiki files.")
    else:
        log.warning("Agent ran for batch but no wiki files were modified.")


def process_file_batch(filepaths: list, existing_source_map: dict = None):
    """Synchronous entrypoint for the Async 2-Step CoT ingest pipeline."""
    if not filepaths: return False
    
    try:
        asyncio.run(_run_batch_async(filepaths, existing_source_map))
    except Exception as e:
        log.error(f"Async batch failed: {e}")
        return False
        
    return True

def sync_all():
    log.info("Starting Native Agent Ingest Sync (Async 2-Step CoT)...")
    
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
