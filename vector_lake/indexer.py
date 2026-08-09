import json
import hashlib
import logging
import math
import os
import re
import subprocess
import sys
import threading
import time
import unicodedata
import uuid
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path

from filelock import FileLock, Timeout

from vector_lake import governance_metrics
from vector_lake import governance_store
from vector_lake import db_store
from vector_lake.wiki_utils import (
    VALID_PREFIXES,
    get_claim_graph_path,
    get_index_path,
    get_projection_manifest_path,
    get_wiki_dir,
    read_markdown_file,
)

from vector_lake.schema_validator import validate_schema, SchemaViolationException



logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("vector-lake-indexer")

DEFAULT_TTL = {
    "source": 365,
    "synthesis": 730,
    "vendor": 1095,
    "product": 1095,
    "person": 1095,
    "event": 1095,
    "concept": 1825,
    "policy": 1095,
    "standard": 1095,
}

RELEVANCE_WEIGHTS = {
    "direct_link": 3.0,
    "source_overlap": 4.0,
    "common_neighbor": 1.5,
    "type_affinity": 1.0,
}

MAX_EDGES_PER_NODE = 15
EDGE_CANDIDATE_MULTIPLIER = 4
MAX_SOURCE_FANOUT = 250
MAX_GRAPH_INSIGHTS = 25
INDEX_PARITY_FIELDS = (
    "id", "title", "summary", "raw_text", "type", "domain", "topic_cluster",
    "status", "epistemic_status", "categories", "tags", "aliases", "relations",
    "sources", "tension_edges", "links", "outbound_links", "triples", "updated",
    "updated_at",
)
PROJECTION_MANIFEST_KEY = "projection_manifest"
PROJECTION_CONTRACT = "index-claim-graph-pair"
PROJECTION_CONTRACT_VERSION = 1
PROJECTION_SIDECAR_CONTRACT = "index-claim-graph-sidecar"
PROJECTION_SIDECAR_VERSION = 1
PROJECTION_SIDECAR_STAGE_SUFFIX = ".pair-manifest"
_PROJECTION_DIGEST_CACHE_MAX = 16
_PROJECTION_DIGEST_CACHE_LOCK = threading.Lock()
_PROJECTION_DIGEST_CACHE: dict[tuple[str, tuple[int, ...]], str] = {}

CANONICAL_PROJECTION_SURFACES = (
    "entities",
    "claims",
    "sources",
    "page_graph_edges",
    "claim_graph_edges",
)
CANONICAL_GENERATION_ALGORITHM = "runtime-generations-sha256-v2"

def _topology_worker_timeout_seconds() -> float:
    try:
        value = float(
            os.environ.get("VECTOR_LAKE_TOPOLOGY_WORKER_TIMEOUT_SECONDS", "60")
        )
    except (TypeError, ValueError):
        value = 60.0
    if not math.isfinite(value):
        value = 60.0
    return max(5.0, min(300.0, value))


