import json
import logging
import math
import os
import re
from datetime import datetime, timezone

import yaml
from filelock import FileLock, Timeout

from vector_lake import governance_metrics
from vector_lake import governance_store
from vector_lake.wiki_utils import get_claim_graph_path, get_index_path, get_wiki_dir

try:
    import networkx as nx
    from community import community_louvain
except ImportError:
    nx = None
    community_louvain = None


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("vector-lake-indexer")

VALID_PREFIXES = ("Concept_", "Source_", "Entity_", "Synthesis_", "Event_", "Person_", "Project_", "Term_", "System_")

RELEVANCE_WEIGHTS = {
    "direct_link": 3.0,
    "source_overlap": 4.0,
    "common_neighbor": 1.5,
    "type_affinity": 1.0,
}

TYPE_AFFINITY = {
    "entity": {"entity": 0.8, "concept": 1.2, "source": 1.0, "synthesis": 1.0},
    "concept": {"entity": 1.2, "concept": 0.8, "source": 1.0, "synthesis": 1.2},
    "source": {"entity": 1.0, "concept": 1.0, "source": 0.5, "synthesis": 1.0},
    "synthesis": {"entity": 1.0, "concept": 1.2, "source": 1.0, "synthesis": 0.8},
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _wiki_dir() -> str:
    return str(get_wiki_dir())


def _empty_index_data() -> dict:
    return {
        "nodes": {},
        "aliases": {},
        "categories": set(),
        "weighted_edges": [],
        "error_log": [],
        "communities": {},
        "community_labels": {},
        "graph_insights": [],
        "graph_state": {
            "dirty": False,
            "reason": "",
            "updated_at": None,
        },
    }


def _load_index_unlocked(output_path: str) -> dict | None:
    if not os.path.exists(output_path):
        return None
    with open(output_path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _write_json_payload(output_path: str, payload: dict):
    lock_path = output_path + ".lock"
    try:
        with FileLock(lock_path, timeout=15):
            temp_path = output_path + ".tmp"
            with open(temp_path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
            os.replace(temp_path, output_path)
    except Timeout:
        log.error(f"Timeout while acquiring lock for {output_path}")


def _write_index(output_path: str, index_data: dict):
    _write_json_payload(output_path, index_data)


def _write_claim_graph(output_path: str, claim_graph_data: dict):
    _write_json_payload(output_path, claim_graph_data)


def _mark_graph_dirty(index_data: dict, reason: str):
    graph_state = index_data.setdefault("graph_state", {})
    graph_state["dirty"] = True
    graph_state["reason"] = reason
    graph_state["updated_at"] = _utc_now()


def _mark_graph_clean(index_data: dict):
    graph_state = index_data.setdefault("graph_state", {})
    graph_state["dirty"] = False
    graph_state["reason"] = ""
    graph_state["updated_at"] = _utc_now()


def is_graph_dirty(index_data: dict | None) -> bool:
    if not index_data:
        return True
    graph_state = index_data.get("graph_state", {})
    return bool(graph_state.get("dirty"))


def _parse_wiki_node(filepath: str, node_key: str):
    try:
        with open(filepath, "r", encoding="utf-8") as handle:
            content = handle.read()
    except (UnicodeDecodeError, OSError) as e:
        log.warning(f"Cannot read {os.path.basename(filepath)}: {e}")
        return None

    frontmatter_match = re.search(r"^---\n(.*?)\n---", content, re.MULTILINE | re.DOTALL)
    if not frontmatter_match:
        return None

    fm_str = frontmatter_match.group(1)
    fm_data = yaml.safe_load(fm_str) or {}

    node_id = fm_data.get("id", "")
    title = fm_data.get("title", node_key)

    raw_type = str(fm_data.get("type", "concept")).lower().strip().replace('"', "").replace("'", "")
    if raw_type in ["entity", "person", "system", "project", "organization"]:
        node_type = "concept" if raw_type == "system" else "entity"
    elif raw_type in ["source", "reference"]:
        node_type = "source"
    elif raw_type in ["synthesis", "comparison", "report"]:
        node_type = "synthesis"
    else:
        node_type = "concept"

    updated = str(fm_data.get("updated", ""))
    categories = fm_data.get("categories", [])
    domain = fm_data.get("domain")
    topic_cluster = fm_data.get("topic_cluster", "General")
    status = fm_data.get("status")

    if not domain or not status:
        log.warning(f"Schema violation: Missing 'domain' or 'status' in {os.path.basename(filepath)}. Node excluded from index.")
        return None

    raw_aliases = fm_data.get("aliases", [])
    if isinstance(raw_aliases, list):
        aliases = [str(alias).strip() for alias in raw_aliases]
    elif isinstance(raw_aliases, str):
        aliases = [raw_aliases.strip()]
    else:
        aliases = []

    raw_sources = fm_data.get("sources", [])
    if isinstance(raw_sources, list):
        sources = [str(source).strip() for source in raw_sources if source]
    elif isinstance(raw_sources, str):
        sources = [raw_sources.strip()]
    else:
        sources = []

    links = set()
    body = content[frontmatter_match.end() :]
    for match in re.finditer(r"\[([^\[\]]+?)::\s*\[\[(.*?)\]\]\]", body):
        links.add(match.group(2).split("|")[0].strip().replace(".md", ""))
    for match in re.finditer(r"\[\[(.*?)\]\]", body):
        link_text = match.group(1).split("|")[0].strip().replace(".md", "")
        if "::" in link_text:
            link_text = link_text.split("::", 1)[1].strip()
        links.add(link_text)
    links.discard("")

    summary_text = re.sub(r"#.*?\n", "", body)
    summary_text = re.sub(r"\[\[([^\]]*?\|)?([^\]]*?)\]\]", r"\2", summary_text)
    summary_text = re.sub(r"\[([^\[\]]+?)::\s*\[\[.*?\]\]\]", "", summary_text)
    summary_text = summary_text.strip().replace("\n", " ")

    return {
        "id": node_id,
        "title": title,
        "type": node_type,
        "updated": updated,
        "categories": categories,
        "domain": domain,
        "topic_cluster": topic_cluster,
        "status": status,
        "aliases": aliases,
        "sources": sources,
        "links": sorted(links),
        "summary": summary_text[:240],
    }


def calculate_relevance(node_a: dict, node_b: dict, all_nodes: dict) -> float:
    score = 0.0
    key_a = node_a.get("_key", "")
    key_b = node_b.get("_key", "")

    links_a = set(node_a.get("links", []))
    links_b = set(node_b.get("links", []))

    if key_b in links_a:
        score += RELEVANCE_WEIGHTS["direct_link"]
    if key_a in links_b:
        score += RELEVANCE_WEIGHTS["direct_link"]

    sources_a = set(node_a.get("sources", []))
    sources_b = set(node_b.get("sources", []))
    shared_sources = len(sources_a & sources_b)
    score += shared_sources * RELEVANCE_WEIGHTS["source_overlap"]

    common_neighbors = links_a & links_b
    for neighbor_key in common_neighbors:
        neighbor = all_nodes.get(neighbor_key, {})
        degree = len(neighbor.get("links", []))
        if degree > 1:
            score += (1.0 / math.log(degree)) * RELEVANCE_WEIGHTS["common_neighbor"]

    type_a = node_a.get("type", "concept").lower()
    type_b = node_b.get("type", "concept").lower()
    affinity = TYPE_AFFINITY.get(type_a, {}).get(type_b, 0.5) * RELEVANCE_WEIGHTS["type_affinity"]
    score += affinity

    return round(score, 3)


def _calculate_weighted_edges(index_data: dict) -> list[dict]:
    nodes_dict = index_data["nodes"]
    node_keys = list(nodes_dict.keys())

    for key, node in nodes_dict.items():
        node["_key"] = key

    edges = []
    for key_a in node_keys:
        node_a = nodes_dict[key_a]
        links_a = set(node_a.get("links", []))
        sources_a = set(node_a.get("sources", []))

        for key_b in node_keys:
            if key_a >= key_b:
                continue

            node_b = nodes_dict[key_b]
            links_b = set(node_b.get("links", []))
            sources_b = set(node_b.get("sources", []))

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
                    "weight": relevance,
                })

    for node in nodes_dict.values():
        node.pop("_key", None)

    edges.sort(key=lambda edge: edge["weight"], reverse=True)
    return edges


def _apply_graph_topology(index_data: dict):
    edges = index_data.get("weighted_edges", [])
    node_keys = list(index_data["nodes"].keys())
    index_data["communities"] = {}
    index_data["community_labels"] = {}
    index_data["graph_insights"] = []

    if not (nx and community_louvain and edges):
        _mark_graph_clean(index_data)
        return

    G = nx.Graph()
    for key in node_keys:
        G.add_node(key)
    for edge in edges:
        G.add_edge(edge["source"], edge["target"], weight=edge["weight"])

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
            community_labels[int(comm_id)] = f"Comm {comm_id}: {' / '.join(titles) if titles else 'Unknown'}"
        index_data["community_labels"] = community_labels
    except Exception as e:
        log.error(f"Graph analysis failed: {e}")
    finally:
        _mark_graph_clean(index_data)


def generate_index():
    wiki_dir = _wiki_dir()
    if not os.path.exists(wiki_dir):
        log.warning(f"Wiki directory not found at {wiki_dir}")
        return

    index_data = _empty_index_data()
    files = [name for name in os.listdir(wiki_dir) if name.endswith(".md") and name not in ("index.md", "log.md", "overview.md")]

    for filename in files:
        filepath = os.path.join(wiki_dir, filename)
        if not filename.startswith(VALID_PREFIXES) and filename not in ("index.md", "log.md"):
            index_data["error_log"].append({"file": filename, "error": "Schema violation: Missing valid entity prefix."})
            log.warning(f"Schema violation in {filename}, bypassing index inclusion.")
            continue

        node_key = filename[:-3]
        try:
            node_data = _parse_wiki_node(filepath, node_key)
        except yaml.YAMLError as e:
            index_data["error_log"].append({"file": filename, "error": str(e)})
            log.warning(f"YAML Error in {filename}, suppressing to error_log.")
            continue

        if node_data is None:
            continue

        index_data["nodes"][node_key] = node_data
        if node_data["id"]:
            index_data["aliases"][node_data["id"]] = node_key
        index_data["aliases"][node_key] = node_key
        for alias in node_data["aliases"]:
            index_data["aliases"][alias] = node_key
        if isinstance(node_data["categories"], list):
            for category in node_data["categories"]:
                index_data["categories"].add(category)

    index_data["weighted_edges"] = _calculate_weighted_edges(index_data)
    _apply_graph_topology(index_data)
    index_data["categories"] = list(index_data["categories"])
    index_data["governance_metrics"] = governance_metrics.compute_debt_metrics()
    index_data["schema_version"] = "8.0"

    output_path = str(get_index_path())
    claim_graph_path = str(get_claim_graph_path())
    _write_claim_graph(claim_graph_path, governance_store.build_claim_graph_projection())
    _write_index(output_path, index_data)
    log.info(
        f"Generated index.json with {len(index_data['nodes'])} nodes | "
        f"{len(index_data['weighted_edges'])} weighted edges | "
        f"{len(index_data.get('error_log', []))} errors."
    )
    return output_path


def update_index_item(filename: str):
    if not filename.endswith(".md") or filename in ("index.md", "log.md", "overview.md"):
        return

    output_path = str(get_index_path())
    wiki_dir = _wiki_dir()
    if not os.path.exists(output_path):
        return generate_index()

    lock_path = output_path + ".lock"
    needs_full_rebuild = False
    try:
        with FileLock(lock_path, timeout=15):
            try:
                index_data = _load_index_unlocked(output_path)
            except json.JSONDecodeError:
                needs_full_rebuild = True
                index_data = None

            if index_data is None:
                needs_full_rebuild = True
            else:
                if isinstance(index_data.get("categories"), list):
                    index_data["categories"] = set(index_data["categories"])

                filepath = os.path.join(wiki_dir, filename)
                node_key = filename[:-3]

                if not filename.startswith(VALID_PREFIXES) and filename not in ("index.md", "log.md"):
                    index_data.setdefault("error_log", [])
                    index_data["error_log"] = [item for item in index_data["error_log"] if item.get("file") != filename]
                    index_data["error_log"].append({"file": filename, "error": "Schema violation: Missing valid entity prefix."})
                    log.warning(f"Schema violation in {filename} during partial update.")
                    index_data.get("nodes", {}).pop(node_key, None)
                    index_data["weighted_edges"] = [
                        edge for edge in index_data.get("weighted_edges", [])
                        if edge["source"] != node_key and edge["target"] != node_key
                    ]
                else:
                    index_data["aliases"] = {key: value for key, value in index_data.get("aliases", {}).items() if value != node_key}
                    index_data.setdefault("error_log", [])
                    index_data["error_log"] = [item for item in index_data["error_log"] if item.get("file") != filename]

                    if not os.path.exists(filepath):
                        index_data.get("nodes", {}).pop(node_key, None)
                        index_data["weighted_edges"] = [
                            edge for edge in index_data.get("weighted_edges", [])
                            if edge["source"] != node_key and edge["target"] != node_key
                        ]
                    else:
                        try:
                            node_data = _parse_wiki_node(filepath, node_key)
                        except yaml.YAMLError as e:
                            index_data["error_log"].append({"file": filename, "error": str(e)})
                            log.warning(f"YAML Error in {filename} during partial update.")
                            node_data = None

                        if node_data is None:
                            index_data["error_log"].append({"file": filename, "error": "Schema violation: Missing 'domain' or 'status'. Node excluded."})
                            index_data.get("nodes", {}).pop(node_key, None)
                        else:
                            index_data["nodes"][node_key] = node_data
                            if node_data["id"]:
                                index_data["aliases"][node_data["id"]] = node_key
                            index_data["aliases"][node_key] = node_key
                            for alias in node_data["aliases"]:
                                index_data["aliases"][alias] = node_key

                            if isinstance(node_data["categories"], list):
                                categories = set(index_data.get("categories", []))
                                for category in node_data["categories"]:
                                    categories.add(category)
                                index_data["categories"] = categories

                            index_data["weighted_edges"] = [
                                edge for edge in index_data.get("weighted_edges", [])
                                if edge["source"] != node_key and edge["target"] != node_key
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

                _mark_graph_dirty(index_data, f"Partial update for {filename}")
                index_data["categories"] = list(index_data.get("categories", []))
                index_data["governance_metrics"] = governance_metrics.compute_debt_metrics()
                index_data["schema_version"] = "8.0"
                temp_path = output_path + ".tmp"
                with open(temp_path, "w", encoding="utf-8") as handle:
                    json.dump(index_data, handle, ensure_ascii=False, separators=(",", ":"))
                os.replace(temp_path, output_path)
                _write_claim_graph(str(get_claim_graph_path()), governance_store.build_claim_graph_projection())
    except Timeout:
        log.error(f"Timeout while acquiring lock for {output_path}")
        return

    if needs_full_rebuild:
        return generate_index()


def refresh_graph_topology_if_dirty() -> bool:
    output_path = str(get_index_path())
    if not os.path.exists(output_path):
        generate_index()
        return True

    lock_path = output_path + ".lock"
    try:
        with FileLock(lock_path, timeout=15):
            try:
                index_data = _load_index_unlocked(output_path)
            except json.JSONDecodeError:
                index_data = None
    except Timeout:
        log.error(f"Timeout while acquiring lock for {output_path}")
        return False

    if is_graph_dirty(index_data):
        generate_index()
        return True
    return False


if __name__ == "__main__":
    generate_index()

