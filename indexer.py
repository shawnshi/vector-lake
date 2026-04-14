import os
import json
import re
import math
import yaml
import logging
from filelock import FileLock, Timeout

try:
    import networkx as nx
    from community import community_louvain
except ImportError:
    nx = None
    community_louvain = None

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("vector-lake-indexer")

WIKI_DIR = os.path.expanduser("~/.gemini/MEMORY/wiki")

VALID_PREFIXES = ("Concept_", "Source_", "Entity_", "Synthesis_", "Event_", "Person_", "Project_", "Term_", "System_")

# ── 4-Signal Relevance Model ─────────────────────────────────────────────────

RELEVANCE_WEIGHTS = {
    "direct_link": 3.0,      # Explicit [[wikilink]] connection
    "source_overlap": 4.0,   # Shared source file (strongest signal)
    "common_neighbor": 1.5,  # Adamic-Adar common neighbor algorithm
    "type_affinity": 1.0,    # Page type compatibility matrix
}

# Type affinity matrix: how naturally related are two types?
TYPE_AFFINITY = {
    "entity": {"entity": 0.8, "concept": 1.2, "source": 1.0, "synthesis": 1.0},
    "concept": {"entity": 1.2, "concept": 0.8, "source": 1.0, "synthesis": 1.2},
    "source": {"entity": 1.0, "concept": 1.0, "source": 0.5, "synthesis": 1.0},
    "synthesis": {"entity": 1.0, "concept": 1.2, "source": 1.0, "synthesis": 0.8},
}


def _parse_wiki_node(filepath: str, node_key: str):
    """Parse a single wiki markdown file and extract structured metadata.
    Returns (node_data, aliases_list, categories_list) on success, or None on failure.
    Raises yaml.YAMLError if YAML parsing fails.
    """
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()
    except (UnicodeDecodeError, OSError) as e:
        log.warning(f"Cannot read {os.path.basename(filepath)}: {e}")
        return None

    frontmatter_match = re.search(r'^---\n(.*?)\n---', content, re.MULTILINE | re.DOTALL)
    if not frontmatter_match:
        return None

    fm_str = frontmatter_match.group(1)
    fm_data = yaml.safe_load(fm_str) or {}

    node_id = fm_data.get('id', "")
    title = fm_data.get('title', node_key)
    
    raw_type = str(fm_data.get('type', "concept")).lower().strip().replace('"', '').replace("'", "")
    if raw_type in ["entity", "person", "system", "project", "organization"]:
        node_type = "concept" if raw_type == "system" else "entity"
    elif raw_type in ["source", "reference"]:
        node_type = "source"
    elif raw_type in ["synthesis", "comparison", "report"]:
        node_type = "synthesis"
    else:
        node_type = "concept"
        
    updated = str(fm_data.get('updated', ""))
    cats = fm_data.get('categories', [])

    # --- Strict Mode Enforcement ---
    domain = fm_data.get('domain')
    topic_cluster = fm_data.get('topic_cluster', 'General')
    status = fm_data.get('status')

    if not domain or not status:
        log.warning(f"Schema violation: Missing 'domain' or 'status' in {os.path.basename(filepath)}. Node excluded from index.")
        return None

    raw_aliases = fm_data.get('aliases', [])
    aliases = []
    if isinstance(raw_aliases, list):
        aliases = [str(a).strip() for a in raw_aliases]
    elif isinstance(raw_aliases, str):
        aliases = [raw_aliases.strip()]

    # Extract sources from frontmatter for source-overlap relevance
    raw_sources = fm_data.get('sources', [])
    sources = []
    if isinstance(raw_sources, list):
        sources = [str(s).strip() for s in raw_sources if s]
    elif isinstance(raw_sources, str):
        sources = [raw_sources.strip()]

    # Extract [[bidirectional links]] from content body
    links = set()
    body = content[frontmatter_match.end():]
    # V7.0 relation links: [Relation:: [[Target]]]
    for m in re.finditer(r'\[([^\[\]]+?)::\s*\[\[(.*?)\]\]\]', body):
        links.add(m.group(2).split('|')[0].strip().replace('.md', ''))
    # Standard wiki links: [[Target]] or [[Target|Alias]]
    for m in re.finditer(r'\[\[(.*?)\]\]', body):
        link_text = m.group(1).split('|')[0].strip().replace('.md', '')
        if '::' in link_text:
            link_text = link_text.split('::', 1)[1].strip()
        links.add(link_text)
    links.discard('')  # Remove empty strings

    # Extract summary (first 200 chars of clean body text)
    summary_text = re.sub(r'#.*?\n', '', body)  # Remove headings
    summary_text = re.sub(r'\[\[([^\]]*?\|)?([^\]]*?)\]\]', r'\2', summary_text)  # Clean wiki links
    summary_text = re.sub(r'\[([^\[\]]+?)::\s*\[\[.*?\]\]\]', '', summary_text)  # Remove relation links
    summary_text = summary_text.strip().replace('\n', ' ')
    summary = summary_text[:500]

    node_data = {
        "id": node_id,
        "title": title,
        "type": node_type,
        "updated": updated,
        "categories": cats,
        "domain": domain,
        "topic_cluster": topic_cluster,
        "status": status,
        "aliases": aliases,
        "sources": sources,
        "links": sorted(links),
        "summary": summary
    }

    return node_data


