import os
import json
import logging
import re
from pathlib import Path
import ingest

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("vector-lake-tools")

EXTENSION_ROOT = Path(__file__).parent

def search_vector_lake(query: str, top_k: int = 5, as_xml: bool = False):
    """Search Wiki pages via index.json metadata and file content scanning."""
    wiki_dir = os.path.join(os.path.expanduser("~"), ".gemini", "MEMORY", "wiki")
    index_path = os.path.join(wiki_dir, "index.json")
    
    if not os.path.exists(index_path):
        return "index.json not found. Run sync first."
    
    try:
        with open(index_path, "r", encoding="utf-8") as f:
            index_data = json.load(f)
    except Exception as e:
        return f"Error reading index.json: {e}"
    
    # index.json stores nodes as dict: { "node_key": { id, title, ... } }
    raw_nodes = index_data.get("nodes", {})
    nodes = [{"_key": k, **v} for k, v in raw_nodes.items()]
    query_lower = query.lower()
    query_terms = query_lower.split()
    
    scored = []
    for node in nodes:
        score = 0
        node_key = node.get("_key", "").lower()
        title = (node.get("title") or node.get("id", "")).lower()
        node_type = (node.get("type") or "").lower()
        aliases = [a.lower() for a in node.get("aliases", [])]
        links = [l.lower() for l in node.get("links", [])]
        summary = (node.get("summary") or "").lower()
        
        for term in query_terms:
            if term in node_key: score += 10
            if term in title: score += 10
            if term in summary: score += 3
            for alias in aliases:
                if term in alias: score += 5
            for link in links:
                if term in link: score += 2
        
        if score > 0:
            scored.append((score, node))
    
    scored.sort(key=lambda x: x[0], reverse=True)
    results = scored[:top_k]
    
    if not results:
        return "No relevant information found in the Wiki."
    
    formatted_output = ""
    for i, (score, node) in enumerate(results):
        node_key = node.get("_key", "Unknown")
        title = node.get("title", node_key)
        node_type = node.get("type", "unknown")
        filepath = os.path.join(wiki_dir, f"{node_key}.md")
        
        snippet = ""
        if os.path.exists(filepath):
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    content = f.read()
                # Strip YAML frontmatter
                body = re.sub(r'^---.*?---\s*', '', content, flags=re.DOTALL)
                snippet = body.strip()[:300]
            except Exception:
                snippet = "(unable to read)"
        
        if not as_xml:
            formatted_output += f"- **{title}** ({node_type})\n  {snippet}...\n\n"
        else:
            formatted_output += f'<Evidence_Node ID="Wiki_{i}" Source="{node_key}.md">\n{snippet}\n</Evidence_Node>\n'
    
    return formatted_output

def sync_vector_lake():
    ingest.sync_all()
    import indexer
    indexer.generate_index()
    return "Ingestion Sync and Index generation completed."

