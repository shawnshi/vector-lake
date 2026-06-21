import json
import logging
import math
import os
import re
from datetime import datetime, timezone
import time
from filelock import FileLock

try:
    import networkx as nx
    from community import community_louvain
except ImportError:
    nx = None
    community_louvain = None

# Ensure vector_lake is in path
import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from vector_lake.wiki_utils import get_index_path, get_wiki_dir, get_meta_dir
from vector_lake.governance_store import load_governance_queue, save_governance_queue

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("vector-lake-clustering-daemon")

def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()

def _mark_graph_clean(index_data: dict):
    if "graph_state" not in index_data:
        index_data["graph_state"] = {}
    index_data["graph_state"]["dirty"] = False
    index_data["graph_state"]["reason"] = "Community clustering applied"
    index_data["graph_state"]["updated_at"] = _utc_now()

def run_clustering():
    index_file = get_index_path()
    if not index_file.exists():
        log.warning("Index file not found, skipping clustering.")
        return

    lock = FileLock(str(index_file) + ".lock", timeout=30)
    
    with lock:
        with open(index_file, "r", encoding="utf-8") as f:
            index_data = json.load(f)

        # Check if dirty
        graph_state = index_data.get("graph_state", {})
        if not graph_state.get("dirty", True):
            log.info("Graph is not dirty. Skipping clustering.")
            return

        log.info("Starting heavy graph topology clustering...")
        
        edges = index_data.get("weighted_edges", [])
        node_keys = list(index_data.get("nodes", {}).keys())
        index_data["communities"] = {}
        index_data["community_labels"] = {}
        index_data["graph_insights"] = []

        if not (nx and community_louvain and edges):
            _mark_graph_clean(index_data)
            with open(index_file, "w", encoding="utf-8") as f:
                json.dump(index_data, f, ensure_ascii=False)
            return

        G = nx.Graph()
        for key in node_keys:
            G.add_node(key)
        for edge in edges:
            G.add_edge(edge["source"], edge["target"], weight=edge["weight"])

        if nx and G.number_of_nodes() > 0:
            try:
                pageranks = nx.pagerank(G, weight="weight")
                pr_scale = len(node_keys) if len(node_keys) > 0 else 1
                for node_key in node_keys:
                    pr_score = pageranks.get(node_key, 0.0) * pr_scale
                    node = index_data["nodes"][node_key]
                    node["centrality_score"] = round(pr_score, 4)
                    node["node_score"] = round(node.get("decay_weight", 1.0) * pr_score, 4)
            except Exception as e:
                log.error(f"PageRank computation failed: {e}")
                for node_key in node_keys:
                    node = index_data["nodes"][node_key]
                    node["centrality_score"] = 1.0
                    node["node_score"] = round(node.get("decay_weight", 1.0), 4)
        else:
            for node_key in node_keys:
                node = index_data["nodes"][node_key]
                node["centrality_score"] = 1.0
                node["node_score"] = round(node.get("decay_weight", 1.0), 4)

        try:
            partition = community_louvain.best_partition(G, weight="weight")
            index_data["communities"] = partition

            community_nodes = {}
            for node, comm_id in partition.items():
                community_nodes.setdefault(comm_id, []).append(node)

            for comm_id, nodes in community_nodes.items():
                if len(nodes) < 3:
                    continue
                subgraph = G.subgraph(nodes)
                possible_edges = len(nodes) * (len(nodes) - 1) / 2
                actual_edges = subgraph.number_of_edges()
                cohesion = actual_edges / possible_edges if possible_edges > 0 else 0
                if cohesion < 0.15:
                    index_data["graph_insights"].append({
                        "type": "sparse_community",
                        "community_id": int(comm_id),
                        "nodes": nodes,
                        "cohesion": float(cohesion),
                        "description": f"Community {comm_id} has low internal cohesion ({cohesion:.2f}). Indicates a potential knowledge gap.",
                    })

            for node in node_keys:
                if G.degree(node) <= 1:
                    index_data["graph_insights"].append({
                        "type": "isolated_node",
                        "node": node,
                        "description": f"Node '{node}' is isolated or weakly connected (Degree <= 1).",
                    })

            for node in node_keys:
                connected_communities = {partition.get(neighbor) for neighbor in G.neighbors(node)}
                connected_communities.discard(None)
                if len(connected_communities) >= 3:
                    index_data["graph_insights"].append({
                        "type": "bridge_node",
                        "node": node,
                        "connected_communities": [int(comm_id) for comm_id in connected_communities],
                        "description": f"Node '{node}' connects {len(connected_communities)} distinct communities. High strategic value.",
                    })

            community_labels = {}
            for comm_id, nodes in community_nodes.items():
                sorted_nodes = sorted(nodes, key=lambda node: G.degree(node), reverse=True)
                top_nodes = sorted_nodes[:2]
                titles = []
                for node in top_nodes:
                    node_data = index_data["nodes"].get(node)
                    titles.append(node_data.get("title", node) if node_data else node)
                label = f"Comm {comm_id}: {' / '.join(titles) if titles else 'Unknown'}"
                community_labels[int(comm_id)] = label
                
                # --- PROGRESSIVE DISCLOSURE INDEX ---
                try:
                    wiki_dir = get_wiki_dir()
                    sanitized_title = re.sub(r'[\\/*?:"<>|\'#\s\[\]\(\)&]', '_', label.replace(f"Comm {comm_id}:", "").strip())
                    sanitized_title = re.sub(r'_+', '_', sanitized_title).strip('_ ')
                    if not sanitized_title:
                        sanitized_title = "Unknown"
                    index_filename = f"System_Community_{comm_id}_{sanitized_title}.md"
                    index_filepath = wiki_dir / index_filename
                    
                    hubs_markdown = "\n".join([f"- [[{node}]]" for node in sorted_nodes[:5]])
                    members_markdown = "\n".join([f"- [[{node}]]" for node in sorted_nodes[5:]])
                    
                    existing_summary = "*(To be generated by LLM during Review/Synthesis)*"
                    import glob
                    old_files = glob.glob(str(wiki_dir / f"System_Community_{comm_id}_*.md"))
                    for old_file in old_files:
                        try:
                            with open(old_file, "r", encoding="utf-8") as old_f:
                                old_content = old_f.read()
                                if "## 语义总结" in old_content:
                                    parts = old_content.split("## 语义总结", 1)
                                    if len(parts) > 1:
                                        extracted = parts[1].split("\n", 1)[-1].strip()
                                        if extracted and extracted != "*(To be generated by LLM during Review/Synthesis)*" and not extracted.startswith("*(To be generated"):
                                            existing_summary = extracted
                                            break
                        except Exception:
                            pass
                            
                    if existing_summary.startswith("*(To be generated"):
                        try:
                            import uuid
                            queue = load_governance_queue()
                            if not any(item.get("community_id") == comm_id and item.get("type") == "community_naming" and item.get("status") == "pending" for item in queue.get("items", [])):
                                queue["items"].append({
                                    "item_id": f"gov_{uuid.uuid4().hex[:12]}",
                                    "type": "community_naming",
                                    "community_id": comm_id,
                                    "title": f"Community {comm_id} Requires Semantic Naming",
                                    "description": f"Hubs: {' / '.join(titles)}. Provide a 3-5 word abstraction.",
                                    "created_at": _utc_now(),
                                    "status": "pending",
                                    "source": "indexer",
                                    "affected_pages": [index_filename],
                                    "hubs": titles
                                })
                                save_governance_queue(queue)
                        except Exception as e:
                            log.warning(f"Failed to queue community_naming for Comm {comm_id}: {e}")
                            
                    for old_file in old_files:
                        if os.path.basename(old_file) != index_filename:
                            try:
                                os.remove(old_file)
                            except OSError:
                                pass
                    
                    content = f"""---
title: "{label}"
type: system
status: Active
community_id: {comm_id}
---
# {label}

> [!NOTE]
> 这是一个系统自动生成的**社区索引文件 (Progressive Disclosure Index)**。下游 Agent 可以通过优先阅读此文件来快速掌握该知识聚类的全局拓扑。

## 核心节点 (Hubs)
{hubs_markdown}

## 社区成员 (Members)
{members_markdown}

## 语义总结 (Semantic Summary)
{existing_summary}
"""
                    with open(index_filepath, "w", encoding="utf-8") as f:
                        f.write(content)
                except Exception as e:
                    log.warning(f"Failed to generate Progressive Disclosure Index for Comm {comm_id}: {e}")

            index_data["community_labels"] = community_labels
            
            # --- EVOLUTION TRACKING ---
            try:
                snapshot_file = get_meta_dir() / "community_snapshot.json"
                
                if snapshot_file.exists():
                    with open(snapshot_file, "r", encoding="utf-8") as f:
                        old_snapshot = json.load(f)
                    old_partition = old_snapshot.get("partition", {})
                    old_labels = old_snapshot.get("labels", {})
                    
                    old_comm_nodes = {}
                    for node, old_c in old_partition.items():
                        old_comm_nodes.setdefault(str(old_c), []).append(node)
                        
                    for old_cid, old_nodes in old_comm_nodes.items():
                        if len(old_nodes) < 5: continue
                        
                        destinations = {}
                        for node in old_nodes:
                            new_cid = partition.get(node)
                            if new_cid is not None:
                                destinations[str(new_cid)] = destinations.get(str(new_cid), 0) + 1
                                
                        for new_cid, count in destinations.items():
                            new_nodes = community_nodes.get(int(new_cid))
                            if not new_nodes: continue
                            
                            ratio_old = float(count) / len(old_nodes)
                            ratio_new = float(count) / len(new_nodes)
                            
                            if count >= 3 and 0.15 <= ratio_old < 0.85 and 0.15 <= ratio_new < 0.85:
                                old_label = old_labels.get(old_cid, f"Comm {old_cid}")
                                new_label = community_labels.get(int(new_cid), f"Comm {new_cid}")
                                index_data["graph_insights"].append({
                                    "type": "strategic_convergence",
                                    "old_community": int(old_cid),
                                    "new_community": int(new_cid),
                                    "node_count": count,
                                    "description": f"Convergence Signal: {count} nodes migrated from [{old_label}] to [{new_label}]. This indicates cross-disciplinary fusion or topic fission."
                                })
                                
                with open(snapshot_file, "w", encoding="utf-8") as f:
                    json.dump({"partition": partition, "labels": community_labels}, f, ensure_ascii=False)
            except Exception as e:
                log.warning(f"Community drift tracking failed: {e}")
                
        except Exception as e:
            log.error(f"Graph analysis failed: {e}")
        finally:
            _mark_graph_clean(index_data)
            with open(index_file, "w", encoding="utf-8") as f:
                json.dump(index_data, f, ensure_ascii=False)
                
        log.info("Heavy graph topology clustering complete.")

if __name__ == "__main__":
    run_clustering()