def _louvain_partition_in_subprocess(
    node_keys: list[str],
    weighted_edges: list[dict],
) -> dict[str, str]:
    """Compute Louvain out of process so NumPy memory dies with the worker."""
    payload = json.dumps(
        {
            "nodes": node_keys,
            "edges": [
                [
                    edge["source"],
                    edge["target"],
                    float(edge.get("weight", 1.0) or 1.0),
                ]
                for edge in weighted_edges
            ],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    worker_path = Path(__file__).with_name("topology_worker.py").resolve()
    completed = subprocess.run(
        [sys.executable, "-I", str(worker_path)],
        input=payload,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="strict",
        timeout=_topology_worker_timeout_seconds(),
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if completed.returncode != 0:
        detail = str(completed.stderr or "").strip()[-1000:]
        raise RuntimeError(
            f"topology worker exited with {completed.returncode}: {detail}"
        )
    if len(completed.stdout) > max(1_000_000, len(node_keys) * 256):
        raise RuntimeError("topology worker output exceeds the bounded result size")
    result = json.loads(completed.stdout)
    partition = result.get("partition") if isinstance(result, dict) else None
    if not isinstance(partition, dict) or set(partition) != set(node_keys):
        raise RuntimeError("topology worker returned an incomplete partition")
    return {node_key: str(partition[node_key]) for node_key in node_keys}



def _bounded_node_edge_candidates(
    source: str,
    weighted_targets: list[tuple[float, str]],
    limit: int | None = None,
) -> list[dict]:
    """Retain a deterministic bounded edge frontier before global pruning."""
    candidate_limit = limit or (MAX_EDGES_PER_NODE * EDGE_CANDIDATE_MULTIPLIER)
    strongest = sorted(weighted_targets, key=lambda item: (-item[0], item[1]))[:candidate_limit]
    return [
        {"source": source, "target": target, "weight": weight}
        for weight, target in strongest
    ]


def _strip_markdown_suffix(value: str) -> str:
    text = str(value)
    return text[:-3] if text.casefold().endswith(".md") else text


def _normalize_graph_source(source: object) -> str:
    value = str(source or "").strip().replace("\\", "/")
    if value.startswith("[[") and value.endswith("]]" ):
        value = value[2:-2].split("|", 1)[0]
    value = _strip_markdown_suffix(value.rsplit("/", 1)[-1])
    return re.sub(r"[\W_]+", "_", value.lower(), flags=re.UNICODE).strip("_")


def _is_informative_graph_source(source: object) -> bool:
    """Reject provenance placeholders that must never imply semantic similarity."""
    normalized = _normalize_graph_source(source)
    if not normalized:
        return False
    return normalized not in {
        "auto_fixed",
        "source_auto_fixed",
        "source_generated",
        "source_placeholder",
        "source_stub",
        "source_unknown",
        "unknown",
    }


def _graph_source_index(nodes_dict: dict[str, dict]) -> dict[str, list[str]]:
    source_to_nodes: dict[str, list[str]] = {}
    for node_key, node in nodes_dict.items():
        for source in set(node.get("sources") or []):
            if _is_informative_graph_source(source):
                source_to_nodes.setdefault(str(source), []).append(node_key)
    return {
        source: sorted(node_keys)
        for source, node_keys in source_to_nodes.items()
        if len(node_keys) <= MAX_SOURCE_FANOUT
    }


def _index_node_signature(node: dict) -> str:
    stable = {field: node.get(field) for field in INDEX_PARITY_FIELDS}
    return json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _normalize_projection_identity(value: object) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).casefold()


def is_system_page_filename(filename: str) -> bool:
    page_key = filename[:-3] if filename.casefold().endswith(".md") else filename
    return _normalize_projection_identity(page_key).startswith("system_")


def index_projection_matches_canonical(
    filenames: list[str],
    allowed_alias_redirects: dict[str, str] | None = None,
) -> bool:
    """Prove that selected canonical pages are already reflected in index.json."""
    allowed_alias_redirects = allowed_alias_redirects or {}
    selected_page_keys = {
        filename[:-3]
        for filename in filenames
        if filename.casefold().endswith(".md")
    }
    system_identities = {
        _normalize_projection_identity(page_key)
        for page_key in selected_page_keys
        if is_system_page_filename(page_key)
    }
    page_keys = {
        page_key
        for page_key in selected_page_keys
        if _normalize_projection_identity(page_key) not in system_identities
    }
    if not selected_page_keys or not get_index_path().exists():
        return False
    try:
        index_data = read_committed_index_snapshot(
            get_index_path(),
            lock_timeout=15,
            _require_current_generation=False,
            _require_verified_binding=False,
        )
    except (
        Timeout,
        OSError,
        json.JSONDecodeError,
        ProjectionPairContractError,
        ProjectionSnapshotChanged,
        ValueError,
    ):
        return False
    nodes = index_data.get("nodes") or {}
    expected: dict[str, set[str]] = {page_key: set() for page_key in page_keys}
    conn = db_store.get_connection()
    ordered = sorted(page_keys)
    for offset in range(0, len(ordered), 500):
        batch = ordered[offset:offset + 500]
        placeholders = ",".join("?" for _ in batch)
        rows = conn.execute(
            "SELECT entity_id, data_json FROM entities "
            f"WHERE json_extract(data_json, '$.page_key') IN ({placeholders})",
            tuple(batch),
        ).fetchall()
        for row in rows:
            entity = json.loads(row["data_json"])
            projected_key, projected = _entity_to_index_node(entity, str(row["entity_id"]))
            if projected_key in expected:
                expected[projected_key].add(_index_node_signature(projected))
    aliases = index_data.get("aliases") or {}
    edges = index_data.get("weighted_edges") or []
    for page_key, signatures in expected.items():
        observed = nodes.get(page_key)
        if signatures:
            if observed is None or _index_node_signature(observed) not in signatures:
                return False
        else:
            alias_value = aliases.get(page_key)
            expected_redirect = allowed_alias_redirects.get(page_key)
            if observed is not None:
                return False
            if alias_value is not None and alias_value != expected_redirect:
                return False
            if expected_redirect is not None and alias_value != expected_redirect:
                return False
            if page_key in aliases.values():
                return False
            if any(edge.get("source") == page_key or edge.get("target") == page_key for edge in edges):
                return False
    if any(
        _normalize_projection_identity(page_key) in system_identities
        for page_key in nodes
    ):
        return False
    if any(
        _normalize_projection_identity(value) in system_identities
        for key, target in aliases.items()
        for value in (key, target)
    ):
        return False
    if any(
        _normalize_projection_identity(edge.get(endpoint)) in system_identities
        for edge in edges
        for endpoint in ("source", "target")
    ):
        return False
    if system_identities:
        search_keys = conn.execute(
            "SELECT node_key FROM wiki_search_index"
        ).fetchall()
        if any(
            _normalize_projection_identity(row["node_key"]) in system_identities
            for row in search_keys
        ):
            return False
        vector_keys = db_store.get_vector_connection().execute(
            "SELECT entity_id FROM vec_embeddings"
        ).fetchall()
        if any(
            _normalize_projection_identity(row["entity_id"]) in system_identities
            for row in vector_keys
        ):
            return False
    return True

PRED_WEIGHT_TAXONOMY = frozenset({"属于", "parent", "is-a", "belongs_to", "instance_of", "has_part", "核心构件"})
PRED_WEIGHT_RELATION = frozenset({"类似", "related_to", "see_also", "peer", "关联"})
PRED_WEIGHT_MENTION = frozenset({"mentions", "提及", "引用"})

def get_pred_weight(pred: str) -> float:
    pred_lower = pred.lower()
    if pred_lower in PRED_WEIGHT_TAXONOMY:
        return RELEVANCE_WEIGHTS["direct_link"] * 3.0
    if pred_lower in PRED_WEIGHT_RELATION:
        return RELEVANCE_WEIGHTS["direct_link"] * 1.5
    if pred_lower in PRED_WEIGHT_MENTION:
        return RELEVANCE_WEIGHTS["direct_link"] * 0.4
    return RELEVANCE_WEIGHTS["direct_link"]

TYPE_AFFINITY = {
    "vendor": {"vendor": 0.8, "product": 1.2, "person": 1.0, "concept": 1.2, "source": 1.0, "synthesis": 1.0, "policy": 1.0, "standard": 1.0},
    "product": {"vendor": 1.2, "product": 0.8, "person": 1.0, "concept": 1.2, "source": 1.0, "synthesis": 1.0, "policy": 1.0, "standard": 1.0},
    "person": {"vendor": 1.0, "product": 1.0, "person": 0.8, "concept": 1.2, "source": 1.0, "synthesis": 1.0, "policy": 1.0, "standard": 1.0},
    "event": {"vendor": 1.0, "product": 1.0, "person": 1.0, "event": 0.8, "concept": 1.2, "source": 1.0, "synthesis": 1.0, "policy": 1.0, "standard": 1.0},
    "concept": {"vendor": 1.2, "product": 1.2, "person": 1.2, "event": 1.2, "concept": 0.8, "source": 1.0, "synthesis": 1.2, "policy": 1.0, "standard": 1.0},
    "source": {"vendor": 1.0, "product": 1.0, "person": 1.0, "event": 1.0, "concept": 1.0, "source": 0.5, "synthesis": 1.0, "policy": 1.0, "standard": 1.0},
    "synthesis": {"vendor": 1.0, "product": 1.0, "person": 1.0, "event": 1.0, "concept": 1.2, "source": 1.0, "synthesis": 0.8, "policy": 1.0, "standard": 1.0},
    "policy": {"vendor": 1.0, "product": 1.0, "person": 1.0, "event": 1.0, "concept": 1.0, "source": 1.0, "synthesis": 1.0, "policy": 0.8, "standard": 1.2},
    "standard": {"vendor": 1.0, "product": 1.0, "person": 1.0, "event": 1.0, "concept": 1.0, "source": 1.0, "synthesis": 1.0, "policy": 1.2, "standard": 0.8},
}

LEGACY_EMBEDDED_INDEX_KEYS = (
    "claim_graph",
    "claim_index",
    "entity_index",
    "source_index",
)


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



def _tokenize(text: str) -> list[str]:
    if not text:
        return []
    text = text.lower()
    tokens = []
    words = re.findall(r'[a-z0-9]+', text)
    tokens.extend(words)
    chinese_chars = re.findall(r'[\u4e00-\u9fa5]', text)
    tokens.extend(chinese_chars)
    for i in range(len(chinese_chars) - 1):
        tokens.append(chinese_chars[i] + chinese_chars[i+1])
    return tokens
def _tokenize_for_fts(text: str) -> str:
    if not text:
        return ""
    try:
        from vector_lake.tokenizer_runtime import tokenize_for_fts
        return tokenize_for_fts(text)
    except ImportError:
        return text

def _build_bm25_index(index_data: dict, embeddings_map: dict):
    nodes = (index_data.get("nodes") or {})
    total = len(nodes)
    log.info(f"Building BM25 index for {total} nodes...")
    for i, (node_key, node) in enumerate(nodes.items()):
        if i % 1000 == 0:
            log.info(f"Tokenized {i}/{total} nodes...")
        aliases_str = " ".join((node.get("aliases") or [])) if isinstance(node.get("aliases"), list) else ""
        text = f"{aliases_str} {node.get('raw_text', '')}"
        t_title = _tokenize_for_fts(node.get('title', ''))
        t_summary = _tokenize_for_fts(node.get('summary', ''))
        t_text = _tokenize_for_fts(text)
        db_store.upsert_search_index(node_key, t_title, t_summary, t_text)
        
        if node_key in embeddings_map:
            db_store.upsert_embedding(node_key, embeddings_map[node_key])


def _search_projection_row(
    node_key: str,
    node: dict,
) -> tuple[str, str, str, str]:
    aliases = node.get("aliases") or []
    aliases_text = " ".join(aliases) if isinstance(aliases, list) else ""
    text = f"{aliases_text} {node.get('raw_text', '')}"
    return (
        str(node_key),
        _tokenize_for_fts(node.get("title", "")),
        _tokenize_for_fts(node.get("summary", "")),
        _tokenize_for_fts(text),
    )


def _search_projection_upserts(index_data: dict) -> list[tuple[str, str, str, str]]:
    """Pre-tokenize the FTS payload without holding a SQLite write transaction."""
    rows = []
    nodes = index_data.get("nodes") or {}
    total = len(nodes)
    log.info("Preparing BM25 projection rows for %s nodes...", total)
    for offset, (node_key, node) in enumerate(nodes.items()):
        if offset % 1000 == 0:
            log.info("Tokenized %s/%s nodes...", offset, total)
        rows.append(_search_projection_row(node_key, node))
    return rows




def _strip_legacy_embedded_payloads(index_data: dict) -> list[str]:
    removed = []
    # Strip any heavy artifacts that might have been saved in index.json previously
    for key in ["bm25_index", "governance_queue", "alias_registry", "entities"]:
        if key in index_data:
            del index_data[key]
            removed.append(key)
    return removed


def _strip_system_nodes(index_data: dict) -> list[str]:
    """Keep System_ pages outside the user-facing search projection in warm and cold paths."""
    nodes = index_data.get("nodes") or {}
    removed = [str(key) for key in nodes if str(key).startswith("System_")]
    if not removed:
        return []
    removed_set = set(removed)
    for key in removed:
        nodes.pop(key, None)
    index_data["aliases"] = {
        key: value
        for key, value in (index_data.get("aliases") or {}).items()
        if key not in removed_set and value not in removed_set
    }
    index_data["weighted_edges"] = [
        edge
        for edge in (index_data.get("weighted_edges") or [])
        if edge.get("source") not in removed_set and edge.get("target") not in removed_set
    ]
    return removed


def _write_json_payload(output_path: str, data: dict):
    temp_path = output_path + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, separators=(",", ":"))
    import time
    for attempt in range(5):
        try:
            os.replace(temp_path, output_path)
            return
        except PermissionError as e:
            if attempt < 4:
                time.sleep(0.1 * (2 ** attempt))
            else:
                log.critical(f"Failed to write {output_path} due to file lock after 5 attempts. Index update aborted.")
                try:
                    os.remove(temp_path)
                except Exception:
                    pass
                raise e

def _write_json_stage(stage_path: str, data: dict):
    with open(stage_path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, separators=(",", ":"))
        handle.flush()
        os.fsync(handle.fileno())


def _deduplicate_weighted_edges(edges: list[dict]) -> list[dict]:
    """Collapse repeated undirected edges and retain the strongest weight."""
    strongest: dict[tuple[str, str], dict] = {}
    for edge in edges:
        source = str(edge.get("source") or "")
        target = str(edge.get("target") or "")
        if not source or not target or source == target:
            continue
        left, right = sorted((source, target))
        normalized = dict(edge)
        normalized["source"] = left
        normalized["target"] = right
        try:
            normalized["weight"] = float(edge.get("weight", 1.0))
        except (TypeError, ValueError):
            normalized["weight"] = 1.0
        current = strongest.get((left, right))
        if current is None or normalized["weight"] > current["weight"]:
            strongest[(left, right)] = normalized
    return sorted(
        strongest.values(),
        key=lambda edge: (-edge["weight"], edge["source"], edge["target"]),
    )


def _prune_weighted_edges(
    edges: list[dict],
    node_keys: set[str] | None = None,
    max_edges_per_node: int = MAX_EDGES_PER_NODE,
) -> list[dict]:
    """Enforce one deterministic degree budget on every index publication path."""
    counts: dict[str, int] = {}
    pruned: list[dict] = []
    for edge in _deduplicate_weighted_edges(edges):
        source = edge["source"]
        target = edge["target"]
        if node_keys is not None and (source not in node_keys or target not in node_keys):
            continue
        if counts.get(source, 0) >= max_edges_per_node:
            continue
        if counts.get(target, 0) >= max_edges_per_node:
            continue
        pruned.append(edge)
        counts[source] = counts.get(source, 0) + 1
        counts[target] = counts.get(target, 0) + 1
    return pruned


def _prepare_index_payload(index_data: dict):
    removed = _strip_legacy_embedded_payloads(index_data)
    if removed:
        log.info(f"Stripped legacy embedded payloads before writing index: {', '.join(removed)}")
    index_data["weighted_edges"] = _prune_weighted_edges(
        index_data.get("weighted_edges") or [],
        set((index_data.get("nodes") or {}).keys()),
    )


def _write_index(output_path: str, index_data: dict):
    _prepare_index_payload(index_data)

    _write_json_payload(output_path, index_data)


def _write_claim_graph(output_path: str, claim_graph_data: dict):
    _write_json_payload(output_path, claim_graph_data)


class ProjectionPairContractError(RuntimeError):
    """Raised when index and claim-graph projections cannot form one snapshot."""


class ProjectionCanonicalGenerationChanged(RuntimeError):
    """Raised when canonical rows change while a projection is materialized."""


def canonical_runtime_generation_snapshot(
    connection=None,
) -> dict[str, int]:
    """Read the tracked canonical generations used by the projection pair."""
    connection = connection or db_store.get_connection()
    dirty_reader = getattr(connection, "generation_dirty_snapshot", None)
    if callable(dirty_reader):
        dirty_surfaces = set(dirty_reader())
        if dirty_surfaces.intersection(CANONICAL_PROJECTION_SURFACES):
            raise ProjectionPairContractError(
                "Cannot bind a projection to uncommitted canonical mutations."
            )
    placeholders = ", ".join("?" for _ in CANONICAL_PROJECTION_SURFACES)
    rows = connection.execute(
        f"SELECT surface, generation FROM runtime_generations "
        f"WHERE surface IN ({placeholders})",
        CANONICAL_PROJECTION_SURFACES,
    ).fetchall()
    snapshot = {str(row["surface"]): int(row["generation"]) for row in rows}
    missing = set(CANONICAL_PROJECTION_SURFACES) - set(snapshot)
    if missing:
        raise ProjectionPairContractError(
            "Canonical runtime-generation registry is incomplete: "
            + ", ".join(sorted(missing))
        )
    return {
        surface: snapshot[surface]
        for surface in CANONICAL_PROJECTION_SURFACES
    }


def _assert_canonical_generation(
    expected: dict[str, int],
    *,
    context: str,
) -> None:
    observed = canonical_runtime_generation_snapshot()
    if observed != expected:
        raise ProjectionCanonicalGenerationChanged(
            "Canonical runtime generation changed "
            f"{context}; discard the staged projection and retry."
        )


def _canonical_generation_token(snapshot: dict[str, int]) -> str:
    payload = json.dumps(
        snapshot,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _verified_canonical_generation(snapshot: dict[str, int]) -> dict:
    normalized = {
        surface: int(snapshot[surface])
        for surface in CANONICAL_PROJECTION_SURFACES
    }
    return {
        "status": "verified",
        "algorithm": CANONICAL_GENERATION_ALGORITHM,
        "token": _canonical_generation_token(normalized),
        "runtime_generations": normalized,
    }


def _unverifiable_canonical_generation(reason: str) -> dict:
    return {
        "status": "unverifiable",
        "algorithm": CANONICAL_GENERATION_ALGORITHM,
        "token": None,
        "runtime_generations": {},
        "reason": str(reason),
    }


def _validate_canonical_generation_binding(manifest: dict) -> dict:
    binding = manifest.get("canonical_generation")
    if binding is None:
        return _unverifiable_canonical_generation(
            "legacy-projection-manifest-has-no-canonical-generation"
        )
    if not isinstance(binding, dict):
        raise ProjectionPairContractError(
            "Projection canonical-generation binding is malformed."
        )
    status = binding.get("status")
    if (
        binding.get("algorithm") != CANONICAL_GENERATION_ALGORITHM
        or status not in {"verified", "unverifiable"}
    ):
        raise ProjectionPairContractError(
            "Unsupported projection canonical-generation binding."
        )
    if status == "unverifiable":
        if (
            binding.get("token") is not None
            or binding.get("runtime_generations") not in ({}, None)
            or not isinstance(binding.get("reason"), str)
            or not binding["reason"]
        ):
            raise ProjectionPairContractError(
                "Unverifiable projection canonical-generation binding is malformed."
            )
        return dict(binding)

    snapshot = binding.get("runtime_generations")
    if not isinstance(snapshot, dict) or set(snapshot) != set(
        CANONICAL_PROJECTION_SURFACES
    ):
        raise ProjectionPairContractError(
            "Verified projection canonical-generation coverage is incomplete."
        )
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value < 0
        for value in snapshot.values()
    ):
        raise ProjectionPairContractError(
            "Verified projection canonical-generation values are invalid."
        )
    normalized = {
        surface: snapshot[surface]
        for surface in CANONICAL_PROJECTION_SURFACES
    }
    if binding.get("token") != _canonical_generation_token(normalized):
        raise ProjectionPairContractError(
            "Projection canonical-generation token does not match its snapshot."
        )
    return {
        "status": "verified",
        "algorithm": CANONICAL_GENERATION_ALGORITHM,
        "token": binding["token"],
        "runtime_generations": normalized,
    }


def _new_projection_manifest(canonical_generation: dict | None = None) -> dict:
    candidate = (
        canonical_generation
        if canonical_generation is not None
        else _unverifiable_canonical_generation(
            "publisher-did-not-prove-full-canonical-generation"
        )
    )
    normalized = _validate_canonical_generation_binding(
        {"canonical_generation": candidate}
    )
    return {
        "contract": PROJECTION_CONTRACT,
        "version": PROJECTION_CONTRACT_VERSION,
        "generation": uuid.uuid4().hex,
        "published_at": _utc_now(),
        "canonical_generation": normalized,
    }


def validate_projection_pair(index_data: dict, claim_graph_data: dict) -> str:
    """Return the shared generation or reject a legacy/inconsistent pair.

    Legacy readers fail closed; the next generate/refresh publish upgrades both files.
    """
    index_manifest = index_data.get(PROJECTION_MANIFEST_KEY)
    graph_manifest = claim_graph_data.get(PROJECTION_MANIFEST_KEY)
    if index_manifest is None and graph_manifest is None:
        raise ProjectionPairContractError(
            "Legacy index/claim-graph projections have no shared generation; "
            "run sync or rebuild the index once to migrate them."
        )
    if not isinstance(index_manifest, dict) or not isinstance(graph_manifest, dict):
        raise ProjectionPairContractError(
            "Index/claim-graph projection manifest is missing or malformed; "
            "run sync to rebuild both projections."
        )
    if index_manifest != graph_manifest:
        raise ProjectionPairContractError(
            "Index and claim-graph projection generations do not match; "
            "run sync to rebuild both projections."
        )
    if (
        index_manifest.get("contract") != PROJECTION_CONTRACT
        or index_manifest.get("version") != PROJECTION_CONTRACT_VERSION
        or not isinstance(index_manifest.get("generation"), str)
        or not index_manifest["generation"]
        or not isinstance(index_manifest.get("published_at"), str)
        or not index_manifest["published_at"]
    ):
        raise ProjectionPairContractError(
            "Unsupported index/claim-graph projection manifest; "
            "run sync to migrate both projections."
        )
    _validate_canonical_generation_binding(index_manifest)
    return index_manifest["generation"]


def projection_canonical_generation(
    index_data: dict,
    claim_graph_data: dict,
) -> dict:
    """Return the pair's validated canonical binding, including legacy uncertainty."""
    validate_projection_pair(index_data, claim_graph_data)
    return _validate_canonical_generation_binding(
        index_data[PROJECTION_MANIFEST_KEY]
    )


def _stable_file_sha256(path: str) -> tuple[str, tuple[int, ...]]:
    """Hash one file only when its physical identity remains unchanged."""
    last_error = None
    for _attempt in range(2):
        before = _projection_file_identity(path)
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        after = _projection_file_identity(path)
        if before == after:
            return digest.hexdigest(), after
        last_error = ProjectionSnapshotChanged(
            f"Projection changed while hashing {path}"
        )
    raise last_error or ProjectionSnapshotChanged(
        f"Projection could not be hashed stably: {path}"
    )


def _cached_projection_sha256(path: str) -> tuple[str, tuple[int, ...]]:
    resolved = os.path.normcase(str(Path(path).resolve()))
    identity = _projection_file_identity(path)
    cache_key = (resolved, identity)
    with _PROJECTION_DIGEST_CACHE_LOCK:
        cached = _PROJECTION_DIGEST_CACHE.get(cache_key)
    if cached is not None:
        return cached, identity

    digest, stable_identity = _stable_file_sha256(path)
    cache_key = (resolved, stable_identity)
    with _PROJECTION_DIGEST_CACHE_LOCK:
        _PROJECTION_DIGEST_CACHE[cache_key] = digest
        while len(_PROJECTION_DIGEST_CACHE) > _PROJECTION_DIGEST_CACHE_MAX:
            _PROJECTION_DIGEST_CACHE.pop(next(iter(_PROJECTION_DIGEST_CACHE)))
    return digest, stable_identity


def _projection_sidecar_payload(
    manifest: dict,
    *,
    index_stage: str,
    claim_graph_stage: str,
    index_data: dict,
    claim_graph_data: dict,
) -> dict:
    index_digest, _index_identity = _stable_file_sha256(index_stage)
    graph_digest, _graph_identity = _stable_file_sha256(claim_graph_stage)
    return {
        "contract": PROJECTION_SIDECAR_CONTRACT,
        "version": PROJECTION_SIDECAR_VERSION,
        "projection_manifest": dict(manifest),
        "artifacts": {
            get_index_path().name: {
                "sha256": index_digest,
                "bytes": os.path.getsize(index_stage),
                "node_count": len(index_data.get("nodes") or {}),
                "edge_count": len(index_data.get("weighted_edges") or []),
            },
            get_claim_graph_path().name: {
                "sha256": graph_digest,
                "bytes": os.path.getsize(claim_graph_stage),
                "node_count": len(claim_graph_data.get("nodes") or []),
                "edge_count": len(claim_graph_data.get("edges") or []),
            },
        },
    }


def _validate_projection_sidecar(sidecar: dict) -> tuple[dict, dict]:
    if not isinstance(sidecar, dict):
        raise ProjectionPairContractError("Projection sidecar is not an object.")
    if (
        sidecar.get("contract") != PROJECTION_SIDECAR_CONTRACT
        or sidecar.get("version") != PROJECTION_SIDECAR_VERSION
    ):
        raise ProjectionPairContractError("Unsupported projection sidecar contract.")
    manifest = sidecar.get("projection_manifest")
    if not isinstance(manifest, dict):
        raise ProjectionPairContractError("Projection sidecar manifest is missing.")
    if (
        manifest.get("contract") != PROJECTION_CONTRACT
        or manifest.get("version") != PROJECTION_CONTRACT_VERSION
        or not isinstance(manifest.get("generation"), str)
        or not manifest.get("generation")
        or not isinstance(manifest.get("published_at"), str)
        or not manifest.get("published_at")
    ):
        raise ProjectionPairContractError("Projection sidecar manifest is invalid.")
    _validate_canonical_generation_binding(manifest)

    artifacts = sidecar.get("artifacts")
    expected_names = {get_index_path().name, get_claim_graph_path().name}
    if not isinstance(artifacts, dict) or set(artifacts) != expected_names:
        raise ProjectionPairContractError(
            "Projection sidecar artifacts must use the fixed projection filenames."
        )
    for filename in sorted(expected_names):
        metadata = artifacts.get(filename)
        if not isinstance(metadata, dict):
            raise ProjectionPairContractError(
                f"Projection sidecar metadata is invalid for {filename}."
            )
        digest = metadata.get("sha256")
        size = metadata.get("bytes")
        if (
            not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
        ):
            raise ProjectionPairContractError(
                f"Projection sidecar digest metadata is invalid for {filename}."
            )
    return manifest, artifacts


def read_committed_index_snapshot(
    index_path: str | Path | None = None,
    *,
    lock_timeout: float = 5.0,
    connection=None,
    _acquire_lock: bool = True,
    _require_current_generation: bool = True,
    _require_verified_binding: bool = True,
    _mutable: bool = False,
) -> dict:
    """Return index.json only when the complete projection commit is current.

    ``projection_pair_manifest.json`` is the commit marker.  A caller never
    receives an index snapshot unless both artifacts still match that marker,
    the parsed index embeds the same manifest, and the marker is bound to the
    current canonical runtime generation.  Low-level diagnostic readers may
    continue to use :func:`vector_lake.index_snapshot.load_index_snapshot` when
    they explicitly need to inspect broken state.
    """
    resolved_index = Path(index_path or get_index_path())
    claim_graph_path = resolved_index.with_name(get_claim_graph_path().name)
    sidecar_path = resolved_index.with_name(get_projection_manifest_path().name)
    expected_names = {get_index_path().name, get_claim_graph_path().name}
    if resolved_index.name != get_index_path().name:
        raise ProjectionPairContractError(
            "Committed projection reads require the fixed index filename; run sync."
        )

    def read_under_contract() -> dict:
        missing = [
            path.name
            for path in (resolved_index, claim_graph_path, sidecar_path)
            if not path.is_file()
        ]
        if missing:
            raise ProjectionPairContractError(
                "Committed projection is incomplete (missing "
                + ", ".join(missing)
                + "); run sync to rebuild it."
            )

        sidecar_identity = _projection_file_identity(str(sidecar_path))
        with open(sidecar_path, "r", encoding="utf-8") as handle:
            sidecar = json.load(handle)
        if _projection_file_identity(str(sidecar_path)) != sidecar_identity:
            raise ProjectionPairContractError(
                "Projection commit marker changed while it was read; retry sync."
            )
        manifest, artifacts = _validate_projection_sidecar(sidecar)
        binding = _validate_canonical_generation_binding(manifest)
        if _require_verified_binding and binding.get("status") != "verified":
            raise ProjectionPairContractError(
                "Projection canonical-generation binding is unverifiable; "
                "run a full sync."
            )
        if set(artifacts) != expected_names:
            raise ProjectionPairContractError(
                "Projection commit marker does not cover the fixed artifact pair; "
                "run sync."
            )

        artifact_identities: dict[str, tuple[int, ...]] = {}
        for path in (resolved_index, claim_graph_path):
            metadata = artifacts[path.name]
            if path.stat().st_size != metadata["bytes"]:
                raise ProjectionPairContractError(
                    f"Projection commit size does not match {path.name}; run sync."
                )
            digest, identity = _cached_projection_sha256(str(path))
            if digest != metadata["sha256"]:
                raise ProjectionPairContractError(
                    f"Projection commit digest does not match {path.name}; run sync."
                )
            if _projection_file_identity(str(path)) != identity:
                raise ProjectionPairContractError(
                    f"Projection artifact changed while reading {path.name}; retry."
                )
            artifact_identities[path.name] = identity

        # Import lazily so index_snapshot remains a low-level, cycle-free
        # diagnostic/cache module.
        if _mutable:
            index_data = _load_index_unlocked(str(resolved_index))
            if index_data is None:
                raise ProjectionPairContractError(
                    "index.json disappeared while its committed snapshot was "
                    "decoded; retry."
                )
        else:
            from vector_lake.index_snapshot import load_index_snapshot

            index_data = load_index_snapshot(resolved_index)
        if (
            _projection_file_identity(str(resolved_index))
            != artifact_identities[resolved_index.name]
        ):
            raise ProjectionPairContractError(
                "index.json changed while its committed snapshot was decoded; retry."
            )
        if index_data.get(PROJECTION_MANIFEST_KEY) != manifest:
            raise ProjectionPairContractError(
                "index.json manifest does not match the projection commit marker; "
                "run sync."
            )

        if _require_current_generation:
            if binding.get("status") != "verified":
                raise ProjectionPairContractError(
                    "Projection canonical-generation binding is unverifiable; "
                    "run a full sync."
                )
            current_generation = canonical_runtime_generation_snapshot(connection)
            if binding["runtime_generations"] != current_generation:
                raise ProjectionPairContractError(
                    "Projection canonical-generation binding is stale; run sync."
                )
        for path in (resolved_index, claim_graph_path):
            if (
                _projection_file_identity(str(path))
                != artifact_identities[path.name]
            ):
                raise ProjectionPairContractError(
                    f"Projection artifact changed while validating {path.name}; retry."
                )
        if _projection_file_identity(str(sidecar_path)) != sidecar_identity:
            raise ProjectionPairContractError(
                "Projection commit marker changed while validating the snapshot; retry."
            )
        return index_data

    try:
        if _acquire_lock:
            with FileLock(str(resolved_index) + ".lock", timeout=lock_timeout):
                return read_under_contract()
        # Runtime diagnostics use the same digest/identity checks without
        # creating a transient lockfile that would invalidate their own stable
        # directory token. Publishers remain detectable because the sidecar is
        # replaced last and every artifact identity is checked before return.
        return read_under_contract()
    except Timeout:
        raise
    except ProjectionPairContractError:
        raise
    except (OSError, json.JSONDecodeError, ValueError, ProjectionSnapshotChanged) as exc:
        raise ProjectionPairContractError(
            f"Committed projection could not be verified ({exc}); run sync."
        ) from exc
    except Exception as exc:
        raise ProjectionPairContractError(
            f"Committed projection validation failed ({exc}); run sync."
        ) from exc


def projection_pair_matches_current_generation() -> bool:
    """Prove current binding from the small commit marker and artifact digests."""
    index_path = get_index_path()
    claim_graph_path = get_claim_graph_path()
    sidecar_path = get_projection_manifest_path()
    if (
        not index_path.exists()
        or not claim_graph_path.exists()
        or not sidecar_path.exists()
    ):
        return False
    try:
        with FileLock(str(index_path) + ".lock", timeout=0):
            with open(sidecar_path, "r", encoding="utf-8") as handle:
                sidecar = json.load(handle)
            manifest, artifacts = _validate_projection_sidecar(sidecar)
            binding = _validate_canonical_generation_binding(manifest)
            if binding.get("status") != "verified":
                return False

            for path in (index_path, claim_graph_path):
                metadata = artifacts[path.name]
                if path.stat().st_size != metadata["bytes"]:
                    return False
                digest, stable_identity = _cached_projection_sha256(str(path))
                if digest != metadata["sha256"]:
                    return False
                if _projection_file_identity(str(path)) != stable_identity:
                    return False

            return binding.get("runtime_generations") == (
                canonical_runtime_generation_snapshot()
            )
    except (
        Timeout,
        OSError,
        json.JSONDecodeError,
        ProjectionPairContractError,
        ProjectionSnapshotChanged,
    ):
        return False


def _canonical_generation_for_existing_index(
    index_data: dict,
    before: dict[str, int],
    after: dict[str, int],
) -> dict:
    if before != after:
        return _unverifiable_canonical_generation(
            "canonical-generation-changed-during-projection-refresh"
        )
    manifest = index_data.get(PROJECTION_MANIFEST_KEY)
    if not isinstance(manifest, dict):
        return _unverifiable_canonical_generation(
            "existing-index-has-no-valid-projection-manifest"
        )
    existing = _validate_canonical_generation_binding(manifest)
    if (
        existing["status"] == "verified"
        and existing["runtime_generations"] == before
    ):
        return existing
    return _unverifiable_canonical_generation(
        "existing-index-generation-does-not-match-current-canonical-generation"
    )


def _read_verified_projection_for_writer_unlocked(
    output_path: str,
    expected_generation: dict[str, int],
    *,
    connection=None,
) -> tuple[dict, dict]:
    """Load a reusable projection pair while the caller owns the publish lock.

    Incremental publishers must never turn an incomplete, tampered, or stale
    projection into a new commit.  The sidecar/digest checks prove the complete
    pair, while the explicit expected-generation comparison closes the window
    between the caller's canonical snapshot and the committed read.
    """
    index_data = read_committed_index_snapshot(
        output_path,
        connection=connection,
        _acquire_lock=False,
        _require_current_generation=False,
        _mutable=True,
    )
    manifest = index_data.get(PROJECTION_MANIFEST_KEY)
    if not isinstance(manifest, dict):
        raise ProjectionPairContractError(
            "Existing projection manifest is missing; run a full sync."
        )
    binding = _validate_canonical_generation_binding(manifest)
    if (
        binding.get("status") != "verified"
        or binding.get("runtime_generations") != expected_generation
    ):
        raise ProjectionPairContractError(
            "Existing projection binding does not match the writer's canonical "
            "snapshot; run a full sync."
        )
    return index_data, binding


def _cleanup_projection_stages(*stage_paths: str):
    for stage_path in stage_paths:
        for candidate in (
            stage_path,
            stage_path + PROJECTION_SIDECAR_STAGE_SUFFIX,
        ):
            try:
                os.remove(candidate)
            except FileNotFoundError:
                pass


def _stage_projection_pair(
    output_path: str,
    index_data: dict,
    claim_graph_data: dict,
    canonical_generation: dict | None = None,
) -> tuple[str, str, dict]:
    """Serialize both files with one generation before either is published."""
    manifest = _new_projection_manifest(canonical_generation)
    index_data[PROJECTION_MANIFEST_KEY] = dict(manifest)
    claim_graph_data[PROJECTION_MANIFEST_KEY] = dict(manifest)
    _prepare_index_payload(index_data)

    claim_graph_path = str(get_claim_graph_path())
    stage_suffix = f".{manifest['generation']}.tmp"
    tmp_output = output_path + stage_suffix
    tmp_claim = claim_graph_path + stage_suffix
    try:
        _write_json_stage(tmp_claim, claim_graph_data)
        _write_json_stage(tmp_output, index_data)
        sidecar = _projection_sidecar_payload(
            manifest,
            index_stage=tmp_output,
            claim_graph_stage=tmp_claim,
            index_data=index_data,
            claim_graph_data=claim_graph_data,
        )
        _write_json_stage(
            tmp_output + PROJECTION_SIDECAR_STAGE_SUFFIX,
            sidecar,
        )
    except Exception:
        _cleanup_projection_stages(tmp_claim, tmp_output)
        raise
    return tmp_output, tmp_claim, manifest


def _replace_projection_stage(stage_path: str, output_path: str):
    for attempt in range(5):
        try:
            os.replace(stage_path, output_path)
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.1 * (2 ** attempt))