def sanitize_wiki_node(filepath: str):
    import os, re, yaml, random, string, datetime
    if not os.path.exists(filepath) or not filepath.endswith(".md"):
        return
        
    # Load SCHEMA_CATEGORIES
    schema_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "SCHEMA_CATEGORIES.md")
    allowed_cats = set()
    if os.path.exists(schema_path):
        with open(schema_path, 'r', encoding='utf-8') as f:
            for line in f:
                m = re.match(r'^-\s*`(\w+)`', line.strip())
                if m: allowed_cats.add(m.group(1))
    if not allowed_cats:
        allowed_cats = {"Uncategorized", "Artificial_Intelligence", "Healthcare_IT", "Strategy_and_Business", "System_Architecture", "Philosophy_and_Cognitive", "Biomedicine"}

    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()

    match = re.match(r'^---\n(.*?)\n---\n(.*)', content, re.DOTALL)
    fm = {}
    body = content
    if match:
        body = match.group(2)
        try:
            fm = yaml.safe_load(match.group(1)) or {}
        except Exception:
            fm = {}

    if not isinstance(fm, dict): fm = {}

    today = datetime.datetime.now().strftime("%Y%m%d")
    
    # Check ID
    current_id = fm.get('id', "")
    if not current_id or str(current_id).startswith("YYYYMMDD") or str(current_id).startswith("Concept_") or str(current_id).startswith("Source_") or str(current_id).startswith("Entity_") or str(current_id).startswith("Synthesis_"):
        rnd = ''.join(random.choices(string.ascii_lowercase + string.digits, k=6))
        fm['id'] = f"{today}_{rnd}"

    # Standardize metadata
    fm['created'] = fm.get('created', today)
    if fm['created'] == "YYYY-MM-DD": fm['created'] = today
    fm['updated'] = today

    if not fm.get('title'):
        fm['title'] = os.path.basename(filepath).replace(".md", "")
        
    if not fm.get('type'):
        prefix = os.path.basename(filepath).split("_")[0].lower()
        fm['type'] = prefix if prefix in ["concept", "source", "entity", "synthesis"] else "concept"
        
    if not fm.get('epistemic-status'):
        fm['epistemic-status'] = "sprouting"

    if 'sources' not in fm or not fm['sources']:
        fm['sources'] = ["raw/legacy_import.md"]
        
    cats = fm.get('categories', [])
    if isinstance(cats, str): cats = [cats]
    if not isinstance(cats, list): cats = []
    
    new_cats = []
    for c in cats:
        c_str = str(c).strip()
        if c_str in allowed_cats:
            new_cats.append(c_str)
        else:
            # Map common hallucinatory categories
            cat_map = {"Healthcare IT": "Healthcare_IT", "Generative AI": "Artificial_Intelligence", "Cognitive Science": "Philosophy_and_Cognitive", "Policy & Strategy": "Strategy_and_Business", "Scientific Research": "Uncategorized"}
            if c_str in cat_map: new_cats.append(cat_map[c_str])
            else: new_cats.append("Uncategorized")
            
    if not new_cats: new_cats = ["Uncategorized"]
    fm['categories'] = list(set(new_cats))

    new_yaml = yaml.dump(fm, allow_unicode=True, default_flow_style=False, sort_keys=False)
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(f"---\n{new_yaml}---\n{body.lstrip()}")


