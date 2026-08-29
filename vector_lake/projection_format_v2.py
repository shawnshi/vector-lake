"""Version-2 content-addressed projection format and commit protocol.

The two historical public filenames are immutable locators.  Every generation
is represented by immutable trie objects and one small, atomically replaced
sidecar.  SQLite records the exact pending/ready sidecar so a crash can never
silently mix FTS state, canonical generations, and filesystem roots.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import shutil
from typing import Any, Callable, Iterable, Mapping

from vector_lake.durability import (
    durable_replace_file,
    sync_directory,
    sync_file,
    sync_open_file,
)
from vector_lake.projection_store_v2 import (
    DEFAULT_READ_OBJECT_LIMIT,
    ProjectionStoreV2,
    canonical_json_bytes,
)
from vector_lake.search_projection_contract import (
    CANONICAL_PROJECTION_SURFACES,
    normalize_runtime_generations,
)


FORMAT_VERSION = 2
LOCATOR_CONTRACT = "vector-lake-projection-locator"
SIDECAR_CONTRACT = "vector-lake-projection-v2"
ROOT_CONTRACT = "vector-lake-projection-root-v2"
SIDECAR_FILENAME = "projection_pair_manifest.json"
INDEX_FILENAME = "index.json"
CLAIM_GRAPH_FILENAME = "claim_graph.json"
MAX_SIDECAR_BYTES = 64 * 1024
MAX_FRONTIER = 512
MAX_CLAIM_GRAPH_NODES = 2_500
# The canonical claim projection bounds each ordinary node to degree 12.  Keep
# a separate edge ceiling so a valid 2,500-node graph is not rejected merely
# because it contains more than 2,500 edges, while forced contradiction edges
# still cannot make a rebuild unbounded.
MAX_CLAIM_GRAPH_EDGES = MAX_CLAIM_GRAPH_NODES * 12

_ROOT_METADATA_FIELDS = frozenset({"contract", "format_version", "projection"})
_INDEX_ROOT_COMPONENTS = frozenset(
    {
        "aliases",
        "aliases_by_node",
        "categories",
        "category_counts",
        "edge_candidate_incidence",
        "edge_candidates",
        "edge_incidence",
        "edges",
        "error_log",
        "errors_by_file",
        "meta",
        "nodes",
        "reverse_links",
        "reverse_sources",
        "search_rows",
    }
)
_CLAIM_GRAPH_ROOT_COMPONENTS = frozenset({"edges", "meta", "nodes"})
_ROOT_DESCRIPTOR_FIELD_VARIANTS = {
    # errors_by_file was added without changing physical format_version=2;
    # retain the one exact legacy shape that materialize_index can still read.
    "index": (
        _ROOT_METADATA_FIELDS | _INDEX_ROOT_COMPONENTS,
        _ROOT_METADATA_FIELDS | (_INDEX_ROOT_COMPONENTS - {"errors_by_file"}),
    ),
    "claim_graph": (
        _ROOT_METADATA_FIELDS | _CLAIM_GRAPH_ROOT_COMPONENTS,
    ),
}
MAX_COMPONENT_ITEMS = 1_000_000
MAX_CLOSURE_OBJECTS = DEFAULT_READ_OBJECT_LIMIT * 16
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


class ProjectionV2ContractError(RuntimeError):
    """The v2 locator, sidecar, runtime state, or object graph is invalid."""


class ProjectionHeavyRebuildRequired(ProjectionV2ContractError):
    """A bounded incremental update cannot safely cover its dependency graph."""

    def __init__(self) -> None:
        super().__init__("projection_heavy_rebuild_required")


@dataclass(frozen=True, slots=True)
class PreparedProjectionV2:
    index_root_sha256: str
    claim_graph_root_sha256: str
    projection_generation: str
    canonical_generation: dict[str, int]
    sidecar: dict[str, Any]
    sidecar_json: str
    object_new_bytes: int
    object_new_count: int
    object_reused_bytes: int
    object_reused_count: int


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ProjectionV2ContractError(f"canonical_json_invalid:{exc}") from exc


def _require_digest(value: object, label: str) -> str:
    digest = str(value or "")
    if _DIGEST_RE.fullmatch(digest) is None:
        raise ProjectionV2ContractError(f"{label}_invalid")
    return digest


def _normalized_generations(value: object) -> dict[str, int]:
    generations = normalize_runtime_generations(value)
    if generations is None:
        raise ProjectionV2ContractError("canonical_generation_invalid")
    return generations


def locator_payload(projection: str) -> dict[str, Any]:
    if projection not in {"index", "claim_graph"}:
        raise ProjectionV2ContractError("locator_projection")
    return {
        "contract": LOCATOR_CONTRACT,
        "format_version": FORMAT_VERSION,
        "projection": projection,
        "sidecar": SIDECAR_FILENAME,
    }


def locator_bytes(projection: str) -> bytes:
    return canonical_json_bytes(locator_payload(projection))


def _write_durable_replace(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    )
    try:
        with temporary.open("xb") as handle:
            handle.write(payload)
            sync_open_file(handle)
        durable_replace_file(temporary, path, source_synced=True)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def validate_locator(path: str | os.PathLike[str], projection: str) -> dict[str, Any]:
    target = Path(path)
    try:
        payload = target.read_bytes()
        observed = json.loads(payload.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ProjectionV2ContractError(f"locator_unreadable:{target.name}") from exc
    expected = locator_payload(projection)
    if observed != expected or payload != canonical_json_bytes(expected):
        if isinstance(observed, dict) and observed.get("projection") != projection:
            raise ProjectionV2ContractError("locator_projection")
        raise ProjectionV2ContractError(f"locator_contract:{target.name}")
    return observed


def ensure_static_locators(base_dir: str | os.PathLike[str]) -> None:
    base = Path(base_dir)
    for filename, projection in (
        (INDEX_FILENAME, "index"),
        (CLAIM_GRAPH_FILENAME, "claim_graph"),
    ):
        path = base / filename
        expected = locator_bytes(projection)
        try:
            if path.read_bytes() == expected:
                continue
        except FileNotFoundError:
            pass
        if path.is_symlink():
            raise ProjectionV2ContractError(f"locator_symlink:{filename}")
        _write_durable_replace(path, expected)


def is_v2_locator(path: str | os.PathLike[str], projection: str | None = None) -> bool:
    target = Path(path)
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    if not isinstance(value, dict):
        return False
    if (
        value.get("contract") != LOCATOR_CONTRACT
        or value.get("format_version") != FORMAT_VERSION
        or value.get("sidecar") != SIDECAR_FILENAME
    ):
        return False
    return projection is None or value.get("projection") == projection


def require_bounded_frontier(values: Iterable[Any]) -> tuple[Any, ...]:
    result: list[Any] = []
    for value in values:
        if len(result) >= MAX_FRONTIER:
            raise ProjectionHeavyRebuildRequired()
        result.append(value)
    return tuple(result)


def _edge_key(edge: Mapping[str, Any], ordinal: int = 0) -> str:
    source = str(edge.get("source") or "")
    target = str(edge.get("target") or "")
    pair = "\x1f".join((min(source, target), max(source, target)))
    signature = hashlib.sha256(canonical_json_bytes(dict(edge))).hexdigest()
    return f"{pair}\x1f{ordinal:08d}\x1f{signature}"


def _claim_item_key(item: Mapping[str, Any], ordinal: int) -> str:
    identity = str(item.get("id") or item.get("claim_id") or "")
    digest = hashlib.sha256(canonical_json_bytes(dict(item))).hexdigest()
    return f"{identity}\x1f{ordinal:08d}\x1f{digest}"


def _error_key(item: Mapping[str, Any], ordinal: int) -> str:
    filename = str(item.get("file") or "")
    digest = hashlib.sha256(canonical_json_bytes(dict(item))).hexdigest()
    return f"{filename}\x1f{ordinal:08d}\x1f{digest}"


def _search_row(node_key: str, node: Mapping[str, Any]) -> list[str]:
    text = " ".join(
        str(value or "")
        for value in (
            node.get("title"),
            node.get("summary"),
            node.get("raw_text"),
        )
    )
    return [
        str(node_key),
        str(node.get("title") or ""),
        str(node.get("summary") or ""),
        text,
    ]


def _reverse_indexes(
    nodes: Mapping[str, Mapping[str, Any]],
    aliases: Mapping[str, str],
) -> tuple[dict[str, list[str]], dict[str, list[str]], dict[str, int]]:
    links: dict[str, list[str]] = {}
    sources: dict[str, list[str]] = {}
    categories: dict[str, int] = {}
    for node_key in sorted(nodes):
        node = nodes[node_key]
        for link in sorted(
            {
                str(aliases.get(str(item), str(item)))
                for item in (node.get("links") or [])
            }
        ):
            links.setdefault(link, []).append(node_key)
        for source in sorted({str(item) for item in (node.get("sources") or [])}):
            sources.setdefault(source, []).append(node_key)
        for category in {str(item) for item in (node.get("categories") or [])}:
            categories[category] = categories.get(category, 0) + 1
    return links, sources, categories


def _edge_components(
    edges: Iterable[Mapping[str, Any]],
) -> tuple[dict[str, dict[str, Any]], dict[str, list[str]]]:
    values: dict[str, dict[str, Any]] = {}
    incidence: dict[str, list[str]] = {}
    for ordinal, edge in enumerate(edges):
        normalized = dict(edge)
        normalized["__ordinal__"] = ordinal
        key = _edge_key(edge, ordinal)
        values[key] = normalized
        for endpoint in {str(edge.get("source") or ""), str(edge.get("target") or "")}:
            if endpoint:
                incidence.setdefault(endpoint, []).append(key)
    return values, incidence


def _apply_component(
    store: ProjectionStoreV2,
    values: Mapping[str, Any],
) -> tuple[str, tuple[int, int, int, int]]:
    result = store.apply(None, sets=values)
    return result.root_digest, (
        result.new_bytes,
        result.new_objects,
        result.reused_bytes,
        result.reused_objects,
    )


def _add_stats(
    total: list[int],
    stats: tuple[int, int, int, int],
) -> None:
    for index, value in enumerate(stats):
        total[index] += value


def build_claim_graph_root(
    base_dir: str | os.PathLike[str],
    claim_graph_data: Mapping[str, Any],
) -> tuple[str, tuple[int, int, int, int]]:
    """Build the bounded claim-graph descriptor without touching index roots."""
    claim_nodes_raw = list(claim_graph_data.get("nodes") or [])
    claim_edges_raw = list(claim_graph_data.get("edges") or [])
    if (
        len(claim_nodes_raw) > MAX_CLAIM_GRAPH_NODES
        or len(claim_edges_raw) > MAX_CLAIM_GRAPH_EDGES
    ):
        raise ProjectionHeavyRebuildRequired()
    store = ProjectionStoreV2(base_dir)
    claim_nodes = {
        _claim_item_key(item, ordinal): dict(item)
        for ordinal, item in enumerate(claim_nodes_raw)
    }
    claim_edges = {
        _claim_item_key(item, ordinal): dict(item)
        for ordinal, item in enumerate(claim_edges_raw)
    }
    claim_meta = {
        str(key): value
        for key, value in claim_graph_data.items()
        if key not in {"nodes", "edges", "projection_manifest"}
    }
    descriptor: dict[str, Any] = {
        "contract": ROOT_CONTRACT,
        "format_version": FORMAT_VERSION,
        "projection": "claim_graph",
    }
    stats = [0, 0, 0, 0]
    for name, values in (
        ("nodes", claim_nodes),
        ("edges", claim_edges),
        ("meta", claim_meta),
    ):
        root, component_stats = _apply_component(store, values)
        descriptor[name] = root
        _add_stats(stats, component_stats)
    result = store.apply(None, sets=descriptor)
    _add_stats(
        stats,
        (
            result.new_bytes,
            result.new_objects,
            result.reused_bytes,
            result.reused_objects,
        ),
    )
    return result.root_digest, tuple(stats)


def build_projection_roots(
    base_dir: str | os.PathLike[str],
    index_data: Mapping[str, Any],
    claim_graph_data: Mapping[str, Any],
    *,
    canonical_generation: Mapping[str, int],
    published_at_utc: str | None = None,
) -> PreparedProjectionV2:
    """Durably build all immutable objects without publishing a pointer."""
    generations = _normalized_generations(dict(canonical_generation))
    base = Path(base_dir)
    store = ProjectionStoreV2(base)
    nodes = {
        str(key): dict(value)
        for key, value in (index_data.get("nodes") or {}).items()
    }
    aliases = {
        str(key): str(value)
        for key, value in (index_data.get("aliases") or {}).items()
    }
    aliases_by_node: dict[str, list[str]] = {}
    for alias, node_key in aliases.items():
        aliases_by_node.setdefault(node_key, []).append(alias)
    aliases_by_node = {
        node_key: sorted(values)
        for node_key, values in aliases_by_node.items()
    }
    edges, incidence = _edge_components(index_data.get("weighted_edges") or [])
    edge_candidates, candidate_incidence = _edge_components(
        index_data.get("_projection_edge_candidates")
        or index_data.get("weighted_edges")
        or []
    )
    reverse_links, reverse_sources, category_counts = _reverse_indexes(
        nodes,
        aliases,
    )
    categories = {
        str(value): True for value in (index_data.get("categories") or [])
    }
    errors = {
        _error_key(item, ordinal): dict(item)
        for ordinal, item in enumerate(index_data.get("error_log") or [])
    }
    errors_by_file: dict[str, list[dict[str, Any]]] = {}
    for item in index_data.get("error_log") or []:
        errors_by_file.setdefault(str(item.get("file") or ""), []).append(
            dict(item)
        )
    search_rows = {key: _search_row(key, node) for key, node in nodes.items()}
    handled = {
        "nodes",
        "aliases",
        "weighted_edges",
        "categories",
        "error_log",
        "projection_manifest",
        "_projection_edge_candidates",
    }
    meta = {
        str(key): value for key, value in index_data.items() if key not in handled
    }
    component_values: dict[str, Mapping[str, Any]] = {
        "nodes": nodes,
        "aliases": aliases,
        "aliases_by_node": aliases_by_node,
        "edges": edges,
        "categories": categories,
        "error_log": errors,
        "errors_by_file": errors_by_file,
        "search_rows": search_rows,
        "reverse_links": reverse_links,
        "reverse_sources": reverse_sources,
        "category_counts": category_counts,
        # The complete candidate set is intentionally retained independently
        # from final incidence. Full builders may later supply a wider bounded
        # frontier without changing the descriptor contract.
        "edge_candidates": edge_candidates,
        "edge_incidence": incidence,
        "edge_candidate_incidence": candidate_incidence,
        "meta": meta,
    }
    stats = [0, 0, 0, 0]
    index_descriptor: dict[str, Any] = {
        "contract": ROOT_CONTRACT,
        "format_version": FORMAT_VERSION,
        "projection": "index",
    }
    for name in sorted(component_values):
        root, component_stats = _apply_component(store, component_values[name])
        index_descriptor[name] = root
        _add_stats(stats, component_stats)
    index_root_result = store.apply(None, sets=index_descriptor)
    _add_stats(
        stats,
        (
            index_root_result.new_bytes,
            index_root_result.new_objects,
            index_root_result.reused_bytes,
            index_root_result.reused_objects,
        ),
    )

    claim_root_digest, claim_stats = build_claim_graph_root(
        base,
        claim_graph_data,
    )
    _add_stats(stats, claim_stats)

    generation_payload = {
        "canonical_generation": generations,
        "claim_graph_root_sha256": claim_root_digest,
        "index_root_sha256": index_root_result.root_digest,
    }
    generation = hashlib.sha256(canonical_json_bytes(generation_payload)).hexdigest()
    sidecar = {
        "canonical_generation": generations,
        "claim_graph_root_sha256": claim_root_digest,
        "contract": SIDECAR_CONTRACT,
        "counts": {
            "claim_edges": len(claim_graph_data.get("edges") or []),
            "claim_nodes": len(claim_graph_data.get("nodes") or []),
            "index_edges": len(edges),
            "index_nodes": len(nodes),
        },
        "format_version": FORMAT_VERSION,
        "index_root_sha256": index_root_result.root_digest,
        "projection_generation": generation,
        "published_at_utc": published_at_utc or datetime.now(timezone.utc).isoformat(),
    }
    sidecar_json = _canonical_json(sidecar)
    if len(sidecar_json.encode("utf-8")) > MAX_SIDECAR_BYTES:
        raise ProjectionV2ContractError("sidecar_too_large")
    return PreparedProjectionV2(
        index_root_sha256=index_root_result.root_digest,
        claim_graph_root_sha256=claim_root_digest,
        projection_generation=generation,
        canonical_generation=generations,
        sidecar=sidecar,
        sidecar_json=sidecar_json,
        object_new_bytes=stats[0],
        object_new_count=stats[1],
        object_reused_bytes=stats[2],
        object_reused_count=stats[3],
    )


def validate_sidecar(value: object) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ProjectionV2ContractError("sidecar_json_invalid") from exc
    else:
        decoded = value
    if not isinstance(decoded, dict):
        raise ProjectionV2ContractError("sidecar_object_required")
    if decoded.get("contract") != SIDECAR_CONTRACT:
        raise ProjectionV2ContractError("sidecar_contract")
    if decoded.get("format_version") != FORMAT_VERSION:
        raise ProjectionV2ContractError("sidecar_format_version")
    index_root = _require_digest(decoded.get("index_root_sha256"), "index_root")
    claim_root = _require_digest(
        decoded.get("claim_graph_root_sha256"), "claim_graph_root"
    )
    generation = _require_digest(
        decoded.get("projection_generation"), "projection_generation"
    )
    generations = _normalized_generations(decoded.get("canonical_generation"))
    published = decoded.get("published_at_utc")
    if not isinstance(published, str) or not published or len(published) > 64:
        raise ProjectionV2ContractError("published_at_utc_invalid")
    expected_generation = hashlib.sha256(
        canonical_json_bytes(
            {
                "canonical_generation": generations,
                "claim_graph_root_sha256": claim_root,
                "index_root_sha256": index_root,
            }
        )
    ).hexdigest()
    if not hmac.compare_digest(expected_generation, generation):
        raise ProjectionV2ContractError("projection_generation_mismatch")
    canonical = _canonical_json(decoded)
    if len(canonical.encode("utf-8")) > MAX_SIDECAR_BYTES:
        raise ProjectionV2ContractError("sidecar_too_large")
    return decoded


def prepare_projection_from_roots(
    base_dir: str | os.PathLike[str],
    *,
    index_root_sha256: str,
    claim_graph_root_sha256: str,
    canonical_generation: Mapping[str, int],
    counts: Mapping[str, int] | None = None,
    published_at_utc: str | None = None,
    mutation_stats: tuple[int, int, int, int] = (0, 0, 0, 0),
) -> PreparedProjectionV2:
    """Prepare a sidecar for already-durable descriptor roots."""
    generations = _normalized_generations(dict(canonical_generation))
    index_root = _require_digest(index_root_sha256, "index_root")
    claim_root = _require_digest(claim_graph_root_sha256, "claim_graph_root")
    store = ProjectionStoreV2(base_dir)
    _root_descriptor(store, index_root, "index")
    _root_descriptor(store, claim_root, "claim_graph")
    generation_payload = {
        "canonical_generation": generations,
        "claim_graph_root_sha256": claim_root,
        "index_root_sha256": index_root,
    }
    generation = hashlib.sha256(canonical_json_bytes(generation_payload)).hexdigest()
    normalized_counts: dict[str, int] = {}
    for key, value in (counts or {}).items():
        if isinstance(value, bool) or int(value) < 0:
            raise ProjectionV2ContractError("sidecar_count_invalid")
        normalized_counts[str(key)] = int(value)
    sidecar = {
        "canonical_generation": generations,
        "claim_graph_root_sha256": claim_root,
        "contract": SIDECAR_CONTRACT,
        "counts": normalized_counts,
        "format_version": FORMAT_VERSION,
        "index_root_sha256": index_root,
        "projection_generation": generation,
        "published_at_utc": published_at_utc or datetime.now(timezone.utc).isoformat(),
    }
    sidecar_json = _canonical_json(sidecar)
    if len(sidecar_json.encode("utf-8")) > MAX_SIDECAR_BYTES:
        raise ProjectionV2ContractError("sidecar_too_large")
    return PreparedProjectionV2(
        index_root_sha256=index_root,
        claim_graph_root_sha256=claim_root,
        projection_generation=generation,
        canonical_generation=generations,
        sidecar=sidecar,
        sidecar_json=sidecar_json,
        object_new_bytes=int(mutation_stats[0]),
        object_new_count=int(mutation_stats[1]),
        object_reused_bytes=int(mutation_stats[2]),
        object_reused_count=int(mutation_stats[3]),
    )


def _root_descriptor(
    store: ProjectionStoreV2,
    digest: str,
    projection: str,
) -> dict[str, Any]:
    _require_digest(digest, f"{projection}_root")
    allowed_fields = _ROOT_DESCRIPTOR_FIELD_VARIANTS.get(projection)
    if allowed_fields is None:
        raise ProjectionV2ContractError("projection_root_kind")
    max_fields = max(len(fields) for fields in allowed_fields)
    items = store.iter_items(
        digest,
        # One item beyond the exact contract makes surplus fields observable
        # without materializing an attacker-controlled descriptor.
        limit=max_fields + 1,
        max_objects=MAX_CLOSURE_OBJECTS,
    )
    descriptor = dict(items)
    if not any(set(descriptor) == fields for fields in allowed_fields):
        raise ProjectionV2ContractError(f"{projection}_root_fields")
    if (
        descriptor.get("contract") != ROOT_CONTRACT
        or descriptor.get("format_version") != FORMAT_VERSION
        or descriptor.get("projection") != projection
    ):
        raise ProjectionV2ContractError(f"{projection}_root_contract")
    return descriptor


def _component_items(
    store: ProjectionStoreV2,
    descriptor: Mapping[str, Any],
    name: str,
) -> list[tuple[str, Any]]:
    root = _require_digest(descriptor.get(name), f"component_{name}")
    return list(
        store.iter_items(
            root,
            limit=MAX_COMPONENT_ITEMS,
            max_objects=MAX_CLOSURE_OBJECTS,
        )
    )


def _legacy_projection_manifest(sidecar: Mapping[str, Any]) -> dict[str, Any]:
    generations = _normalized_generations(sidecar.get("canonical_generation"))
    token = hashlib.sha256(
        _canonical_json(generations).encode("utf-8")
    ).hexdigest()
    return {
        "contract": "index-claim-graph-pair",
        "version": 1,
        "generation": sidecar["projection_generation"],
        "published_at": sidecar["published_at_utc"],
        "canonical_generation": {
            "status": "verified",
            "algorithm": "runtime-generations-sha256-v2",
            "token": token,
            "runtime_generations": generations,
        },
    }


def materialize_index(
    base_dir: str | os.PathLike[str],
    sidecar: Mapping[str, Any],
) -> dict[str, Any]:
    validated = validate_sidecar(dict(sidecar))
    store = ProjectionStoreV2(base_dir)
    descriptor = _root_descriptor(
        store, validated["index_root_sha256"], "index"
    )
    result = dict(_component_items(store, descriptor, "meta"))
    result["nodes"] = dict(_component_items(store, descriptor, "nodes"))
    result["aliases"] = dict(_component_items(store, descriptor, "aliases"))
    result["categories"] = sorted(
        key for key, _value in _component_items(store, descriptor, "categories")
    )
    weighted_edges: list[tuple[int, dict[str, Any]]] = []
    for _key, raw_edge in _component_items(store, descriptor, "edges"):
        edge = dict(raw_edge)
        ordinal = int(edge.pop("__ordinal__", len(weighted_edges)))
        weighted_edges.append((ordinal, edge))
    result["weighted_edges"] = [
        edge for _ordinal, edge in sorted(weighted_edges, key=lambda item: item[0])
    ]
    if "errors_by_file" in descriptor:
        result["error_log"] = [
            dict(item)
            for _filename, items in sorted(
                _component_items(store, descriptor, "errors_by_file"),
                key=lambda pair: pair[0],
            )
            for item in items
        ]
    else:
        errors: list[tuple[int, dict[str, Any]]] = []
        for key, raw_error in _component_items(store, descriptor, "error_log"):
            try:
                ordinal = int(key.rsplit("\x1f", 2)[-2])
            except (IndexError, ValueError):
                ordinal = len(errors)
            errors.append((ordinal, dict(raw_error)))
        result["error_log"] = [
            item for _ordinal, item in sorted(errors, key=lambda pair: pair[0])
        ]
    result["projection_manifest"] = _legacy_projection_manifest(validated)
    return result


def materialize_claim_graph(
    base_dir: str | os.PathLike[str],
    sidecar: Mapping[str, Any],
) -> dict[str, Any]:
    validated = validate_sidecar(dict(sidecar))
    store = ProjectionStoreV2(base_dir)
    descriptor = _root_descriptor(
        store, validated["claim_graph_root_sha256"], "claim_graph"
    )
    result = dict(_component_items(store, descriptor, "meta"))
    for name in ("nodes", "edges"):
        ordered: list[tuple[int, dict[str, Any]]] = []
        for key, value in _component_items(store, descriptor, name):
            try:
                ordinal = int(key.rsplit("\x1f", 2)[-2])
            except (IndexError, ValueError):
                ordinal = len(ordered)
            ordered.append((ordinal, dict(value)))
        result[name] = [
            item for _ordinal, item in sorted(ordered, key=lambda pair: pair[0])
        ]
    result["projection_manifest"] = _legacy_projection_manifest(validated)
    return result


def _path_identity(path: Path) -> tuple[int, int, int, int, int]:
    stat = path.stat()
    return (
        int(stat.st_dev),
        int(stat.st_ino),
        int(stat.st_size),
        int(stat.st_mtime_ns),
        int(stat.st_ctime_ns),
    )


def sidecar_identity(base_dir: str | os.PathLike[str]) -> tuple[int, int, int, int, int]:
    return _path_identity(Path(base_dir) / SIDECAR_FILENAME)


def _runtime_current_generations(connection: Any) -> dict[str, int]:
    placeholders = ",".join("?" for _ in CANONICAL_PROJECTION_SURFACES)
    rows = connection.execute(
        "SELECT surface, generation FROM runtime_generations "
        f"WHERE surface IN ({placeholders})",
        CANONICAL_PROJECTION_SURFACES,
    ).fetchall()
    observed = {str(row[0]): int(row[1]) for row in rows}
    return _normalized_generations(observed)


def read_committed_sidecar(
    base_dir: str | os.PathLike[str],
    *,
    connection: Any | None = None,
    require_current_generation: bool = True,
) -> tuple[dict[str, Any], tuple[int, int, int, int, int], dict[str, Any]]:
    from vector_lake import db_store

    base = Path(base_dir)
    validate_locator(base / INDEX_FILENAME, "index")
    validate_locator(base / CLAIM_GRAPH_FILENAME, "claim_graph")
    conn = connection or db_store.get_connection()
    runtime_before = db_store.get_projection_runtime_v9(conn)
    if runtime_before.get("status") != "ready":
        raise ProjectionV2ContractError(
            f"projection_runtime_not_ready:{runtime_before.get('status')}"
        )
    sidecar_path = base / SIDECAR_FILENAME
    try:
        identity = _path_identity(sidecar_path)
        payload = sidecar_path.read_bytes()
    except OSError as exc:
        raise ProjectionV2ContractError("sidecar_unreadable") from exc
    if len(payload) > MAX_SIDECAR_BYTES:
        raise ProjectionV2ContractError("sidecar_too_large")
    try:
        text = payload.decode("utf-8")
        sidecar = validate_sidecar(text)
    except UnicodeError as exc:
        raise ProjectionV2ContractError("sidecar_utf8_invalid") from exc
    if payload != _canonical_json(sidecar).encode("utf-8"):
        raise ProjectionV2ContractError("sidecar_not_canonical")
    digest = hashlib.sha256(payload).hexdigest()
    if not hmac.compare_digest(digest, str(runtime_before.get("sidecar_sha256") or "")):
        raise ProjectionV2ContractError("sidecar_runtime_digest_mismatch")
    if sidecar != runtime_before.get("sidecar"):
        raise ProjectionV2ContractError("sidecar_runtime_payload_mismatch")
    if sidecar["projection_generation"] != runtime_before.get(
        "projection_generation"
    ):
        raise ProjectionV2ContractError("sidecar_runtime_generation_mismatch")
    if sidecar["canonical_generation"] != runtime_before.get(
        "canonical_generation"
    ):
        raise ProjectionV2ContractError("sidecar_runtime_canonical_mismatch")
    if require_current_generation and (
        sidecar["canonical_generation"] != _runtime_current_generations(conn)
    ):
        raise ProjectionV2ContractError("canonical_generation_stale")
    if _path_identity(sidecar_path) != identity:
        raise ProjectionV2ContractError("sidecar_changed_during_read")
    return sidecar, identity, runtime_before


def load_committed_index(
    base_dir: str | os.PathLike[str],
    *,
    connection: Any | None = None,
    require_current_generation: bool = True,
) -> dict[str, Any]:
    from vector_lake import db_store

    conn = connection or db_store.get_connection()
    sidecar, identity, runtime_before = read_committed_sidecar(
        base_dir,
        connection=conn,
        require_current_generation=require_current_generation,
    )
    result = materialize_index(base_dir, sidecar)
    sidecar_path = Path(base_dir) / SIDECAR_FILENAME
    runtime_after = db_store.get_projection_runtime_v9(conn)
    if _path_identity(sidecar_path) != identity or runtime_after != runtime_before:
        raise ProjectionV2ContractError("projection_changed_during_materialization")
    return result


def load_committed_claim_graph(
    base_dir: str | os.PathLike[str],
    *,
    connection: Any | None = None,
    require_current_generation: bool = True,
) -> dict[str, Any]:
    from vector_lake import db_store

    conn = connection or db_store.get_connection()
    sidecar, identity, runtime_before = read_committed_sidecar(
        base_dir,
        connection=conn,
        require_current_generation=require_current_generation,
    )
    result = materialize_claim_graph(base_dir, sidecar)
    sidecar_path = Path(base_dir) / SIDECAR_FILENAME
    runtime_after = db_store.get_projection_runtime_v9(conn)
    if _path_identity(sidecar_path) != identity or runtime_after != runtime_before:
        raise ProjectionV2ContractError("projection_changed_during_materialization")
    return result


def load_committed_pair(
    base_dir: str | os.PathLike[str],
    *,
    connection: Any | None = None,
    require_current_generation: bool = True,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Materialize index and claim graph from one verified commit marker."""
    from vector_lake import db_store

    conn = connection or db_store.get_connection()
    sidecar, identity, runtime_before = read_committed_sidecar(
        base_dir,
        connection=conn,
        require_current_generation=require_current_generation,
    )
    index = materialize_index(base_dir, sidecar)
    claim_graph = materialize_claim_graph(base_dir, sidecar)
    sidecar_path = Path(base_dir) / SIDECAR_FILENAME
    try:
        marker_unchanged = _path_identity(sidecar_path) == identity
    except OSError as exc:
        raise ProjectionV2ContractError(
            "projection_changed_during_materialization"
        ) from exc
    runtime_after = db_store.get_projection_runtime_v9(conn)
    if not marker_unchanged or runtime_after != runtime_before:
        raise ProjectionV2ContractError("projection_changed_during_materialization")
    return index, claim_graph


