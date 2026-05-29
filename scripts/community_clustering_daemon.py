import json
import logging
import os
import sys
import uuid
from datetime import datetime, timezone

try:
    import networkx as nx
except ImportError:
    print("Error: networkx is not installed. Please run `pip install networkx`.")
    sys.exit(1)

# Ensure vector_lake is in sys.path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from vector_lake.wiki_utils import get_claim_graph_path, get_meta_dir
from vector_lake.governance_store import load_governance_queue, save_governance_queue

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("community-clustering")

def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()

def get_purpose_vectors() -> dict:
    path = get_meta_dir() / "purpose_vectors.json"
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"keywords": [], "weight_boost": 0.0}

def is_strategic_cluster(nodes: list, purpose_keywords: list) -> bool:
    if not purpose_keywords:
        return True
    
    text_corpus = " ".join([n.get("name", "") + " " + n.get("summary", "") for n in nodes]).lower()
    for kw in purpose_keywords:
        if kw.lower() in text_corpus:
            return True
    return False

def run_clustering():
    graph_path = get_claim_graph_path()
    if not graph_path.exists():
        log.warning("No claim_graph.json found. Skipping clustering.")
        return

    with open(graph_path, "r", encoding="utf-8") as f:
        graph_data = json.load(f)

    G = nx.Graph()
    
    # 1. Build Graph
    nodes_map = {n["id"]: n for n in graph_data.get("nodes", [])}
    for node in graph_data.get("nodes", []):
        G.add_node(node["id"], **node)
        
    for edge in graph_data.get("edges", []):
        # Incorporate Louvain and Adamic-Adar weights
        weight = float(edge.get("weight", 1.0))
        G.add_edge(edge["source"], edge["target"], weight=weight)

    if G.number_of_nodes() == 0:
        log.info("Graph is empty. Skipping clustering.")
        return

    # 2. Louvain Community Detection
    try:
        from networkx.algorithms.community import louvain_communities
        communities = louvain_communities(G, weight='weight')
    except AttributeError:
        # Fallback for older networkx
        from networkx.algorithms.community import greedy_modularity_communities
        communities = greedy_modularity_communities(G, weight='weight')

    log.info(f"Detected {len(communities)} communities in the claim graph.")

    purpose_vectors = get_purpose_vectors()
    keywords = purpose_vectors.get("keywords", [])
    
    queue = load_governance_queue()
    created_tasks = 0

    # 3. Compute Cohesion and Detect Blind Spots
    for i, comm in enumerate(communities):
        subgraph = G.subgraph(comm)
        n = subgraph.number_of_nodes()
        if n < 3:
            continue
            
        # Calculate cohesion: Actual edges / Possible edges
        possible_edges = n * (n - 1) / 2
        actual_edges = subgraph.number_of_edges()
        cohesion = actual_edges / possible_edges if possible_edges > 0 else 0
        
        nodes_list = [nodes_map[nid] for nid in comm if nid in nodes_map]
        
        # Check if this cluster is strategically important
        if is_strategic_cluster(nodes_list, keywords) and cohesion < 0.15:
            # Blind spot detected!
            log.info(f"Blind spot detected in Cluster {i} (Cohesion: {cohesion:.3f}, Nodes: {n})")
            
            top_nodes = sorted(nodes_list, key=lambda x: x.get("degree", 0), reverse=True)[:3]
            query_terms = " ".join([n.get("name", "")[:30] for n in top_nodes])
            
            pair_key = f"research_blindspot_cluster_{i}"
            
            # Check if already in queue
            existing = any(item.get("pair_key") == pair_key for item in queue["items"])
            if not existing:
                queue["items"].append({
                    "item_id": f"gov_{uuid.uuid4().hex[:12]}",
                    "type": "research_wanted",
                    "title": f"Autonomous Research: Low Cohesion in Strategic Cluster",
                    "description": f"Cluster contains strategic keywords but has low cohesion ({cohesion:.3f}). Entities involved: {query_terms}",
                    "created_at": _utc_now(),
                    "status": "pending",
                    "source": "community_clustering_daemon",
                    "pair_key": pair_key,
                    "search_queries": [f"Deep dive connection between {n.get('name', '')}" for n in top_nodes],
                    "affected_ids": [n["id"] for n in top_nodes],
                })
                created_tasks += 1

    if created_tasks > 0:
        save_governance_queue(queue)
        log.info(f"Injected {created_tasks} RESEARCH_WANTED tasks into the governance queue.")
    else:
        log.info("No blind spots requiring autonomous research were found.")

if __name__ == "__main__":
    run_clustering()