def calculate_relevance(node_a: dict, node_b: dict, all_nodes: dict) -> float:
    """Calculate 4-signal relevance score between two wiki nodes.
    
    Signals:
      1. Direct link:      A links to B or B links to A (weight: 3.0)
      2. Source overlap:    A and B share source files  (weight: 4.0, strongest)
      3. Common neighbor:  Adamic-Adar algorithm       (weight: 1.5)
      4. Type affinity:    Entity-Concept > Entity-Entity etc. (weight: 1.0)
    """
    score = 0.0
    key_a = node_a.get("_key", "")
    key_b = node_b.get("_key", "")
    
    links_a = set(node_a.get("links", []))
    links_b = set(node_b.get("links", []))
    
    # Signal 1: Direct link
    if key_b in links_a:
        score += RELEVANCE_WEIGHTS["direct_link"]
    if key_a in links_b:
        score += RELEVANCE_WEIGHTS["direct_link"]
    
    # Signal 2: Source overlap (most powerful — same source = same topic)
    sources_a = set(node_a.get("sources", []))
    sources_b = set(node_b.get("sources", []))
    shared_sources = len(sources_a & sources_b)
    score += shared_sources * RELEVANCE_WEIGHTS["source_overlap"]
    
    # Signal 3: Common neighbors (Adamic-Adar index)
    common_neighbors = links_a & links_b
    for neighbor_key in common_neighbors:
        neighbor = all_nodes.get(neighbor_key, {})
        degree = len(neighbor.get("links", []))
        if degree > 1:
            score += (1.0 / math.log(degree)) * RELEVANCE_WEIGHTS["common_neighbor"]
    
    # Signal 4: Type affinity
    type_a = node_a.get("type", "concept").lower()
    type_b = node_b.get("type", "concept").lower()
    affinity = TYPE_AFFINITY.get(type_a, {}).get(type_b, 0.5) * RELEVANCE_WEIGHTS["type_affinity"]
    score += affinity
    
    return round(score, 3)