def validate_root_closure(
    base_dir: str | os.PathLike[str],
    sidecar: Mapping[str, Any],
) -> tuple[Path, ...]:
    validated = validate_sidecar(dict(sidecar))
    store = ProjectionStoreV2(base_dir)
    paths: list[Path] = []
    for projection, field in (
        ("index", "index_root_sha256"),
        ("claim_graph", "claim_graph_root_sha256"),
    ):
        root = validated[field]
        descriptor = _root_descriptor(store, root, projection)
        paths.extend(store.object_paths(root, max_objects=MAX_CLOSURE_OBJECTS))
        for name in sorted(set(descriptor) - _ROOT_METADATA_FIELDS):
            component_root = descriptor[name]
            _require_digest(component_root, f"component_{name}")
            paths.extend(
                store.object_paths(
                    component_root,
                    max_objects=MAX_CLOSURE_OBJECTS,
                )
            )
    return tuple(dict.fromkeys(paths))


def recover_pending_publish(
    base_dir: str | os.PathLike[str],
    *,
    connection: Any | None = None,
) -> bool:
    """Finish an exact durable pending intent; never reconstruct intent."""
    from vector_lake import db_store

    conn = connection or db_store.get_connection()
    runtime = db_store.get_projection_runtime_v9(conn)
    if runtime.get("status") != "publish_pending":
        return False
    sidecar = validate_sidecar(runtime.get("sidecar"))
    canonical = _canonical_json(sidecar)
    expected_digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    if not hmac.compare_digest(
        expected_digest, str(runtime.get("sidecar_sha256") or "")
    ):
        raise ProjectionV2ContractError("pending_sidecar_digest_mismatch")
    if sidecar["canonical_generation"] != _runtime_current_generations(conn):
        raise ProjectionV2ContractError("pending_canonical_generation_stale")
    validate_root_closure(base_dir, sidecar)
    ensure_static_locators(base_dir)
    marker = Path(base_dir) / SIDECAR_FILENAME
    marker_matches = False
    try:
        marker_matches = hmac.compare_digest(
            hashlib.sha256(marker.read_bytes()).hexdigest(), expected_digest
        )
    except FileNotFoundError:
        pass
    if not marker_matches:
        _write_durable_replace(marker, canonical.encode("utf-8"))
    with db_store.transaction() as transaction_connection:
        db_store.mark_projection_runtime_ready(
            transaction_connection,
            expected_projection_generation=sidecar["projection_generation"],
            expected_sidecar_sha256=expected_digest,
        )
    return True


