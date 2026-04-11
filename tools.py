import os
import json
import logging
import re
import random
import string
import datetime
import webbrowser
from pathlib import Path
from collections import defaultdict
import yaml
import ingest

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("vector-lake-tools")

EXTENSION_ROOT = Path(__file__).parent

# ── Token Budget Allocation ──────────────────────────────────────────────────
# Inspired by LLM Wiki's 60/20/5/15 budget split
TOKEN_BUDGET = {
    "wiki_pages": 0.60,     # 60% for wiki page content
    "chat_history": 0.20,   # 20% for conversation history
    "index_summary": 0.05,  # 5% for index.json compact view
    "system_prompt": 0.15,  # 15% for schema + purpose
}
DEFAULT_MAX_CHARS = 200000  # ~200K chars, ~50K tokens


# ── CJK Tokenizer ───────────────────────────────────────────────────────────
CJK_REGEX = re.compile(r'[\u4e00-\u9fff\u3400-\u4dbf]')
STOP_WORDS = {
    "的", "了", "在", "是", "我", "有", "和", "就", "不", "人", "都", "一", "一个",
    "the", "a", "an", "is", "are", "was", "were", "in", "on", "at", "to", "for",
    "of", "and", "or", "but", "with", "by", "from", "as", "it", "this", "that",
}

def _tokenize_query(query: str) -> list:
    """Tokenize query with CJK bigram support.
    
    For CJK text: produce bigrams + unigrams + original tokens.
    For Latin text: space-split + stopword filter.
    """
    tokens = set()
    
    for word in query.strip().split():
        word_lower = word.lower()
        if word_lower in STOP_WORDS:
            continue
        
        has_cjk = bool(CJK_REGEX.search(word))
        if has_cjk:
            # Bigrams for CJK
            chars = list(word)
            for i in range(len(chars) - 1):
                tokens.add(chars[i] + chars[i + 1])
            # Individual characters
            for c in chars:
                if CJK_REGEX.match(c):
                    tokens.add(c)
            # Full token
            tokens.add(word)
        else:
            tokens.add(word_lower)
    
    return list(tokens)


def search_vector_lake(query: str, top_k: int = 5, as_xml: bool = False, domain: str = None, cluster: str = None, include_history: bool = False):
    """Enhanced search with CJK tokenization, body matching, and graph expansion.
    
    Scoring:
    - Title match: +10 per token
    - Summary match: +3 per token  
    - Body match: +1 per token (first 2000 chars)
    - Graph expansion: top results' high-relevance neighbors injected as bonus results
    """
    wiki_dir = os.path.join(os.path.expanduser("~"), ".gemini", "MEMORY", "wiki")
    index_path = os.path.join(wiki_dir, "index.json")
    if not os.path.exists(index_path): return "index.json not found."
    try:
        with open(index_path, "r", encoding="utf-8") as f: index_data = json.load(f)
    except Exception:
        import indexer
        indexer.generate_index()
        with open(index_path, "r", encoding="utf-8") as f: index_data = json.load(f)
    
    nodes = [{"_key": k, **v} for k, v in index_data.get("nodes", {}).items()]
    tokens = _tokenize_query(query)
    
    if not tokens:
        return "No valid search tokens."
    
    scored = []
    for node in nodes:
        # Domain filter
        if domain and node.get("domain", "").lower() != domain.lower():
            continue
        # Cluster filter
        if cluster and node.get("topic_cluster", "").lower() != cluster.lower():
            continue
        # Status filter (skip deprecated unless include_history)
        if not include_history and node.get("status", "").lower() in ("deprecated", "archived"):
            continue
            
        score = 0
        title = (node.get("title") or "").lower()
        summary = (node.get("summary") or "").lower()
        
        for term in tokens:
            if term in title:
                score += 10  # Title match is highest signal
            if term in summary:
                score += 3   # Summary match
        
        # Body match (read first 2000 chars of actual file for deeper matching)
        if score == 0:
            fp = os.path.join(wiki_dir, f"{node['_key']}.md")
            if os.path.exists(fp):
                try:
                    with open(fp, "r", encoding="utf-8", errors="replace") as f:
                        body_preview = f.read(2000).lower()
                    # Strip frontmatter
                    body_preview = re.sub(r'^---.*?---\s*', '', body_preview, flags=re.DOTALL)
                    for term in tokens:
                        if term in body_preview:
                            score += 1
                except Exception:
                    pass
        
        if score > 0:
            scored.append((score, node))
    
    scored.sort(key=lambda x: x[0], reverse=True)
    
    # ── Graph expansion: inject high-relevance neighbors of top results ──
    top_keys = {n['_key'] for _, n in scored[:3]}  # seed from top 3
    if top_keys and index_data.get("weighted_edges"):
        expansion_candidates = {}
        for edge in index_data["weighted_edges"]:
            src, tgt, weight = edge["source"], edge["target"], edge["weight"]
            if src in top_keys and tgt not in top_keys:
                expansion_candidates[tgt] = max(expansion_candidates.get(tgt, 0), weight)
            elif tgt in top_keys and src not in top_keys:
                expansion_candidates[src] = max(expansion_candidates.get(src, 0), weight)
        
        # Add top 3 graph-expanded neighbors if they aren't already in results
        existing_keys = {n['_key'] for _, n in scored}
        sorted_expansions = sorted(expansion_candidates.items(), key=lambda x: x[1], reverse=True)
        for exp_key, exp_weight in sorted_expansions[:3]:
            if exp_key not in existing_keys:
                exp_node = index_data["nodes"].get(exp_key)
                if exp_node:
                    scored.append((exp_weight, {"_key": exp_key, **exp_node}))
    
    # Format output
    res = ""
    for i, (s, n) in enumerate(scored[:top_k]):
        fp = os.path.join(wiki_dir, f"{n['_key']}.md")
        snip = ""
        if os.path.exists(fp):
            with open(fp, "r", encoding="utf-8", errors="replace") as f: content = f.read()
            snip = re.sub(r'^---.*?---\s*', '', content, flags=re.DOTALL)[:300]
        if as_xml: res += f"<Evidence_Node ID='Wiki_{i}' Source='{n['_key']}.md'>{snip}</Evidence_Node>\n" 
        else: res += f"- **{n.get('title', n['_key'])}** (score: {s:.1f})\n  {snip}...\n\n"
    return res