def _publish_staged_projection_pair(
    output_path: str,
    tmp_output: str,
    tmp_claim: str,
):
    """Publish graph, index, then the sidecar as the pair's commit marker."""
    claim_graph_path = str(get_claim_graph_path())
    sidecar_stage = tmp_output + PROJECTION_SIDECAR_STAGE_SUFFIX
    sidecar_path = str(get_projection_manifest_path())
    for staged_path in (tmp_claim, tmp_output, sidecar_stage):
        if not os.path.exists(staged_path):
            raise FileNotFoundError(
                f"Missing staged projection file before publish: {staged_path}"
            )
    try:
        _replace_projection_stage(tmp_claim, claim_graph_path)
        _replace_projection_stage(tmp_output, output_path)
        _replace_projection_stage(sidecar_stage, sidecar_path)
    finally:
        _cleanup_projection_stages(tmp_claim, tmp_output)


def _publish_staged_projection_pair_guarded(
    output_path: str,
    tmp_output: str,
    tmp_claim: str,
    expected_generation: dict[str, int],
):
    """Publish while a short SQLite writer guard prevents canonical drift."""
    with db_store.transaction():
        _assert_canonical_generation(
            expected_generation,
            context="immediately before publishing the staged projection pair",
        )
        _publish_staged_projection_pair(output_path, tmp_output, tmp_claim)