def generate_index():
    """Generates a lightweight index.json mapping from the wiki markdown files.
    Now includes weighted_edges for graph visualization.
    """
    if not os.path.exists(WIKI_DIR):
        log.warning(f"Wiki directory not found at {WIKI_DIR}")
        return

    index_data = {
        "nodes": {},     # Metadata by filename (without .md)
        "aliases": {},   # Mapping of alias/id to filename
        "categories": set(),
        "weighted_edges": [],  # NEW: relevance-weighted edges for graph
        "error_log": []
    }

    files = [f for f in os.listdir(WIKI_DIR) if f.endswith(".md") and f not in ("index.md", "log.md", "overview.md")]
    
    for filename in files:
        filepath = os.path.join(WIKI_DIR, filename)
        
        if not filename.startswith(VALID_PREFIXES) and filename not in ("index.md", "log.md"):
            index_data["error_log"].append({"file": filename, "error": "Schema violation: Missing valid entity prefix."})
            log.warning(f"Schema violation in {filename}, bypassing index inclusion.")
            continue
            
        node_key = filename[:-3] # remove .md
        
        try:
            node_data = _parse_wiki_node(filepath, node_key)
        except yaml.YAMLError as e:
            index_data["error_log"].append({"file": filename, "error": str(e)})
            log.warning(f"YAML Error in {filename}, suppressing to error_log.")
            continue

        if node_data is None:
            continue

        # Register in nodes
        index_data["nodes"][node_key] = node_data

        # Register aliases & id mapping back to node_key
        node_id = node_data["id"]
        if node_id:
            index_data["aliases"][node_id] = node_key
        index_data["aliases"][node_key] = node_key
        for al in node_data["aliases"]:
            index_data["aliases"][al] = node_key
            
        cats = node_data["categories"]
        if isinstance(cats, list):
            for c in cats:
                index_data["categories"].add(c)
    
    # ── Calculate weighted edges (4-signal relevance) ─────────────
    nodes_dict = index_data["nodes"]
    node_keys = list(nodes_dict.keys())
    # Add _key to each node for relevance calculation
    for key, node in nodes_dict.items():
        node["_key"] = key
    
    edges = []
    seen_edges = set()
    
    for key_a in node_keys:
        node_a = nodes_dict[key_a]
        # Only compute edges for nodes that have links (performance optimization)
        links_a = set(node_a.get("links", []))
        sources_a = set(node_a.get("sources", []))
        
        for key_b in node_keys:
            if key_a >= key_b:  # Avoid duplicates (A-B same as B-A)
                continue
            
            node_b = nodes_dict[key_b]
            links_b = set(node_b.get("links", []))
            sources_b = set(node_b.get("sources", []))
            
            # Skip if no possible connection (optimization: prune O(n²))
            has_direct = key_b in links_a or key_a in links_b
            has_source_overlap = bool(sources_a & sources_b)
            has_common_neighbor = bool(links_a & links_b)
            
            if not (has_direct or has_source_overlap or has_common_neighbor):
                continue
            
            relevance = calculate_relevance(node_a, node_b, nodes_dict)
            if relevance >= 1.5:
                edges.append({
                    "source": key_a,
                    "target": key_b,
                    "weight": relevance
                })
    
    # Sort edges by weight descending for the graph visualization
    edges.sort(key=lambda e: e["weight"], reverse=True)
    index_data["weighted_edges"] = edges
    
    # --- Topology Audit (Louvain Community Detection) ---
    index_data["communities"] = {}
    index_data["graph_insights"] = []
    
    if nx and community_louvain and edges:
        G = nx.Graph()
        for key in node_keys:
            G.add_node(key)
        for e in edges:
            G.add_edge(e["source"], e["target"], weight=e["weight"])
            
        try:
            # 1. Community Partitioning
            partition = community_louvain.best_partition(G, weight='weight')
            index_data["communities"] = partition
            
            # 2. Cohesion Scoring
            community_nodes = {}
            for node, comm_id in partition.items():
                if comm_id not in community_nodes:
                    community_nodes[comm_id] = []
                community_nodes[comm_id].append(node)
                
            for comm_id, nodes in community_nodes.items():
                if len(nodes) < 3:
                    continue # Too small for cohesion analysis
                    
                subgraph = G.subgraph(nodes)
                possible_edges = len(nodes) * (len(nodes) - 1) / 2
                actual_edges = subgraph.number_of_edges()
                cohesion = actual_edges / possible_edges if possible_edges > 0 else 0
                
                # Insight: Sparse Community (Knowledge Gap)
                if cohesion < 0.15:
                    index_data["graph_insights"].append({
                        "type": "sparse_community",
                        "community_id": int(comm_id),
                        "nodes": nodes,
                        "cohesion": float(cohesion),
                        "description": f"Community {comm_id} has low internal cohesion ({cohesion:.2f}). Indicates a potential knowledge gap."
                    })
                    
            # 3. Identify Isolated Nodes
            for node in node_keys:
                if G.degree(node) <= 1:
                    index_data["graph_insights"].append({
                        "type": "isolated_node",
                        "node": node,
                        "description": f"Node '{node}' is isolated or weakly connected (Degree <= 1)."
                    })
                    
            # 4. Identify Bridge Nodes
            for node in node_keys:
                connected_communities = set()
                for neighbor in G.neighbors(node):
                    connected_communities.add(partition.get(neighbor))
                if len(connected_communities) >= 3:
                    index_data["graph_insights"].append({
                        "type": "bridge_node",
                        "node": node,
                        "connected_communities": [int(c) for c in connected_communities if c is not None],
                        "description": f"Node '{node}' connects {len(connected_communities)} distinct communities. High strategic value."
                    })
                    
            # 5. Generate Community Labels
            community_labels = {}
            for comm_id, nodes in community_nodes.items():
                sorted_nodes = sorted(nodes, key=lambda n: G.degree(n), reverse=True)
                top_nodes = sorted_nodes[:2]
                titles = []
                for n in top_nodes:
                    node_data = nodes_dict.get(n)
                    if node_data and "title" in node_data:
                        titles.append(node_data["title"])
                    else:
                        titles.append(n)
                label_name = " / ".join(titles) if titles else "Unknown"
                community_labels[int(comm_id)] = f"Comm {comm_id}: {label_name}"
            
            index_data["community_labels"] = community_labels
        except Exception as e:
            log.error(f"Graph analysis failed: {e}")
    # ----------------------------------------------------
    
    # Clean up _key from nodes before serialization
    for node in nodes_dict.values():
        node.pop("_key", None)
    
    # Convert sets to list for JSON serialization
    index_data["categories"] = list(index_data["categories"])
    
    output_path = os.path.join(WIKI_DIR, "index.json")
    lock_path = output_path + ".lock"

    try:
        with FileLock(lock_path, timeout=15):
            temp_path = output_path + ".tmp"
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(index_data, f, ensure_ascii=False, separators=(',', ':'))
            os.replace(temp_path, output_path)
    except Timeout:
        log.error(f"Timeout while acquiring lock for {output_path}")

    log.info(f"Generated index.json with {len(index_data['nodes'])} nodes | "             f"{len(edges)} weighted edges | "
             f"{len(index_data.get('error_log', []))} errors.")
    return output_path

