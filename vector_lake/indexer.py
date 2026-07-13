import json
import logging
import math
import os
import re
import time
from datetime import datetime, timezone

import yaml
from filelock import FileLock, Timeout

from vector_lake import governance_metrics
from vector_lake import governance_store
from vector_lake import db_store
from vector_lake.wiki_utils import get_claim_graph_path, get_index_path, get_wiki_dir, read_markdown_file
from vector_lake.yaml_utils import load_yaml
from vector_lake.schema_validator import validate_schema, SchemaViolationException

try:
    import networkx as nx
    from community import community_louvain
except ImportError:
    nx = None
    community_louvain = None


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("vector-lake-indexer")

VALID_PREFIXES = ("Concept_", "Vendor_", "Institution_", "Product_", "Person_", "Event_", "Policy_", "Standard_", "Source_", "Synthesis_")

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


from collections import Counter, defaultdict

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
    if not text: return ""
    try:
        import jieba
        return " ".join(jieba.cut(text))
    except ImportError:
        return text

def _compute_embeddings_unlocked(index_data: dict) -> dict:
    embeddings_map = {}
    import os
    if os.environ.get("GEMINI_API_KEY"):
        try:
            from google import genai
            client = genai.Client()
            nodes = (index_data.get("nodes") or {})
            for node_key, node in nodes.items():
                embedding_content = f"{node.get('title', '')} {node.get('summary', '')} {node.get('raw_text', '')}"
                response = client.models.embed_content(
                    model="gemini-embedding-2",
                    contents=embedding_content[:15000]
                )
                if hasattr(response, 'embeddings') and len(response.embeddings) > 0:
                    embeddings_map[node_key] = response.embeddings[0].values
        except Exception as e:
            log.warning(f"Failed to compute embeddings: {e}")
    return embeddings_map

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

def _write_index(output_path: str, index_data: dict):
    removed = _strip_legacy_embedded_payloads(index_data)
    if removed:
        log.info(f"Stripped legacy embedded payloads before writing index: {', '.join(removed)}")
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
        "tension_edges": tension_edges,
        "links": sorted(links),
        "triples": triples,
        "summary": summary_text[:240],
        "raw_text": body,
        "decay_weight": round(decay_weight, 4),
        "alignment_score": round(alignment_score, 2),
    }


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

    if links_a is None: links_a = frozenset((node_a.get("links") or []))
    if links_b is None: links_b = frozenset((node_b.get("links") or []))
    
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

    if sources_a is None: sources_a = frozenset((node_a.get("sources") or []))
    if sources_b is None: sources_b = frozenset((node_b.get("sources") or []))

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

    if type_a is None: type_a = node_a.get("type", "concept").lower()
    if type_b is None: type_b = node_b.get("type", "concept").lower()

    type_a_dict = TYPE_AFFINITY.get(type_a)
    if type_a_dict:
        affinity = type_a_dict.get(type_b, 0.5) * RELEVANCE_WEIGHTS["type_affinity"]
    else:
        affinity = 0.5 * RELEVANCE_WEIGHTS["type_affinity"]
    score += affinity

    if decay_a is None: decay_a = node_a.get("decay_weight", 1.0)
    if decay_b is None: decay_b = node_b.get("decay_weight", 1.0)
    
    if align_a is None:
        align_a = node_a.get("alignment_score", 100.0) / 100.0
        if align_a < 0.1: align_a = 0.1

    if align_b is None:
        align_b = node_b.get("alignment_score", 100.0) / 100.0
        if align_b < 0.1: align_b = 0.1
    
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

    for key, node in nodes_dict.items():
        resolved_links = set()
        for link in (node.get("links") or []):
            if link in alias_map:
                resolved_links.add(alias_map[link])
            else:
                resolved_links.add(link)
        node_links[key] = frozenset(resolved_links)

        node_types[key] = node.get("type", "concept").lower()
        
        td = {}
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

    if "mentions" not in pred_weights:
        pred_weights["mentions"] = get_pred_weight("mentions")
    # ⚡ Bolt: Cache the fallback mention weight to avoid lookups in the hot loop
    mention_weight = pred_weights["mentions"]

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
        if align < 0.1: align = 0.1
        node_multipliers[key] = math.sqrt(decay * align)

    source_to_nodes = {}
    for key, sources in node_sources.items():
        for source in sources:
            source_to_nodes.setdefault(source, []).append(key)
            
    reverse_links = {}
    for key, links in node_links.items():
        for link in links:
            reverse_links.setdefault(link, []).append(key)

    for key_a in node_keys:
        links_a = node_links[key_a]
        sources_a = node_sources[key_a]
        type_a = node_types[key_a]
        triples_a = node_triples[key_a]
        multiplier_a = node_multipliers[key_a]
        affinity_dict_a = type_affinity_precomputed[type_a]

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

        for key_b in candidates:
            if key_b not in node_links:
                continue

            type_b = node_types[key_b]
            multiplier_b = node_multipliers[key_b]

            score = 0.0

            if key_b in links_a:
                score += triples_a.get(key_b, mention_weight)

            if key_a in node_links[key_b]:
                score += node_triples[key_b].get(key_a, mention_weight)

            if key_b in candidate_source_overlaps:
                score += candidate_source_overlaps[key_b] * overlap_weight

            if key_b in candidate_neighbor_scores:
                score += candidate_neighbor_scores[key_b]

            score += affinity_dict_a[type_b]

            score *= multiplier_a * multiplier_b
            relevance = round(score, 3)

            if relevance >= 1.5:
                edges.append({
                    "source": key_a,
                    "target": key_b,
                    "weight": relevance,
                })

    for node in nodes_dict.values():
        node.pop("_key", None)

    # --- Incorporate zero-LLM edges from SQLite ---
    try:
        from vector_lake.db_store import get_connection
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT source_id, target_id, weight FROM claim_graph_edges")
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
        logging.getLogger("vector-lake-indexer").debug(f"Could not load claim_graph_edges from SQLite: {e}")

    edges.sort(key=lambda edge: edge["weight"], reverse=True)

    # --- Top-K Edge Pruning to prevent Force Collapse ---
    MAX_EDGES_PER_NODE = 15
    node_edge_counts = {k: 0 for k in node_keys}
    pruned_edges = []
    
    for edge in edges:
        src = edge["source"]
        tgt = edge["target"]
        if node_edge_counts.get(src, 0) < MAX_EDGES_PER_NODE and node_edge_counts.get(tgt, 0) < MAX_EDGES_PER_NODE:
            pruned_edges.append(edge)
            node_edge_counts[src] += 1
            node_edge_counts[tgt] += 1

    return pruned_edges