def _publish_projection_pair(
    output_path: str,
    index_data: dict,
    claim_graph_data: dict,
    canonical_generation: dict,
    expected_generation: dict[str, int],
) -> dict:
    tmp_output, tmp_claim, manifest = _stage_projection_pair(
        output_path,
        index_data,
        claim_graph_data,
        canonical_generation,
    )
    try:
        _publish_staged_projection_pair_guarded(
            output_path,
            tmp_output,
            tmp_claim,
            expected_generation,
        )
    except Exception:
        _cleanup_projection_stages(tmp_claim, tmp_output)
        raise
    return manifest


def _mark_graph_dirty(index_data: dict, reason: str):
    graph_state = index_data.setdefault("graph_state", {})
    graph_state["dirty"] = True
    graph_state["reason"] = reason
    graph_state["updated_at"] = _utc_now()


def _mark_graph_clean(index_data: dict, reason: str = "Topology analysis complete"):
    graph_state = index_data.setdefault("graph_state", {})
    graph_state["dirty"] = False
    graph_state["reason"] = reason
    graph_state["updated_at"] = _utc_now()


def is_graph_dirty(index_data: dict | None) -> bool:
    if not index_data:
        return True
    graph_state = (index_data.get("graph_state") or {})
    return bool(graph_state.get("dirty"))