def update_index_item(filename: str):
    """O(1) partial update for a single file in index.json."""
    if not filename.endswith(".md") or filename in ("index.md", "log.md", "overview.md"):
        return
        
    output_path = os.path.join(WIKI_DIR, "index.json")
    if not os.path.exists(output_path):
        return generate_index()
        
    with open(output_path, 'r', encoding='utf-8') as f:
        try:
            index_data = json.load(f)
        except json.JSONDecodeError:
            return generate_index()
            
    filepath = os.path.join(WIKI_DIR, filename)
    
    if not filename.startswith(VALID_PREFIXES) and filename not in ("index.md", "log.md"):
        if "error_log" not in index_data:
            index_data["error_log"] = []
        index_data["error_log"] = [e for e in index_data["error_log"] if e.get("file") != filename]
        index_data["error_log"].append({"file": filename, "error": "Schema violation: Missing valid entity prefix."})
        log.warning(f"Schema violation in {filename} during partial update.")
        
        node_key = filename[:-3]
        if node_key in index_data.get("nodes", {}):
            del index_data["nodes"][node_key]
            
        lock_path = output_path + ".lock"
        try:
            with FileLock(lock_path, timeout=15):
                temp_path = output_path + ".tmp"
                with open(temp_path, "w", encoding="utf-8") as f:
                    json.dump(index_data, f, ensure_ascii=False, separators=(',', ':'))
                os.replace(temp_path, output_path)
        except Timeout:
            log.error(f"Timeout while acquiring lock for {output_path}")
        return
        
    node_key = filename[:-3]
    
    # Prune old aliases mapping for this key
    index_data["aliases"] = {k: v for k, v in index_data.get("aliases", {}).items() if v != node_key}
    
    if "error_log" not in index_data:
        index_data["error_log"] = []
    index_data["error_log"] = [e for e in index_data["error_log"] if e.get("file") != filename]
    
    if not os.path.exists(filepath):
        # Was deleted
        if node_key in index_data.get("nodes", {}):
            del index_data["nodes"][node_key]
        # Remove related edges
        index_data["weighted_edges"] = [
            e for e in index_data.get("weighted_edges", [])
            if e["source"] != node_key and e["target"] != node_key
        ]
    else:
        try:
            node_data = _parse_wiki_node(filepath, node_key)
        except yaml.YAMLError as e:
            index_data["error_log"].append({"file": filename, "error": str(e)})
            log.warning(f"YAML Error in {filename} during partial update.")
            if node_key in index_data.get("nodes", {}):
                del index_data["nodes"][node_key]
            node_data = None

        if node_data is None:
            index_data["error_log"].append({"file": filename, "error": "Schema violation: Missing 'domain' or 'status'. Node excluded."})
            if node_key in index_data.get("nodes", {}):
                del index_data["nodes"][node_key]
        else:
            index_data["nodes"][node_key] = node_data
            
            node_id = node_data["id"]
            if node_id: index_data["aliases"][node_id] = node_key
            index_data["aliases"][node_key] = node_key
            for al in node_data["aliases"]:
                index_data["aliases"][al] = node_key
            
            cats = node_data["categories"]
            if isinstance(cats, list):
                cat_set = set(index_data.get("categories", []))
                for c in cats: cat_set.add(c)
                index_data["categories"] = list(cat_set)

            # Recalculate edges for this node
            index_data["weighted_edges"] = [
                e for e in index_data.get("weighted_edges", [])
                if e["source"] != node_key and e["target"] != node_key
            ]
            node_data["_key"] = node_key
            all_nodes = index_data["nodes"]
            for other_key, other_node in all_nodes.items():
                if other_key == node_key:
                    continue
                other_node["_key"] = other_key
                relevance = calculate_relevance(node_data, other_node, all_nodes)
                if relevance >= 1.5:
                    index_data["weighted_edges"].append({
                        "source": min(node_key, other_key),
                        "target": max(node_key, other_key),
                        "weight": relevance,
                    })
                other_node.pop("_key", None)
            node_data.pop("_key", None)

    lock_path = output_path + ".lock"
    try:
        with FileLock(lock_path, timeout=15):
            temp_path = output_path + ".tmp"
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(index_data, f, ensure_ascii=False, separators=(',', ':'))
            os.replace(temp_path, output_path)
    except Timeout:
        log.error(f"Timeout while acquiring lock for {output_path}")

if __name__ == "__main__":
    generate_index()