def assemble_context(query: str, max_chars: int = DEFAULT_MAX_CHARS) -> dict:
    """Budget-controlled context assembly for query operations.
    
    Returns dict with 'wiki_context', 'index_summary', 'purpose' sections,
    each truncated to their budget allocation.
    """
    wiki_dir = os.path.join(os.path.expanduser("~"), ".gemini", "MEMORY", "wiki")
    
    # Budget allocation
    wiki_budget = int(max_chars * TOKEN_BUDGET["wiki_pages"])
    index_budget = int(max_chars * TOKEN_BUDGET["index_summary"])
    
    # Get relevant wiki pages
    search_results = search_vector_lake(query, top_k=15, as_xml=False)
    
    # Assemble wiki context with budget control
    wiki_context = ""
    page_count = 0
    for match in re.finditer(r'\*\*(.+?)\*\*.*?\n\s+(.*?)\.\.\.\n', search_results, re.DOTALL):
        page_content = match.group(0)
        if len(wiki_context) + len(page_content) > wiki_budget:
            break
        wiki_context += page_content
        page_count += 1
    
    # Index summary (compact)
    index_summary = ""
    index_path = os.path.join(wiki_dir, "index.json")
    if os.path.exists(index_path):
        try:
            with open(index_path, "r", encoding="utf-8") as f:
                idx = json.load(f)
            lines = []
            for key, node in list(idx.get("nodes", {}).items())[:50]:
                title = node.get("title", key)
                ntype = node.get("type", "?")
                lines.append(f"[{ntype}] {title}")
            index_summary = "\n".join(lines)[:index_budget]
        except Exception:
            pass
    
    # Purpose
    purpose = ""
    purpose_path = os.path.join(os.path.expanduser("~"), ".gemini", "MEMORY", "purpose.md")
    try:
        with open(purpose_path, "r", encoding="utf-8") as f:
            purpose = f.read()
    except Exception:
        pass
    
    return {
        "wiki_context": wiki_context,
        "wiki_page_count": page_count,
        "index_summary": index_summary,
        "purpose": purpose,
        "budget_used": len(wiki_context) + len(index_summary) + len(purpose),
        "budget_max": max_chars,
    }


def sync_vector_lake():
    ingest.sync_all()
    import indexer
    indexer.generate_index()
    return "Ingestion Sync (2-Step CoT) and Index generation completed."

