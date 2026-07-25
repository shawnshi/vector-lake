import json
import hashlib
import logging
from collections import defaultdict
import math
import os
import re
import time
import uuid
from datetime import datetime, timezone

from filelock import FileLock, Timeout

from vector_lake import governance_metrics
from vector_lake import governance_store
from vector_lake import db_store
from vector_lake.wiki_utils import get_claim_graph_path, get_index_path, get_wiki_dir, read_markdown_file, VALID_PREFIXES

from vector_lake.schema_validator import validate_schema, SchemaViolationException

try:
    import networkx as nx
    from community import community_louvain
except ImportError:
    nx = None
    community_louvain = None


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


def _normalize_graph_source(source: object) -> str:
    value = str(source or "").strip().replace("\\", "/")
    if value.startswith("[[") and value.endswith("]]" ):
        value = value[2:-2].split("|", 1)[0]
    value = value.rsplit("/", 1)[-1]
    if value.lower().endswith(".md"):
        value = value[:-3]
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


def index_projection_matches_canonical(
    filenames: list[str],
    allowed_alias_redirects: dict[str, str] | None = None,
) -> bool:
    """Prove that selected canonical pages are already reflected in index.json."""
    allowed_alias_redirects = allowed_alias_redirects or {}
    page_keys = {
        filename[:-3]
        for filename in filenames
        if filename.endswith(".md") and not filename.startswith("System_")
    }
    if not page_keys or not get_index_path().exists():
        return False
    lock_path = str(get_index_path()) + ".lock"
    try:
        with FileLock(lock_path, timeout=15):
            index_data = _load_index_unlocked(str(get_index_path()))
    except (Timeout, OSError, json.JSONDecodeError):
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
    # ⚡ Bolt: Use defaultdict(int) to optimize high-frequency counting
    counts: dict[str, int] = defaultdict(int)
    pruned: list[dict] = []
    for edge in _deduplicate_weighted_edges(edges):
        source = edge["source"]
        target = edge["target"]
        if node_keys is not None and (source not in node_keys or target not in node_keys):
            continue
        if counts[source] >= max_edges_per_node:
            continue
        if counts[target] >= max_edges_per_node:
            continue
        pruned.append(edge)
        counts[source] += 1
        counts[target] += 1
    return pruned


def _write_index(output_path: str, index_data: dict):
    removed = _strip_legacy_embedded_payloads(index_data)
    if removed:
        log.info(f"Stripped legacy embedded payloads before writing index: {', '.join(removed)}")
    index_data["weighted_edges"] = _prune_weighted_edges(
        index_data.get("weighted_edges") or [],
        set((index_data.get("nodes") or {}).keys()),
    )
    _write_json_payload(output_path, index_data)


def _write_claim_graph(output_path: str, claim_graph_data: dict):
    _write_json_payload(output_path, claim_graph_data)


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
        target = match.group(2).split("|")[0].strip().replace(".md", "")
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

        # ⚡ Bolt: Use defaultdict(int) and defaultdict(float) for faster frequency counting
        candidate_source_overlaps = defaultdict(int)
        candidate_neighbor_scores = defaultdict(float)
        
        for source in sources_a:
            for key_b in source_to_nodes.get(source, []):
                if key_a < key_b:
                    candidate_source_overlaps[key_b] += 1
                    
        for neighbor in links_a:
            for key_b in reverse_links.get(neighbor, []):
                if key_a < key_b:
                    candidate_neighbor_scores[key_b] += node_degrees[neighbor]

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
        for row in db_edges:
            src = row["source_id"]
            tgt = row["target_id"]
            if src in nodes_dict and tgt in nodes_dict:
                edges.append({
                    "source": src,
                    "target": tgt,
                    "weight": float(row["weight"]) if row["weight"] else 1.0,
                })
    except Exception as e:
        import logging
        logging.getLogger("vector-lake-indexer").debug(f"Could not load graph edges from SQLite: {e}")

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
    if nx is not None and community_louvain is not None and index_data["weighted_edges"]:
        graph = nx.Graph()
        graph.add_nodes_from(node_keys)
        graph.add_weighted_edges_from(
            (
                edge["source"],
                edge["target"],
                float(edge.get("weight", 1.0) or 1.0),
            )
            for edge in index_data["weighted_edges"]
        )
        try:
            raw_partition = community_louvain.best_partition(
                graph,
                weight="weight",
                random_state=0,
            )
        except Exception as exc:
            log.warning("Louvain topology analysis failed; using connected components: %s", exc)

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