def lint_vector_lake(auto_fix: bool = False):
    """Comprehensive schema-aware lint for the Vector Lake wiki.

    Checks:
      1. YAML Frontmatter completeness (id, title, type, epistemic-status, categories, created, updated, sources)
      2. File name prefix compliance (Source_/Entity_/Concept_/Synthesis_/...)
      3. Type & epistemic-status value legality
      4. Categories against SCHEMA_CATEGORIES.md controlled vocabulary
      5. Duplicate YAML IDs
      6. Alias ↔ ID collisions & shared aliases
      7. Broken links (outbound [[links]] pointing to non-existent nodes)
      8. Orphan pages (zero inbound links from other nodes)
      9. File name similarity (> 0.8 SequenceMatcher ratio)
     10. Knowledge decay (sprouting > 60 days)
    """
    import re
    import yaml
    import difflib
    from collections import defaultdict
    from datetime import datetime

    wiki_dir = os.path.join(os.path.expanduser("~"), ".gemini", "MEMORY", "wiki")
    if not os.path.exists(wiki_dir):
        return "Wiki directory not found."

    # --- Load controlled vocabulary from SCHEMA_CATEGORIES.md ---
    schema_categories_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "SCHEMA_CATEGORIES.md")
    allowed_categories = set()
    if os.path.exists(schema_categories_path):
        with open(schema_categories_path, 'r', encoding='utf-8') as f:
            for line in f:
                m = re.match(r'^-\s*`(\w+)`', line.strip())
                if m:
                    allowed_categories.add(m.group(1))
    if not allowed_categories:
        allowed_categories = {"Uncategorized", "Artificial_Intelligence", "Healthcare_IT",
                              "Strategy_and_Business", "System_Architecture",
                              "Philosophy_and_Cognitive", "Biomedicine"}

    VALID_PREFIXES = ("Concept_", "Source_", "Entity_", "Synthesis_", "Event_", "Person_", "Project_", "Term_", "System_")
    VALID_TYPES = {"entity", "concept", "source", "synthesis"}
    VALID_EPISTEMIC = {"seed", "sprouting", "evergreen", "deprecated"}
    REQUIRED_FM_FIELDS = ("id", "title", "type", "epistemic-status", "categories", "created", "updated", "sources")

    # --- Collection containers ---
    ids = {}                           # id → filename
    aliases_map = defaultdict(list)    # alias → [node_ids]
    node_keys = set()                  # all node_key (filename without .md)
    outbound_links = defaultdict(set)  # node_key → {target_keys}
    inbound_count = defaultdict(int)   # node_key → int

    # Per-file issues
    prefix_violations = []
    fm_missing = []                    # missing frontmatter entirely
    fm_field_issues = []               # missing required fields
    type_violations = []
    epistemic_violations = []
    category_violations = []
    duplicate_ids = []
    decay_warnings = []

    # --- Single pass: parse every file ---
    filenames = [f for f in os.listdir(wiki_dir) if f.endswith(".md") and f not in ("index.md", "log.md")]

    for filename in filenames:
        filepath = os.path.join(wiki_dir, filename)
        node_key = filename[:-3]
        node_keys.add(node_key)

        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                content = f.read()
        except (UnicodeDecodeError, OSError) as e:
            fm_missing.append(f"{filename}: Read error — {e}")
            continue

        # --- Check 2: File name prefix ---
        if not filename.startswith(VALID_PREFIXES):
            prefix_violations.append(filename)

        # --- Parse YAML frontmatter (single canonical parse) ---
        frontmatter_match = re.search(r'^---\n(.*?)\n---', content, re.MULTILINE | re.DOTALL)
        if not frontmatter_match:
            fm_missing.append(filename)
            continue

        try:
            fm_data = yaml.safe_load(frontmatter_match.group(1)) or {}
        except yaml.YAMLError:
            fm_missing.append(f"{filename}: YAML parse error")
            continue

        if not isinstance(fm_data, dict):
            fm_missing.append(f"{filename}: Frontmatter is not a dict")
            continue

        # --- Check 1: Required field completeness ---
        missing_fields = [field for field in REQUIRED_FM_FIELDS if field not in fm_data or fm_data[field] in (None, "", [])]
        if missing_fields:
            fm_field_issues.append(f"{filename}: missing [{', '.join(missing_fields)}]")

        # --- Check 3: Type legality ---
        node_type = str(fm_data.get("type", "")).strip().lower()
        if node_type and node_type not in VALID_TYPES:
            type_violations.append(f"{filename}: type='{fm_data.get('type')}' (valid: {', '.join(sorted(VALID_TYPES))})")

        # --- Check 3: Epistemic-status legality ---
        status = str(fm_data.get("epistemic-status", "")).strip().lower()
        if status and status not in VALID_EPISTEMIC:
            epistemic_violations.append(f"{filename}: epistemic-status='{fm_data.get('epistemic-status')}' (valid: {', '.join(sorted(VALID_EPISTEMIC))})")

        # --- Check 4: Categories against controlled vocabulary ---
        cats = fm_data.get("categories", [])
        if isinstance(cats, list):
            for cat in cats:
                cat_str = str(cat).strip()
                if cat_str and cat_str not in allowed_categories:
                    category_violations.append(f"{filename}: category='{cat_str}' not in SCHEMA_CATEGORIES")

        # --- Check 5: Duplicate ID ---
        node_id = str(fm_data.get("id", "")).strip()
        if node_id:
            if node_id in ids:
                duplicate_ids.append(f"ID '{node_id}' in both '{ids[node_id]}' and '{filename}'")
            else:
                ids[node_id] = filename

        # --- Check 6: Alias collection ---
        raw_aliases = fm_data.get("aliases", [])
        if isinstance(raw_aliases, list):
            for a in raw_aliases:
                aliases_map[str(a).strip()].append(node_key)
        elif isinstance(raw_aliases, str):
            aliases_map[raw_aliases.strip()].append(node_key)

        # --- Collect outbound links for Check 7 & 8 ---
        body = content[frontmatter_match.end():]
        # V7.0 relation links: [Relation:: [[Target]]]
        for m in re.finditer(r'\[([^\[\]]+?)::\s*\[\[(.*?)\]\]\]', body):
            target = m.group(2).split('|')[0].strip().replace('.md', '')
            if target:
                outbound_links[node_key].add(target)
        # Standard wiki links: [[Target]] or [[Target|Alias]]
        for m in re.finditer(r'\[\[(.*?)\]\]', body):
            link_text = m.group(1).split('|')[0].strip().replace('.md', '')
            if '::' in link_text:
                link_text = link_text.split('::', 1)[1].strip()
            if link_text:
                outbound_links[node_key].add(link_text)

        # --- Check 10: Knowledge decay ---
        if status == 'sprouting' and fm_data.get('updated'):
            try:
                dt = datetime.strptime(str(fm_data['updated']), "%Y-%m-%d")
                if (datetime.now() - dt).days > 60:
                    if auto_fix:
                        fm_data['decayed'] = True
                        after_fm = content.split('---', 2)[2]
                        new_content = "---\n" + yaml.dump(fm_data, allow_unicode=True, default_flow_style=False).strip() + "\n---" + after_fm
                        with open(filepath, 'w', encoding='utf-8') as wf:
                            wf.write(new_content)
                        decay_warnings.append(f"[AUTO-FIXED] '{filename}': sprouting >60 days → decayed: true")
                    else:
                        decay_warnings.append(f"'{filename}': sprouting since {fm_data['updated']} (>{(datetime.now() - dt).days} days)")
            except (ValueError, TypeError):
                pass

    # --- Post-scan: Compute broken links & orphans ---
    # Build alias → node_key lookup (node_key itself is also a valid target)
    alias_lookup = {}
    for nk in node_keys:
        alias_lookup[nk] = nk
    for nid, fname in ids.items():
        alias_lookup[nid] = fname.replace('.md', '')
    for alias, nks in aliases_map.items():
        if len(nks) == 1:
            alias_lookup[alias] = nks[0]

    # Check 7: Broken links
    broken_links = []
    for source_key, targets in outbound_links.items():
        for target in targets:
            resolved = alias_lookup.get(target)
            if resolved:
                inbound_count[resolved] += 1
            elif target not in node_keys:
                broken_links.append(f"{source_key}.md → [[{target}]]")

    # Check 8: Orphan pages (zero inbound)
    orphan_pages = [nk for nk in node_keys if inbound_count.get(nk, 0) == 0]

    # Check 9: File name similarity (O(n²) — cap at 50 reports)
    similars = []
    for i in range(len(filenames)):
        if len(similars) >= 50:
            break
        for j in range(i + 1, len(filenames)):
            ratio = difflib.SequenceMatcher(None, filenames[i], filenames[j]).ratio()
            if ratio > 0.8:
                similars.append((filenames[i], filenames[j], ratio))

    # Check 6 resolved: Alias ↔ ID conflicts & shared aliases
    alias_id_conflicts = []
    shared_aliases = []
    id_set = set(ids.keys())
    for alias, nks in aliases_map.items():
        if alias in id_set:
            alias_id_conflicts.append(f"Alias '{alias}' in node(s) {nks} conflicts with ID (file: {ids[alias]})")
        if len(nks) > 1:
            shared_aliases.append(f"Alias '{alias}' shared by: {nks}")

    # ======= Build Report =======
    total_files = len(filenames)
    section_num = 0

    def section(title):
        nonlocal section_num
        section_num += 1
        return f"\n{section_num}. {title}:"

    report = [f"{'='*50}", f" Vector Lake Lint Report (V7.0)", f" Scanned: {total_files} files | auto_fix={'ON' if auto_fix else 'OFF'}", f"{'='*50}"]

    # S1: Frontmatter completeness
    report.append(section("YAML Frontmatter Issues"))
    if fm_missing:
        report.append(f"  Missing or unparseable frontmatter ({len(fm_missing)}):")
        for item in fm_missing[:20]:
            report.append(f"    - {item}")
    if fm_field_issues:
        report.append(f"  Missing required fields ({len(fm_field_issues)}):")
        for item in fm_field_issues[:30]:
            report.append(f"    - {item}")
    if not fm_missing and not fm_field_issues:
        report.append("  ✅ All files have complete frontmatter.")

    # S2: Prefix compliance
    report.append(section("File Name Prefix Compliance"))
    if prefix_violations:
        report.append(f"  Non-compliant filenames ({len(prefix_violations)}):")
        for pv in prefix_violations[:20]:
            report.append(f"    - {pv}")
    else:
        report.append("  ✅ All files follow naming convention.")

    # S3: Type & epistemic-status legality
    report.append(section("Type & Epistemic-Status Legality"))
    if type_violations:
        for tv in type_violations:
            report.append(f"    - {tv}")
    if epistemic_violations:
        for ev in epistemic_violations:
            report.append(f"    - {ev}")
    if not type_violations and not epistemic_violations:
        report.append("  ✅ All type and epistemic-status values are valid.")

    # S4: Category validation
    report.append(section("Categories vs SCHEMA_CATEGORIES"))
    if category_violations:
        report.append(f"  Unauthorized categories ({len(category_violations)}):")
        for cv in category_violations[:20]:
            report.append(f"    - {cv}")
    else:
        report.append("  ✅ All categories are within controlled vocabulary.")

    # S5: Duplicate IDs
    report.append(section("Duplicate IDs"))
    if duplicate_ids:
        for d in duplicate_ids:
            report.append(f"    - {d}")
    else:
        report.append("  ✅ No duplicate IDs found.")

    # S6: Alias conflicts & shared aliases
    report.append(section("Alias Conflicts & Sharing"))
    if alias_id_conflicts:
        report.append(f"  Alias ↔ ID conflicts ({len(alias_id_conflicts)}):")
        for a in alias_id_conflicts:
            report.append(f"    - {a}")
    if shared_aliases:
        report.append(f"  Shared aliases ({len(shared_aliases)}):")
        for s in shared_aliases:
            report.append(f"    - {s}")
    if not alias_id_conflicts and not shared_aliases:
        report.append("  ✅ No alias conflicts.")

    # S7: Broken links
    report.append(section("Broken Links (outbound → non-existent target)"))
    if broken_links:
        report.append(f"  Broken links ({len(broken_links)}):")
        for bl in broken_links[:30]:
            report.append(f"    - {bl}")
        if len(broken_links) > 30:
            report.append(f"    ... and {len(broken_links) - 30} more.")
    else:
        report.append("  ✅ No broken links detected.")

    # S8: Orphan pages
    report.append(section("Orphan Pages (zero inbound links)"))
    if orphan_pages:
        report.append(f"  Orphan pages ({len(orphan_pages)}):")
        for op in sorted(orphan_pages)[:30]:
            report.append(f"    - {op}.md")
        if len(orphan_pages) > 30:
            report.append(f"    ... and {len(orphan_pages) - 30} more.")
    else:
        report.append("  ✅ All pages have at least one inbound link.")

    # S9: File name similarity
    report.append(section("File Name Similarity (ratio > 0.80)"))
    if similars:
        for sim in similars[:20]:
            report.append(f"    - {sim[0]} ↔ {sim[1]} ({sim[2]:.2f})")
        if len(similars) > 20:
            report.append(f"    ... and {len(similars) - 20} more.")
    else:
        report.append("  ✅ No suspiciously similar filenames.")

    # S10: Knowledge Decay
    report.append(section("Knowledge Decay (sprouting > 60 days)"))
    if decay_warnings:
        for dw in decay_warnings:
            report.append(f"    - {dw}")
    else:
        report.append("  ✅ No knowledge decay detected.")

    # Summary
    total_issues = (len(fm_missing) + len(fm_field_issues) + len(prefix_violations)
                    + len(type_violations) + len(epistemic_violations) + len(category_violations)
                    + len(duplicate_ids) + len(alias_id_conflicts) + len(shared_aliases)
                    + len(broken_links) + len(decay_warnings))
    report.append(f"\n{'='*50}")
    report.append(f" TOTAL: {total_issues} issues | {len(orphan_pages)} orphans | {len(similars)} similar-name pairs")
    report.append(f"{'='*50}")

    return "\n".join(report)