def sanitize_wiki_node(filepath: str):
    if not os.path.exists(filepath) or not filepath.endswith(".md"): return
    with open(filepath, 'r', encoding='utf-8', errors='replace') as f: content = f.read()
    match = re.match(r'^---\n(.*?)\n---\n(.*)', content, re.DOTALL)
    fm = {}
    body = content
    if match:
        body = match.group(2)
        try: fm = yaml.safe_load(match.group(1)) or {}
        except Exception: fm = {}
    if not isinstance(fm, dict): fm = {}
    today = datetime.datetime.now().strftime("%Y%m%d")
    if not fm.get('id'): fm['id'] = f"{today}_{''.join(random.choices(string.ascii_lowercase + string.digits, k=6))}"
    fm['updated'] = today
    new_yaml = yaml.dump(fm, allow_unicode=True, default_flow_style=False, sort_keys=False)
    with open(filepath, 'w', encoding='utf-8') as f: f.write(f"---\n{new_yaml}---\n{body.lstrip()}")

def lint_vector_lake(auto_fix: bool = False):
    """10-point structural health check of the Wiki Markdown network.
    
    Checks:
      1. Frontmatter completeness (required fields)
      2. Naming compliance (valid prefix)
      3. Type/status legality (valid enum values)
      4. Category vocabulary (from SCHEMA_CATEGORIES.md)
      5. Duplicate IDs
      6. Alias conflicts
      7. Broken links (wikilinks to non-existent pages)
      8. Orphan pages (no inbound links)
      9. Filename similarity (near-duplicate detection)
      10. Knowledge decay (>60 days as 'sprouting')
    """
    wiki_dir = os.path.join(os.path.expanduser("~"), ".gemini", "MEMORY", "wiki")
    if not os.path.exists(wiki_dir):
        return "Wiki directory not found."
    
    SKIP_FILES = {"index.md", "log.md", "overview.md"}
    VALID_TYPES = {"entity", "concept", "source", "synthesis"}
    VALID_STATUS = {"active", "deprecated", "archived", "contested"}
    VALID_EPISTEMIC = {"seed", "sprouting", "evergreen"}
    VALID_CATEGORIES = {
        "Uncategorized", "Artificial_Intelligence", "Healthcare_IT",
        "Strategy_and_Business", "System_Architecture",
        "Philosophy_and_Cognitive", "Biomedicine",
    }
    VALID_PREFIXES = ("Concept_", "Source_", "Entity_", "Synthesis_")
    REQUIRED_FIELDS = ["title", "type", "domain", "status", "epistemic-status", "categories"]
    
    files = [f for f in os.listdir(wiki_dir) if f.endswith(".md") and f not in SKIP_FILES]
    
    issues = {
        "frontmatter": [],
        "naming": [],
        "type_status": [],
        "category": [],
        "duplicate_id": [],
        "alias_conflict": [],
        "broken_links": [],
        "orphan": [],
        "similarity": [],
        "decay": [],
    }
    fixes_applied = 0
    
    # Parse all files
    parsed = {}  # filename -> {fm: dict, body: str, links: set}
    id_map = {}  # id -> [filenames]
    alias_map = {}  # alias -> [filenames]
    all_keys = set()  # All valid node keys (filename without .md)
    inbound_count = defaultdict(int)  # key -> count of inbound links
    
    for filename in files:
        filepath = os.path.join(wiki_dir, filename)
        node_key = filename[:-3]
        all_keys.add(node_key)
        
        try:
            with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
                content = f.read()
        except Exception:
            issues["frontmatter"].append(f"{filename}: Cannot read file")
            continue
        
        fm_match = re.match(r'^---\n(.*?)\n---\n(.*)', content, re.DOTALL)
        if not fm_match:
            issues["frontmatter"].append(f"{filename}: Missing YAML frontmatter entirely")
            continue
        
        try:
            fm = yaml.safe_load(fm_match.group(1)) or {}
        except Exception as e:
            issues["frontmatter"].append(f"{filename}: YAML parse error: {e}")
            continue
        
        if not isinstance(fm, dict):
            issues["frontmatter"].append(f"{filename}: Frontmatter is not a dict")
            continue
        
        body = fm_match.group(2)
        
        # Extract links
        links = set()
        for m in re.finditer(r'\[\[([^\]|]+?)(?:\|[^\]]+?)?\]\]', content):
            links.add(m.group(1).strip().replace('.md', ''))
        for m in re.finditer(r'\[[^\[\]]+?::\s*\[\[([^\]]+?)\]\]\]', content):
            links.add(m.group(1).strip().split('|')[0].strip().replace('.md', ''))
        links.discard('')
        
        parsed[filename] = {"fm": fm, "body": body, "links": links, "content": content, "path": filepath}
        
        # Track IDs
        nid = fm.get("id", "")
        if nid:
            id_map.setdefault(str(nid), []).append(filename)
        
        # Track aliases
        aliases = fm.get("aliases", [])
        if isinstance(aliases, str):
            aliases = [aliases]
        if isinstance(aliases, list):
            for a in aliases:
                alias_map.setdefault(str(a).strip(), []).append(filename)
        
        # Track inbound links for orphan detection
        for link_target in links:
            inbound_count[link_target] += 1
    
    # ── CHECK 1: Frontmatter completeness ─────────────────────────
    for filename, data in parsed.items():
        fm = data["fm"]
        missing = [f for f in REQUIRED_FIELDS if not fm.get(f)]
        if missing:
            issues["frontmatter"].append(f"{filename}: Missing fields: {', '.join(missing)}")
            if auto_fix:
                changed = False
                if "domain" not in fm or not fm["domain"]:
                    fm["domain"] = "General"
                    changed = True
                if "topic_cluster" not in fm or not fm["topic_cluster"]:
                    fm["topic_cluster"] = "General"
                    changed = True
                if "status" not in fm or not fm["status"]:
                    fm["status"] = "Active"
                    changed = True
                if "epistemic-status" not in fm or not fm["epistemic-status"]:
                    fm["epistemic-status"] = "seed"
                    changed = True
                if "categories" not in fm or not fm["categories"]:
                    fm["categories"] = ["Uncategorized"]
                    changed = True
                if changed:
                    _write_fixed_frontmatter(data["path"], fm, data["body"])
                    fixes_applied += 1
    
    # ── CHECK 2: Naming compliance ────────────────────────────────
    for filename in files:
        if filename in SKIP_FILES:
            continue
        if not filename.startswith(VALID_PREFIXES):
            issues["naming"].append(f"{filename}: Does not start with valid prefix ({'/'.join(VALID_PREFIXES)})")
    
    # ── CHECK 3: Type/Status legality ─────────────────────────────
    for filename, data in parsed.items():
        fm = data["fm"]
        ftype = str(fm.get("type", "")).lower()
        if ftype and ftype not in VALID_TYPES:
            issues["type_status"].append(f"{filename}: Invalid type '{ftype}' (valid: {VALID_TYPES})")
        
        fstatus = str(fm.get("status", "")).lower()
        if fstatus and fstatus not in VALID_STATUS:
            issues["type_status"].append(f"{filename}: Invalid status '{fstatus}' (valid: {VALID_STATUS})")
        
        epistemic = str(fm.get("epistemic-status", "")).lower()
        if epistemic and epistemic not in VALID_EPISTEMIC:
            issues["type_status"].append(f"{filename}: Invalid epistemic-status '{epistemic}' (valid: {VALID_EPISTEMIC})")
    
    # ── CHECK 4: Category vocabulary ──────────────────────────────
    for filename, data in parsed.items():
        cats = data["fm"].get("categories", [])
        if isinstance(cats, str):
            cats = [cats]
        if isinstance(cats, list):
            for cat in cats:
                if cat not in VALID_CATEGORIES:
                    issues["category"].append(f"{filename}: Invalid category '{cat}'")
    
    # ── CHECK 5: Duplicate IDs ────────────────────────────────────
    for nid, fnames in id_map.items():
        if len(fnames) > 1:
            issues["duplicate_id"].append(f"ID '{nid}' shared by: {', '.join(fnames)}")
    
    # ── CHECK 6: Alias conflicts ──────────────────────────────────
    for alias, fnames in alias_map.items():
        if len(fnames) > 1:
            issues["alias_conflict"].append(f"Alias '{alias}' claimed by: {', '.join(fnames)}")
    
    # ── CHECK 7: Broken links ─────────────────────────────────────
    for filename, data in parsed.items():
        for target in data["links"]:
            if target not in all_keys:
                issues["broken_links"].append(f"{filename} -> [[{target}]]: target does not exist")
    
    # ── CHECK 8: Orphan pages (no inbound links) ──────────────────
    for filename in files:
        if filename in SKIP_FILES:
            continue
        node_key = filename[:-3]
        if inbound_count.get(node_key, 0) == 0:
            # Source pages are allowed to be orphaned (they link outward)
            if not filename.startswith("Source_"):
                issues["orphan"].append(f"{filename}: No inbound links (orphan)")
    
    # ── CHECK 9: Filename similarity (near-duplicate detection) ───
    from difflib import SequenceMatcher
    keys_list = sorted(all_keys)
    for i in range(len(keys_list)):
        for j in range(i + 1, min(i + 50, len(keys_list))):  # Only check nearby entries
            a, b = keys_list[i], keys_list[j]
            # Skip if same prefix type (e.g., both Source_)
            a_prefix = a.split('_')[0] if '_' in a else ""
            b_prefix = b.split('_')[0] if '_' in b else ""
            if a_prefix != b_prefix:
                continue
            # Compare the part after prefix
            a_name = a.split('_', 1)[1] if '_' in a else a
            b_name = b.split('_', 1)[1] if '_' in b else b
            ratio = SequenceMatcher(None, a_name.lower(), b_name.lower()).ratio()
            if ratio > 0.85 and a != b:
                issues["similarity"].append(f"Possible duplicate: {a}.md <-> {b}.md (similarity: {ratio:.0%})")
    
    # ── CHECK 10: Knowledge decay (>60 days as 'sprouting') ───────
    today = datetime.datetime.now()
    for filename, data in parsed.items():
        fm = data["fm"]
        epistemic = str(fm.get("epistemic-status", "")).lower()
        if epistemic != "sprouting":
            continue
        updated_str = str(fm.get("updated", ""))
        if not updated_str:
            continue
        try:
            # Handle both YYYYMMDD and YYYY-MM-DD formats
            updated_str_clean = updated_str.replace("-", "")
            if len(updated_str_clean) >= 8:
                updated = datetime.datetime.strptime(updated_str_clean[:8], "%Y%m%d")
                age_days = (today - updated).days
                if age_days > 60:
                    issues["decay"].append(f"{filename}: 'sprouting' for {age_days} days (updated: {updated_str})")
                    if auto_fix:
                        fm["epistemic-status"] = "evergreen"
                        _write_fixed_frontmatter(data["path"], fm, data["body"])
                        fixes_applied += 1
        except (ValueError, TypeError):
            pass
    
    # ── Build report ──────────────────────────────────────────────
    CHECK_NAMES = {
        "frontmatter": "1. Frontmatter Completeness",
        "naming": "2. Naming Compliance",
        "type_status": "3. Type/Status Legality",
        "category": "4. Category Vocabulary",
        "duplicate_id": "5. Duplicate IDs",
        "alias_conflict": "6. Alias Conflicts",
        "broken_links": "7. Broken Links",
        "orphan": "8. Orphan Pages",
        "similarity": "9. Filename Similarity",
        "decay": "10. Knowledge Decay",
    }
    
    total_issues = sum(len(v) for v in issues.values())
    
    lines = [f"=== Vector Lake Lint Report ==="]
    lines.append(f"Scanned: {len(files)} files | Issues: {total_issues} | Auto-fixed: {fixes_applied}")
    lines.append("")
    
    for key, name in CHECK_NAMES.items():
        items = issues[key]
        status = "[PASS]" if not items else f"[FAIL: {len(items)}]"
        lines.append(f"{name}: {status}")
        for item in items[:10]:  # Cap display at 10 per category
            lines.append(f"    {item}")
        if len(items) > 10:
            lines.append(f"    ... and {len(items) - 10} more")
        lines.append("")
    
    return "\n".join(lines)


