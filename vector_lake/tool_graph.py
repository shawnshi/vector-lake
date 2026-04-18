import json
import logging
import os
import webbrowser
from pathlib import Path

from filelock import FileLock, Timeout

from vector_lake import get_extension_root
from vector_lake import indexer
from vector_lake import review
from vector_lake import governance_store
from vector_lake.wiki_utils import get_index_path, get_memory_dir


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("vector-lake-tool-graph")


def _graph_output_path(memory_dir: str) -> str:
    extension_root = str(get_extension_root())
    candidates = [
        os.path.join(os.path.dirname(memory_dir), "tmp", "vector_lake_graph.html"),
        os.path.join(extension_root, "data", "tmp", "vector_lake_graph.html"),
    ]
    for candidate in candidates:
        try:
            os.makedirs(os.path.dirname(candidate), exist_ok=True)
            with open(candidate, "a", encoding="utf-8"):
                pass
            return candidate
        except OSError:
            continue
    return candidates[-1]


def _build_page_graph(index_data: dict) -> dict:
    nodes_dict = index_data.get("nodes", {})
    communities = index_data.get("communities", {})
    weighted_edges = index_data.get("weighted_edges", [])
    aliases = index_data.get("aliases", {})

    links_count = {key: 0 for key in nodes_dict}
    for key, node in nodes_dict.items():
        for target in node.get("links", []):
            target_key = aliases.get(target, target)
            if target_key in nodes_dict:
                links_count[target_key] = links_count.get(target_key, 0) + 1
            links_count[key] += 1

    graph_nodes = []
    for key, node in nodes_dict.items():
        valid_links = []
        for target in node.get("links", []):
            target_key = aliases.get(target, target)
            if target_key in nodes_dict:
                valid_links.append(target_key)
        graph_nodes.append({
            "id": key,
            "nid": node.get("id", ""),
            "name": node.get("title", key),
            "group": str(node.get("type", "unknown")).capitalize(),
            "community": communities.get(key, 0),
            "degree": links_count.get(key, 0),
            "updated": node.get("updated", ""),
            "summary": node.get("summary", ""),
            "sources": node.get("sources", []),
            "semantic_links": sorted(set(valid_links)),
            "node_kind": "page",
            "status": node.get("status", ""),
            "domain": node.get("domain", ""),
        })

    return {
        "nodes": graph_nodes,
        "edges": weighted_edges,
        "community_labels": index_data.get("community_labels", {}),
    }


def _build_claim_graph(index_data: dict) -> dict:
    claim_graph = index_data.get("claim_graph", {}) or {}
    nodes = []
    for node in claim_graph.get("nodes", []):
        nodes.append({
            "id": node.get("id"),
            "name": node.get("name", node.get("id", "")),
            "group": node.get("group", "Claim"),
            "degree": node.get("degree", 0),
            "updated": node.get("updated", ""),
            "summary": node.get("full_text", node.get("summary", "")),
            "sources": node.get("source_pages", []),
            "semantic_links": node.get("semantic_links", []),
            "node_kind": "claim",
            "validity_state": node.get("validity_state", "unknown"),
            "claim_type": node.get("claim_type", "claim"),
            "confidence": node.get("confidence"),
            "subject_entities": node.get("subject_entities", []),
        })
    return {
        "nodes": nodes,
        "edges": claim_graph.get("edges", []),
        "community_labels": {},
    }


def _build_graph_payload(index_data: dict) -> dict:
    page_graph = _build_page_graph(index_data)
    claim_graph = _build_claim_graph(index_data)
    return {
        "nodes": page_graph["nodes"],
        "edges": page_graph["edges"],
        "community_labels": page_graph["community_labels"],
        "pageGraph": page_graph,
        "claimGraph": claim_graph,
        "governanceMetrics": index_data.get("governance_metrics", {}),
    }


def visualize_vector_lake():
    bootstrap = governance_store.ensure_canonical_store_populated()
    if bootstrap.get("bootstrapped"):
        indexer.generate_index()
    else:
        indexer.refresh_graph_topology_if_dirty()

    extension_root = get_extension_root()
    memory_dir = str(get_memory_dir())
    index_path = str(get_index_path())
    lock_path = index_path + ".lock"
    template_path = str(extension_root / "templates" / "topology.html")
    output_path = _graph_output_path(memory_dir)

    if not os.path.exists(index_path):
        return "Error: index.json not found."
    if not os.path.exists(template_path):
        return "Error: template not found."

    try:
        with FileLock(lock_path, timeout=5):
            with open(index_path, "r", encoding="utf-8") as handle:
                index_data = json.load(handle)
    except Timeout:
        log.warning("Timeout acquiring lock for index.json during graph generation. Falling back to read-only mode.")
        try:
            with open(index_path, "r", encoding="utf-8") as handle:
                index_data = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return "Error: System is busy generating the index. Please try again later."
    except json.JSONDecodeError:
        return "Error: Failed to parse index.json."

    claim_graph = index_data.get("claim_graph", {}) or {}
    if not claim_graph.get("nodes") and index_data.get("nodes"):
        projection = governance_store.governance_projection()
        index_data["entity_index"] = projection.get("entity_index", {})
        index_data["claim_index"] = projection.get("claim_index", {})
        index_data["source_index"] = projection.get("source_index", {})
        index_data["claim_graph"] = projection.get("claim_graph", {"nodes": [], "edges": []})

    graph_data = _build_graph_payload(index_data)

    with open(template_path, "r", encoding="utf-8") as handle:
        html = handle.read()
    html = html.replace("%%GRAPH_DATA%%", json.dumps(graph_data, ensure_ascii=False))
    html = html.replace("%%MEMORY_BASE_PATH%%", f"file:///{memory_dir.replace(os.sep, '/')}/")

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as handle:
        handle.write(html)

    webbrowser.open(f"file:///{output_path.replace(os.sep, '/')}")
    return (
        f"Visualized {len(graph_data['pageGraph']['nodes'])} page nodes / "
        f"{len(graph_data['claimGraph']['nodes'])} claim nodes. Opened graph in browser: {output_path}"
    )


def audit_graph() -> str:
    indexer.refresh_graph_topology_if_dirty()

    index_path = str(get_index_path())
    if not os.path.exists(index_path):
        return "Error: index.json not found."

    with open(index_path, "r", encoding="utf-8") as handle:
        data = json.load(handle)

    insights = data.get("graph_insights", [])
    if not insights:
        return "No graph insights found. Please ensure 'sync' has been run recently."

    items = []
    for insight in insights:
        search_queries = [insight.get("node", "")] if insight.get("node") else []
        affected_pages = [f"wiki/{insight.get('node', '')}.md"] if insight.get("node") else []
        items.append({
            "type": "suggestion",
            "title": f"Topology Insight: {insight['type'].replace('_', ' ').title()}",
            "description": insight.get("description", "A topological insight was detected."),
            "search_queries": search_queries,
            "affected_pages": affected_pages,
            "source": "audit-graph",
            "created": None,
            "resolved": False,
            "resolution": None,
        })

    if items:
        review.add_items(items)
        return f"Audit complete. Pushed {len(items)} graph topology insights into the async review queue."
    return "Audit complete. No actionable insights found."