def query_logic_lake(query_str: str, dry_run: bool = False):
    import subprocess
    import re
    wiki_dir = os.path.join(os.path.expanduser("~"), ".gemini", "MEMORY", "wiki")
    schema_path = os.path.join(EXTENSION_ROOT, "schema.md")
    
    # ... (same logic for context gathering)
    try:
        index_path = os.path.join(wiki_dir, "index.json")
        evidence_context = ""
        subgraph_context = ""
        
        if os.path.exists(index_path):
            with open(index_path, "r", encoding="utf-8") as f:
                index_data = json.load(f)
            
            raw_nodes = index_data.get("nodes", {})
            nodes = [{"_key": k, **v} for k, v in raw_nodes.items()]
            query_lower = query_str.lower()
            query_terms = query_lower.split()
            
            # Score and rank nodes
            scored = []
            for node in nodes:
                score = 0
                title = (node.get("title") or node.get("id", "")).lower()
                for term in query_terms:
                    if term in title: score += 10
                    for alias in [a.lower() for a in node.get("aliases", [])]:
                        if term in alias: score += 5
                    for link in [l.lower() for l in node.get("links", [])]:
                        if term in link: score += 2
                if score > 0:
                    scored.append((score, node))
            
            scored.sort(key=lambda x: x[0], reverse=True)
            top_nodes = scored[:10]
            
            # Build evidence context from top matching files
            evidence_context = "=== Index Search Evidence (Top 10) ===\n"
            for _, node in top_nodes:
                node_key = node.get("_key", "")
                filepath = os.path.join(wiki_dir, f"{node_key}.md")
                if os.path.exists(filepath):
                    with open(filepath, "r", encoding="utf-8") as f:
                        content = f.read()
                    evidence_context += f"Evidence Segment ({node_key}): {content[:500]}...\n\n"
            
            # Build tacit subgraph context from top 3
            focal_nodes = top_nodes[:3]
            subgraph_context = "=== 默会图谱关联上下文 (Tacit Subgraph Topology) ===\n"
            if not focal_nodes:
                subgraph_context += "No immediate focal nodes found.\n"
            else:
                subgraph_context += f"Found {len(focal_nodes)} focal nodes. Extracting 1-degree tacit connections...\n"
                for _, node in focal_nodes:
                    node_id = node.get("id", "")
                    links = node.get("links", [])
                    subgraph_context += f"\n--- Focal Node: {node_id} ---\n"
                    if not links:
                        subgraph_context += "  (No outgoing links)\n"
                    else:
                        subgraph_context += "  -> Tacit Neighbors: " + ", ".join(links) + "\n"
        else:
            evidence_context = "index.json not found. Run sync first."
            subgraph_context = ""
    except Exception as e:
        evidence_context = "Failed to fetch search evidence."
        subgraph_context = f"Failed to compile tacit subgraph: {e}"
        log.error(subgraph_context)

    persistence_instruction = "Please execute synthesis and output the resulting Markdown to stdout. DO NOT call any write tools or persist to disk." if dry_run else "Please execute synthesis and persist to Wiki."

    prompt = f"""
@vector-lake-synthesizer
[Deep Research Synthesis Sequence Triggered]

Target Query:
"{query_str}"

{evidence_context}
{subgraph_context}

{persistence_instruction}
"""
    gemini_exec = "gemini.cmd" if os.name == "nt" else "gemini"
    cmd = [gemini_exec, "--prompt", prompt, "--approval-mode", "yolo"]
    
    log.info(f"Triggering Native Agent for deep query: '{query_str}'...")
    try:
        result = subprocess.run(cmd, input=prompt, text=True, encoding='utf-8', timeout=300)
        if result.returncode == 0:
            import time
            now_time = time.time()
            for f in os.listdir(wiki_dir):
                if f.endswith(".md"):
                    p = os.path.join(wiki_dir, f)
                    if os.path.getmtime(p) > now_time - 300:
                        sanitize_wiki_node(p)
            return "Query logic lake operation completed successfully."
        else:
            return f"Agent returned non-zero exit code: {result.returncode}"
    except Exception as e:
        return f"Error triggering agent: {e}"