def _parse_wiki_node(filepath: str, node_key: str):
    try:
        fm_data, body, _ = read_markdown_file(filepath)
    except (UnicodeDecodeError, OSError) as e:
        log.warning(f"Cannot read {os.path.basename(filepath)}: {e}")
        return None
    except Exception as e:
        log.warning(f"Failed to parse frontmatter in {os.path.basename(filepath)}: {e}")
        return None

    if not fm_data:
        return None

    try:
        validate_schema(fm_data, body, os.path.basename(filepath))
    except SchemaViolationException as e:
        log.warning(f"Schema violation in {os.path.basename(filepath)}: {e}")
        # Note: In a real system we would append to error_log, but we only have node_key here.
        # We will let generate_index log this as a warning.
        return None

    node_id = fm_data.get("id", "")
    title = fm_data.get("title", node_key)

    raw_type = str(fm_data.get("type", "concept")).lower().strip().replace('"', "").replace("'", "")
    if raw_type in ["vendor", "product", "person", "event", "concept", "source", "synthesis"]:
        node_type = raw_type
    elif raw_type in ["entity", "organization"]:
        node_type = "vendor"
    elif raw_type in ["system", "project"]:
        node_type = "product"
    elif raw_type in ["reference"]:
        node_type = "source"
    elif raw_type in ["comparison", "report"]:
        node_type = "synthesis"
    else:
        node_type = raw_type if raw_type.isalnum() else "concept"

    updated = str(fm_data.get("updated", ""))
    categories = fm_data.get("categories", [])
    domain = fm_data.get("domain")
    topic_cluster = fm_data.get("topic_cluster", "General")
    status = fm_data.get("status")

    ttl = fm_data.get("ttl")
    if not isinstance(ttl, (int, float)):
        ttl = DEFAULT_TTL.get(node_type, 1095)

    decay_weight = 1.0
    try:
        updated_dt = datetime.fromisoformat(updated.replace("Z", "+00:00"))
        if updated_dt.tzinfo is None:
            updated_dt = updated_dt.replace(tzinfo=timezone.utc)
        age_days = (datetime.now(timezone.utc) - updated_dt).days
        if age_days > 0 and ttl > 0:
            decay_weight = 0.5 ** (age_days / ttl)
    except Exception:
        pass

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

    # STQM: Extract tension edges from frontmatter
    raw_tension_edges = fm_data.get("tension_edges", [])
    tension_edges = []
    if isinstance(raw_tension_edges, list):
        for te in raw_tension_edges:
            if isinstance(te, dict) and te.get("target"):
                tension_edges.append({
                    "target": str(te.get("target")).strip(),
                    "polarity": float(te.get("polarity", 0.0)),
                    "intensity": float(te.get("intensity", 0.0)),
                    "context": str(te.get("context", "")).strip()
                })

    alignment_score = 100.0  # V7.2: Removed manual LLM alignment_score scoring. Handled algorithmically.

    links = set()
    triples = []
    
    # Strip code blocks to prevent parsing AST links inside markdown code sections
    import re
    clean_body = re.sub(r'```.*?```', '', body, flags=re.DOTALL)
    clean_body = re.sub(r'`.*?`', '', clean_body)
    
    for match in re.finditer(r"\[([^\[\]]+?)::\s*\[\[(.*?)\]\]\]", clean_body):
        predicate = match.group(1).strip()
        target = _strip_markdown_suffix(match.group(2).split("|")[0].strip())
        if target:
            links.add(target)
            triples.append({"predicate": predicate, "target": target})
            
    links.discard("")

    summary_text = re.sub(r"#.*?\n", "", body)
    summary_text = re.sub(r"\[([^\[\]]+?)::\s*\[\[.*?\]\]\]", "", summary_text)
    summary_text = re.sub(r"\[\[([^\]]*?\|)?([^\]]*?)\]\]", r"\2", summary_text)
    summary_text = summary_text.strip().replace("\n", " ")

    from vector_lake.wiki_utils import enforce_entity_dict
    return enforce_entity_dict({
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
        "tension_edges": tension_edges,
        "links": sorted(links),
        "triples": triples,
        "summary": summary_text[:240],
        "raw_text": body,
        "decay_weight": round(decay_weight, 4),
        "alignment_score": round(alignment_score, 2),
    })


def _entity_to_index_node(entity_data: dict, entity_id: str = "") -> tuple[str, dict]:
    """Project one canonical SQLite entity into the index read model."""
    node_key = str(
        entity_data.get("page_key")
        or os.path.splitext(str(entity_data.get("source_page") or ""))[0]
        or entity_data.get("canonical_name")
        or entity_id
    ).strip()
    if not node_key:
        raise ValueError("Canonical entity is missing page_key and fallback identity fields.")

    node_type = str(entity_data.get("type") or entity_data.get("entity_type") or "concept").lower()
    updated = str(entity_data.get("updated") or entity_data.get("updated_at") or "")
    ttl = entity_data.get("ttl")
    if not isinstance(ttl, (int, float)) or ttl <= 0:
        ttl = DEFAULT_TTL.get(node_type, 1095)

    decay_weight = entity_data.get("decay_weight")
    if not isinstance(decay_weight, (int, float)):
        decay_weight = 1.0
        try:
            updated_dt = datetime.fromisoformat(updated.replace("Z", "+00:00"))
            if updated_dt.tzinfo is None:
                updated_dt = updated_dt.replace(tzinfo=timezone.utc)
            age_days = max(0, (datetime.now(timezone.utc) - updated_dt).days)
            decay_weight = 0.5 ** (age_days / ttl)
        except (TypeError, ValueError):
            pass

    categories = entity_data.get("categories") or []
    if isinstance(categories, str):
        categories = [categories]
    aliases = entity_data.get("aliases") or []
    if isinstance(aliases, str):
        aliases = [aliases]
    sources = entity_data.get("sources") or []
    if isinstance(sources, str):
        sources = [sources]
    links = entity_data.get("links") or entity_data.get("outbound_links") or []
    if isinstance(links, str):
        links = [links]

    title = str(entity_data.get("title") or entity_data.get("canonical_name") or node_key.replace("_", " "))
    node_data = {
        "id": entity_data.get("id") or entity_id or node_key,
        "title": title,
        "summary": entity_data.get("summary") or "",
        "raw_text": entity_data.get("raw_text") or "",
        "type": node_type,
        "domain": entity_data.get("domain") or "General",
        "topic_cluster": entity_data.get("topic_cluster") or "General",
        "status": entity_data.get("status") or "Active",
        "epistemic_status": entity_data.get("epistemic-status") or entity_data.get("epistemic_status") or "draft",
        "categories": list(categories),
        "tags": entity_data.get("tags") or [],
        "aliases": list(aliases),
        "relations": entity_data.get("relations") or [],
        "sources": list(sources),
        "tension_edges": entity_data.get("tension_edges") or [],
        "links": list(links),
        "outbound_links": list(links),
        "triples": entity_data.get("triples") or [],
        "ttl": ttl,
        "decay_weight": round(float(decay_weight), 4),
        "alignment_score": float(entity_data.get("alignment_score", 100.0)),
        "node_score": round(float(decay_weight), 4),
        "updated": updated,
        "updated_at": updated,
    }
    return node_key, node_data


def calculate_relevance(node_a: dict, node_b: dict, all_nodes: dict,
                        links_a=None, links_b=None,
                        sources_a=None, sources_b=None,
                        type_a=None, type_b=None,
                        decay_a=None, decay_b=None,
                        align_a=None, align_b=None,
                        triples_a=None, triples_b=None) -> float:
    """Calculates relevance score between two nodes. O(N^2) hot path."""
    score = 0.0
    key_a = node_a.get("_key", "")
    key_b = node_b.get("_key", "")

    if links_a is None:
        links_a = frozenset((node_a.get("links") or []))
    if links_b is None:
        links_b = frozenset((node_b.get("links") or []))
    
    if key_b in links_a:
        if triples_a is not None:
            pred = triples_a.get(key_b, "mentions")
        else:
            pred = "mentions"
            for t in (node_a.get("triples") or []):
                if t.get("target") == key_b:
                    pred = t.get("predicate", "mentions")
                    break
        score += get_pred_weight(pred)
        
    if key_a in links_b:
        if triples_b is not None:
            pred = triples_b.get(key_a, "mentions")
        else:
            pred = "mentions"
            for t in (node_b.get("triples") or []):
                if t.get("target") == key_a:
                    pred = t.get("predicate", "mentions")
                    break
        score += get_pred_weight(pred)

    if sources_a is None:
        sources_a = frozenset((node_a.get("sources") or []))
    if sources_b is None:
        sources_b = frozenset((node_b.get("sources") or []))

    # Optimization: Use isdisjoint() guard to prevent expensive set allocations in O(N^2) path
    if not sources_a.isdisjoint(sources_b):
        shared_sources = len(sources_a & sources_b)
        score += shared_sources * RELEVANCE_WEIGHTS["source_overlap"]

    # Optimization: Use isdisjoint() guard to prevent expensive set allocations in O(N^2) path
    if not links_a.isdisjoint(links_b):
        common_neighbors = links_a & links_b
        for neighbor_key in common_neighbors:
            neighbor = all_nodes.get(neighbor_key)
            if neighbor:
                links = neighbor.get("links")
                if links:
                    degree = len(links)
                    if degree > 1:
                        score += (1.0 / math.log(degree)) * RELEVANCE_WEIGHTS["common_neighbor"]

    if type_a is None:
        type_a = node_a.get("type", "concept").lower()
    if type_b is None:
        type_b = node_b.get("type", "concept").lower()

    type_a_dict = TYPE_AFFINITY.get(type_a)
    if type_a_dict:
        affinity = type_a_dict.get(type_b, 0.5) * RELEVANCE_WEIGHTS["type_affinity"]
    else:
        affinity = 0.5 * RELEVANCE_WEIGHTS["type_affinity"]
    score += affinity

    if decay_a is None:
        decay_a = node_a.get("decay_weight", 1.0)
    if decay_b is None:
        decay_b = node_b.get("decay_weight", 1.0)
    
    if align_a is None:
        align_a = node_a.get("alignment_score", 100.0) / 100.0
        if align_a < 0.1:
            align_a = 0.1

    if align_b is None:
        align_b = node_b.get("alignment_score", 100.0) / 100.0
        if align_b < 0.1:
            align_b = 0.1
    
    score *= math.sqrt(decay_a * decay_b)
    score *= math.sqrt(align_a * align_b)

    return round(score, 3)


