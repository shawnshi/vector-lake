import json
import logging
import math
import os
import re
import uuid
import glob
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

def _stabilize_community_ids(new_partition, old_partition, old_uuids):
    new_comm_nodes = {}
    for n, c in new_partition.items():
        new_comm_nodes.setdefault(c, []).append(n)
        
    stable_uuids = {}
    diffs = {}
    
    for new_cid, nodes in new_comm_nodes.items():
        overlap_counts = {}
        for n in nodes:
            old_u = old_partition.get(n)
            if old_u:
                overlap_counts[old_u] = overlap_counts.get(old_u, 0) + 1
                
        if overlap_counts:
            best_old_u = max(overlap_counts.items(), key=lambda x: x[1])[0]
            old_nodes_in_u = [n for n, u in old_partition.items() if u == best_old_u]
            overlap = overlap_counts[best_old_u]
            added = len(nodes) - overlap
            removed = len(old_nodes_in_u) - overlap
            total_old = len(old_nodes_in_u) if old_nodes_in_u else 1
            diff_ratio = (added + removed) / total_old
            
            stable_uuids[new_cid] = best_old_u
            diffs[best_old_u] = {"ratio": diff_ratio, "added": [n for n in nodes if old_partition.get(n) != best_old_u]}
        else:
            new_u = uuid.uuid4().hex[:8]
            stable_uuids[new_cid] = new_u
            diffs[new_u] = {"ratio": 1.0, "added": nodes}
            
    final_partition = {n: stable_uuids[c] for n, c in new_partition.items()}
    return final_partition, diffs, stable_uuids