def publish_prepared_projection(
    base_dir: str | os.PathLike[str],
    prepared: PreparedProjectionV2,
    *,
    transaction_mutation: Callable[[Any, PreparedProjectionV2], Any] | None = None,
    noop_transaction_mutation: Callable[
        [Any, PreparedProjectionV2], Any
    ] | None = None,
    assert_canonical: Callable[[], None] | None = None,
) -> Any:
    """Publish objects -> DB pending -> sidecar -> DB ready in that order."""
    from vector_lake import db_store

    recover_pending_publish(base_dir)
    conn = db_store.get_connection()
    state = db_store.get_projection_runtime_v9(conn)
    if (
        state.get("status") == "ready"
        and state.get("projection_generation")
        == prepared.projection_generation
    ):
        recorded = state.get("sidecar") or {}
        if (
            recorded.get("index_root_sha256")
            != prepared.index_root_sha256
            or recorded.get("claim_graph_root_sha256")
            != prepared.claim_graph_root_sha256
            or recorded.get("canonical_generation")
            != prepared.canonical_generation
        ):
            raise ProjectionV2ContractError(
                "projection_generation_collision"
            )
        try:
            committed, _identity, _runtime = read_committed_sidecar(
                base_dir,
                connection=conn,
            )
        except ProjectionV2ContractError:
            # A ready DB row is the durable commit authority. Repair only its
            # exact marker after proving the referenced immutable closure; do
            # not reconstruct or infer publication state from loose objects.
            validate_root_closure(base_dir, recorded)
            recorded_json = str(state.get("sidecar_json") or "")
            if recorded_json != _canonical_json(recorded):
                raise ProjectionV2ContractError(
                    "ready_sidecar_runtime_payload_invalid"
                )
            if assert_canonical is not None:
                assert_canonical()
            with db_store.transaction() as transaction_connection:
                if assert_canonical is not None:
                    assert_canonical()
                current = db_store.get_projection_runtime_v9(
                    transaction_connection
                )
                if (
                    current.get("status") != "ready"
                    or current.get("projection_generation")
                    != prepared.projection_generation
                    or current.get("sidecar_sha256")
                    != state.get("sidecar_sha256")
                ):
                    raise ProjectionV2ContractError(
                        "projection_runtime_changed_before_marker_repair"
                    )
                db_store.cas_projection_runtime_publish_pending(
                    transaction_connection,
                    expected_status="ready",
                    expected_projection_generation=(
                        prepared.projection_generation
                    ),
                    projection_generation=prepared.projection_generation,
                    canonical_generation=prepared.canonical_generation,
                    sidecar_json=recorded_json,
                )
            ensure_static_locators(base_dir)
            _write_durable_replace(
                Path(base_dir) / SIDECAR_FILENAME,
                recorded_json.encode("utf-8"),
            )
            with db_store.transaction() as transaction_connection:
                db_store.mark_projection_runtime_ready(
                    transaction_connection,
                    expected_projection_generation=(
                        prepared.projection_generation
                    ),
                    expected_sidecar_sha256=str(state["sidecar_sha256"]),
                )
            committed, _identity, _runtime = read_committed_sidecar(
                base_dir,
                connection=conn,
            )
        if (
            committed.get("index_root_sha256")
            != prepared.index_root_sha256
            or committed.get("claim_graph_root_sha256")
            != prepared.claim_graph_root_sha256
            or committed.get("canonical_generation")
            != prepared.canonical_generation
        ):
            raise ProjectionV2ContractError(
                "projection_generation_collision"
            )
        transaction_result = None
        if noop_transaction_mutation is not None:
            if assert_canonical is not None:
                assert_canonical()
            with db_store.transaction() as transaction_connection:
                if assert_canonical is not None:
                    assert_canonical()
                current = db_store.get_projection_runtime_v9(
                    transaction_connection
                )
                if (
                    current.get("status") != "ready"
                    or current.get("projection_generation")
                    != prepared.projection_generation
                    or current.get("sidecar_sha256")
                    != state.get("sidecar_sha256")
                ):
                    raise ProjectionV2ContractError(
                        "projection_runtime_changed_before_noop_mutation"
                    )
                transaction_result = noop_transaction_mutation(
                    transaction_connection, prepared
                )
        # published_at_utc is publication metadata, not logical content. An
        # identical root/canonical tuple reuses the ready marker byte-for-byte.
        return transaction_result
    if assert_canonical is not None:
        assert_canonical()
    transaction_result = None
    with db_store.transaction() as transaction_connection:
        if assert_canonical is not None:
            assert_canonical()
        if transaction_mutation is not None:
            transaction_result = transaction_mutation(
                transaction_connection, prepared
            )
        db_store.cas_projection_runtime_publish_pending(
            transaction_connection,
            expected_status=str(state["status"]),
            expected_projection_generation=state.get("projection_generation"),
            projection_generation=prepared.projection_generation,
            canonical_generation=prepared.canonical_generation,
            sidecar_json=prepared.sidecar_json,
        )
    ensure_static_locators(base_dir)
    marker = Path(base_dir) / SIDECAR_FILENAME
    _write_durable_replace(marker, prepared.sidecar_json.encode("utf-8"))
    expected_digest = hashlib.sha256(
        prepared.sidecar_json.encode("utf-8")
    ).hexdigest()
    with db_store.transaction() as transaction_connection:
        db_store.mark_projection_runtime_ready(
            transaction_connection,
            expected_projection_generation=prepared.projection_generation,
            expected_sidecar_sha256=expected_digest,
        )
    return transaction_result


