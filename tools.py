import os
import json
import logging
from pathlib import Path
import ingest

from db import get_chroma_client
from embedding import GeminiEmbeddingFunction

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("vector-lake-tools")

EXTENSION_ROOT = Path(__file__).parent

def search_vector_lake(query: str, top_k: int = 5, as_xml: bool = False):
    """Semantic search over the Wiki pages via ChromaDB."""
    client = get_chroma_client()
    embedding_func = GeminiEmbeddingFunction()
    
    try:
        collection = client.get_collection(name="vector_lake", embedding_function=embedding_func)
        results = collection.query(
            query_texts=[query],
            n_results=top_k
        )
        
        if not results.get('documents') or not results['documents'][0]:
            return "No relevant information found in the Wiki."
            
        formatted_output = ""
        unique_sources = set()
        
        for i, doc in enumerate(results['documents'][0]):
            meta = results['metadatas'][0][i] if results.get('metadatas') and results['metadatas'][0] else {}
            source = meta.get('source', 'Unknown')
            unique_sources.add(source)
            if not as_xml:
                formatted_output += f"- **Source**: {os.path.basename(source)}\n  **Content**: {doc[:300]}...\n\n"
            else:
                formatted_output += f'<Evidence_Node ID="Wiki_{i}" Source="{os.path.basename(source)}">\n{doc}\n</Evidence_Node>\n'
                
        return formatted_output
    except Exception as e:
        return f"Error querying vector lake: {e}"

def sync_vector_lake():
    ingest.sync_all()
    return "Ingestion Sync completed."

def lint_vector_lake():
    import re
    from collections import defaultdict
    import difflib
    
    wiki_dir = os.path.join(os.path.expanduser("~"), ".gemini", "MEMORY", "wiki")
    if not os.path.exists(wiki_dir):
        return "Wiki directory not found."
    
    ids = {}
    aliases_map = defaultdict(list)
    potential_dups = []
    
    for filename in os.listdir(wiki_dir):
        if not filename.endswith(".md") or filename in ("index.md", "log.md"):
            continue
        filepath = os.path.join(wiki_dir, filename)
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()
            id_match = re.search(r'^id:\s*"(.*?)"', content, re.MULTILINE)
            node_id = id_match.group(1) if id_match else filename.replace(".md", "")
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
        
    return "\n".join(report)