def _calculate_weighted_edges(index_data: dict) -> list[dict]:
    nodes_dict = index_data["nodes"]
    node_keys = list(nodes_dict.keys())

    for key, node in nodes_dict.items():
        node["_key"] = key

    edges = []

    # Build alias resolution map: link string -> node_key
    alias_map = {}
    for k, node in nodes_dict.items():
        alias_map[k] = k
        if node.get("title"):
            alias_map[node["title"]] = k
        for alias in node.get("aliases", []):
            alias_map[alias] = k

    # Pre-compute resolved links and sources sets for O(1) access inside the nested loop
    node_links = {}
    node_types = {}
    node_triples = {}
    node_sources = {}
    node_degrees = {}
    pred_weights = {}

    if "mentions" not in pred_weights:
        pred_weights["mentions"] = get_pred_weight("mentions")
    # ⚡ Bolt: Cache the fallback mention weight to avoid lookups in the hot loop
    mention_weight = pred_weights["mentions"]

    for key, node in nodes_dict.items():
        resolved_links = set()
        for link in (node.get("links") or []):
            if link in alias_map:
                resolved_links.add(alias_map[link])
            else:
                resolved_links.add(link)
        node_links[key] = frozenset(resolved_links)

        node_types[key] = node.get("type", "concept").lower()
        
        td = {link: mention_weight for link in resolved_links}
        for t in (node.get("triples") or []):
            if t.get("target"):
                pred = t.get("predicate", "mentions")
                if pred not in pred_weights:
                    pred_weights[pred] = get_pred_weight(pred)
                target = t["target"]
                # ⚡ Bolt: Store the pre-calculated numeric weight directly in the triples dict
                # instead of the string predicate name. This eliminates a secondary dictionary
                # lookup during the expensive O(N^2) _calculate_weighted_edges inner loop.
                if target in alias_map:
                    td[alias_map[target]] = pred_weights[pred]
                else:
                    td[target] = pred_weights[pred]
        node_triples[key] = td

        node_sources[key] = frozenset((node.get("sources") or []))

        links_len = len(node.get("links") or [])
        if links_len > 1:
            node_degrees[key] = (1.0 / math.log(links_len)) * RELEVANCE_WEIGHTS["common_neighbor"]
        else:
            node_degrees[key] = 0.0

    # Bolt Optimization: Pre-populate node_degrees with 0.0 for all unresolved links
    # to avoid expensive dictionary .get() fallbacks in the O(N^2) inner loop.
    for links in node_links.values():
        for link in links:
            if link not in node_degrees:
                node_degrees[link] = 0.0

    default_affinity = 0.5 * RELEVANCE_WEIGHTS["type_affinity"]

    # Pre-populate nested dictionary for ALL type combinations to allow fast O(1)
    # direct dictionary lookups instead of expensive .get() fallbacks in the O(N^2) hot loop
    all_types_observed = set(node_types.values())
    all_types_precomp = set(TYPE_AFFINITY.keys()) | all_types_observed

    type_affinity_precomputed = {}
    for type_a in all_types_precomp:
        type_affinity_precomputed[type_a] = {}
        for type_b in all_types_precomp:
            a_dict = TYPE_AFFINITY.get(type_a)
            if a_dict:
                affinity_val = a_dict.get(type_b, 0.5) * RELEVANCE_WEIGHTS["type_affinity"]
            else:
                affinity_val = default_affinity
            type_affinity_precomputed[type_a][type_b] = affinity_val
    overlap_weight = RELEVANCE_WEIGHTS["source_overlap"]

    node_multipliers = {}
    for key, node in nodes_dict.items():
        decay = node.get("decay_weight", 1.0)
        align = node.get("alignment_score", 100.0) / 100.0
        if align < 0.1:
            align = 0.1
        node_multipliers[key] = math.sqrt(decay * align)

    source_to_nodes = _graph_source_index(nodes_dict)
            
    _temp_reverse_links = {}
    for key, links in node_links.items():
        for link in links:
            _temp_reverse_links.setdefault(link, []).append(key)

    reverse_links = {k: frozenset(v) for k, v in _temp_reverse_links.items()}

    for key_a in node_keys:
        links_a = node_links[key_a]
        sources_a = node_sources[key_a]
        type_a = node_types[key_a]
        triples_a = node_triples[key_a]
        multiplier_a = node_multipliers[key_a]
        affinity_dict_a = type_affinity_precomputed[type_a]
        reverse_links_a = reverse_links.get(key_a, frozenset())

        candidate_source_overlaps = {}
        candidate_neighbor_scores = {}
        
        for source in sources_a:
            for key_b in source_to_nodes.get(source, []):
                if key_a < key_b:
                    candidate_source_overlaps[key_b] = candidate_source_overlaps.get(key_b, 0) + 1
                    
        for neighbor in links_a:
            for key_b in reverse_links.get(neighbor, []):
                if key_a < key_b:
                    candidate_neighbor_scores[key_b] = candidate_neighbor_scores.get(key_b, 0.0) + node_degrees[neighbor]

        candidates = set(candidate_source_overlaps.keys())
        candidates.update(candidate_neighbor_scores.keys())
        
        for key_b in links_a:
            if key_a < key_b:
                candidates.add(key_b)
        
        for key_b in reverse_links.get(key_a, []):
            if key_a < key_b:
                candidates.add(key_b)

        weighted_targets = []
        for key_b in candidates:
            if key_b not in node_links:
                continue

            type_b = node_types[key_b]
            multiplier_b = node_multipliers[key_b]

            score = 0.0

            if key_b in links_a:
                score += triples_a[key_b]

            if key_b in reverse_links_a:
                score += node_triples[key_b][key_a]

            if key_b in candidate_source_overlaps:
                score += candidate_source_overlaps[key_b] * overlap_weight

            if key_b in candidate_neighbor_scores:
                score += candidate_neighbor_scores[key_b]

            score += affinity_dict_a[type_b]

            score *= multiplier_a * multiplier_b
            relevance = round(score, 3)

            if relevance >= 1.5:
                weighted_targets.append((relevance, key_b))
        edges.extend(_bounded_node_edge_candidates(key_a, weighted_targets))

    for node in nodes_dict.values():
        node.pop("_key", None)

    # --- Incorporate zero-LLM edges from SQLite ---
    try:
        from vector_lake.db_store import get_connection
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT source_id, target_id, weight FROM claim_graph_edges "
            "UNION SELECT source_id, target_id, weight FROM page_graph_edges"
        )
        db_edges = cursor.fetchall()
    except Exception as exc:
        raise RuntimeError(
            "Canonical graph-edge query failed; projection publication aborted."
        ) from exc
    for row in db_edges:
        src = row["source_id"]
        tgt = row["target_id"]
        if src in nodes_dict and tgt in nodes_dict:
            edges.append({
                "source": src,
                "target": tgt,
                "weight": float(row["weight"]) if row["weight"] else 1.0,
            })

    return _prune_weighted_edges(edges, set(node_keys))


def _initialize_graph_topology_pending(index_data: dict, reason: str) -> None:
    """Invalidate derived topology while keeping deterministic search-safe defaults."""
    _mark_graph_dirty(index_data, reason)
    index_data["communities"] = {}
    index_data["community_labels"] = {}
    index_data["graph_insights"] = []
    for node in (index_data.get("nodes") or {}).values():
        node["centrality_score"] = 1.0
        node["node_score"] = round(float(node.get("decay_weight", 1.0) or 0.0), 4)


def _stable_community_id(members: list[str]) -> str:
    payload = "\0".join(sorted(members))
    return "community_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def _apply_graph_topology(index_data: dict):
    """Compute bounded, deterministic topology without mutating Wiki or governance state."""
    nodes_dict = index_data.get("nodes") or {}
    node_keys = sorted(nodes_dict)
    index_data["weighted_edges"] = _prune_weighted_edges(
        index_data.get("weighted_edges") or [],
        set(node_keys),
    )

    adjacency: dict[str, dict[str, float]] = {key: {} for key in node_keys}
    for edge in index_data["weighted_edges"]:
        source = edge["source"]
        target = edge["target"]
        weight = max(0.0, float(edge.get("weight", 1.0) or 0.0))
        adjacency[source][target] = max(weight, adjacency[source].get(target, 0.0))
        adjacency[target][source] = max(weight, adjacency[target].get(source, 0.0))

    weighted_degrees = {
        key: sum(neighbors.values())
        for key, neighbors in adjacency.items()
    }
    nonzero_degrees = [value for value in weighted_degrees.values() if value > 0]
    average_weighted_degree = (
        sum(nonzero_degrees) / len(nonzero_degrees)
        if nonzero_degrees
        else 1.0
    )
    for node_key in node_keys:
        relative_degree = weighted_degrees[node_key] / average_weighted_degree
        centrality = min(5.0, relative_degree) if relative_degree > 0 else 0.1
        node = nodes_dict[node_key]
        node["centrality_score"] = round(centrality, 4)
        decay = float(node.get("decay_weight", 1.0) or 0.0)
        node["node_score"] = round(decay * centrality, 4)

    components: list[list[str]] = []
    unseen = set(node_keys)
    while unseen:
        root = min(unseen)
        stack = [root]
        unseen.remove(root)
        component = []
        while stack:
            current = stack.pop()
            component.append(current)
            for neighbor in adjacency[current]:
                if neighbor in unseen:
                    unseen.remove(neighbor)
                    stack.append(neighbor)
        components.append(sorted(component))
    components.sort(key=lambda members: (-len(members), members[0] if members else ""))

    raw_partition: dict[str, object] = {}
    if index_data["weighted_edges"]:
        try:
            raw_partition = _louvain_partition_in_subprocess(
                node_keys,
                index_data["weighted_edges"],
            )
        except Exception as exc:
            log.warning(
                "Isolated Louvain analysis failed; using connected components: %s",
                exc,
            )

    if not raw_partition:
        raw_partition = {
            node_key: component_index
            for component_index, members in enumerate(components)
            for node_key in members
        }

    grouped: dict[object, list[str]] = {}
    for node_key in node_keys:
        grouped.setdefault(raw_partition.get(node_key, node_key), []).append(node_key)

    communities: dict[str, str] = {}
    labels: dict[str, str] = {}
    for members in grouped.values():
        members = sorted(members)
        community_id = _stable_community_id(members)
        for node_key in members:
            communities[node_key] = community_id
        hubs = sorted(
            members,
            key=lambda key: (-weighted_degrees.get(key, 0.0), key),
        )[:2]
        hub_titles = [str(nodes_dict[key].get("title") or key) for key in hubs]
        labels[community_id] = " / ".join(hub_titles) or community_id

    insights: list[dict] = []
    isolated_nodes = [key for key in node_keys if not adjacency[key]]
    for node_key in isolated_nodes[:15]:
        insights.append({
            "type": "isolated_node",
            "node": node_key,
            "description": f"{node_key} has no retained semantic graph edges.",
        })
    for members in (members for members in components if 1 < len(members) <= 5):
        if len(insights) >= 23:
            break
        insights.append({
            "type": "sparse_community",
            "nodes": members,
            "description": f"Sparse component contains {len(members)} nodes.",
        })
    if len(components) > 1 and len(insights) < MAX_GRAPH_INSIGHTS:
        largest_size = len(components[0]) if components else 0
        insights.append({
            "type": "fragmented_graph",
            "nodes": components[0][:5] if components else [],
            "description": (
                f"Graph contains {len(components)} connected components; "
                f"the largest contains {largest_size} of {len(node_keys)} nodes."
            ),
        })

    index_data["communities"] = communities
    index_data["community_labels"] = labels
    index_data["graph_insights"] = insights[:MAX_GRAPH_INSIGHTS]
    _mark_graph_clean(
        index_data,
        reason=f"Topology analysis complete: {len(labels)} communities",
    )


