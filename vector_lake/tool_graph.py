import hashlib
import json
import logging
import os
import stat
import tempfile
import webbrowser
from pathlib import Path

from filelock import FileLock, Timeout

# ``governance_store`` is re-exported deliberately: no code in this module reads it
# (the write path re-imports it locally), but tests/test_runtime_contracts.py patches
# ``tool_graph.governance_store.insert_governance_item_if_absent`` to prove the audit
# preview never writes.  Dropping the import removes that handle.
from vector_lake import (  # noqa: F401
    db_store,
    get_extension_root,
    governance_store,
)
from vector_lake.cancellation import cancellation_checkpoint, non_interruptible_phase
from vector_lake.indexer import (
    ProjectionPairContractError,
    read_committed_index_snapshot,
)
from vector_lake.projection_format_v2 import (
    ProjectionV2ContractError,
    is_v2_locator,
    load_committed_pair,
)
from vector_lake.projection_store_v2 import ProjectionStoreError
from vector_lake.wiki_utils import (
    get_claim_graph_path,
    get_index_path,
    get_memory_dir,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("vector-lake-tool-graph")


def _assert_graph_path(path: Path) -> None:
    """Reject links/reparse points before resolving a host-authorized path."""
    for current in (*reversed(path.parents), path):
        try:
            details = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(details.st_mode) or getattr(details, "st_file_attributes", 0) & 0x400:
            raise ValueError("Graph output path contains a symlink or reparse point.")
        if current != path and not stat.S_ISDIR(details.st_mode):
            raise ValueError("Graph output ancestor is not a directory.")


def _graph_output_path(output_dir: str | None = None) -> Path:
    """Authorize without creating anything; never infer host sandbox roots."""
    roots = []
    for value in os.environ.get("VECTOR_LAKE_AGENT_SANDBOX_ROOTS", "").split(os.pathsep):
        if not value.strip():
            continue
        root = Path(value.strip()).expanduser()
        if not root.is_absolute() or ".." in root.parts:
            raise ValueError("VECTOR_LAKE_AGENT_SANDBOX_ROOTS requires absolute roots without traversal.")
        _assert_graph_path(root)
        if root.exists() and not root.is_dir():
            raise ValueError("Approved graph sandbox root is not a directory.")
        roots.append(root.resolve())
    if not roots:
        raise ValueError("Graph output requires an approved agent sandbox configured by VECTOR_LAKE_AGENT_SANDBOX_ROOTS (also for omitted output_dir).")
    if output_dir is not None and (not isinstance(output_dir, str) or not output_dir.strip()):
        raise ValueError("Graph output_dir must not be empty.")
    directory = Path(output_dir).expanduser() if output_dir is not None else roots[0]
    if not directory.is_absolute() or ".." in directory.parts:
        raise ValueError("Graph output_dir must be absolute and contain no traversal.")
    _assert_graph_path(directory)
    if directory.exists() and not directory.is_dir():
        raise ValueError("Graph output_dir is not a directory.")
    directory = directory.resolve()
    if not any(directory.is_relative_to(root) for root in roots):
        raise ValueError("Graph output must be within an approved agent sandbox configured by VECTOR_LAKE_AGENT_SANDBOX_ROOTS.")
    target = directory / "vector_lake_graph.html"
    _assert_graph_path(target)
    if target.exists() and not target.is_file():
        raise ValueError("Graph output target is not a regular file.")
    return target


def _script_json(value) -> str:
    return (json.dumps(value, ensure_ascii=False)
            .replace("<", r"\u003c").replace(">", r"\u003e").replace("&", r"\u0026")
            .replace("\u2028", r"\u2028").replace("\u2029", r"\u2029"))


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
            raise ProjectionPairContractError(
                "claim_graph.json is missing; run sync to rebuild both projections."
            )
        if not (
            is_v2_locator(index_path, "index")
            and is_v2_locator(claim_graph_path, "claim_graph")
        ):
            raise ProjectionPairContractError(
                "Projection v2 static locators are required; legacy v1 is "
                "available only to migration and rollback helpers."
            )
        try:
            with db_store.read_only_transaction_snapshot() as connection:
                return load_committed_pair(Path(index_path).parent, connection=connection)
        except (ProjectionV2ContractError, ProjectionStoreError) as exc:
            reason = str(exc)
            if reason == "sidecar_unreadable":
                message = "Projection v2 sidecar is missing or unreadable."
            elif reason == "canonical_generation_stale":
                message = "Projection canonical-generation binding is stale."
            else:
                message = f"Projection v2 pair verification failed: {reason}."
            raise ProjectionPairContractError(
                f"{message} Run sync to rebuild the graph projections."
            ) from exc


def visualize_vector_lake(output_dir: str | None = None):
    """Export a verified, as-of committed projection; never bootstrap or repair."""
    try:
        output_path = _graph_output_path(output_dir)
    except (ValueError, OSError) as exc:
        return f"Error: {exc}"

    memory_dir = get_memory_dir()
    index_path = str(get_index_path())
    claim_graph_path = str(get_claim_graph_path())
    template_path = get_extension_root() / "templates" / "topology.html"
    if not os.path.exists(index_path):
        return "Error: Committed index.json not found. Run an explicit projection rebuild first."
    if not template_path.is_file():
        return "Error: template not found."
    try:
        cancellation_checkpoint("graph_before_projection_read")
        index_data, claim_graph = _read_projection_pair(index_path, claim_graph_path)
    except Timeout:
        return "Error: System is busy publishing graph projections. Please try again later."
    except (ProjectionPairContractError, db_store.ReadOnlySnapshotUnavailable) as exc:
        return f"Error: {exc}"
    except (OSError, json.JSONDecodeError):
        return "Error: Failed to read a consistent graph projection pair."

    cancellation_checkpoint("graph_before_render")
    graph_data = _build_graph_payload(index_data, claim_graph)
    manifest = index_data.get("projection_manifest", {})
    graph_data["exportMetadata"] = {
        "projection_generation": manifest.get("generation"),
        "canonical_generation": manifest.get("canonical_generation"),
        "published_at": manifest.get("published_at"),
        "as_of_committed_projection": True,
        "page_nodes": len(graph_data["pageGraph"]["nodes"]),
        "page_edges": len(graph_data["pageGraph"]["edges"]),
        "claim_nodes": len(graph_data["claimGraph"]["nodes"]),
        "claim_edges": len(graph_data["claimGraph"]["edges"]),
        "claim_scope_warning": "Committed claim projection only; default upstream cap is 2500 nodes. Claims may be omitted; this export does not rebuild or expand it.",
    }
    temporary_path = None
    try:
        html = template_path.read_text(encoding="utf-8")
        html = html.replace("%%MEMORY_BASE_PATH%%", _script_json(memory_dir.resolve().as_uri() + "/"))
        html = html.replace("%%GRAPH_DATA%%", _script_json(graph_data))
        cancellation_checkpoint("graph_before_artifact_write")
        # Trusted single-user host: rechecking is not an OS-level defense
        # against hostile concurrent directory replacement.
        _graph_output_path(str(output_path.parent))
        output_path.parent.mkdir(parents=True, exist_ok=True)
        _graph_output_path(str(output_path.parent))
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=output_path.parent,
                                         prefix=".vector_lake_graph-", suffix=".tmp", delete=False) as handle:
            temporary_path = Path(handle.name)
            handle.write(html)
            handle.flush()
            os.fsync(handle.fileno())
        with non_interruptible_phase("graph_artifact_publish"):
            _graph_output_path(str(output_path.parent))
            os.replace(temporary_path, output_path)
    except (OSError, ValueError) as exc:
        return f"Error: Graph artifact was not published: {exc}"
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)

    try:
        opened = bool(webbrowser.open(output_path.as_uri()))
        browser_status = "Browser opened." if opened else "Browser did not open; open the saved file manually."
    except Exception:
        browser_status = "Browser launch failed; open the saved file manually."
    return (
        f"Saved graph: {output_path}. "
        f"Visualized {len(graph_data['pageGraph']['nodes'])} page nodes / "
        f"{len(graph_data['claimGraph']['nodes'])} claim nodes; "
        f"as-of projection generation: {manifest.get('generation')}. "
        f"{graph_data['exportMetadata']['claim_scope_warning']} "
        "Privacy: local graph data is embedded in this HTML. No external resources load until you choose the CDN button; external scripts can access all embedded graph data. "
        f"{browser_status}"
    )