def _apply_graph_topology(index_data: dict):
    if "graph_state" not in index_data:
        index_data["graph_state"] = {}
    index_data["graph_state"]["dirty"] = True
    index_data["graph_state"]["reason"] = "Index generated, awaiting async clustering"
    index_data["graph_state"]["updated_at"] = _utc_now()
    
    # Initialize basic node scores so BM25 doesn't crash
    node_keys = list(index_data["nodes"].keys())
    for node_key in node_keys:
        node = index_data["nodes"][node_key]
        node["centrality_score"] = 1.0
        node["node_score"] = round(node.get("decay_weight", 1.0), 4)


def generate_index():
    index_data = _empty_index_data()
    wiki_dir = _wiki_dir()
    
    # Read from canonical Markdown files instead of SQLite to preserve full topology and text
    import os
    for filename in os.listdir(wiki_dir):
        if not filename.endswith(".md") or filename in ("index.md", "log.md", "overview.md", "orphan_pages.md", "wiki_link_stats.md", "Synthesis_log.md") or filename.startswith("System_"):
            continue
        
        filepath = os.path.join(wiki_dir, filename)
        node_key = filename[:-3]
        
        try:
            node_data = _parse_wiki_node(filepath, node_key)
        except Exception as e:
            log.warning(f"Failed to parse markdown for {node_key}: {e}")
            continue
            
        if not node_data:
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

    from vector_lake.db_store import transaction
    index_data["weighted_edges"] = _calculate_weighted_edges(index_data)
    _apply_graph_topology(index_data)
    
    index_data["categories"] = list(index_data["categories"])
    index_data["governance_metrics"] = governance_metrics.compute_debt_metrics(skip_heavy=True)
    index_data["schema_version"] = "8.0"

    output_path = str(get_index_path())
    claim_graph_path = str(get_claim_graph_path())
    tmp_output = output_path + ".tmp"
    tmp_claim = claim_graph_path + ".tmp"
    
    _write_claim_graph(tmp_claim, governance_store.build_claim_graph_projection())
    _write_index(tmp_output, index_data)

    # PHASE 1 FIX: Compute embeddings OUTSIDE of DB transaction to prevent Network Blockade Deadlock
    embeddings_map = _compute_embeddings_unlocked(index_data)
    with transaction():
        _build_bm25_index(index_data, embeddings_map)

    os.replace(tmp_claim, claim_graph_path)
    os.replace(tmp_output, output_path)

    log.info(
        f"Generated index.json with {len(index_data['nodes'])} nodes | "
        f"{len(index_data['weighted_edges'])} weighted edges | "
        f"{len((index_data.get('error_log') or []))} errors."
    )
    return output_path


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

    # Pre-parse and pre-embed to prevent holding FileLock during Network I/O
    pre_parsed_data = {}
    import os
    try:
        from google import genai
        client = genai.Client() if os.environ.get("GEMINI_API_KEY") else None
    except ImportError:
        client = None

    wiki_dir = _wiki_dir()
    for filename in valid_filenames:
        filepath = os.path.join(wiki_dir, filename)
        node_key = filename[:-3]
        if not os.path.exists(filepath):
            continue
        try:
            node_data = _parse_wiki_node(filepath, node_key)
        except Exception:
            node_data = None
        if node_data:
            pre_parsed_data[node_key] = node_data
            if client:
                try:
                    embedding_content = f"{node_data.get('title', '')} {node_data.get('summary', '')} {node_data.get('raw_text', '')}"
                    response = client.models.embed_content(
                        model="gemini-embedding-2",
                        contents=embedding_content[:15000]
                    )
                    if hasattr(response, 'embeddings') and len(response.embeddings) > 0:
                        node_data["_pre_embedded"] = response.embeddings[0].values
                except Exception as e:
                    log.warning(f"Pre-embedding failed for {node_key}: {e}")

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
    
                    for filename in valid_filenames:
                        filepath = os.path.join(wiki_dir, filename)
                        node_key = filename[:-3]
    
                        if not filename.startswith(VALID_PREFIXES) and filename not in ("index.md", "log.md"):
                            index_data.setdefault("error_log", [])
                            index_data["error_log"] = [item for item in index_data["error_log"] if item.get("file") != filename]
                            index_data["error_log"].append({"file": filename, "error": "Schema violation: Missing valid entity prefix."})
                            log.warning(f"Schema violation in {filename} during partial update.")
                            old_node = (index_data.get("nodes") or {}).pop(node_key, None)
                            if old_node:
                                db_store.delete_node_cascade(node_key)
                            index_data["weighted_edges"] = [
                                edge for edge in (index_data.get("weighted_edges") or [])
                                if edge["source"] != node_key and edge["target"] != node_key
                            ]
                        else:
                            index_data["aliases"] = {key: value for key, value in (index_data.get("aliases") or {}).items() if value != node_key}
                            index_data.setdefault("error_log", [])
                            index_data["error_log"] = [item for item in index_data["error_log"] if item.get("file") != filename]
    
                            if not os.path.exists(filepath):
                                old_node = (index_data.get("nodes") or {}).pop(node_key, None)
                                if old_node:
                                    db_store.delete_node_cascade(node_key)
                                index_data["weighted_edges"] = [
                                    edge for edge in (index_data.get("weighted_edges") or [])
                                    if edge["source"] != node_key and edge["target"] != node_key
                                ]
                            else:
                                node_data = pre_parsed_data.get(node_key)
                                if node_data is None:
                                    try:
                                        node_data = _parse_wiki_node(filepath, node_key)
                                    except Exception as e:
                                        index_data["error_log"].append({"file": filename, "error": str(e)})
                                        node_data = None
    
                                if node_data is None:
                                    index_data["error_log"].append({"file": filename, "error": "Schema violation: Missing 'domain' or 'status'. Node excluded."})
                                    old_node = (index_data.get("nodes") or {}).pop(node_key, None)
                                    if old_node:
                                        db_store.delete_node_cascade(node_key)
                                else:
                                    old_node = index_data["nodes"].get(node_key)
                                    if old_node:
                                        db_store.delete_node_cascade(node_key)
                                    
                                    index_data["nodes"][node_key] = node_data
                                    aliases_str = " ".join((node_data.get("aliases") or [])) if isinstance(node_data.get("aliases"), list) else ""
                                    text = f"{aliases_str} {node_data.get('raw_text', '')}"
                                    t_title = _tokenize_for_fts(node_data.get('title', ''))
                                    t_summary = _tokenize_for_fts(node_data.get('summary', ''))
                                    t_text = _tokenize_for_fts(text)
                                    db_store.upsert_search_index(node_key, t_title, t_summary, t_text)
                                    
                                    # Issue 4 Fix: Missing embedding recalculation
                                    if "_pre_embedded" in node_data:
                                        db_store.upsert_embedding(node_key, node_data["_pre_embedded"])
                                    
                                    if node_data["id"]:
                                        index_data["aliases"][node_data["id"]] = node_key
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
                                        cursor.execute("SELECT source_id, target_id, weight FROM claim_graph_edges WHERE source_id = ? OR target_id = ?", (node_key, node_key))
                                        for row in cursor.fetchall():
                                            index_data["weighted_edges"].append({
                                                "source": row["source_id"],
                                                "target": row["target_id"],
                                                "weight": float(row["weight"]) if row["weight"] else 1.0,
                                            })
                                    except Exception as e:
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
                                    node_sources = set((node_data.get("sources") or []))
                                    for other_key, other_node in all_nodes.items():
                                        if other_key == node_key:
                                            continue
                                            
                                        other_links = set((other_node.get("links") or []))
                                        other_sources = set((other_node.get("sources") or []))
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
    
                    _mark_graph_dirty(index_data, f"Partial batch update for {len(valid_filenames)} items")
                    index_data["categories"] = list((index_data.get("categories") or []))
                    # Do not recompute heavy debt metrics on partial update
                    index_data["governance_metrics"] = (index_data.get("governance_metrics") or {})
                    index_data["schema_version"] = "8.0"
                    # V11.3 Fixed: Write partial updates back to disk to prevent ghost updates
                    _write_index(output_path, index_data)
    except Timeout:
        log.error(f"Timeout while acquiring lock for {output_path}")
        return

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
                    generate_index()
                    return True
    
                removed_legacy_keys = _strip_legacy_embedded_payloads(index_data)
                if removed_legacy_keys:
                    log.info(
                        "Detected legacy embedded governance payloads during graph refresh "
                        f"({', '.join(removed_legacy_keys)}). Triggering full rebuild."
                    )
                    generate_index()
                    return True
    
                if is_graph_dirty(index_data):
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