def _generate_index_unlocked(
    skip_embeddings: bool = True,
    *,
    invalidate_embedding_ids: Iterable[str] = (),
):
    index_data = _empty_index_data()
    from vector_lake.db_store import get_connection
    conn = get_connection()
    canonical_before = canonical_runtime_generation_snapshot()

    # Read from canonical SQLite instead of Markdown files
    rows = conn.execute("SELECT entity_id, data_json FROM entities").fetchall()
    
    for row in rows:
        try:
            entity_data = json.loads(row["data_json"])
            node_key, node_data = _entity_to_index_node(entity_data, row["entity_id"])
        except Exception as exc:
            raise RuntimeError(
                "Canonical entity projection failed for "
                f"{row['entity_id']}; projection publication aborted."
            ) from exc
        if node_key.startswith("System_") or node_data.get("type") == "system":
            continue

        index_data["nodes"][node_key] = node_data
        if node_data["id"]:
            index_data["aliases"][node_data["id"]] = node_key
        if node_data.get("title"):
            index_data["aliases"][node_data["title"]] = node_key
        index_data["aliases"][node_key] = node_key
        for alias in node_data.get("aliases", []):
            index_data["aliases"][alias] = node_key
        if isinstance(node_data.get("categories"), list):
            for category in node_data["categories"]:
                index_data["categories"].add(category)

    index_data["weighted_edges"] = _calculate_weighted_edges(index_data)
    _initialize_graph_topology_pending(
        index_data,
        "Index generated, awaiting bounded topology analysis",
    )
    
    index_data["categories"] = list(index_data["categories"])
    index_data["governance_metrics"] = governance_metrics.compute_debt_metrics(skip_heavy=True)
    index_data["schema_version"] = "8.0"

    output_path = str(get_index_path())
    claim_graph_data = governance_store.build_claim_graph_projection()
    canonical_after = canonical_runtime_generation_snapshot()
    if canonical_before != canonical_after:
        raise ProjectionCanonicalGenerationChanged(
            "Canonical runtime generation changed while rebuilding projections; retry."
        )
    # Embeddings are a separate resumable projection. Index rebuilds never call an external API.
    search_upserts = _search_projection_upserts(index_data)
    vector_conn = db_store.get_vector_connection()
    existing_embedding_ids = {
        str(row["entity_id"])
        for row in vector_conn.execute(
            "SELECT entity_id FROM vec_embeddings"
        ).fetchall()
    }
    stale_embedding_ids = existing_embedding_ids - set(index_data["nodes"])
    stale_embedding_ids.update(
        str(entity_id)
        for entity_id in invalidate_embedding_ids
        if str(entity_id)
    )
    tmp_output, tmp_claim, _ = _stage_projection_pair(
        output_path,
        index_data,
        claim_graph_data,
        _verified_canonical_generation(canonical_before),
    )
    try:
        with db_store.transaction():
            _assert_canonical_generation(
                canonical_before,
                context="after acquiring the SQLite write lock",
            )
            db_store.apply_search_projection_mutations(
                vector_conn,
                upserts=search_upserts,
                embedding_deletes=stale_embedding_ids,
                reset_search=True,
            )
        _publish_staged_projection_pair_guarded(
            output_path,
            tmp_output,
            tmp_claim,
            canonical_before,
        )
    except Exception:
        _cleanup_projection_stages(tmp_claim, tmp_output)
        raise

    log.info(
        f"Generated index.json with {len(index_data['nodes'])} nodes | "
        f"{len(index_data['weighted_edges'])} weighted edges | "
        f"{len((index_data.get('error_log') or []))} errors."
    )
    return output_path


def generate_index(
    skip_embeddings: bool = True,
    *,
    invalidate_embedding_ids: Iterable[str] = (),
):
    """Build and publish every index projection under the shared publish lock."""
    output_path = str(get_index_path())
    try:
        with FileLock(output_path + ".lock", timeout=15):
            return _generate_index_unlocked(
                skip_embeddings=skip_embeddings,
                invalidate_embedding_ids=invalidate_embedding_ids,
            )
    except Timeout as exc:
        raise TimeoutError(f"Timeout while acquiring lock for {output_path}") from exc


def _claim_graph_signatures(items: list[dict]) -> set[str]:
    return {
        json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        for item in items
    }


class ProjectionSnapshotChanged(RuntimeError):
    """Raised when a projection changes repeatedly during a read snapshot."""


def _projection_file_identity(path: str) -> tuple[int, ...]:
    stat = os.stat(path)
    return (
        int(stat.st_dev),
        int(stat.st_ino),
        int(stat.st_size),
        int(stat.st_mtime_ns),
        int(stat.st_ctime_ns),
    )


def _read_claim_graph_snapshot(path: str) -> dict:
    """Read an atomically published graph without waiting on the writer lock."""
    if not os.path.exists(path):
        return {"nodes": [], "edges": []}
    last_error = None
    for attempt in range(3):
        try:
            before = _projection_file_identity(path)
            with open(path, "r", encoding="utf-8") as handle:
                observed = json.load(handle)
            after = _projection_file_identity(path)
            if before == after:
                return observed
            last_error = ProjectionSnapshotChanged(
                f"Projection changed while reading {path}"
            )
        except FileNotFoundError as exc:
            if not os.path.exists(path):
                return {"nodes": [], "edges": []}
            last_error = exc
        except (OSError, json.JSONDecodeError) as exc:
            last_error = exc
        if attempt < 2:
            time.sleep(0.01)
    assert last_error is not None
    raise last_error


def claim_graph_projection_parity(*, connection=None) -> dict[str, int]:
    """Compare exact claim-graph node and edge payloads with canonical SQLite."""
    expected = (
        governance_store.build_claim_graph_projection()
        if connection is None
        else governance_store.build_claim_graph_projection(
            connection=connection,
        )
    )
    observed = _read_claim_graph_snapshot(str(get_claim_graph_path()))

    expected_nodes = _claim_graph_signatures(expected.get("nodes") or [])
    observed_nodes = _claim_graph_signatures(observed.get("nodes") or [])
    expected_edges = _claim_graph_signatures(expected.get("edges") or [])
    observed_edges = _claim_graph_signatures(observed.get("edges") or [])
    return {
        "canonical_nodes": len(expected_nodes),
        "projection_nodes": len(observed_nodes),
        "missing_nodes": len(expected_nodes - observed_nodes),
        "extra_nodes": len(observed_nodes - expected_nodes),
        "canonical_edges": len(expected_edges),
        "projection_edges": len(observed_edges),
        "missing_edges": len(expected_edges - observed_edges),
        "extra_edges": len(observed_edges - expected_edges),
    }


def refresh_claim_graph_projection() -> str:
    """Refresh the graph while publishing a new consistent projection pair."""
    output_path = str(get_index_path())
    claim_graph_path = str(get_claim_graph_path())
    try:
        with FileLock(output_path + ".lock", timeout=15):
            canonical_before = canonical_runtime_generation_snapshot()
            try:
                index_data, canonical_binding = (
                    _read_verified_projection_for_writer_unlocked(
                        output_path,
                        canonical_before,
                        connection=db_store.get_connection(),
                    )
                )
            except ProjectionPairContractError as exc:
                log.warning(
                    "Claim-graph refresh cannot reuse the existing projection "
                    "pair (%s); using a full rebuild.",
                    exc,
                )
                _generate_index_unlocked()
            else:
                claim_graph_data = governance_store.build_claim_graph_projection()
                canonical_after = canonical_runtime_generation_snapshot()
                if canonical_before != canonical_after:
                    raise ProjectionCanonicalGenerationChanged(
                        "Canonical runtime generation changed during claim-graph "
                        "projection refresh; retry."
                    )
                _publish_projection_pair(
                    output_path,
                    index_data,
                    claim_graph_data,
                    canonical_binding,
                    canonical_before,
                )
    except Timeout as exc:
        raise TimeoutError(f"Timeout while acquiring lock for {output_path}") from exc
    return claim_graph_path