def _write_fixed_frontmatter(filepath: str, fm: dict, body: str):
    """Write corrected frontmatter back to file."""
    try:
        new_yaml = yaml.dump(fm, allow_unicode=True, default_flow_style=False, sort_keys=False)
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(f"---\n{new_yaml}---\n{body.lstrip()}")
    except Exception as e:
        log.warning(f"Failed to write fixed frontmatter to {filepath}: {e}")

def query_logic_lake(query_str: str, dry_run: bool = False):
    """Deep reasoning with budget-controlled context + auto-reingest.
    
    After the Synthesizer Agent creates a Synthesis page, this function:
    1. Detects newly created wiki files
    2. Re-runs the indexer to rebuild weighted edges
    3. Scans for mentioned-but-nonexistent entities → generates stub pages
    """
    import subprocess
    
    wiki_dir = os.path.join(os.path.expanduser("~"), ".gemini", "MEMORY", "wiki")
    
    # Snapshot before
    before_files = set()
    if os.path.exists(wiki_dir):
        before_files = {f for f in os.listdir(wiki_dir) if f.endswith(".md")}
    
    # Assemble budget-controlled context
    ctx = assemble_context(query_str)
    
    context_block = ""
    if ctx["wiki_context"]:
        context_block = f"\n\n--- RELEVANT WIKI PAGES ({ctx['wiki_page_count']} pages, {ctx['budget_used']}/{ctx['budget_max']} chars) ---\n{ctx['wiki_context']}"
    if ctx["purpose"]:
        context_block += f"\n\n--- PURPOSE ---\n{ctx['purpose']}"
    
    prompt = f"@vector-lake-synthesizer\nQuery: {query_str}{context_block}"
    try:
        subprocess.run(["gemini.cmd", "-p", "", "--approval-mode", "yolo"], input=prompt, text=True, encoding='utf-8', timeout=300)
    except Exception as e:
        return f"Error: {e}"
    
    # ── Auto-reingest: detect new files → rebuild index → generate stubs ──
    after_files = set()
    if os.path.exists(wiki_dir):
        after_files = {f for f in os.listdir(wiki_dir) if f.endswith(".md")}
    
    new_files = after_files - before_files
    if new_files:
        log.info(f"[Auto-Reingest] Detected {len(new_files)} new wiki file(s): {new_files}")
        
        # Sanitize new files
        for nf in new_files:
            sanitize_wiki_node(os.path.join(wiki_dir, nf))
        
        # Rebuild index with weighted edges
        import indexer
        indexer.generate_index()
        
        # Scan new files for broken links → generate stub pages
        stubs_created = _generate_stubs_for_broken_links(wiki_dir, new_files)
        if stubs_created:
            indexer.generate_index()  # Re-index after stub creation
        
        return f"Query completed. {len(new_files)} new page(s) created. {stubs_created} stub(s) generated."
    
    return "Query completed."