def load_component_roots(
    base_dir: str | os.PathLike[str],
    sidecar: Mapping[str, Any],
    projection: str = "index",
) -> dict[str, Any]:
    validated = validate_sidecar(dict(sidecar))
    field = "index_root_sha256" if projection == "index" else "claim_graph_root_sha256"
    return _root_descriptor(
        ProjectionStoreV2(base_dir), validated[field], projection
    )


def _artifact_identity(path: Path) -> dict[str, Any]:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return {"exists": False}
    return {
        "exists": True,
        "device": int(stat.st_dev),
        "inode": int(stat.st_ino),
        "size": int(stat.st_size),
        "mtime_ns": int(stat.st_mtime_ns),
        "ctime_ns": int(stat.st_ctime_ns),
    }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _artifact_record(path: Path, base: Path) -> dict[str, Any]:
    resolved = path.resolve()
    try:
        relative = resolved.relative_to(base.resolve()).as_posix()
    except ValueError as exc:
        raise RuntimeError("projection_v2_artifact_outside_root") from exc
    identity = _artifact_identity(resolved)
    record: dict[str, Any] = {
        "name": resolved.name,
        "relative_path": relative,
        "source_path": str(resolved),
        "source_identity": identity,
    }
    if identity.get("exists"):
        if resolved.is_symlink():
            raise RuntimeError(f"projection_reparse_forbidden:{relative}")
        record.update(
            {
                "bytes": int(identity["size"]),
                "sha256": _sha256_file(resolved),
            }
        )
    return record