def _generate_index_unlocked(skip_embeddings: bool = True):
    index_data = _empty_index_data()
    from vector_lake.db_store import get_connection
    conn = get_connection()

    # Read from canonical SQLite instead of Markdown files
    rows = conn.execute("SELECT entity_id, data_json FROM entities").fetchall()
    
    for row in rows:
        try:
            entity_data = json.loads(row["data_json"])
            node_key, node_data = _entity_to_index_node(entity_data, row["entity_id"])
        except Exception as e:
            log.warning(f"Failed to project canonical entity {row['entity_id']}: {e}")
            continue
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

    from vector_lake.db_store import transaction
    index_data["weighted_edges"] = _calculate_weighted_edges(index_data)
    _initialize_graph_topology_pending(
        index_data,
        "Index generated, awaiting bounded topology analysis",
    )
    
    index_data["categories"] = list(index_data["categories"])
    index_data["governance_metrics"] = governance_metrics.compute_debt_metrics(skip_heavy=True)
    index_data["schema_version"] = "8.0"

    output_path = str(get_index_path())
    claim_graph_path = str(get_claim_graph_path())
    stage_suffix = f".{uuid.uuid4().hex}.tmp"
    tmp_output = output_path + stage_suffix
    tmp_claim = claim_graph_path + stage_suffix
    
    _write_json_stage(tmp_claim, governance_store.build_claim_graph_projection())
    removed = _strip_legacy_embedded_payloads(index_data)
    if removed:
        log.info(f"Stripped legacy embedded payloads before writing index: {', '.join(removed)}")
    _write_json_stage(tmp_output, index_data)

    # Embeddings are a separate resumable projection. Index rebuilds never call an external API.
    embeddings_map = {}
    with transaction():
        conn.execute("DELETE FROM wiki_search_index")
        db_store.delete_stale_embeddings(set(index_data["nodes"]))
        _build_bm25_index(index_data, embeddings_map)

    for staged_path in (tmp_claim, tmp_output):
        if not os.path.exists(staged_path):
            raise FileNotFoundError(f"Missing staged projection file before publish: {staged_path}")
    os.replace(tmp_claim, claim_graph_path)
    os.replace(tmp_output, output_path)

    log.info(
        f"Generated index.json with {len(index_data['nodes'])} nodes | "
        f"{len(index_data['weighted_edges'])} weighted edges | "
        f"{len((index_data.get('error_log') or []))} errors."
    )
    return output_path


def generate_index(skip_embeddings: bool = True):
    """Build and publish every index projection under the shared publish lock."""
    output_path = str(get_index_path())
    try:
        with FileLock(output_path + ".lock", timeout=15):
            return _generate_index_unlocked(skip_embeddings=skip_embeddings)
    except Timeout as exc:
        raise TimeoutError(f"Timeout while acquiring lock for {output_path}") from exc


def _claim_graph_signatures(items: list[dict]) -> set[str]:
    return {
        json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        for item in items
    }


