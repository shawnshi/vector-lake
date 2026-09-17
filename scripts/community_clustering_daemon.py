import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from filelock import FileLock

try:
    import networkx as nx
except ImportError:
    nx = None

try:
    import igraph as ig
    import leidenalg
except ImportError:
    ig = None
    leidenalg = None

# Leiden replaces Louvain.
#
# Louvain exposed a dendrogram with explicit levels; Leiden is parameterised by
# resolution instead, so the two hierarchy levels are produced by two runs:
# L0 (Global, coarse) at a low resolution and L1 (Micro, fine) at a high one.
# Leiden is randomised, so a fixed seed keeps runs reproducible.
def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


LEIDEN_L0_RESOLUTION = _env_float("VECTOR_LAKE_LEIDEN_L0_RESOLUTION", 1.0)
LEIDEN_L1_RESOLUTION = _env_float("VECTOR_LAKE_LEIDEN_L1_RESOLUTION", 2.0)
LEIDEN_SEED = int(_env_float("VECTOR_LAKE_LEIDEN_SEED", 42))

# Ensure vector_lake is in path
import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from vector_lake.wiki_utils import get_index_path, get_wiki_dir, get_meta_dir
from vector_lake import governance_store
from vector_lake.governance_store import load_governance_queue, save_governance_queue

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("vector-lake-clustering-daemon")

# A community index written by a clustering run: ``System_Community[-_]L<level>[-_]<uuid8|digits>``.
# Hand-named indexes (``...-L0-Eroom-s-Law.md``), operator-acknowledged stubs
# (``System_Community-L0.md``) and any other spelling deliberately do not match.
COMMUNITY_ARTIFACT_PATTERN = re.compile(
    r"^System_Community[_-]L\d+[_-](?:[0-9a-f]{8}|\d+)\.md$"
)


def superseded_community_artifacts(names, written) -> list[str]:
    """Names of machine-generated community indexes from an earlier generation.

    ``written`` is the set of filenames the current run produced; those are never
    returned.  Everything else that matches the machine pattern is retired, so a
    naming-generation change cannot strand the previous generation's pages --
    which is exactly what happened when the glob was ``System_Community_[0-9]*.md``
    (an underscore plus a digit, matching none of the 1078 live artifacts).
    """
    keep = set(written)
    return sorted(
        name for name in names
        if name not in keep and COMMUNITY_ARTIFACT_PATTERN.match(name)
    )

def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()

def _mark_graph_clean(index_data: dict):
    if "graph_state" not in index_data:
        index_data["graph_state"] = {}
    index_data["graph_state"]["dirty"] = False
    index_data["graph_state"]["reason"] = "Community clustering applied"
    index_data["graph_state"]["updated_at"] = _utc_now()

def _leiden_partition(nodes: list[str], edges: list[dict], resolution: float) -> dict:
    """Leiden partition of the weighted page graph, keyed by node name.

    Membership values are run-local integers; ``_stabilize_community_ids`` maps
    them onto stable UUIDs by node overlap, so re-running with a different
    partition does not orphan the community index pages.
    """
    graph = ig.Graph(directed=False)
    graph.add_vertices(len(nodes))
    graph.vs["name"] = list(nodes)
    position = {name: index for index, name in enumerate(nodes)}

    if not nodes:
        return {}
    pairs: list[tuple[int, int]] = []
    weights: list[float] = []
    for edge in edges:
        source = edge.get("source")
        target = edge.get("target")
        if source not in position or target not in position or source == target:
            continue
        pairs.append((position[source], position[target]))
        # Leiden modularity requires strictly positive weights.
        weights.append(max(float(edge.get("weight") or 1.0), 1e-6))
    if pairs:
        graph.add_edges(pairs)
        graph.es["weight"] = weights

    partition = leidenalg.find_partition(
        graph,
        leidenalg.RBConfigurationVertexPartition,
        weights="weight",
        resolution_parameter=resolution,
        seed=LEIDEN_SEED,
    )
    membership = partition.membership
    return {nodes[index]: membership[index] for index in range(len(nodes))}


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

        if not (nx and ig and leidenalg and edges):
            _mark_graph_clean(index_data)
            from vector_lake.wiki_utils import atomic_write_text
            atomic_write_text(index_file, json.dumps(index_data, ensure_ascii=False, indent=2))
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
            except Exception as exc:
                log.warning("Centrality scoring skipped for %s: %s", node_key, exc)

        try:
            # Two Leiden runs replace the Louvain dendrogram levels.
            raw_part_L1 = _leiden_partition(node_keys, edges, LEIDEN_L1_RESOLUTION)  # Micro
            raw_part_L0 = _leiden_partition(node_keys, edges, LEIDEN_L0_RESOLUTION)  # Global

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

            if not raw_part_L0:
                raw_part_L0 = raw_part_L1 or {}

            part_L0, diffs_L0, uuid_map_L0 = _stabilize_community_ids(raw_part_L0, old_part_L0, {})
            part_L1, diffs_L1, uuid_map_L1 = _stabilize_community_ids(raw_part_L1, old_part_L1, {})

            index_data["communities"] = part_L0

            wiki_dir = get_wiki_dir()
            wiki_mutations = []

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
                            # The queue is persisted by key-diff replacement, so the
                            # whole read -> append -> save cycle must hold the shared
                            # lock or a concurrent writer's item is silently dropped.
                            with governance_store.governance_queue_session():
                                queue = load_governance_queue()
                                already_queued = any(
                                    item.get("community_id") == c_uuid
                                    and item.get("status") == "pending"
                                    for item in queue.get("items", [])
                                )
                                if not already_queued:
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
id: {index_filename[:-3]}
title: "{label}"
type: system
status: Active
categories: [System]
updated: {_utc_now()}
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
                    wiki_mutations.append({"filename": index_filename, "content": content})

            process_level("L0", part_L0, diffs_L0)
            process_level("L1", part_L1, diffs_L1)

            # Retire community indexes left by an earlier naming generation.
            #
            # This used to glob ``System_Community_[0-9]*.md``, which requires an
            # underscore followed by a digit.  Every artifact in the live wiki is
            # named ``System_Community[-_]L<level>...``, so the glob matched
            # nothing and 506 pages from the previous generation survived
            # indefinitely -- together with ~41k canonical rows (claims, evidence)
            # keyed to them.  Match the machine pattern explicitly instead, and
            # never delete a page this run just wrote.
            names = [path.name for path in wiki_dir.glob("System_Community*.md")]
            for name in superseded_community_artifacts(
                names, {mutation["filename"] for mutation in wiki_mutations}
            ):
                wiki_mutations.append({"filename": name, "is_delete": True})

            if wiki_mutations:
                from vector_lake.mutation_coordinator import execute_mutation_batch
                execute_mutation_batch(wiki_mutations)

            with open(snapshot_file, "w", encoding="utf-8") as f:
                json.dump({"partition_L0": part_L0, "partition_L1": part_L1}, f, ensure_ascii=False)

            _mark_graph_clean(index_data)
            from vector_lake.wiki_utils import atomic_write_text
            atomic_write_text(index_file, json.dumps(index_data, ensure_ascii=False, indent=2))
        except Exception as e:
            log.error(f"Graph analysis failed: {e}")
            raise
                
        log.info("V9 Heavy graph topology clustering complete.")

if __name__ == "__main__":
    run_clustering()