def audit_graph(*, dry_run: bool = True, confirmation: str = "") -> str:
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
    # Do not approve operations that cannot fit a complete, bounded preview.
    if len(insights) > 50 or sum(
        len(str(insight.get(field, "")))
        for insight in insights for field in ("type", "node", "description")
    ) > 40_000:
        raise ValueError("Audit plan exceeds preview limits; no operations applied")

    from datetime import datetime, timezone

    items = []
    for insight in insights:
        search_queries = [insight.get("node", "")] if insight.get("node") else []
        affected_pages = [f"wiki/{insight.get('node', '')}.md"] if insight.get("node") else []
        identity = json.dumps(
            {
                "type": insight.get("type", ""),
                "node": insight.get("node", ""),
                "description": insight.get("description", ""),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        item_digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        items.append({
            "item_id": f"gov_topology_{item_digest[:12]}",
            "type": "suggestion",
            "title": f"Topology Insight: {insight['type'].replace('_', ' ').title()}",
            "description": insight.get("description", "A topological insight was detected."),
            "search_queries": search_queries,
            "affected_pages": affected_pages,
            "source": "audit-graph",
            "status": "pending",
            "created_at": "<apply-time>",
        })

    if items:
        preview_body = json.dumps(items, ensure_ascii=False, indent=2)
        if len(preview_body) + 200 > 40_000:
            raise ValueError("Audit plan exceeds preview limits; no operations applied")
        plan_fingerprint = hashlib.sha256(
            json.dumps(
                items,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        if dry_run:
            return (
                f"Audit preview: {len(items)} topology insight(s); complete plan follows.\n"
                f"{preview_body}\nconfirmation={plan_fingerprint}"
            )
        if not confirmation or confirmation.casefold() != plan_fingerprint:
            raise ValueError(
                "Audit apply requires the exact confirmation fingerprint from a current preview"
            )
        from vector_lake.runtime_health import enforce_runtime_write_health

        enforce_runtime_write_health(validation_mode="full")
        applied_at = datetime.now(timezone.utc).isoformat()
        for item in items:
            item["created_at"] = applied_at
        from vector_lake import governance_store
        created = sum(
            1
            for item in items
            if governance_store.insert_governance_item_if_absent(item)
        )
        if created:
            return f"Audit complete. Pushed {created} new graph topology insights into the async review queue ({len(items) - created} duplicates skipped)."
        else:
            return f"Audit complete. No new actionable insights found ({len(items)} existing insights already in queue)."
    return "Audit complete. No actionable insights found."