def _generate_stubs_for_broken_links(wiki_dir: str, files_to_scan: set) -> int:
    """Scan files for [[wikilinks]] pointing to non-existent pages.
    Generate minimal stub pages for them (epistemic-status: seed).
    """
    existing_files = {f.replace('.md', '') for f in os.listdir(wiki_dir) if f.endswith(".md")}
    
    # Collect all link targets from the specified files
    broken_targets = set()
    for filename in files_to_scan:
        filepath = os.path.join(wiki_dir, filename)
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                content = f.read()
        except Exception:
            continue
        
        # Extract all [[wikilink]] targets
        for m in re.finditer(r'\[\[([^\]|]+?)(?:\|[^\]]+?)?\]\]', content):
            target = m.group(1).strip().replace('.md', '')
            if target and target not in existing_files:
                broken_targets.add(target)
        # Also extract [Relation:: [[Target]]] targets
        for m in re.finditer(r'\[[^\[\]]+?::\s*\[\[([^\]]+?)\]\]\]', content):
            target = m.group(1).strip().split('|')[0].strip().replace('.md', '')
            if target and target not in existing_files:
                broken_targets.add(target)
    
    if not broken_targets:
        return 0
    
    stubs = 0
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    for target in broken_targets:
        # Determine type prefix from name
        if target.startswith("Concept_") or target.startswith("Entity_") or target.startswith("Source_") or target.startswith("Synthesis_"):
            node_type = target.split('_')[0].lower()
        else:
            node_type = "concept"  # Default to concept for unknown prefixes
        
        stub_content = f"""---
title: "{target.replace('_', ' ')}"
type: "{node_type}"
domain: "General"
topic_cluster: "General"
status: "Active"
epistemic-status: "seed"
categories: ["Uncategorized"]
tags: ["auto-stub"]
created: "{today}"
updated: "{today}"
sources: []
---

# {target.replace('_', ' ')}

> This is an auto-generated stub page. It was referenced by another wiki page but did not exist.
> Please expand with real content when information becomes available.
"""
        stub_path = os.path.join(wiki_dir, f"{target}.md")
        try:
            with open(stub_path, 'w', encoding='utf-8') as f:
                f.write(stub_content)
            stubs += 1
            existing_files.add(target)
            log.info(f"[Stub] Created seed page: {target}.md")
        except Exception as e:
            log.warning(f"Failed to create stub {target}.md: {e}")
    
    return stubs


