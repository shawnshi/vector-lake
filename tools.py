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

def lint_vector_lake(auto_fix: bool = False):
    import re
    from collections import defaultdict
    import difflib
    
    wiki_dir = os.path.join(os.path.expanduser("~"), ".gemini", "MEMORY", "wiki")
    if not os.path.exists(wiki_dir):
        return "Wiki directory not found."
    
    ids = {}
    aliases_map = defaultdict(list)
    potential_dups = []
    decay_warnings = []
    
    import yaml
    from datetime import datetime
    
    for filename in os.listdir(wiki_dir):
        if not filename.endswith(".md") or filename in ("index.md", "log.md"):
            continue
        filepath = os.path.join(wiki_dir, filename)
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()
            
            try:
                parts = content.split('---')
                if len(parts) >= 3:
                    fm_data = yaml.safe_load(parts[1])
                    if fm_data and isinstance(fm_data, dict):
                        status = fm_data.get('epistemic-status', '').strip().lower()
                        updated = fm_data.get('updated', '')
                        if status == 'sprouting' and updated:
                            try:
                                dt = datetime.strptime(str(updated), "%Y-%m-%d")
                                if (datetime.now() - dt).days > 60:
                                    if auto_fix:
                                        # auto-fix logic
                                        fm_data['decayed'] = True
                                        new_fm_str = yaml.dump(fm_data, allow_unicode=True, default_flow_style=False)
                                        # Write back modifying just the frontmatter
                                        new_content = "---\n" + new_fm_str + "---\n" + '---'.join(parts[2:])
                                        if len(parts) > 2:
                                            # in case there are multiple '---' in the text body
                                            new_content = "---\n" + new_fm_str.strip() + "\n" + '---'.join([''] + parts[2:])
                                        
                                        # More precise replacement
                                        # parts[1] is the frontmatter
                                        # To avoid issues with multiple --- in content, we replace the first occurrence
                                        
                                        # Actually, safe reconstruct:
                                        after_frontmatter = content.split('---', 2)[2]
                                        new_content = "---\n" + yaml.dump(fm_data, allow_unicode=True, default_flow_style=False).strip() + "\n---" + after_frontmatter
                                        
                                        with open(filepath, 'w', encoding='utf-8') as wf:
                                            wf.write(new_content)
                                        decay_warnings.append(f"DECAY WARNING [AUTO-FIXED]: '{filename}' status is sprouting and unmodified for >60 days. Added `decayed: true`.")
                                    else:
                                        decay_warnings.append(f"DECAY WARNING: '{filename}' status is sprouting and unmodified for >60 days.")
                            except ValueError:
                                pass
            except Exception:
                pass

            id_match = re.search(r'^id:\s*"(.*?)"|^id:\s*\'(.*?)\'|^id:\s*([^\s]+)', content, re.MULTILINE)
            node_id = id_match.group(1) or id_match.group(2) or id_match.group(3) if id_match else filename.replace(".md", "")
            if node_id in ids:
                potential_dups.append(f"DUPLICATE ID: '{node_id}' found in both '{ids[node_id]}' and '{filename}'")
            else:
                ids[node_id] = filename
                
            alias_match = re.search(r'^aliases:\s*\[(.*?)\]', content, re.MULTILINE)
            if alias_match:
                aliases_str = alias_match.group(1)
                aliases = [a.strip().strip('"').strip("'") for a in aliases_str.split(',') if a.strip()]
                for alias in aliases:
                    aliases_map[alias].append(node_id)
                    
    report = ["--- Wiki Lint Report ---"]
    if potential_dups:
        report.append("1. Duplicate IDs Found:")
        for dup in potential_dups:
            report.append(f"  - {dup}")
    else:
        report.append("1. No Duplicate IDs Found.")
        
    report.append("\n2. Alias Conflicts (Alias matching an existing ID):")
    conflict_found = False
    for alias, nodes in aliases_map.items():
        if alias in ids:
            report.append(f"  - Alias '{alias}' in node(s) {nodes} conflicts with existing Node ID '{alias}' (file: {ids[alias]})")
            conflict_found = True
    if not conflict_found:
        report.append("  - No Alias Conflicts Found.")
        
    report.append("\n3. Shared Aliases (Multiple nodes using the same alias):")
    shared_found = False
    for alias, nodes in aliases_map.items():
        if len(nodes) > 1:
            report.append(f"  - Alias '{alias}' is shared by nodes: {nodes}")
            shared_found = True
    if not shared_found:
        report.append("  - No Shared Aliases Found.")
        
    report.append("\n4. Checking for File Name similarities:")
    filenames = [f for f in os.listdir(wiki_dir) if f.endswith(".md") and f not in ("index.md", "log.md")]
    similars = []
    for i in range(len(filenames)):
        for j in range(i+1, len(filenames)):
            seq = difflib.SequenceMatcher(None, filenames[i], filenames[j])
            if seq.ratio() > 0.8:
                similars.append((filenames[i], filenames[j], seq.ratio()))
    if similars:
        for sim in similars:
            report.append(f"  - Similar files: {sim[0]} and {sim[1]} (similarity: {sim[2]:.2f})")
    else:
        report.append("  - No obvious similar filenames.")
        
    report.append("\n5. Knowledge Decay Warnings (sprouting > 60 days):")
    if decay_warnings:
        for warning in decay_warnings:
            report.append(f"  - {warning}")
    else:
        report.append("  - No knowledge decay detected.")
        
    return "\n".join(report)

def query_logic_lake(query_str: str):
    import subprocess
    import re
    wiki_dir = os.path.join(os.path.expanduser("~"), ".gemini", "MEMORY", "wiki")
    schema_path = os.path.join(EXTENSION_ROOT, "schema.md")
    
    # --- Evidence Context from index.json + File Scanning ---
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
    # -------------------------------------------------------------

    prompt = f"""
@vector-lake-synthesizer
[Deep Research Synthesis Sequence Triggered]

Target Query:
"{query_str}"

{evidence_context}
{subgraph_context}

Please execute synthesis and persist to Wiki.
"""
    gemini_exec = "gemini.cmd" if os.name == "nt" else "gemini"
    cmd = [gemini_exec, "--prompt", prompt, "--approval-mode", "yolo"]
    
    log.info(f"Triggering Native Agent for deep query: '{query_str}'...")
    try:
        result = subprocess.run(cmd, input=prompt, text=True, encoding='utf-8', timeout=300)
        if result.returncode == 0:
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