def claim_graph_projection_parity() -> dict[str, int]:
    """Compare exact claim-graph node and edge payloads with canonical SQLite."""
    expected = governance_store.build_claim_graph_projection()
    claim_graph_path = str(get_claim_graph_path())
    output_path = str(get_index_path())
    try:
        with FileLock(output_path + ".lock", timeout=15):
            if os.path.exists(claim_graph_path):
                with open(claim_graph_path, "r", encoding="utf-8") as handle:
                    observed = json.load(handle)
            else:
                observed = {"nodes": [], "links": []}
    except Timeout as exc:
        raise TimeoutError(f"Timeout while acquiring lock for {output_path}") from exc

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
    """Publish claim_graph.json independently under the shared projection lock."""
    output_path = str(get_index_path())
    claim_graph_path = str(get_claim_graph_path())
    try:
        with FileLock(output_path + ".lock", timeout=15):
            _write_claim_graph(claim_graph_path, governance_store.build_claim_graph_projection())
    except Timeout as exc:
        raise TimeoutError(f"Timeout while acquiring lock for {output_path}") from exc
    return claim_graph_path


def update_index_items(filenames: list[str]):
    if not filenames:
        return

    # Filter valid files
    valid_filenames = []
    for filename in filenames:
        if not filename.endswith(".md") or filename in ("index.md", "log.md", "overview.md", "orphan_pages.md", "wiki_link_stats.md", "Synthesis_log.md") or filename.startswith("System_"):
            continue
        valid_filenames.append(filename)
        
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
        return generate_index()

    # Pre-parse canonical nodes. Embedding refresh is handled by the explicit backfill scheduler.
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
                projected_key, node_data = _entity_to_index_node(json.loads(row["data_json"]), row["entity_id"])
                if projected_key != node_key:
                    raise ValueError(f"Canonical page_key mismatch: expected {node_key}, got {projected_key}")
            else:
                node_data = None
        except Exception as exc:
            log.error(f"Failed to load canonical entity for {filename}: {exc}")
            canonical_load_errors[filename] = str(exc)
            continue
        if node_data:
            pre_parsed_data[node_key] = node_data

    if canonical_load_errors:
        detail = "; ".join(f"{name}: {error}" for name, error in sorted(canonical_load_errors.items()))
        raise RuntimeError(f"Canonical index batch aborted; source rows could not be loaded: {detail}")

    output_path = str(get_index_path())
    if not os.path.exists(output_path):
        return generate_index()

    lock_path = output_path + ".lock"
    needs_full_rebuild = False
    try:
        with FileLock(lock_path, timeout=15):
            from vector_lake.db_store import transaction
            with transaction():
                try:
                    index_data = _load_index_unlocked(output_path)
                except json.JSONDecodeError:
                    needs_full_rebuild = True
                    index_data = None
    
                if index_data is None:
                    needs_full_rebuild = True
                else:
                    removed_system_keys = _strip_system_nodes(index_data)
                    for system_key in removed_system_keys:
                        db_store.delete_search_index(system_key)
                    removed_legacy_keys = _strip_legacy_embedded_payloads(index_data)
                    if removed_legacy_keys:
                        log.info(
                            "Detected legacy embedded governance payloads in index.json "
                            f"({', '.join(removed_legacy_keys)}). Triggering full rebuild."
                        )
                        needs_full_rebuild = True
                        index_data = None
    
                if index_data is not None:
                    if isinstance(index_data.get("categories"), list):
                        index_data["categories"] = set(index_data["categories"])
    
                    all_nodes_triples = {}
                    for k, v in (index_data.get("nodes") or {}).items():
                        td = {}
                        for t in (v.get("triples") or []):
                            if t.get("target"):
                                td[t["target"]] = t.get("predicate", "mentions")
                        all_nodes_triples[k] = td
                    source_preview = dict(index_data.get("nodes") or {})
                    source_preview.update(pre_parsed_data)
                    allowed_graph_sources = set(_graph_source_index(source_preview))
    
                    for filename in valid_filenames:
                        node_key = filename[:-3]
    
                        if not filename.startswith(VALID_PREFIXES) and filename not in ("index.md", "log.md"):
                            index_data.setdefault("error_log", [])
                            index_data["error_log"] = [item for item in index_data["error_log"] if item.get("file") != filename]
                            index_data["error_log"].append({"file": filename, "error": "Schema violation: Missing valid entity prefix."})
                            log.warning(f"Schema violation in {filename} during partial update.")
                            old_node = (index_data.get("nodes") or {}).pop(node_key, None)
                            if old_node:
                                db_store.delete_search_index(node_key)
                            index_data["weighted_edges"] = [
                                edge for edge in (index_data.get("weighted_edges") or [])
                                if edge["source"] != node_key and edge["target"] != node_key
                            ]
                        else:
                            index_data["aliases"] = {key: value for key, value in (index_data.get("aliases") or {}).items() if value != node_key}
                            index_data.setdefault("error_log", [])
                            index_data["error_log"] = [item for item in index_data["error_log"] if item.get("file") != filename]
    
                            node_data = pre_parsed_data.get(node_key)
                            if node_data is None:
                                old_node = (index_data.get("nodes") or {}).pop(node_key, None)
                                if old_node:
                                    db_store.delete_search_index(node_key)
                                index_data["weighted_edges"] = [
                                    edge for edge in (index_data.get("weighted_edges") or [])
                                    if edge["source"] != node_key and edge["target"] != node_key
                                ]
                            else:
                                if node_data is not None:
                                    index_data["nodes"][node_key] = node_data
                                    aliases_str = " ".join((node_data.get("aliases") or [])) if isinstance(node_data.get("aliases"), list) else ""
                                    text = f"{aliases_str} {node_data.get('raw_text', '')}"
                                    t_title = _tokenize_for_fts(node_data.get('title', ''))
                                    t_summary = _tokenize_for_fts(node_data.get('summary', ''))
                                    t_text = _tokenize_for_fts(text)
                                    db_store.upsert_search_index(node_key, t_title, t_summary, t_text)
                                    # The old vector is now stale; explicit backfill will replace it.
                                    db_store.delete_embedding(node_key)
                                    
                                    if node_data["id"]:
                                        index_data["aliases"][node_data["id"]] = node_key
                                    if node_data.get("title"):
                                        index_data["aliases"][node_data["title"]] = node_key
                                    index_data["aliases"][node_key] = node_key
                                    for alias in node_data["aliases"]:
                                        index_data["aliases"][alias] = node_key
    
                                    if isinstance(node_data["categories"], list):
                                        categories = set((index_data.get("categories") or []))
                                        for category in node_data["categories"]:
                                            categories.add(category)
                                        index_data["categories"] = categories
    
                                    index_data["weighted_edges"] = [
                                        edge for edge in (index_data.get("weighted_edges") or [])
                                        if edge["source"] != node_key and edge["target"] != node_key
                                    ]
                                    # Preserve manual edges from SQLite
                                    try:
                                        conn = db_store.get_connection()
                                        cursor = conn.cursor()
                                        cursor.execute(
                                            "SELECT source_id, target_id, weight FROM claim_graph_edges WHERE source_id = ? OR target_id = ? "
                                            "UNION SELECT source_id, target_id, weight FROM page_graph_edges WHERE source_id = ? OR target_id = ?",
                                            (node_key, node_key, node_key, node_key),
                                        )
                                        for row in cursor.fetchall():
                                            index_data["weighted_edges"].append({
                                                "source": row["source_id"],
                                                "target": row["target_id"],
                                                "weight": float(row["weight"]) if row["weight"] else 1.0,
                                            })
                                    except Exception:
                                        pass
                                    node_data["_key"] = node_key
                                    all_nodes = index_data["nodes"]
    
                                    td = {}
                                    for t in (node_data.get("triples") or []):
                                        if t.get("target"):
                                            td[t["target"]] = t.get("predicate", "mentions")
                                    all_nodes_triples[node_key] = td
                                    triples_a = td
                                    
                                    node_links = set((node_data.get("links") or []))
                                    node_sources = {
                                        source
                                        for source in (node_data.get("sources") or [])
                                        if str(source) in allowed_graph_sources
                                    }
                                    for other_key, other_node in all_nodes.items():
                                        if other_key == node_key:
                                            continue
                                            
                                        other_links = set((other_node.get("links") or []))
                                        other_sources = {
                                            source
                                            for source in (other_node.get("sources") or [])
                                            if str(source) in allowed_graph_sources
                                        }
                                        triples_b = all_nodes_triples.get(other_key)
                                        
                                        has_direct = other_key in node_links or node_key in other_links
                                        # Optimization: Replace bool(set1 & set2) with not isdisjoint() to prevent allocating a new set just to check for overlap
                                        has_source_overlap = not node_sources.isdisjoint(other_sources)
                                        has_common_neighbor = not node_links.isdisjoint(other_links)
                                        
                                        if not (has_direct or has_source_overlap or has_common_neighbor):
                                            continue
    
                                        other_node["_key"] = other_key
                                        relevance = calculate_relevance(
                                            node_data, other_node, all_nodes,
                                            links_a=node_links, links_b=other_links,
                                            sources_a=node_sources, sources_b=other_sources,
                                            triples_a=triples_a, triples_b=triples_b
                                        )
                                        if relevance >= 1.5:
                                            index_data["weighted_edges"].append({
                                                "source": min(node_key, other_key),
                                                "target": max(node_key, other_key),
                                                "weight": relevance,
                                            })
                                        other_node.pop("_key", None)
                                    node_data.pop("_key", None)
    
                    _initialize_graph_topology_pending(
                        index_data,
                        f"Partial batch update for {len(valid_filenames)} items",
                    )
                    index_data["categories"] = list((index_data.get("categories") or []))
                    # Do not recompute heavy debt metrics on partial update
                    index_data["governance_metrics"] = (index_data.get("governance_metrics") or {})
                    index_data["schema_version"] = "8.0"
                    # V11.3 Fixed: Write partial updates back to disk to prevent ghost updates
                    _write_index(output_path, index_data)
                    _write_claim_graph(str(get_claim_graph_path()), governance_store.build_claim_graph_projection())
    except Timeout:
        raise TimeoutError(f"Timeout while acquiring lock for {output_path}")

    if needs_full_rebuild:
        return generate_index()