def query_logic_lake(query_str: str):
    import subprocess
    wiki_dir = os.path.join(os.path.expanduser("~"), ".gemini", "MEMORY", "wiki")
    schema_path = os.path.join(EXTENSION_ROOT, "schema.md")
    
    prompt = f"""
You are the Vector Lake Deep Research Agent (Query-to-Page Compiler).
The user has requested a deep analysis/insight generation on the following topic or query:
"{query_str}"

Your mandate:
1. Execute semantic search using the `run_shell_command` tool to execute: `python C:\\Users\\shich\\.gemini\\extensions\\vector-lake\\cli.py search "{query_str}" --top_k 10`.
2. Based on the retrieved evidence, synthesize a high-density, structured insight report. 
3. You MUST physically save this report as a new Markdown node in {wiki_dir} using `write_file`. Follow the rules (YAML frontmatter, [[双向链接]]) defined in {schema_path}.
4. Terminate immediately after the file system operations are complete. Do not ask for further instructions.
"""
    gemini_exec = "gemini.cmd" if os.name == "nt" else "gemini"
    cmd = [gemini_exec, "--prompt", "", "--approval-mode", "yolo"]
    
    log.info(f"Triggering Native Agent for deep query: '{query_str}'...")
    try:
        result = subprocess.run(cmd, input=prompt, text=True, encoding='utf-8')
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
    
    prompt = f"""
You are the Vector Lake Serendipity Engine.
Your task is to find hidden, non-obvious cross-disciplinary insights between two random nodes.

Node A: {node_a}
Node B: {node_b}

Workflow:
1. Read both markdown files in {wiki_dir}.
2. Actively look for structural isomorphisms, hidden contradictions, or orthogonal synthesis points.
3. If the collision yields a high-density insight, save it as a new page named `Synthesis_Serendipity_[Topic].md` in {wiki_dir}. Use epistemic-status and [[Relation::Target]] formatting.
4. Link back to Node A and Node B using `[[衍生于::{node_a}]]` and `[[衍生于::{node_b}]]`.
5. Update `MEMORY/wiki/index.md` and `log.md`.
Never ask for user permission. Just do it and terminate.
"""
    gemini_exec = "gemini.cmd" if os.name == "nt" else "gemini"
    cmd = [gemini_exec, "--prompt", "", "--approval-mode", "yolo"]
    
    log.info(f"Triggering Serendipity Collider between {node_a} and {node_b}...")
    try:
        result = subprocess.run(cmd, input=prompt, text=True, encoding='utf-8')
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

            # Extract semantic bidirectional links [[Relation::Link]] or [[Link|Alias]]
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

    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Vector Lake 3D Topology (V5.0)</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
    <style> 
        body {{ margin: 0; padding: 0; background-color: #0A0F14; overflow: hidden; font-family: 'Inter', sans-serif; color: #E5E7EB; }} 
        ::-webkit-scrollbar {{ width: 6px; height: 6px; background: transparent; }}
        ::-webkit-scrollbar-thumb {{ background: rgba(156, 163, 175, 0.2); border-radius: 3px; }}
        ::-webkit-scrollbar-thumb:hover {{ background: rgba(156, 163, 175, 0.4); }}
        ::-webkit-scrollbar-corner {{ background: transparent; }}
        #info-panel {{
            position: absolute;
            top: 20px;
            left: 20px;
            width: 350px;
            max-height: 80vh;
            overflow-y: auto;
            background: rgba(15, 23, 42, 0.85);
            border: 1px solid #374151;
            border-radius: 8px;
            padding: 20px;
            box-shadow: 0 10px 15px -3px rgba(0, 0, 0, 0.5);
            z-index: 10;
            display: none;
            backdrop-filter: blur(8px);
            pointer-events: none; /* Let clicks pass through if needed, though we probably want text selection */
        }}
        #info-panel.visible {{ display: block; pointer-events: auto; }}
        #info-title {{ margin: 0 0 12px 0; font-size: 1.3rem; color: #60A5FA; line-height: 1.2; }}
        #info-meta {{ font-size: 0.8rem; color: #9CA3AF; margin-bottom: 12px; border-bottom: 1px solid #374151; padding-bottom: 8px; }}
        #info-meta span {{ display: block; margin-bottom: 4px; }}
        #info-summary {{ font-size: 0.9rem; line-height: 1.5; margin-bottom: 15px; color: #D1D5DB; }}
        #info-sources {{ font-size: 0.85rem; color: #10B981; word-break: break-all; background: rgba(0,0,0,0.3); padding: 8px; border-radius: 4px; }}
        #info-sources ul {{ margin: 5px 0 0 0; padding-left: 20px; }}
        #close-btn {{ position: absolute; top: 10px; right: 15px; cursor: pointer; color: #9CA3AF; font-size: 1.2rem; }}
        #close-btn:hover {{ color: #FFFFFF; }}
        #info-neighbors {{ margin-top: 15px; border-top: 1px dashed #374151; padding-top: 10px; font-size: 0.9rem; color: #E5E7EB; }}
        #search-container {{ position: absolute; top: 20px; right: 20px; width: 250px; z-index: 10; font-family: sans-serif; }}
        #search-input {{ width: 100%; padding: 10px; background: rgba(15, 23, 42, 0.85); border: 1px solid #374151; border-radius: 6px; color: #E5E7EB; font-size: 0.9rem; outline: none; box-sizing: border-box; backdrop-filter: blur(8px); }}
        #search-input:focus {{ border-color: #60A5FA; }}
        #search-results {{ background: rgba(15, 23, 42, 0.95); border: 1px solid #374151; border-radius: 0 0 6px 6px; border-top: none; max-height: 300px; overflow-y: auto; display: none; backdrop-filter: blur(8px); box-shadow: 0 10px 15px -3px rgba(0, 0, 0, 0.5); }}
        .search-item {{ padding: 8px 10px; cursor: pointer; font-size: 0.85rem; border-bottom: 1px solid #1F2937; }}
        .search-item:hover {{ background: #374151; color: #60A5FA; }}
        #node-tooltip {{ position: absolute; background: rgba(15, 23, 42, 0.85); border: 1px solid #374151; border-radius: 6px; padding: 6px 10px; color: #E5E7EB; font-size: 0.8rem; pointer-events: none; display: none; z-index: 20; backdrop-filter: blur(8px); white-space: nowrap; box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.5); }}
        #node-tooltip .tt-title {{ font-weight: 600; color: #60A5FA; margin-bottom: 2px; font-size: 0.9rem; }}
        #node-tooltip .tt-meta {{ font-size: 0.75rem; color: #9CA3AF; }}
        #filter-hud {{ position: absolute; top: 20px; right: 285px; width: 180px; background: rgba(15, 23, 42, 0.85); border: 1px solid #374151; border-radius: 8px; padding: 12px 15px; box-shadow: 0 10px 15px -3px rgba(0, 0, 0, 0.5); z-index: 10; backdrop-filter: blur(8px); }}
        #filter-hud h4 {{ margin: 0 0 10px 0; font-size: 0.85rem; color: #9CA3AF; text-transform: uppercase; letter-spacing: 0.05em; }}
        .filter-checkbox {{ display: flex; align-items: center; margin-bottom: 6px; font-size: 0.85rem; cursor: pointer; color: #D1D5DB; }}
        .filter-checkbox input {{ margin-right: 8px; cursor: pointer; accent-color: #60A5FA; }}
        .filter-checkbox:hover {{ color: #FFFFFF; }}
    </style>
    <script src="https://unpkg.com/3d-force-graph"></script>
</head>
<body>
    <div id="info-panel">
        <div id="close-btn" onclick="document.getElementById('info-panel').classList.remove('visible')">×</div>
        <h2 id="info-title"></h2>
        <div id="info-meta"></div>
        <div id="info-summary"></div>
        <div id="info-sources"></div>
        <div id="info-neighbors"></div>
    </div>
    <div id="node-tooltip"></div>
    <div id="filter-hud">
        <h4 id="filter-toggle" style="cursor: pointer; display: flex; justify-content: space-between; align-items: center; margin-bottom: 0;">
            Epistemic Filters <span id="filter-icon" style="font-size: 0.7rem; color: #60A5FA;">▼</span>
        </h4>
        <div id="filter-options" style="display: none; margin-top: 10px;"></div>
    </div>
    <div id="search-container">
        <input type="text" id="search-input" placeholder="Search Node..." autocomplete="off">
        <div id="search-results"></div>
    </div>
    <div id="3d-graph"></div>
    <script>
        document.getElementById('filter-toggle').addEventListener('click', () => {{
            const opts = document.getElementById('filter-options');
            const icon = document.getElementById('filter-icon');
            if (opts.style.display === 'none') {{
                opts.style.display = 'block';
                icon.innerText = '▲';
            }} else {{
                opts.style.display = 'none';
                icon.innerText = '▼';
            }}
        }});
        
        const data = {json.dumps(graph_data)};
        const nodeById = Object.fromEntries(data.nodes.map(n => [n.id, n]));
        
        const highlightNodes = new Set();
        const highlightLinks = new Set();
        let hoverNode = null;
        
        const activeFilters = new Set();
        const groups = [...new Set(data.nodes.map(n => n.group))].sort();
        groups.forEach(g => activeFilters.add(g));
        
        const filterOpts = document.getElementById('filter-options');
        groups.forEach(g => {{
            const lbl = document.createElement('label');
            lbl.className = 'filter-checkbox';
            lbl.innerHTML = `<input type="checkbox" value="${{g}}" checked> ${{g}}`;
            lbl.querySelector('input').addEventListener('change', (e) => {{
                if(e.target.checked) activeFilters.add(g);
                else activeFilters.delete(g);
                Graph.nodeVisibility(Graph.nodeVisibility());
                Graph.linkVisibility(Graph.linkVisibility());
            }});
            filterOpts.appendChild(lbl);
        }});
        
        // Track mouse globally for tooltip
        document.addEventListener('mousemove', e => {{
            const tt = document.getElementById('node-tooltip');
            if (tt.style.display === 'block') {{
                tt.style.left = (e.clientX + 15) + 'px';
                tt.style.top = (e.clientY + 15) + 'px';
            }}
        }});
        
        window.focusNode = function(nodeId) {{
            const node = nodeById[nodeId];
            if (!node) return;
            
            // Spotlight logic
            highlightNodes.clear();
            highlightLinks.clear();
            if (node) {{
                highlightNodes.add(node);
                data.links.forEach(l => {{
                    const s = l.source.id || l.source;
                    const t = l.target.id || l.target;
                    if (s === node.id || t === node.id) {{
                        highlightLinks.add(l);
                        highlightNodes.add(typeof l.source === 'object' ? l.source : nodeById[s]);
                        highlightNodes.add(typeof l.target === 'object' ? l.target : nodeById[t]);
                    }}
                }});
            }}
            if (typeof Graph !== 'undefined') {{
                Graph.nodeColor(Graph.nodeColor()).linkWidth(Graph.linkWidth()).linkColor(Graph.linkColor()).linkDirectionalParticles(Graph.linkDirectionalParticles());
            }}
            
            // Camera focus
            const distance = 40;
            const distRatio = 1 + distance/Math.hypot(node.x || 0, node.y || 0, node.z || 0);
            if (Graph.cameraPosition) {{
                Graph.cameraPosition({{ x: node.x * distRatio, y: node.y * distRatio, z: node.z * distRatio }}, node, 3000);
            }}
            
            // Update Info Panel
            document.getElementById('info-title').innerText = node.name;
            
            let metaHtml = `<span><b>Type:</b> ${{node.group}}</span>`;
            metaHtml += `<span><b>Updated:</b> ${{node.updated || 'N/A'}}</span>`;
            document.getElementById('info-meta').innerHTML = metaHtml;
            
            document.getElementById('info-summary').innerText = node.summary || '';
            
            let memoryBasePath = "file:///{os.path.join(os.path.expanduser('~'), '.gemini', 'MEMORY').replace(chr(92), '/')}/";
            let sourcesHtml = '';
            if (node.sources && node.sources.length > 0) {{
                sourcesHtml = '<b>Raw Sources:</b><ul>' + node.sources.map(s => {{
                    let url = s.startsWith('http') ? s : memoryBasePath + s;
                    return `<li><a href="${{url}}" target="_blank" style="color: #60A5FA; text-decoration: underline; word-wrap: break-word;">${{s}}</a></li>`;
                }}).join('') + '</ul>';
            }}
            document.getElementById('info-sources').innerHTML = sourcesHtml;
            
            // Inject Neighbors Logic
            let inbound = data.links.filter(l => (l.target.id === node.id || l.target === node.id));
            let outbound = data.links.filter(l => (l.source.id === node.id || l.source === node.id));
            
            let neighborsHtml = ``;
            if(outbound.length > 0) {{
                neighborsHtml += `<div><b>Outbound Links:</b><ul style="margin-top:4px; padding-left:15px; font-size:0.85em; list-style-type:none; margin-left:0; padding-left:0;">`;
                outbound.forEach(l => {{
                    let tId = l.target.id || l.target;
                    let rel = l.relation || '关联';
                    let relColor = '#9CA3AF';
                    if (rel.includes('反驳')) relColor = '#EF4444';
                    else if (rel.includes('支持')) relColor = '#10B981';
                    else if (rel.includes('衍生')) relColor = '#60A5FA';
                    
                    neighborsHtml += `<li style="margin-bottom:6px;">
                        <span style="color:${{relColor}}; font-size:0.8em; border:1px solid ${{relColor}}; padding:1px 4px; border-radius:3px; margin-right:6px;">${{rel}}</span> 
                        <a href="javascript:void(0)" onclick="window.focusNode('${{tId}}')" style="color:#D1D5DB; text-decoration:underline;">${{nodeById[tId] ? nodeById[tId].name : tId}}</a>
                    </li>`;
                }});
                neighborsHtml += `</ul></div>`;
            }}
            
            if(inbound.length > 0) {{
                neighborsHtml += `<div style="margin-top:10px;"><b>Inbound Links:</b><ul style="margin-top:4px; padding-left:15px; font-size:0.85em; list-style-type:none; margin-left:0; padding-left:0;">`;
                inbound.forEach(l => {{
                    let sId = l.source.id || l.source;
                    let rel = l.relation || '关联';
                    let relColor = '#9CA3AF';
                    if (rel.includes('反驳')) relColor = '#EF4444';
                    else if (rel.includes('支持')) relColor = '#10B981';
                    else if (rel.includes('衍生')) relColor = '#60A5FA';
                    
                    neighborsHtml += `<li style="margin-bottom:6px;">
                        <span style="color:${{relColor}}; font-size:0.8em; border:1px solid ${{relColor}}; padding:1px 4px; border-radius:3px; margin-right:6px;">${{rel}}</span> 
                        <a href="javascript:void(0)" onclick="window.focusNode('${{sId}}')" style="color:#D1D5DB; text-decoration:underline;">${{nodeById[sId] ? nodeById[sId].name : sId}}</a>
                    </li>`;
                }});
                neighborsHtml += `</ul></div>`;
            }}
            document.getElementById('info-neighbors').innerHTML = neighborsHtml;
            
            document.getElementById('info-panel').classList.add('visible');
        }};
        
        const Graph = ForceGraph3D()
            (document.getElementById('3d-graph'))
            .graphData(data)
            .nodeVisibility(node => activeFilters.has(node.group))
            .linkVisibility(link => {{
                const sGrp = typeof link.source === 'object' ? link.source.group : (nodeById[link.source] || {{}}).group;
                const tGrp = typeof link.target === 'object' ? link.target.group : (nodeById[link.target] || {{}}).group;
                return activeFilters.has(sGrp) && activeFilters.has(tGrp);
            }})
            .nodeLabel(node => '')
            .nodeAutoColorBy('group')
            .nodeColor(node => {{
                const activeMode = highlightNodes.size > 0;
                if (activeMode && !highlightNodes.has(node)) {{
                    return 'rgba(50, 50, 50, 0.1)';
                }}
                return node === hoverNode ? '#FFFFFF' : node.color;
            }})
            .nodeVal('val')
            .linkLabel(link => '')
            .linkDirectionalArrowLength(3.5)
            .linkDirectionalArrowRelPos(1)
            .linkColor(link => {{
                const activeMode = highlightNodes.size > 0;
                if (activeMode && !highlightLinks.has(link)) return 'rgba(50, 50, 50, 0.05)';
                
                const rel = link.relation || "";
                if (rel.includes("反驳") || rel.includes("Contradict")) return activeMode ? "rgba(239, 68, 68, 1)" : "rgba(239, 68, 68, 0.8)";
                if (rel.includes("支持") || rel.includes("Support")) return activeMode ? "rgba(16, 185, 129, 1)" : "rgba(16, 185, 129, 0.8)";
                if (rel.includes("衍生") || rel.includes("Derive")) return activeMode ? "rgba(96, 165, 250, 1)" : "rgba(96, 165, 250, 0.8)";
                return activeMode ? "rgba(255, 255, 255, 0.6)" : "rgba(255, 255, 255, 0.2)";
            }})
            .linkWidth(link => highlightLinks.has(link) ? 3 : 1.5)
            .linkDirectionalParticles(link => highlightLinks.has(link) ? 4 : 2)
            .linkDirectionalParticleWidth(1.5)
            .onNodeHover(node => {{
                const tt = document.getElementById('node-tooltip');
                if (node) {{
                    hoverNode = node;
                    tt.style.display = 'block';
                    tt.innerHTML = `<div class="tt-title">${{node.name}}</div><div class="tt-meta">${{node.group || 'N/A'}} | Links: ${{node.val}}</div>`;
                }} else {{
                    hoverNode = null;
                    tt.style.display = 'none';
                }}
                Graph.nodeColor(Graph.nodeColor());
            }})
            .onNodeClick(node => {{
                window.focusNode(node.id);
            }})
            .onBackgroundClick(() => {{
                document.getElementById('info-panel').classList.remove('visible');
                highlightNodes.clear();
                highlightLinks.clear();
                Graph.nodeColor(Graph.nodeColor()).linkWidth(Graph.linkWidth()).linkColor(Graph.linkColor()).linkDirectionalParticles(Graph.linkDirectionalParticles());
            }});
            
        // Search Logic
        const searchInput = document.getElementById('search-input');
        const searchResults = document.getElementById('search-results');
        
        searchInput.addEventListener('input', (e) => {{
            const val = e.target.value.toLowerCase().trim();
            if (!val) {{
                searchResults.style.display = 'none';
                return;
            }}
            
            const matches = data.nodes.filter(n => 
                (n.name && n.name.toLowerCase().includes(val)) || (n.id && n.id.toLowerCase().includes(val))
            ).slice(0, 10);
            
            if (matches.length > 0) {{
                searchResults.innerHTML = matches.map(m => 
                    `<div class="search-item" onclick="window.focusNode('${{m.id}}'); document.getElementById('search-results').style.display='none'; document.getElementById('search-input').value='';">${{m.name}}</div>`
                ).join('');
                searchResults.style.display = 'block';
            }} else {{
                searchResults.innerHTML = `<div class="search-item" style="color:#9CA3AF;pointer-events:none;">No matches found</div>`;
                searchResults.style.display = 'block';
            }}
        }});
        
        document.addEventListener('click', (e) => {{
            if (!document.getElementById('search-container').contains(e.target)) {{
                searchResults.style.display = 'none';
            }}
        }});
    </script>
</body>
</html>
"""
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