def schema_migration_projection_snapshot() -> dict[str, Any]:
    """Capture the exact v2 marker and immutable transitive object closure."""
    from vector_lake.wiki_utils import get_wiki_dir

    base = get_wiki_dir().resolve()
    fixed = [
        base / INDEX_FILENAME,
        base / CLAIM_GRAPH_FILENAME,
        base / SIDECAR_FILENAME,
    ]
    before = [_artifact_identity(path) for path in fixed]
    existing = sum(bool(item.get("exists")) for item in before)
    if existing == 0:
        return {
            "contract": "vector-lake-pre-projection/v2",
            "format_version": FORMAT_VERSION,
            "status": "absent",
            "generation": None,
            "canonical_generation": None,
            "index_root_sha256": None,
            "claim_graph_root_sha256": None,
            "artifacts": [],
            "issues": [],
        }
    issues: list[str] = []
    artifacts: list[dict[str, Any]] = []
    sidecar: dict[str, Any] | None = None
    if existing != len(fixed):
        issues.append("projection_pair_incomplete")
    try:
        validate_locator(fixed[0], "index")
        validate_locator(fixed[1], "claim_graph")
        raw_sidecar = fixed[2].read_bytes()
        if len(raw_sidecar) > MAX_SIDECAR_BYTES:
            raise ProjectionV2ContractError("sidecar_too_large")
        sidecar = validate_sidecar(raw_sidecar.decode("utf-8"))
        if raw_sidecar != _canonical_json(sidecar).encode("utf-8"):
            raise ProjectionV2ContractError("sidecar_not_canonical")
        closure = validate_root_closure(base, sidecar)
        artifacts = [_artifact_record(path, base) for path in fixed]
        artifacts.extend(_artifact_record(path, base) for path in closure)
        by_relative = {
            str(item["relative_path"]): item for item in artifacts
        }
        artifacts = [by_relative[key] for key in sorted(by_relative)]
    except (OSError, UnicodeError, ProjectionV2ContractError, RuntimeError) as exc:
        issues.append(f"projection_v2_invalid:{exc}")
        artifacts = [
            _artifact_record(path, base)
            for path in fixed
            if _artifact_identity(path).get("exists")
        ]
    after = [_artifact_identity(path) for path in fixed]
    if before != after:
        raise RuntimeError("Projection changed while its v2 binding was read")
    status = "captured" if not issues and sidecar is not None else "incomplete"
    return {
        "contract": "vector-lake-pre-projection/v2",
        "format_version": FORMAT_VERSION,
        "status": status,
        "generation": sidecar.get("projection_generation") if sidecar else None,
        "canonical_generation": (
            sidecar.get("canonical_generation") if sidecar else None
        ),
        "index_root_sha256": sidecar.get("index_root_sha256") if sidecar else None,
        "claim_graph_root_sha256": (
            sidecar.get("claim_graph_root_sha256") if sidecar else None
        ),
        "artifacts": artifacts,
        "issues": list(dict.fromkeys(issues)),
    }