def run_clustering():
    index_file = get_index_path()
    if not index_file.exists():
        log.warning("Index file not found, skipping clustering.")
        return

    lock = FileLock(str(index_file) + ".lock", timeout=30)
    
    with lock:
        with open(index_file, "r", encoding="utf-8") as f:
            index_data = json.load(f)

        # Force run for the migration
        log.info("Starting V9 heavy graph topology clustering (Hierarchical + Stable Hashing)...")
        
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
                pass

        try:
            dendro = community_louvain.generate_dendrogram(G, weight="weight")
            if len(dendro) >= 2:
                raw_part_L1 = community_louvain.partition_at_level(dendro, 0) # Micro
                raw_part_L0 = community_louvain.partition_at_level(dendro, len(dendro)-1) # Global
            else:
                raw_part_L1 = community_louvain.partition_at_level(dendro, 0)
                raw_part_L0 = raw_part_L1

            snapshot_file = get_meta_dir() / "community_snapshot.json"
            old_part_L0 = {}
            old_part_L1 = {}
            if snapshot_file.exists():
                try:
                    with open(snapshot_file, "r", encoding="utf-8") as f:
                        old_snap = json.load(f)
                        # Handle legacy snapshot conversion
                        if "partition" in old_snap and "partition_L0" not in old_snap:
                            old_part_L0 = old_snap["partition"]
                            # Convert integer CIDs to strings for stable matching
                            old_part_L0 = {k: str(v) for k, v in old_part_L0.items()}
                        else:
                            old_part_L0 = old_snap.get("partition_L0", {})
                            old_part_L1 = old_snap.get("partition_L1", {})
                except Exception:
                    pass

            part_L0, diffs_L0, uuid_map_L0 = _stabilize_community_ids(raw_part_L0, old_part_L0, {})
            part_L1, diffs_L1, uuid_map_L1 = _stabilize_community_ids(raw_part_L1, old_part_L1, {})

            index_data["communities"] = part_L0

            wiki_dir = get_wiki_dir()

            def process_level(level_name, final_partition, diffs_info):
                community_nodes = {}
                for node, c_uuid in final_partition.items():
                    community_nodes.setdefault(c_uuid, []).append(node)

                for c_uuid, nodes in community_nodes.items():
                    if len(nodes) < 3: continue
                    sorted_nodes = sorted(nodes, key=lambda node: G.degree(node), reverse=True)
                    titles = [index_data["nodes"].get(n, {}).get("title", n) for n in sorted_nodes[:2]]
                    label = f"{level_name} Comm: {' / '.join(titles) if titles else 'Unknown'}"
                    
                    diff_ratio = diffs_info.get(c_uuid, {}).get("ratio", 1.0)
                    added_nodes = diffs_info.get(c_uuid, {}).get("added", [])

                    index_filename = f"System_Community_{level_name}_{c_uuid}.md"
                    index_filepath = wiki_dir / index_filename
                    
                    hubs_markdown = "\n".join([f"- [[{node}]]" for node in sorted_nodes[:5]])
                    members_markdown = "\n".join([f"- [[{node}]]" for node in sorted_nodes[5:]])
                    
                    existing_summary = "*(To be generated by LLM during Review/Synthesis)*"
                    unassimilated = ""
                    
                    if index_filepath.exists():
                        try:
                            with open(index_filepath, "r", encoding="utf-8") as old_f:
                                old_content = old_f.read()
                                if "## 语义总结" in old_content:
                                    parts = old_content.split("## 语义总结", 1)
                                    if len(parts) > 1:
                                        extracted = parts[1].split("##", 1)[0].strip()
                                        if extracted and not extracted.startswith("*(To be generated"):
                                            existing_summary = extracted
                        except Exception:
                            pass

                    needs_llm = False
                    if diff_ratio >= 0.15 or existing_summary.startswith("*(To be generated"):
                        existing_summary = "*(To be generated by LLM during Review/Synthesis)*"
                        needs_llm = True
                    elif added_nodes:
                        unassimilated = "\n> [!WARNING] **待同化增量 (Unassimilated Delta)**:\n" + "\n".join([f"> - [[{n}]]" for n in added_nodes])

                    if needs_llm:
                        try:
                            queue = load_governance_queue()
                            if not any(item.get("community_id") == c_uuid and item.get("status") == "pending" for item in queue.get("items", [])):
                                queue["items"].append({
                                    "item_id": f"gov_{uuid.uuid4().hex[:12]}",
                                    "type": "community_naming",
                                    "community_id": c_uuid,
                                    "title": f"{level_name} Comm {c_uuid} Requires Synthesis",
                                    "description": f"Diff ratio {diff_ratio:.2f}. Hubs: {' / '.join(titles)}.",
                                    "created_at": _utc_now(),
                                    "status": "pending",
                                    "source": "indexer",
                                    "affected_pages": [index_filename],
                                    "hubs": titles
                                })
                                save_governance_queue(queue)
                        except Exception as e:
                            log.warning(f"Failed to queue naming for {c_uuid}: {e}")

                    content = f"""---
title: "{label}"
type: system
status: Active
community_id: {c_uuid}
level: {level_name}
aliases:
- "{label}"
---
# {label}

> [!NOTE]
> 这是一个系统自动生成的**社区索引文件 (Progressive Disclosure Index)**。
> 当前缩放级别: **{level_name}** ({'Global' if level_name=='L0' else 'Micro'})

## 核心节点 (Hubs)
{hubs_markdown}

## 社区成员 (Members)
{members_markdown}

## 语义总结 (Semantic Summary)
{existing_summary}
{unassimilated}
"""
                    with open(index_filepath, "w", encoding="utf-8") as f:
                        f.write(content)

            process_level("L0", part_L0, diffs_L0)
            process_level("L1", part_L1, diffs_L1)

            # Clean up old legacy flat files safely
            legacy_files = glob.glob(str(wiki_dir / "System_Community_[0-9]*.md"))
            for old_file in legacy_files:
                try: os.remove(old_file)
                except OSError: pass

            with open(snapshot_file, "w", encoding="utf-8") as f:
                json.dump({"partition_L0": part_L0, "partition_L1": part_L1}, f, ensure_ascii=False)

        except Exception as e:
            log.error(f"Graph analysis failed: {e}")
        finally:
            _mark_graph_clean(index_data)
            with open(index_file, "w", encoding="utf-8") as f:
                json.dump(index_data, f, ensure_ascii=False)
                
        log.info("V9 Heavy graph topology clustering complete.")

if __name__ == "__main__":
    run_clustering()
