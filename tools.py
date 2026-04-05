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

            # Basic YAML frontmatter parsing
            frontmatter_match = re.search(r'^---\n(.*?)\n---', content, re.MULTILINE | re.DOTALL)
            if frontmatter_match:
                fm_str = frontmatter_match.group(1)
                for line in fm_str.splitlines():
                    if line.startswith('title:'):
                        title = line.replace('title:', '').strip().strip('"').strip("'")
                    elif line.startswith('type:'):
                        node_type = line.replace('type:', '').strip().strip('"').strip("'")
                    elif line.startswith('updated:'):
                        updated = line.replace('updated:', '').strip().strip('"').strip("'")
                    elif line.startswith('sources:'):
                        s_str = line.replace('sources:', '').strip()
                        # Extract paths within brackets and quotes
                        s_list = re.findall(r'["\'](.*?)["\']', s_str)
                        if s_list:
                            sources = s_list
                        else:
                            clean_str = s_str.strip('[]"\' ')
                            sources = [clean_str] if clean_str else []

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

            # Extract bidirectional links [[Link]] or [[Link|Alias]]
            link_matches = re.finditer(r'\[\[(.*?)\]\]', content)
            for match in link_matches:
                link_text = match.group(1)
                target_id = link_text.split('|')[0].strip()
                edges.append({
                    "source": node_id,
                    "target": target_id
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
    <title>Vector Lake 3D Topology (V4.0)</title>
    <style> 
        body {{ margin: 0; padding: 0; background-color: #0A0F14; overflow: hidden; font-family: sans-serif; color: #E5E7EB; }} 
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
    </div>
    <div id="3d-graph"></div>
    <script>
        const data = {json.dumps(graph_data)};
        
        const Graph = ForceGraph3D()
            (document.getElementById('3d-graph'))
            .graphData(data)
            .nodeLabel('name')
            .nodeAutoColorBy('group')
            .nodeColor(node => node.color)
            .nodeVal('val')
            .linkDirectionalArrowLength(3.5)
            .linkDirectionalArrowRelPos(1)
            .linkColor(() => '#374151')
            .onNodeClick(node => {{
                // Camera focus
                const distance = 40;
                const distRatio = 1 + distance/Math.hypot(node.x, node.y, node.z);
                Graph.cameraPosition({{ x: node.x * distRatio, y: node.y * distRatio, z: node.z * distRatio }}, node, 3000);
                
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
                
                document.getElementById('info-panel').classList.add('visible');
            }})
            .onBackgroundClick(() => {{
                document.getElementById('info-panel').classList.remove('visible');
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
    "visualize_vector_lake"
]