def schema_migration_projection_existing_bytes(snapshot: Mapping[str, Any]) -> int:
    seen: set[str] = set()
    total = 0
    for item in snapshot.get("artifacts") or []:
        if not isinstance(item, Mapping) or not item.get("source_identity", {}).get(
            "exists"
        ):
            continue
        relative = str(item.get("relative_path") or item.get("name") or "")
        if relative in seen:
            continue
        seen.add(relative)
        total += int(item.get("bytes") or 0)
    return total


def schema_migration_projection_content_binding(
    snapshot: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "contract": snapshot.get("contract"),
        "format_version": snapshot.get("format_version"),
        "status": snapshot.get("status"),
        "generation": snapshot.get("generation"),
        "canonical_generation": snapshot.get("canonical_generation"),
        "index_root_sha256": snapshot.get("index_root_sha256"),
        "claim_graph_root_sha256": snapshot.get("claim_graph_root_sha256"),
        "artifacts": [
            {
                "relative_path": item.get("relative_path"),
                "sha256": item.get("sha256"),
                "bytes": item.get("bytes"),
                "exists": bool(item.get("source_identity", {}).get("exists")),
            }
            for item in snapshot.get("artifacts") or []
            if isinstance(item, Mapping)
        ],
        "issues": list(snapshot.get("issues") or []),
    }