def delete_source(raw_path: str, dry_run: bool = False) -> str:
    """Cascade delete: remove a raw source file and clean up all related wiki pages.
    
    Strategy (3-match, from LLM Wiki):
    1. Source pages: wiki pages with sources[] containing the raw filename
    2. Single-source pages: if a page only references this one source → DELETE
    3. Multi-source pages: if a page references multiple sources → REMOVE this source reference
    """
    wiki_dir = os.path.join(os.path.expanduser("~"), ".gemini", "MEMORY", "wiki")
    memory_dir = os.path.join(os.path.expanduser("~"), ".gemini", "MEMORY")
    
    # Normalize the raw path for matching
    raw_basename = os.path.basename(raw_path)
    raw_stem = os.path.splitext(raw_basename)[0]
    
    try:
        raw_ref = os.path.relpath(raw_path, memory_dir).replace("\\", "/")
    except ValueError:
        raw_ref = raw_path.replace("\\", "/")
    raw_ref_lower = raw_ref.lower()
    raw_basename_lower = raw_basename.lower()
    
    if not os.path.exists(wiki_dir):
        return "Wiki directory not found."
    
    actions = []  # List of (action, filepath, detail)
    
    for filename in os.listdir(wiki_dir):
        if not filename.endswith(".md") or filename in ("index.md", "log.md", "overview.md"):
            continue
        
        filepath = os.path.join(wiki_dir, filename)
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                content = f.read()
        except Exception:
            continue
        
        # Parse frontmatter
        fm_match = re.match(r'^---\n(.*?)\n---\n(.*)', content, re.DOTALL)
        if not fm_match:
            continue
        
        try:
            fm = yaml.safe_load(fm_match.group(1)) or {}
        except Exception:
            continue
        
        sources = fm.get('sources', [])
        if not isinstance(sources, list):
            sources = [sources] if sources else []
        
        # Match 1: Source page for this raw file (Source_{stem}.md)
        is_source_page = filename.lower().startswith(f"source_{raw_stem.lower()}")
        
        # Match 2: frontmatter sources[] contains this file
        sources_lower = [str(s).lower().replace("\\", "/") for s in sources]
        has_source_ref = any(
            raw_ref_lower in s or raw_basename_lower in s
            for s in sources_lower
        )
        
        if not (is_source_page or has_source_ref):
            continue
        
        # Determine action
        if len(sources) <= 1 or is_source_page:
            # Single-source or the Source summary page → DELETE
            actions.append(("DELETE", filepath, filename))
        else:
            # Multi-source → REMOVE the reference only
            new_sources = [
                s for s in sources
                if raw_ref_lower not in str(s).lower().replace("\\", "/")
                and raw_basename_lower not in str(s).lower()
            ]
            actions.append(("REMOVE_REF", filepath, f"{filename}: {len(sources)}→{len(new_sources)} sources"))
    
    if not actions:
        return f"No wiki pages reference '{raw_basename}'. Nothing to clean up."
    
    # Report
    lines = [f"[CASCADE DELETE] Found {len(actions)} affected wiki page(s) for '{raw_basename}':"]
    for action, fpath, detail in actions:
        lines.append(f"  [{action}] {detail}")
    
    if dry_run:
        lines.append("\n(Dry run — no changes made. Remove --dry-run to execute.)")
        return "\n".join(lines)
    
    # Execute
    deleted = 0
    updated = 0
    for action, fpath, detail in actions:
        if action == "DELETE":
            try:
                os.remove(fpath)
                deleted += 1
                log.info(f"Deleted: {fpath}")
            except Exception as e:
                log.warning(f"Failed to delete {fpath}: {e}")
        elif action == "REMOVE_REF":
            try:
                with open(fpath, 'r', encoding='utf-8') as f:
                    content = f.read()
                fm_match = re.match(r'^---\n(.*?)\n---\n(.*)', content, re.DOTALL)
                if fm_match:
                    fm = yaml.safe_load(fm_match.group(1)) or {}
                    body = fm_match.group(2)
                    sources = fm.get('sources', [])
                    fm['sources'] = [
                        s for s in sources
                        if raw_ref_lower not in str(s).lower().replace("\\", "/")
                        and raw_basename_lower not in str(s).lower()
                    ]
                    new_yaml = yaml.dump(fm, allow_unicode=True, default_flow_style=False, sort_keys=False)
                    with open(fpath, 'w', encoding='utf-8') as f:
                        f.write(f"---\n{new_yaml}---\n{body}")
                    updated += 1
                    log.info(f"Removed source ref from: {fpath}")
            except Exception as e:
                log.warning(f"Failed to update {fpath}: {e}")
    
    # Rebuild index
    import indexer
    indexer.generate_index()
    
    lines.append(f"\nExecuted: {deleted} deleted, {updated} updated. Index rebuilt.")
    return "\n".join(lines)


