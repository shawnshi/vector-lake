import json
import logging
import os
import sqlite3
import webbrowser

from filelock import FileLock, Timeout

from vector_lake import get_extension_root
from vector_lake import db_store
from vector_lake import governance_store
from vector_lake.indexer import (
    PROJECTION_MANIFEST_KEY,
    ProjectionPairContractError,
    _cached_projection_sha256,
    _projection_file_identity,
    _validate_canonical_generation_binding,
    _validate_projection_sidecar,
    canonical_runtime_generation_snapshot,
    read_committed_index_snapshot,
    validate_projection_pair,
)
from vector_lake.wiki_utils import (
    get_claim_graph_path,
    get_index_path,
    get_legacy_claim_graph_path,
    get_memory_dir,
    get_projection_manifest_path,
)


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
            "group": (node.get("categories") or ["Uncategorized"])[0] if isinstance(node.get("categories"), list) else (node.get("categories") or "Uncategorized"),
            "raw_type": key.split("_")[0] if "_" in key else str(node.get("type", "unknown")).capitalize(),
            "community": communities.get(key, 0),
            "degree": links_count.get(key, 0),
            "updated": node.get("updated", ""),
            "summary": node.get("summary", ""),
            "sources": node.get("sources", []),
            "semantic_links": sorted(set(valid_links)),
            "node_kind": "page",
            "status": node.get("status", ""),
            "domain": node.get("domain", ""),
            "alignment_score": node.get("alignment_score", 100),
            "decay_weight": node.get("decay_weight", 1.0),
        })

    return {
        "nodes": graph_nodes,
        "edges": weighted_edges,
        "community_labels": index_data.get("community_labels", {}),
    }