def schema_migration_projection_backup(
    snapshot: Mapping[str, Any],
    *,
    final_directory: Path,
) -> dict[str, Any]:
    status = str(snapshot.get("status") or "")
    if status == "absent":
        return {
            "contract": "vector-lake-projection-backup/v2",
            "format_version": FORMAT_VERSION,
            "status": "absent",
            "directory": None,
            "generation": None,
            "canonical_generation": None,
            "index_root_sha256": None,
            "claim_graph_root_sha256": None,
            "artifacts": [],
        }
    if status not in {"captured", "incomplete"}:
        raise RuntimeError("projection_backup_source_status_invalid")
    final = Path(final_directory).resolve()
    if final.exists():
        raise RuntimeError("projection_backup_destination_exists")
    final.parent.mkdir(parents=True, exist_ok=True)
    staging = final.with_name(
        f".{final.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    )
    copied: list[dict[str, Any]] = []
    try:
        staging.mkdir()
        for item in snapshot.get("artifacts") or []:
            if not isinstance(item, Mapping) or not item.get(
                "source_identity", {}
            ).get("exists"):
                continue
            relative = Path(str(item.get("relative_path") or ""))
            if relative.is_absolute() or ".." in relative.parts:
                raise RuntimeError("projection_backup_relative_path_invalid")
            source = Path(str(item.get("source_path") or "")).resolve()
            destination = (staging / relative).resolve()
            try:
                destination.relative_to(staging.resolve())
            except ValueError as exc:
                raise RuntimeError("projection_backup_path_escape") from exc
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
            sync_file(destination)
            observed_sha = _sha256_file(destination)
            if (
                observed_sha != item.get("sha256")
                or int(destination.stat().st_size) != int(item.get("bytes") or -1)
            ):
                raise RuntimeError(
                    f"projection_backup_copy_mismatch:{relative.as_posix()}"
                )
            copied.append(
                {
                    "name": destination.name,
                    "relative_path": relative.as_posix(),
                    "path": str(final / relative),
                    "sha256": observed_sha,
                    "bytes": int(destination.stat().st_size),
                }
            )
        if schema_migration_projection_content_binding(
            schema_migration_projection_snapshot()
        ) != schema_migration_projection_content_binding(snapshot):
            raise RuntimeError("Projection changed while its v2 copy was created")
        os.replace(staging, final)
        sync_directory(final.parent)
        backup = {
            "contract": "vector-lake-projection-backup/v2",
            "format_version": FORMAT_VERSION,
            "status": status,
            "directory": str(final),
            "generation": snapshot.get("generation"),
            "canonical_generation": snapshot.get("canonical_generation"),
            "index_root_sha256": snapshot.get("index_root_sha256"),
            "claim_graph_root_sha256": snapshot.get("claim_graph_root_sha256"),
            "artifacts": copied,
        }
        schema_migration_validate_projection_backup(
            snapshot,
            backup,
            backup_root=final.parent,
        )
        return backup
    except BaseException:
        if staging.exists():
            shutil.rmtree(staging)
        raise


def schema_migration_validate_projection_backup(
    source_snapshot: Mapping[str, Any],
    backup: object,
    *,
    backup_root: Path,
) -> None:
    if not isinstance(backup, Mapping):
        raise RuntimeError("projection_backup_missing")
    status = str(source_snapshot.get("status") or "")
    if (
        backup.get("contract") != "vector-lake-projection-backup/v2"
        or backup.get("format_version") != FORMAT_VERSION
        or backup.get("status") != status
        or backup.get("generation") != source_snapshot.get("generation")
        or backup.get("canonical_generation")
        != source_snapshot.get("canonical_generation")
        or backup.get("index_root_sha256")
        != source_snapshot.get("index_root_sha256")
        or backup.get("claim_graph_root_sha256")
        != source_snapshot.get("claim_graph_root_sha256")
    ):
        raise RuntimeError("projection_backup_contract_mismatch")
    if status == "absent":
        if backup.get("directory") is not None or backup.get("artifacts") not in (
            [],
            (),
        ):
            raise RuntimeError("projection_backup_absent_payload_invalid")
        return
    directory = Path(str(backup.get("directory") or "")).resolve()
    root = Path(backup_root).resolve()
    try:
        directory.relative_to(root)
    except ValueError as exc:
        raise RuntimeError("projection_backup_directory_escape") from exc
    expected = {
        str(item.get("relative_path")): item
        for item in source_snapshot.get("artifacts") or []
        if isinstance(item, Mapping)
        and item.get("source_identity", {}).get("exists")
    }
    observed = {
        str(item.get("relative_path")): item
        for item in backup.get("artifacts") or []
        if isinstance(item, Mapping)
    }
    if set(observed) != set(expected):
        raise RuntimeError("projection_backup_artifact_set_mismatch")
    for relative, source in expected.items():
        item = observed[relative]
        path = Path(str(item.get("path") or "")).resolve()
        try:
            path.relative_to(directory)
        except ValueError as exc:
            raise RuntimeError("projection_backup_artifact_escape") from exc
        if not path.is_file() or path.is_symlink():
            raise RuntimeError(f"projection_backup_artifact_missing:{relative}")
        sha = _sha256_file(path)
        size = int(path.stat().st_size)
        if (
            sha != source.get("sha256")
            or sha != item.get("sha256")
            or size != int(source.get("bytes") or -1)
            or size != int(item.get("bytes") or -1)
        ):
            raise RuntimeError(f"projection_backup_artifact_mismatch:{relative}")


def schema_rollback_stage_projection(plan: Mapping[str, Any]) -> dict[str, Any]:
    """Stage every v2 recovery artifact without changing the live projection."""
    from vector_lake.wiki_utils import get_wiki_dir

    try:
        restore = plan["restore"]
        snapshot = restore["pre_projection"]
        backup = restore["projection_backup"]
    except (KeyError, TypeError) as exc:
        raise RuntimeError("projection_v2_rollback_plan_invalid") from exc
    if not isinstance(snapshot, Mapping) or snapshot.get("format_version") != 2:
        raise RuntimeError("projection_v2_rollback_snapshot_invalid")
    backup_root = Path(str(backup.get("directory") or ".")).resolve().parent
    schema_migration_validate_projection_backup(
        snapshot,
        backup,
        backup_root=backup_root,
    )
    status = str(snapshot.get("status") or "")
    if status == "absent":
        return {
            "contract": "vector-lake-projection-rollback-stage/v2",
            "format_version": FORMAT_VERSION,
            "status": "absent",
            "directory": None,
            "artifacts": [],
            "content_binding": schema_migration_projection_content_binding(
                snapshot
            ),
        }
    live_base = get_wiki_dir().resolve()
    token = str(plan.get("fingerprint") or "rollback").removeprefix("sha256:")[:16]
    staging = live_base.parent / (
        f".{live_base.name}.projection-v2-rollback.{token}."
        f"{os.getpid()}.{secrets.token_hex(6)}.tmp"
    )
    if staging.exists():
        raise RuntimeError("projection_v2_rollback_stage_exists")
    staged: list[dict[str, Any]] = []
    try:
        staging.mkdir(parents=True)
        for item in backup.get("artifacts") or []:
            if not isinstance(item, Mapping):
                raise RuntimeError("projection_v2_rollback_artifact_invalid")
            relative = Path(str(item.get("relative_path") or ""))
            if relative.is_absolute() or not relative.parts or ".." in relative.parts:
                raise RuntimeError("projection_v2_rollback_relative_path_invalid")
            source = Path(str(item.get("path") or "")).resolve()
            target = (staging / relative).resolve()
            try:
                target.relative_to(staging.resolve())
            except ValueError as exc:
                raise RuntimeError("projection_v2_rollback_stage_escape") from exc
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            sync_file(target)
            sha = _sha256_file(target)
            size = int(target.stat().st_size)
            if sha != item.get("sha256") or size != int(item.get("bytes") or -1):
                raise RuntimeError(
                    f"projection_v2_rollback_stage_mismatch:{relative.as_posix()}"
                )
            staged.append(
                {
                    "name": target.name,
                    "relative_path": relative.as_posix(),
                    "path": str(target),
                    "sha256": sha,
                    "bytes": size,
                }
            )
        return {
            "contract": "vector-lake-projection-rollback-stage/v2",
            "format_version": FORMAT_VERSION,
            "status": status,
            "directory": str(staging),
            "artifacts": staged,
            "content_binding": schema_migration_projection_content_binding(
                snapshot
            ),
        }
    except BaseException:
        if staging.exists():
            shutil.rmtree(staging)
        raise


def schema_rollback_publish_projection(
    plan: Mapping[str, Any],
    staged: Mapping[str, Any],
) -> None:
    """Merge immutable objects, publish locators, and replace sidecar last."""
    from vector_lake.wiki_utils import get_wiki_dir

    if (
        not isinstance(staged, Mapping)
        or staged.get("contract")
        != "vector-lake-projection-rollback-stage/v2"
        or staged.get("format_version") != FORMAT_VERSION
    ):
        raise RuntimeError("projection_v2_rollback_stage_contract_invalid")
    try:
        snapshot = plan["restore"]["pre_projection"]
    except (KeyError, TypeError) as exc:
        raise RuntimeError("projection_v2_rollback_plan_invalid") from exc
    expected_binding = schema_migration_projection_content_binding(snapshot)
    if staged.get("content_binding") != expected_binding:
        raise RuntimeError("projection_v2_rollback_stage_binding_mismatch")
    base = get_wiki_dir().resolve()
    status = str(staged.get("status") or "")
    if status == "absent":
        for filename in (SIDECAR_FILENAME, CLAIM_GRAPH_FILENAME, INDEX_FILENAME):
            path = base / filename
            if path.is_symlink():
                raise RuntimeError(f"projection_v2_rollback_symlink:{filename}")
            path.unlink(missing_ok=True)
        sync_directory(base)
        return
    stage_directory = Path(str(staged.get("directory") or "")).resolve()
    artifacts = {
        str(item.get("relative_path")): item
        for item in staged.get("artifacts") or []
        if isinstance(item, Mapping)
    }
    expected_relatives = {
        str(item.get("relative_path"))
        for item in snapshot.get("artifacts") or []
        if isinstance(item, Mapping)
        and item.get("source_identity", {}).get("exists")
    }
    if set(artifacts) != expected_relatives:
        raise RuntimeError("projection_v2_rollback_stage_artifact_set_mismatch")

    def publish_relative(relative_text: str) -> None:
        item = artifacts[relative_text]
        source = Path(str(item.get("path") or "")).resolve()
        try:
            source.relative_to(stage_directory)
        except ValueError as exc:
            raise RuntimeError("projection_v2_rollback_source_escape") from exc
        relative = Path(relative_text)
        target = (base / relative).resolve()
        try:
            target.relative_to(base)
        except ValueError as exc:
            raise RuntimeError("projection_v2_rollback_target_escape") from exc
        if target.is_symlink():
            raise RuntimeError(
                f"projection_v2_rollback_target_symlink:{relative_text}"
            )
        if target.is_file():
            if (
                int(target.stat().st_size) == int(item.get("bytes") or -1)
                and hmac.compare_digest(
                    _sha256_file(target), str(item.get("sha256") or "")
                )
            ):
                source.unlink(missing_ok=True)
                return
            if relative.parts[:3] == (".projection-store", "objects", "sha256"):
                raise RuntimeError(
                    f"projection_v2_immutable_object_collision:{relative_text}"
                )
        target.parent.mkdir(parents=True, exist_ok=True)
        # The rollback staging tree is intentionally isolated. Publish through
        # a same-directory durable temporary because the durability contract
        # forbids cross-directory rename assumptions.
        _write_durable_replace(target, source.read_bytes())
        source.unlink(missing_ok=True)

    object_relatives = sorted(
        relative
        for relative in artifacts
        if Path(relative).parts[:3]
        == (".projection-store", "objects", "sha256")
    )
    for relative in object_relatives:
        publish_relative(relative)
    for relative in (INDEX_FILENAME, CLAIM_GRAPH_FILENAME):
        if relative not in artifacts:
            raise RuntimeError(f"projection_v2_rollback_missing:{relative}")
        publish_relative(relative)
    if SIDECAR_FILENAME not in artifacts:
        raise RuntimeError(f"projection_v2_rollback_missing:{SIDECAR_FILENAME}")
    publish_relative(SIDECAR_FILENAME)
    if stage_directory.exists():
        shutil.rmtree(stage_directory)
    observed = schema_migration_projection_snapshot()
    if schema_migration_projection_content_binding(observed) != expected_binding:
        raise RuntimeError("projection_v2_rollback_publish_binding_mismatch")


__all__ = [
    "CLAIM_GRAPH_FILENAME",
    "FORMAT_VERSION",
    "INDEX_FILENAME",
    "LOCATOR_CONTRACT",
    "MAX_FRONTIER",
    "MAX_CLAIM_GRAPH_NODES",
    "MAX_CLAIM_GRAPH_EDGES",
    "MAX_SIDECAR_BYTES",
    "PreparedProjectionV2",
    "ProjectionHeavyRebuildRequired",
    "ProjectionV2ContractError",
    "ROOT_CONTRACT",
    "SIDECAR_CONTRACT",
    "SIDECAR_FILENAME",
    "build_projection_roots",
    "build_claim_graph_root",
    "ensure_static_locators",
    "is_v2_locator",
    "load_committed_claim_graph",
    "load_committed_index",
    "load_committed_pair",
    "load_component_roots",
    "locator_bytes",
    "locator_payload",
    "materialize_claim_graph",
    "materialize_index",
    "publish_prepared_projection",
    "prepare_projection_from_roots",
    "read_committed_sidecar",
    "recover_pending_publish",
    "require_bounded_frontier",
    "schema_migration_projection_backup",
    "schema_migration_projection_content_binding",
    "schema_migration_projection_existing_bytes",
    "schema_migration_projection_snapshot",
    "schema_migration_validate_projection_backup",
    "schema_rollback_publish_projection",
    "schema_rollback_stage_projection",
    "sidecar_identity",
    "validate_locator",
    "validate_root_closure",
    "validate_sidecar",
]