def update_index_items(filenames: list[str]):
    if not filenames:
        return

    excluded = {
        "index.md",
        "log.md",
        "overview.md",
        "orphan_pages.md",
        "wiki_link_stats.md",
        "synthesis_log.md",
    }
    valid_filenames = [
        filename
        for filename in filenames
        if filename.casefold().endswith(".md")
        and filename.casefold() not in excluded
        and not filename.casefold().startswith("system_")
    ]
    if not valid_filenames:
        return

    incremental_limit = max(
        1,
        int(os.environ.get("VECTOR_LAKE_INCREMENTAL_INDEX_LIMIT", "250")),
    )
    if len(valid_filenames) > incremental_limit:
        log.info(
            "Incremental index batch has %s items, above limit %s; using one full rebuild.",
            len(valid_filenames),
            incremental_limit,
        )
        return generate_index(
            invalidate_embedding_ids={
                filename[:-3] for filename in valid_filenames
            }
        )

    canonical_before = canonical_runtime_generation_snapshot()
    pre_parsed_data = {}
    canonical_load_errors = {}
    conn = db_store.get_connection()
    for filename in valid_filenames:
        node_key = filename[:-3]
        try:
            row = conn.execute(
                "SELECT entity_id, data_json FROM entities "
                "WHERE json_extract(data_json, '$.page_key') = ? LIMIT 1",
                (node_key,),
            ).fetchone()
            if row:
                projected_key, node_data = _entity_to_index_node(
                    json.loads(row["data_json"]),
                    row["entity_id"],
                )
                if projected_key != node_key:
                    raise ValueError(
                        "Canonical page_key mismatch: "
                        f"expected {node_key}, got {projected_key}"
                    )
            else:
                node_data = None
        except Exception as exc:
            log.error("Failed to load canonical entity for %s: %s", filename, exc)
            canonical_load_errors[filename] = str(exc)
            continue
        if node_data:
            pre_parsed_data[node_key] = node_data

    if canonical_load_errors:
        detail = "; ".join(
            f"{name}: {error}"
            for name, error in sorted(canonical_load_errors.items())
        )
        raise RuntimeError(
            "Canonical index batch aborted; source rows could not be loaded: "
            f"{detail}"
        )

    output_path = str(get_index_path())
    if not os.path.exists(output_path):
        return generate_index(
            invalidate_embedding_ids={
                filename[:-3] for filename in valid_filenames
            }
        )

    lock_path = output_path + ".lock"
    needs_full_rebuild = False
    try:
        with FileLock(lock_path, timeout=15):
            try:
                index_data, canonical_binding = (
                    _read_verified_projection_for_writer_unlocked(
                        output_path,
                        canonical_before,
                        connection=conn,
                    )
                )
            except ProjectionPairContractError as exc:
                log.warning(
                    "Incremental index update cannot reuse the existing "
                    "projection pair (%s); using a full rebuild.",
                    exc,
                )
                index_data = None
                needs_full_rebuild = True

            if index_data is None:
                needs_full_rebuild = True
            else:
                search_deletes = set(_strip_system_nodes(index_data))
                embedding_deletes = set(search_deletes)
                removed_legacy_keys = _strip_legacy_embedded_payloads(index_data)
                if removed_legacy_keys:
                    log.info(
                        "Detected legacy embedded governance payloads in index.json "
                        "(%s). Triggering full rebuild.",
                        ", ".join(removed_legacy_keys),
                    )
                    needs_full_rebuild = True
                    index_data = None

            if index_data is not None:
                index_data.setdefault("nodes", {})
                index_data.setdefault("aliases", {})
                index_data.setdefault("error_log", [])
                touched_node_keys = {filename[:-3] for filename in valid_filenames}
                active_node_keys = set()
                search_upserts = {}

                for filename in valid_filenames:
                    node_key = filename[:-3]
                    index_data["aliases"] = {
                        key: value
                        for key, value in (index_data.get("aliases") or {}).items()
                        if value != node_key
                    }
                    index_data["error_log"] = [
                        item
                        for item in index_data["error_log"]
                        if item.get("file") != filename
                    ]

                    if not filename.startswith(VALID_PREFIXES):
                        index_data["error_log"].append(
                            {
                                "file": filename,
                                "error": (
                                    "Schema violation: Missing valid entity prefix."
                                ),
                            }
                        )
                        log.warning(
                            "Schema violation in %s during partial update.",
                            filename,
                        )
                        index_data["nodes"].pop(node_key, None)
                        search_deletes.add(node_key)
                        embedding_deletes.add(node_key)
                        continue

                    node_data = pre_parsed_data.get(node_key)
                    if node_data is None:
                        index_data["nodes"].pop(node_key, None)
                        search_deletes.add(node_key)
                        embedding_deletes.add(node_key)
                        continue

                    index_data["nodes"][node_key] = node_data
                    active_node_keys.add(node_key)
                    search_upserts[node_key] = _search_projection_row(
                        node_key,
                        node_data,
                    )
                    embedding_deletes.add(node_key)
                    if node_data.get("id"):
                        index_data["aliases"][node_data["id"]] = node_key
                    if node_data.get("title"):
                        index_data["aliases"][node_data["title"]] = node_key
                    index_data["aliases"][node_key] = node_key
                    for alias in node_data.get("aliases") or []:
                        index_data["aliases"][alias] = node_key

                index_data["weighted_edges"] = [
                    edge
                    for edge in (index_data.get("weighted_edges") or [])
                    if edge.get("source") not in touched_node_keys
                    and edge.get("target") not in touched_node_keys
                ]

                all_nodes = index_data["nodes"]
                all_nodes_triples = {}
                for node_key, node in all_nodes.items():
                    all_nodes_triples[node_key] = {
                        triple["target"]: triple.get("predicate", "mentions")
                        for triple in (node.get("triples") or [])
                        if triple.get("target")
                    }
                allowed_graph_sources = set(_graph_source_index(all_nodes))

                for node_key in sorted(active_node_keys):
                    try:
                        manual_rows = conn.execute(
                            "SELECT source_id, target_id, weight "
                            "FROM claim_graph_edges "
                            "WHERE source_id = ? OR target_id = ? "
                            "UNION SELECT source_id, target_id, weight "
                            "FROM page_graph_edges "
                            "WHERE source_id = ? OR target_id = ?",
                            (node_key, node_key, node_key, node_key),
                        ).fetchall()
                        for row in manual_rows:
                            index_data["weighted_edges"].append(
                                {
                                    "source": row["source_id"],
                                    "target": row["target_id"],
                                    "weight": (
                                        float(row["weight"])
                                        if row["weight"]
                                        else 1.0
                                    ),
                                }
                            )
                    except Exception as exc:
                        raise RuntimeError(
                            "Canonical graph-edge query failed for incremental "
                            f"node {node_key}; projection publication aborted."
                        ) from exc

                    node_data = all_nodes[node_key]
                    node_data["_key"] = node_key
                    node_links = set(node_data.get("links") or [])
                    node_sources = {
                        source
                        for source in (node_data.get("sources") or [])
                        if str(source) in allowed_graph_sources
                    }
                    triples_a = all_nodes_triples.get(node_key) or {}
                    for other_key, other_node in all_nodes.items():
                        if other_key == node_key:
                            continue
                        other_links = set(other_node.get("links") or [])
                        other_sources = {
                            source
                            for source in (other_node.get("sources") or [])
                            if str(source) in allowed_graph_sources
                        }
                        if not (
                            other_key in node_links
                            or node_key in other_links
                            or not node_sources.isdisjoint(other_sources)
                            or not node_links.isdisjoint(other_links)
                        ):
                            continue
                        other_node["_key"] = other_key
                        relevance = calculate_relevance(
                            node_data,
                            other_node,
                            all_nodes,
                            links_a=node_links,
                            links_b=other_links,
                            sources_a=node_sources,
                            sources_b=other_sources,
                            triples_a=triples_a,
                            triples_b=all_nodes_triples.get(other_key),
                        )
                        if relevance >= 1.5:
                            index_data["weighted_edges"].append(
                                {
                                    "source": min(node_key, other_key),
                                    "target": max(node_key, other_key),
                                    "weight": relevance,
                                }
                            )
                        other_node.pop("_key", None)
                    node_data.pop("_key", None)

                _initialize_graph_topology_pending(
                    index_data,
                    f"Partial batch update for {len(valid_filenames)} items",
                )
                index_data["categories"] = sorted(
                    {
                        category
                        for node in all_nodes.values()
                        for category in (node.get("categories") or [])
                    }
                )
                index_data["governance_metrics"] = (
                    index_data.get("governance_metrics") or {}
                )
                index_data["schema_version"] = "8.0"

                claim_graph_data = governance_store.build_claim_graph_projection()
                canonical_after = canonical_runtime_generation_snapshot()
                if canonical_before != canonical_after:
                    raise ProjectionCanonicalGenerationChanged(
                        "Canonical runtime generation changed during incremental "
                        "projection computation; retry."
                    )
                vector_conn = db_store.get_vector_connection()
                tmp_output, tmp_claim, _ = _stage_projection_pair(
                    output_path,
                    index_data,
                    claim_graph_data,
                    canonical_binding,
                )
                try:
                    with db_store.transaction():
                        _assert_canonical_generation(
                            canonical_before,
                            context="after acquiring the SQLite write lock",
                        )
                        db_store.apply_search_projection_mutations(
                            vector_conn,
                            upserts=[
                                search_upserts[key]
                                for key in sorted(search_upserts)
                            ],
                            search_deletes=search_deletes,
                            embedding_deletes=embedding_deletes,
                        )
                    _publish_staged_projection_pair_guarded(
                        output_path,
                        tmp_output,
                        tmp_claim,
                        canonical_before,
                    )
                except Exception:
                    _cleanup_projection_stages(tmp_claim, tmp_output)
                    raise
    except Timeout as exc:
        raise TimeoutError(
            f"Timeout while acquiring lock for {output_path}"
        ) from exc

    if needs_full_rebuild:
        return generate_index(
            invalidate_embedding_ids={
                filename[:-3] for filename in valid_filenames
            }
        )


def update_index_item(filename: str):
    """Legacy single file entrypoint."""
    return update_index_items([filename])


def refresh_graph_topology_if_dirty() -> bool:
    output_path = str(get_index_path())
    if not os.path.exists(output_path):
        generate_index()
        return True

    lock_path = output_path + ".lock"
    needs_full_rebuild = False
    refreshed = False
    try:
        with FileLock(lock_path, timeout=15):
            canonical_before = canonical_runtime_generation_snapshot()
            try:
                index_data, canonical_binding = (
                    _read_verified_projection_for_writer_unlocked(
                        output_path,
                        canonical_before,
                        connection=db_store.get_connection(),
                    )
                )
            except ProjectionPairContractError as exc:
                log.warning(
                    "Topology refresh cannot reuse the existing projection "
                    "pair (%s); using a full rebuild.",
                    exc,
                )
                index_data = None
                needs_full_rebuild = True

            if index_data is None:
                needs_full_rebuild = True
            else:
                removed_system_keys = set(_strip_system_nodes(index_data))
                removed_legacy_keys = _strip_legacy_embedded_payloads(index_data)
                if removed_legacy_keys:
                    log.info(
                        "Detected legacy embedded governance payloads during "
                        "graph refresh (%s). Triggering full rebuild.",
                        ", ".join(removed_legacy_keys),
                    )
                    needs_full_rebuild = True
                else:
                    if removed_system_keys and not is_graph_dirty(index_data):
                        _mark_graph_dirty(
                            index_data,
                            "Legacy system nodes removed during topology refresh",
                        )
                    if is_graph_dirty(index_data):
                        index_data["weighted_edges"] = _calculate_weighted_edges(
                            index_data
                        )
                        _apply_graph_topology(index_data)
                        claim_graph_data = (
                            governance_store.build_claim_graph_projection()
                        )
                        canonical_after = canonical_runtime_generation_snapshot()
                        if canonical_before != canonical_after:
                            raise ProjectionCanonicalGenerationChanged(
                                "Canonical runtime generation changed during "
                                "topology projection computation; retry."
                            )
                        mutation_conn = (
                            db_store.get_vector_connection()
                            if removed_system_keys
                            else db_store.get_connection()
                        )
                        tmp_output, tmp_claim, _ = _stage_projection_pair(
                            output_path,
                            index_data,
                            claim_graph_data,
                            canonical_binding,
                        )
                        try:
                            with db_store.transaction():
                                _assert_canonical_generation(
                                    canonical_before,
                                    context=(
                                        "after acquiring the SQLite write lock"
                                    ),
                                )
                                if removed_system_keys:
                                    db_store.apply_search_projection_mutations(
                                        mutation_conn,
                                        search_deletes=removed_system_keys,
                                        embedding_deletes=removed_system_keys,
                                    )
                            _publish_staged_projection_pair_guarded(
                                output_path,
                                tmp_output,
                                tmp_claim,
                                canonical_before,
                            )
                        except Exception:
                            _cleanup_projection_stages(tmp_claim, tmp_output)
                            raise
                        log.info(
                            "Graph topology refreshed outside the SQLite write "
                            "transaction and published."
                        )
                        refreshed = True
    except Timeout:
        log.error("Timeout while acquiring lock for %s", output_path)
        return False

    if needs_full_rebuild:
        generate_index()
        return True
    return refreshed


if __name__ == "__main__":
    generate_index()