def update_index_item(filename: str):
    """Legacy single file entrypoint."""
    return update_index_items([filename])


def refresh_graph_topology_if_dirty() -> bool:
    output_path = str(get_index_path())
    if not os.path.exists(output_path):
        generate_index()
        return True

    lock_path = output_path + ".lock"
    try:
        with FileLock(lock_path, timeout=15):
            from vector_lake.db_store import transaction
            with transaction():
                try:
                    index_data = _load_index_unlocked(output_path)
                except json.JSONDecodeError:
                    index_data = None
    
                if index_data is None:
                    _generate_index_unlocked()
                    return True

                removed_system_keys = _strip_system_nodes(index_data)
                for system_key in removed_system_keys:
                    db_store.delete_search_index(system_key)

                removed_legacy_keys = _strip_legacy_embedded_payloads(index_data)
                if removed_legacy_keys:
                    log.info(
                        "Detected legacy embedded governance payloads during graph refresh "
                        f"({', '.join(removed_legacy_keys)}). Triggering full rebuild."
                    )
                    _generate_index_unlocked()
                    return True
    
                if is_graph_dirty(index_data):
                    index_data["weighted_edges"] = _calculate_weighted_edges(index_data)
                    _apply_graph_topology(index_data)
                    temp_path = output_path + ".tmp"
                    with open(temp_path, "w", encoding="utf-8") as handle:
                        json.dump(index_data, handle, ensure_ascii=False, separators=(",", ":"))
                    for attempt in range(5):
                        try:
                            os.replace(temp_path, output_path)
                            break
                        except PermissionError as e:
                            if attempt < 4:
                                time.sleep(0.1 * (2 ** attempt))
                            else:
                                log.error(f"Failed to write {output_path} due to file lock after 5 attempts. Graph refresh aborted.")
                                try:
                                    os.remove(temp_path)
                                except Exception:
                                    pass
                                raise e
                    log.info("Graph topology partially refreshed and saved.")
                    return True
            return False
    except Timeout:
        log.error(f"Timeout while acquiring lock for {output_path}")
        return False


if __name__ == "__main__":
    generate_index()