def trigger_serendipity_collision():
    import random
    import subprocess
    wiki_dir = os.path.join(os.path.expanduser("~"), ".gemini", "MEMORY", "wiki")
    
    files = [f for f in os.listdir(wiki_dir) if f.endswith(".md") and f not in ("index.md", "log.md")]
    if len(files) < 2:
        return "Not enough nodes in Wiki to perform serendipity collision."
        
    node_a, node_b = random.sample(files, 2)
    
    node_a_path = os.path.join(wiki_dir, node_a)
    node_b_path = os.path.join(wiki_dir, node_b)
    
    with open(node_a_path, 'r', encoding='utf-8') as f:
        node_a_content = f.read()
    with open(node_b_path, 'r', encoding='utf-8') as f:
        node_b_content = f.read()

    prompt = f"""
@vector-lake-collider
[Serendipity Collision Triggered]

=== Node A: {node_a} ===
{node_a_content}

=== Node B: {node_b} ===
{node_b_content}

Please execute lateral synthesis and persist the result into the Vector Lake.
"""
    gemini_exec = "gemini.cmd" if os.name == "nt" else "gemini"
    cmd = [gemini_exec, "--prompt", prompt, "--approval-mode", "yolo"]
    
    log.info(f"Triggering Serendipity Collider between {node_a} and {node_b}...")
    try:
        result = subprocess.run(cmd, input=prompt, text=True, encoding='utf-8', timeout=300)
        if result.returncode == 0:
            import time
            now_time = time.time()
            for f in os.listdir(wiki_dir):
                if f.endswith(".md"):
                    p = os.path.join(wiki_dir, f)
                    if os.path.getmtime(p) > now_time - 300:
                        sanitize_wiki_node(p)
            return f"Serendipity synthesis completed for {node_a} & {node_b}."
        else:
            return f"Agent returned non-zero exit code: {result.returncode}"
    except Exception as e:
        return f"Error triggering serendipity agent: {e}"