def _build_claim_graph(claim_graph: dict) -> dict:
    claim_graph = claim_graph or {}
    adjacency = {}
    for edge in claim_graph.get("edges", []):
        source = edge.get("source")
        target = edge.get("target")
        if not source or not target:
            continue
        adjacency.setdefault(source, []).append(target)
        adjacency.setdefault(target, []).append(source)

    nodes = []
    for node in claim_graph.get("nodes", []):
        nodes.append({
            "id": node.get("id"),
            "name": node.get("name", node.get("id", "")),
            "group": node.get("group", "Claim"),
            "degree": node.get("degree", 0),
            "updated": node.get("updated", ""),
            "summary": node.get("summary", ""),
            "sources": node.get("source_pages", []),
            "semantic_links": adjacency.get(node.get("id"), []),
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


def _build_graph_payload(index_data: dict, claim_graph_data: dict | None = None) -> dict:
    page_graph = _build_page_graph(index_data)
    claim_graph = _build_claim_graph(claim_graph_data or {})
    return {
        "pageGraph": page_graph,
        "claimGraph": claim_graph,
        "governanceMetrics": index_data.get("governance_metrics", {}),
    }


def _read_current_canonical_generation() -> dict[str, int]:
    """Read live runtime generations through a query-only SQLite snapshot."""
    db_path = db_store.peek_db_path().resolve()
    if not db_path.is_file():
        raise ProjectionPairContractError(
            f"Canonical database is missing: {db_path}"
        )
    connection = None
    try:
        connection = sqlite3.connect(
            f"{db_path.as_uri()}?mode=ro",
            uri=True,
            timeout=5.0,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        return canonical_runtime_generation_snapshot(connection)
    except (OSError, sqlite3.Error) as exc:
        raise ProjectionPairContractError(
            f"Cannot verify current canonical runtime generation: {exc}"
        ) from exc
    finally:
        if connection is not None:
            if connection.in_transaction:
                connection.rollback()
            connection.close()




def _read_projection_pair(
    index_path: str,
    claim_graph_path: str,
    lock_timeout: float = 5,
) -> tuple[dict, dict]:
    """Read and validate one projection generation under the publish lock."""
    lock_path = index_path + ".lock"
    with FileLock(lock_path, timeout=lock_timeout):
        if not os.path.exists(index_path):
            raise FileNotFoundError(index_path)
        if not os.path.exists(claim_graph_path):
            legacy_path = get_legacy_claim_graph_path()
            if legacy_path.exists():
                raise ProjectionPairContractError(
                    "Legacy claim_topology.json cannot be paired with index.json; "
                    "run sync to migrate both projections."
                )
            raise ProjectionPairContractError(
                "claim_graph.json is missing; run sync to rebuild both projections."
            )
        with open(index_path, "r", encoding="utf-8") as handle:
            index_data = json.load(handle)
        with open(claim_graph_path, "r", encoding="utf-8") as handle:
            claim_graph_data = json.load(handle)
        validate_projection_pair(index_data, claim_graph_data)
        manifest = index_data[PROJECTION_MANIFEST_KEY]
        if "canonical_generation" not in manifest:
            raise ProjectionPairContractError(
                "Legacy projection manifest has no canonical_generation; run a full rebuild."
            )
        sidecar_path = get_projection_manifest_path()
        if not sidecar_path.exists():
            raise ProjectionPairContractError(
                "Projection sidecar is missing; run sync to rebuild the pair."
            )
        with open(sidecar_path, "r", encoding="utf-8") as handle:
            sidecar = json.load(handle)
        sidecar_manifest, sidecar_artifacts = _validate_projection_sidecar(sidecar)
        del sidecar
        if sidecar_manifest != manifest:
            raise ProjectionPairContractError(
                "Projection sidecar manifest does not match the projection pair."
            )
        binding = _validate_canonical_generation_binding(manifest)
        if binding.get("status") != "verified":
            raise ProjectionPairContractError(
                "Projection canonical-generation binding is unverifiable; "
                "run a full sync before reading the graph."
            )
        for path in (index_path, claim_graph_path):
            metadata = sidecar_artifacts[os.path.basename(path)]
            if os.path.getsize(path) != metadata["bytes"]:
                raise ProjectionPairContractError(
                    f"Projection sidecar size does not match {os.path.basename(path)}."
                )
            digest, identity = _cached_projection_sha256(path)
            if digest != metadata["sha256"]:
                raise ProjectionPairContractError(
                    f"Projection sidecar digest does not match {os.path.basename(path)}."
                )
            if _projection_file_identity(path) != identity:
                raise ProjectionPairContractError(
                    f"Projection changed while reading {os.path.basename(path)}."
                )
        current_generation = _read_current_canonical_generation()
        if binding["runtime_generations"] != current_generation:
            raise ProjectionPairContractError(
                "Projection canonical-generation binding is stale; "
                "run sync to rebuild the graph projections."
            )
        return index_data, claim_graph_data


def visualize_vector_lake(output_dir: str = None):
    bootstrap = governance_store.ensure_canonical_store_populated()
    if bootstrap.get("bootstrapped"):
        from vector_lake import indexer

        indexer.generate_index()

    extension_root = get_extension_root()
    memory_dir = str(get_memory_dir())
    index_path = str(get_index_path())
    claim_graph_path = str(get_claim_graph_path())
    template_path = str(extension_root / "templates" / "topology.html")
    
    if output_dir:
        output_path = os.path.join(output_dir, "vector_lake_graph.html")
    else:
        output_path = _graph_output_path(memory_dir)


    if not os.path.exists(index_path):
        return "Error: Lake is drying. index.json not found. Please ingest sources first."
    if not os.path.exists(template_path):
        return "Error: template not found."

    try:
        index_data, claim_graph = _read_projection_pair(
            index_path,
            claim_graph_path,
        )
    except Timeout:
        log.warning("Timeout acquiring the graph projection publish lock.")
        return "Error: System is busy publishing graph projections. Please try again later."
    except ProjectionPairContractError as exc:
        return f"Error: {exc}"
    except FileNotFoundError:
        return "Error: Lake is drying. index.json not found. Please ingest sources first."
    except (OSError, json.JSONDecodeError):
        return "Error: Failed to read a consistent graph projection pair."

    graph_data = _build_graph_payload(index_data, claim_graph)

    with open(template_path, "r", encoding="utf-8") as handle:
        html = handle.read()

    # 🛡️ Sentinel: Escape HTML characters to prevent XSS when injecting JSON into a <script> block
    safe_graph_data = json.dumps(graph_data, ensure_ascii=False).replace('<', r'\u003c').replace('>', r'\u003e').replace('&', r'\u0026')
    html = html.replace("%%GRAPH_DATA%%", safe_graph_data)
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
    # Removed synchronous refresh_graph_topology_if_dirty()


    index_path = str(get_index_path())
    if not os.path.exists(index_path):
        return "Error: Lake is drying. index.json not found. Please ingest sources first."

    try:
        data = read_committed_index_snapshot(index_path)
    except Timeout:
        return "Error: System is busy publishing graph projections. Please try again later."
    except ProjectionPairContractError as exc:
        return f"Error: committed graph projection is not ready; {exc}"

    insights = data.get("graph_insights", [])
    if not insights:
        return "No graph insights found. Please ensure 'sync' has been run recently."

    import uuid
    from datetime import datetime, timezone

    items = []
    for insight in insights:
        search_queries = [insight.get("node", "")] if insight.get("node") else []
        affected_pages = [f"wiki/{insight.get('node', '')}.md"] if insight.get("node") else []
        items.append({
            "item_id": f"gov_{uuid.uuid4().hex[:12]}",
            "type": "suggestion",
            "title": f"Topology Insight: {insight['type'].replace('_', ' ').title()}",
            "description": insight.get("description", "A topological insight was detected."),
            "search_queries": search_queries,
            "affected_pages": affected_pages,
            "source": "audit-graph",
            "status": "pending",
            "created_at": datetime.now(timezone.utc).isoformat(),
        })

    if items:
        from vector_lake import governance_store
        created = sum(
            1
            for item in items
            if governance_store.insert_governance_item_if_absent(item, ("title",))
        )
        if created:
            return f"Audit complete. Pushed {created} new graph topology insights into the async review queue ({len(items) - created} duplicates skipped)."
        else:
            return f"Audit complete. No new actionable insights found ({len(items)} existing insights already in queue)."
    return "Audit complete. No actionable insights found."