def review_vector_lake(action: str = "list", index: int = -1, resolution: str = "skip"):
    """CLI interface for the review queue."""
    import review
    if action == "list":
        return review.format_pending_report()
    elif action == "resolve":
        if index < 0:
            return "Error: specify review item index. Usage: cli.py review resolve <index>"
        success = review.resolve_item(index, resolution)
        return f"Resolved item #{index}." if success else f"Failed to resolve item #{index}."
    else:
        return f"Unknown review action: {action}. Use 'list' or 'resolve'."

def trigger_serendipity_collision():
    return "Collision completed."

def visualize_vector_lake():
    import os
    import json
    import webbrowser
    
    base_dir = os.path.dirname(os.path.abspath(__file__))
    gemini_dir = os.path.dirname(os.path.dirname(base_dir))
    index_path = os.path.join(gemini_dir, "MEMORY", "wiki", "index.json")
    template_path = os.path.join(base_dir, "templates", "topology.html")
    output_path = os.path.join(gemini_dir, "tmp", "vector_lake_graph.html")
    
    if not os.path.exists(index_path):
        return "Error: index.json not found."
    if not os.path.exists(template_path):
        return "Error: template not found."
        
    with open(index_path, "r", encoding="utf-8") as f:
        try:
            idx_data = json.load(f)
        except json.JSONDecodeError:
            return "Error: Failed to parse index.json."
            
    nodes_dict = idx_data.get("nodes", {})
    
    links_count = {k: 0 for k in nodes_dict}
    for k, n in nodes_dict.items():
        for t in n.get("links", []):
            if t in nodes_dict:
                links_count[t] = links_count.get(t, 0) + 1
            links_count[k] += 1
            
    graph_nodes = []
    for key, node in nodes_dict.items():
        val = max(1, min(links_count.get(key, 1) // 2, 20))
        
        graph_nodes.append({
            "id": key,
            "nid": node.get("id", ""),
            "name": node.get("title", key),
            "group": str(node.get("type", "unknown")).capitalize(),
            "val": val,
            "updated": node.get("updated", ""),
            "summary": node.get("summary", ""),
            "sources": node.get("sources", [])
        })
        
    graph_links = []
    for key, node in nodes_dict.items():
        for target_key in node.get("links", []):
            if target_key in nodes_dict:
                graph_links.append({
                    "source": key,
                    "target": target_key
                })
                
    graph_data = {
        "nodes": graph_nodes,
        "links": graph_links
    }
    
    with open(template_path, "r", encoding="utf-8") as f:
        html = f.read()
        
    html = html.replace("%%GRAPH_DATA%%", json.dumps(graph_data, ensure_ascii=False))
    
    memory_base_path = f"file:///{gemini_dir.replace(os.sep, '/')}/MEMORY/"
    html = html.replace("%%MEMORY_BASE_PATH%%", memory_base_path)
    
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)
        
    webbrowser.open(f"file:///{output_path.replace(os.sep, '/')}")
    
    return f"Visualized {len(graph_nodes)} nodes and {len(graph_links)} links. Opened graph in browser: {output_path}"

__all__ = ["search_vector_lake", "sync_vector_lake", "lint_vector_lake", "query_logic_lake", 
           "visualize_vector_lake", "trigger_serendipity_collision", "review_vector_lake",
           "assemble_context", "delete_source"]