def visualize_vector_lake():
    import os
    import re
    import json
    import webbrowser

    wiki_dir = os.path.join(os.path.expanduser("~"), ".gemini", "MEMORY", "wiki")
    if not os.path.exists(wiki_dir):
        return "Wiki directory not found."

    nodes = []
    edges = []
    node_ids = set()

    for filename in os.listdir(wiki_dir):
        if not filename.endswith(".md") or filename in ("index.md", "log.md"):
            continue

        filepath = os.path.join(wiki_dir, filename)
        node_id = filename[:-3]
        node_ids.add(node_id)

        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                content = f.read()

            title = node_id
            node_type = "concept"
            updated = "Unknown"
            sources = []

            import yaml
            
            # Robust YAML frontmatter parsing
            frontmatter_match = re.search(r'^---\n(.*?)\n---', content, re.MULTILINE | re.DOTALL)
            if frontmatter_match:
                fm_str = frontmatter_match.group(1)
                try:
                    fm_data = yaml.safe_load(fm_str)
                    if isinstance(fm_data, dict):
                        title = str(fm_data.get('title', title)).strip()
                        node_type = str(fm_data.get('type', node_type)).strip()
                        updated = str(fm_data.get('updated', updated)).strip()
                        
                        raw_sources = fm_data.get('sources', [])
                        if isinstance(raw_sources, list):
                            sources = [str(s).strip() for s in raw_sources]
                        elif isinstance(raw_sources, str):
                            sources = [raw_sources.strip()]
                except yaml.YAMLError as e:
                    log.warning(f"YAML parsing failed for {filename}, falling back to defaults. Error: {e}")

            # Extract Summary (first ~200 chars of text after frontmatter)
            text_content = content[frontmatter_match.end():] if frontmatter_match else content
            text_content = re.sub(r'#.*?\n', '', text_content) # Remove markdown headings
            text_content = re.sub(r'\[\[(.*?)\]\]', lambda m: m.group(1).split('|')[0], text_content) # Clean links
            text_content = text_content.strip().replace('\n', ' ')
            summary = text_content[:200] + "..." if len(text_content) > 200 else text_content

            # Semantic color mapping for the nodes
            color = "#00B0FF" # Default Quantum Blue
            if "entity" in node_type.lower(): color = "#EF4444"
            elif "source" in node_type.lower(): color = "#10B981"
            elif "synthesis" in node_type.lower(): color = "#F59E0B"

            nodes.append({
                "id": node_id,
                "name": title,
                "val": max(5, min(30, len(content) / 200)), # Cap size scaling
                "color": color,
                "group": node_type,
                "updated": updated,
                "sources": sources,
                "summary": summary
            })

            # Extract V6.0 links [Relation:: [[Target]]]
            v6_matches = re.finditer(r'\[([^\[\]]+?)::\s*\[\[(.*?)\]\]\]', content)
            for match in v6_matches:
                relation = match.group(1).strip()
                target_id = match.group(2).split('|')[0].strip().replace('.md', '')
                edges.append({
                    "source": node_id,
                    "target": target_id,
                    "relation": relation
                })

            # Extract semantic bidirectional links [[Relation::Link]] or raw [[Link|Alias]]
            link_matches = re.finditer(r'\[\[(.*?)\]\]', content)
            for match in link_matches:
                link_text = match.group(1)
                relation = "关联 (Related)"
                
                if "::" in link_text:
                    parts = link_text.split("::", 1)
                    relation = parts[0].strip()
                    target_text = parts[1].strip()
                    target_id = target_text.split('|')[0].strip().replace('.md', '')
                else:
                    target_id = link_text.split('|')[0].strip().replace('.md', '')
                    
                edges.append({
                    "source": node_id,
                    "target": target_id,
                    "relation": relation
                })

        except Exception as e:
            log.error(f"Error parsing {filename}: {e}")

    # Add implicit 'ghost' nodes that are linked to but don't exist as files yet
    existing_targets = set(edge["target"] for edge in edges)
    missing_nodes = existing_targets - node_ids
    for missing in missing_nodes:
        nodes.append({
            "id": missing,
            "name": missing,
            "val": 3,
            "color": "#9CA3AF", # Gray
            "group": "ghost",
            "updated": "N/A",
            "sources": [],
            "summary": "This entity has been referenced but does not have a dedicated Markdown file yet."
        })

    graph_data = {"nodes": nodes, "links": edges}

    # Load HTML template from external file
    template_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates", "topology.html")
    with open(template_path, "r", encoding="utf-8") as f:
        html_template = f.read()

    memory_base_path = "file:///" + os.path.join(os.path.expanduser('~'), '.gemini', 'MEMORY').replace(chr(92), '/') + "/"
    html_content = html_template.replace("%%GRAPH_DATA%%", json.dumps(graph_data)).replace("%%MEMORY_BASE_PATH%%", memory_base_path)

    output_html = os.path.join(wiki_dir, "tactical_topology.html")
    with open(output_html, "w", encoding="utf-8") as f:
        f.write(html_content)

    webbrowser.open(f"file:///{output_html.replace(chr(92), '/')}")
    return f"Topology graph generated and opened: {output_html}"

__all__ = [
    "search_vector_lake",
    "sync_vector_lake",
    "lint_vector_lake",
    "query_logic_lake",
    "visualize_vector_lake",
    "trigger_serendipity_collision"
]
