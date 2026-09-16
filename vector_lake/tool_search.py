import json
import logging
import os
import re
from datetime import datetime, timezone

import functools
import ast
import operator

from vector_lake import governance_store
from vector_lake.wiki_utils import get_index_path, get_wiki_dir


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("vector-lake-tool-search")

TOKEN_BUDGET = {
    "operational_memory": 0.30,
    "wiki_pages": 0.45,
    "chat_history": 0.05,
    "index_summary": 0.05,
    "system_prompt": 0.15,
}
DEFAULT_MAX_CHARS = 200000

CJK_REGEX = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")
STOP_WORDS = {
    "的", "了", "在", "是", "我", "有", "和", "就", "不", "人", "都", "一", "一个",
    "the", "a", "an", "is", "are", "was", "were", "in", "on", "at", "to", "for",
    "of", "and", "or", "but", "with", "by", "from", "as", "it", "this", "that",
}

QUERY_EXPANSION_DICT = {
    "医疗信息化": ["HIT", "卫宁", "电子病历", "医疗IT"],
    "大模型": ["LLM", "大语言模型", "Agent", "智能体"],
    "医疗AI": ["临床Agent", "大模型医疗落地", "电子病历 智能化"],
}

def _passes_filters(node: dict, domain: str | None, cluster: str | None, include_history: bool, filter_expr: str | None) -> bool:
    """Single source of truth for the caller-visible result filters.

    Graph expansion and reranking must not bypass these; previously only the
    first scoring pass applied them, so ``domain=`` could return other domains.
    """
    if domain and str(node.get("domain") or "").lower() != domain.lower():
        return False
    if cluster and str(node.get("topic_cluster") or "").lower() != cluster.lower():
        return False
    if not include_history and str(node.get("status") or "").lower() in ("deprecated", "archived"):
        return False
    if filter_expr:
        try:
            if not _safe_eval(filter_expr, node):
                return False
        except Exception as exc:
            log.warning("Filter expr evaluation failed for node %s: %s", node.get("_key"), exc)
            return False
    return True


def _get_query_embedding(query: str) -> tuple[list[float], str | None]:
    """Query vector plus an explicit degradation reason when it is unavailable."""
    if not os.environ.get("GEMINI_API_KEY"):
        return [], "GEMINI_API_KEY is not set"
    try:
        from vector_lake.embedding_scheduler import embed_texts

        embeddings = embed_texts([query])
        if not embeddings:
            return [], "embedding provider returned no vector"
        return embeddings[0], None
    except Exception as e:
        log.warning(f"Failed to get query embedding: {e}")
        return [], f"{type(e).__name__}: {e}"


def _get_vector_search_results(query_vector: list[float], limit: int = 50) -> tuple[dict[str, float], str | None]:
    """Vector hits plus an explicit degradation reason when the query failed."""
    try:
        from vector_lake.db_store import get_connection
        import sqlite_vec
        conn = get_connection()
        query_blob = sqlite_vec.serialize_float32(query_vector)
        # Using match because it's fast. It returns L2 distance.
        # Cosine similarity for normalized vectors: 1 - L2^2 / 2
        cursor = conn.execute(
            "SELECT entity_id, distance FROM vec_embeddings WHERE embedding MATCH ? ORDER BY distance LIMIT ?",
            (query_blob, limit)
        )

        results = {}
        for row in cursor.fetchall():
            # distance is L2. convert to approx sim: 1 - (dist^2)/2
            dist = row["distance"]
            sim = 1.0 - (dist * dist) / 2.0
            if sim > 0.5:
                results[row["entity_id"]] = sim
        return results, None
    except Exception as e:
        log.warning(f"Failed to query vec_embeddings: {e}")
        return {}, f"vec_embeddings query failed: {type(e).__name__}: {e}"

def _get_fts_search_results(query: str, limit: int = 50) -> list[dict]:
    from vector_lake import tokenizer as _tokenizer

    query_tok = _tokenizer.tokenize_joined(query)
        
    # Sanitize query_tok for FTS5 (remove special syntax characters)
    import re
    query_tok = re.sub(r'["*^&|()\-:\[\]{}]', ' ', query_tok)
    # Ensure it's not empty or just spaces
    if not query_tok.strip():
        return []
        
    try:
        from vector_lake.db_store import get_connection
        conn = get_connection()
        cur = conn.execute("""
            SELECT node_key, title, summary, bm25(wiki_search_index) as rank 
            FROM wiki_search_index 
            WHERE wiki_search_index MATCH ? 
            ORDER BY rank LIMIT ?
        """, (query_tok, limit))
        return [dict(row) for row in cur.fetchall()]
    except Exception as e:
        log.warning(f"Failed to query fts5: {e}")
        return []

def _classify_intent(query: str) -> str:
    temporal_keywords = {"上周", "去年", "昨天", "最近", "历史", "last week", "yesterday", "202"}
    entity_keywords = {"是谁", "哪里", "谁在", "who is", "where is", "公司", "人员", "关联", "图谱", "网络"}
    for kw in temporal_keywords:
        if kw in query.lower(): return "temporal"
    for kw in entity_keywords:
        if kw in query.lower(): return "entity"
    return "general"


@functools.lru_cache(maxsize=128)
def _expand_query_locally(query: str) -> list[str]:
    expanded_terms = set([query])
    for key, expansions in QUERY_EXPANSION_DICT.items():
        if key in query:
            expanded_terms.update(expansions)

    tokens = set()
    from vector_lake import tokenizer as _tokenizer

    backend_ready = _tokenizer.backend_name() != "unavailable"
    if backend_ready:
        for term in QUERY_EXPANSION_DICT.keys():
            _tokenizer.add_word(term)
        for expansions in QUERY_EXPANSION_DICT.values():
            for exp in expansions:
                _tokenizer.add_word(exp)

    for term in expanded_terms:
        if backend_ready and CJK_REGEX.search(term):
            for word in _tokenizer.split(term):
                word_lower = word.lower()
                if word_lower not in STOP_WORDS and word_lower.strip():
                    tokens.add(word_lower)
        else:
            for word in term.strip().split():
                word_lower = word.lower()
                if word_lower in STOP_WORDS:
                    continue
                if CJK_REGEX.search(word):
                    chars = list(word)
                    for index in range(len(chars) - 1):
                        tokens.add(chars[index] + chars[index + 1])
                    for char in chars:
                        if CJK_REGEX.match(char):
                            tokens.add(char)
                    tokens.add(word)
                else:
                    tokens.add(word_lower)
    return list(tokens)


def _format_memory_result(memory: dict, as_xml: bool = False, index: int = 0) -> str:
    state = memory.get("validity_state", "active")
    memory_type = memory.get("memory_type", "fact")
    score = memory.get("retrieval_score", memory.get("memory_score", 0))
    text = " ".join(str(memory.get("text", "")).split())[:420]
    source = memory.get("source_page") or memory.get("source_claim_id") or "operational_memory"
    if as_xml:
        attrs = (
            f"ID='Memory_{index}' Type='{memory_type}' State='{state}' "
            f"Score='{score}' Source='{source}'"
        )
        return f"<Memory_Item {attrs}>{text}</Memory_Item>\n"
    return (
        f"- **{memory_type}:{memory.get('memory_key', memory.get('memory_id'))}** "
        f"(score: {score:.2f}, state: {state})\n"
        f"  {text}\n"
        f"  Source: {source}\n\n"
    )


def format_operational_memory_results(query: str, top_k: int = 8, as_xml: bool = False, include_history: bool = False, memory_types: list[str] | None = None) -> str:
    memories = governance_store.search_operational_memory(
        query,
        top_k=top_k,
        include_history=include_history,
        memory_types=memory_types,
    )
    if not memories:
        return "No operational memory matched the query."
    return "".join(_format_memory_result(memory, as_xml=as_xml, index=index) for index, memory in enumerate(memories))


def build_memory_packet(query: str, max_chars: int = 60000) -> dict:
    memories = governance_store.search_operational_memory(query, top_k=24, include_history=False)
    historical = governance_store.search_operational_memory(query, top_k=12, include_history=True)
    stale_or_conflicted = [
        item for item in historical
        if str(item.get("validity_state", "")).lower() in {"conflicted", "review-due", "needs-review", "superseded", "expired"}
    ][:6]

    sections = {
        "Current Preferences": [],
        "Open Decisions": [],
        "Task State": [],
        "Relevant Facts": [],
    }
    type_to_section = {
        "preference": "Current Preferences",
        "decision": "Open Decisions",
        "task_state": "Task State",
        "fact": "Relevant Facts",
    }

    evidence_pointers = []
    for memory in memories:
        section = type_to_section.get(memory.get("memory_type", "fact"), "Relevant Facts")
        text = " ".join(str(memory.get("text", "")).split())
        line = (
            f"- [{memory.get('memory_score', 0):.2f}/{memory.get('validity_state', 'active')}] "
            f"{text[:420]}"
        )
        if memory.get("source_page"):
            line += f" ({memory['source_page']})"
        sections[section].append(line)
        if memory.get("source_claim_id"):
            evidence_pointers.append(
                f"- {memory.get('source_claim_id')} -> {memory.get('source_page', 'unknown')}"
            )

    lines = [
        "<MEMORY_PACKET>",
        f"Generated: {datetime.now(timezone.utc).isoformat()}",
        f"Query: {query}",
        "Policy: Use this packet as the machine-facing runtime memory. If it conflicts with wiki prose, prefer active non-conflicted memory items and surface the conflict.",
        "",
    ]
    for title in ("Current Preferences", "Open Decisions", "Task State", "Relevant Facts"):
        lines.append(f"## {title}")
        lines.extend(sections[title] or ["- None matched."])
        lines.append("")

    lines.append("## Conflicts / Stale Warnings")
    if stale_or_conflicted:
        for memory in stale_or_conflicted:
            lines.append(
                f"- [{memory.get('validity_state')}] {memory.get('memory_type')}:{memory.get('memory_key')} "
                f"-> {str(memory.get('text', ''))[:260]}"
            )
    else:
        lines.append("- None matched.")
    lines.append("")

    lines.append("## Evidence Pointers")
    lines.extend(evidence_pointers[:12] or ["- None matched."])
    lines.append("</MEMORY_PACKET>")

    packet = "\n".join(lines)
    omitted = 0
    if len(packet) > max_chars:
        packet = packet[: max(0, max_chars - 80)].rstrip() + "\n...[memory packet truncated]\n</MEMORY_PACKET>"
        omitted = max(0, len(memories) - 12)
    return {
        "packet": packet,
        "memory_count": len(memories),
        "warning_count": len(stale_or_conflicted),
        "omitted_count": omitted,
    }


def _rerank_candidates_locally(query: str, candidates: list[tuple[float, dict]]) -> list[tuple[float, dict]]:
    """Phase 2: local reranking of the retrieved candidate pool with BM25.

    Candidate *membership* is decided upstream by FTS5 + graph expansion, so
    recall is unchanged; this only re-orders within that pool and replaces the
    raw `-bm25` magnitudes that previously dominated the blend.

    The lexical signal comes from ``bm25s`` over title + summary + aliases. The
    page body is deliberately not read here: it would add per-candidate file IO
    to every query, and ``index.json`` no longer carries it.

    Scores are **pool-normalised**, not absolute: min-max within the pool means
    the leading candidate always reports 1.0, and a pool with no lexical signal
    at all reduces to a monotone rescaling of the upstream order. Ties at the
    pool maximum stay tied.

    ``VECTOR_LAKE_RERANK_WEIGHT`` (default 0.4) blends the two normalised score
    sets; set it to 0 to reproduce the previous ordering exactly. The weight
    keeps upstream influence on purpose so graph-expanded candidates, which have
    no lexical overlap by construction, are not buried.
    """
    if len(candidates) < 2:
        return candidates
    try:
        weight = float(os.environ.get("VECTOR_LAKE_RERANK_WEIGHT", "0.4"))
    except ValueError:
        weight = 0.4
    weight = min(1.0, max(0.0, weight))
    if weight <= 0.0:
        return candidates

    try:
        import bm25s
    except ImportError:
        log.debug("bm25s is not installed; skipping local reranking.")
        return candidates

    from vector_lake import tokenizer as _tokenizer

    def _document(node: dict) -> str:
        aliases = node.get("aliases") or []
        alias_text = " ".join(str(a) for a in aliases) if isinstance(aliases, list) else str(aliases)
        return " ".join(
            part for part in (str(node.get("title") or ""), str(node.get("summary") or ""), alias_text) if part
        ).strip()

    # Pre-tokenize with the project tokenizer, then let bm25s bind the tokens by
    # splitting on whitespace only (its default \w\w+ pattern cannot segment CJK).
    documents = [_tokenizer.tokenize_joined(_document(node)) for _, node in candidates]
    query_tokens = _tokenizer.cut(query)
    if not query_tokens or not any(documents):
        return candidates

    try:
        corpus = bm25s.tokenize(documents, stopwords=[], token_pattern=r"(?u)\S+", show_progress=False)
        retriever = bm25s.BM25()
        retriever.index(corpus, show_progress=False)
        lexical = [float(value) for value in retriever.get_scores(query_tokens)]
    except Exception as exc:
        # Fail open: a reranker fault must never drop results.
        log.warning("Local reranking failed (%s: %s); keeping upstream order.", type(exc).__name__, exc)
        return candidates

    if len(lexical) != len(candidates):
        log.warning("Local reranking returned %s scores for %s candidates; keeping upstream order.",
                    len(lexical), len(candidates))
        return candidates

    def _normalise(values: list[float]) -> list[float]:
        low, high = min(values), max(values)
        if high - low <= 1e-12:
            return [0.0] * len(values)
        return [(value - low) / (high - low) for value in values]

    upstream = _normalise([float(score) for score, _ in candidates])
    lexical_norm = _normalise(lexical)
    blended = [(1.0 - weight) * u + weight * b for u, b in zip(upstream, lexical_norm)]

    # Stable: ties keep their upstream relative order.
    order = sorted(range(len(candidates)), key=lambda index: (-blended[index], index))
    return [(round(blended[index], 6), candidates[index][1]) for index in order]


def _safe_eval(expr: str, context: dict) -> bool:
    allowed_operators = {
        ast.Eq: operator.eq,
        ast.NotEq: operator.ne,
        ast.Gt: operator.gt,
        ast.Lt: operator.lt,
        ast.GtE: operator.ge,
        ast.LtE: operator.le,
        ast.In: lambda a, b: a in b if b is not None else False,
        ast.NotIn: lambda a, b: a not in b if b is not None else False,
        ast.And: lambda a, b: a and b,
        ast.Or: lambda a, b: a or b,
        ast.Not: operator.not_,
    }

    def _eval(node):
        if isinstance(node, ast.Constant):
            return node.value
        elif isinstance(node, ast.Name):
            if node.id in context:
                return context[node.id]
            # Try to get from node if filter_expr assumes node dict (e.g. node.get)
            # Actually, standard LLM output uses `type == 'vendor'` so node.id in context handles it.
            return None
        elif isinstance(node, ast.Compare):
            left = _eval(node.left)
            for op, comparator in zip(node.ops, node.comparators):
                right = _eval(comparator)
                if type(op) not in allowed_operators:
                    raise ValueError(f"Unsupported operator: {type(op)}")
                if not allowed_operators[type(op)](left, right):
                    return False
                left = right
            return True
        elif isinstance(node, ast.BoolOp):
            if isinstance(node.op, ast.And):
                return all(_eval(v) for v in node.values)
            elif isinstance(node.op, ast.Or):
                return any(_eval(v) for v in node.values)
        elif isinstance(node, ast.UnaryOp):
            if isinstance(node.op, ast.Not):
                return not _eval(node.operand)
        elif isinstance(node, ast.Call):
            # To support node.get('key') == 'value' or just get('key')
            # The context is actually `node`. So get() refers to node.get.
            if isinstance(node.func, ast.Attribute) and node.func.attr == 'get':
                obj = _eval(node.func.value)
                if isinstance(obj, dict) and node.args:
                    key = _eval(node.args[0])
                    default = _eval(node.args[1]) if len(node.args) > 1 else None
                    return obj.get(key, default)
            elif isinstance(node.func, ast.Name) and node.func.id == 'get':
                if node.args:
                    key = _eval(node.args[0])
                    default = _eval(node.args[1]) if len(node.args) > 1 else None
                    return context.get(key, default)
        raise ValueError(f"Unsupported AST node: {type(node)}")

    try:
        tree = ast.parse(expr, mode='eval')
        return bool(_eval(tree.body))
    except Exception as e:
        log.warning(f"Failed to safe_eval expression '{expr}': {e}")
        return False

_INDEX_CACHE = {
    "mtime": 0.0,
    "data": None
}

def _search_scored_pages(
    query: str,
    top_k: int,
    domain: str = None,
    cluster: str = None,
    include_history: bool = False,
    filter_expr: str = None,
):
    """Ranked pages plus retrieval notes.

    Split out of ``search_vector_lake`` so callers that need the pages can use
    them directly.  ``assemble_context`` used to re-parse the human-readable
    string with a regex, which silently produced an empty wiki context whenever
    the formatting changed or a title contained the delimiter.
    """
    index_path = str(get_index_path())
    if not os.path.exists(index_path):
        return [], [], "Lake is drying. No index.json found, please ingest sources first."

    try:
        current_mtime = os.path.getmtime(index_path)
        if _INDEX_CACHE["mtime"] != current_mtime or _INDEX_CACHE["data"] is None:
            import time
            for attempt in range(3):
                try:
                    with open(index_path, "r", encoding="utf-8") as handle:
                        _INDEX_CACHE["data"] = json.load(handle)
                        _INDEX_CACHE["mtime"] = current_mtime
                    break
                except Exception as e:
                    if attempt == 2:
                        raise e
                    time.sleep(0.2)
        index_data = _INDEX_CACHE["data"]
    except Exception as e:
        log.error(f"Failed to read index.json: {e}")
        return [], [], "Error reading the knowledge base index. Please ensure the index exists and is not corrupted."

    intent = _classify_intent(query)
    tokens = _expand_query_locally(query)
    if not tokens:
        return [], [], "No valid search tokens."

    scored = []
    
    # PHASE 2 FTS5 + VECTOR HYBRID QUERY
    hybrid_scores = {}
    
    # 1. FTS5 Search
    try:
        # Use expanded tokens as the query basis to preserve LLM synonym expansions
        expanded_query = query + " " + " ".join(tokens)
        fts_results = _get_fts_search_results(expanded_query, limit=top_k * 5)
        for row in fts_results:
            key = row['node_key']
            raw_score = row.get('rank')
            if raw_score is None:
                raw_score = row.get('score', 0)
            fts_score = raw_score * -1.0  # SQLite BM25 is negative
            hybrid_scores[key] = hybrid_scores.get(key, 0.0) + fts_score
    except Exception as e:
        log.error(f"FTS5 Search failed: {e}")

    # 2. Vector Search (Hybrid blending)
    vector_notes = []
    query_vector, embedding_error = _get_query_embedding(query)
    if query_vector:
        vector_results, vector_error = _get_vector_search_results(query_vector, limit=top_k * 5)
        if vector_error:
            vector_notes.append(vector_error)
        elif not vector_results:
            vector_notes.append(
                "no stored vectors matched; run `embedding-backfill --apply` to (re)build the vector projection"
            )
        for key, sim in vector_results.items():
            # scale similarity so it competes/blends with BM25.
            vec_score = (sim ** 2) * 15.0
            hybrid_scores[key] = hybrid_scores.get(key, 0.0) + vec_score
    else:
        vector_notes.append(embedding_error or "query embedding unavailable")

    for key, score in hybrid_scores.items():
        if key in index_data.get('nodes', {}):
            node = {'_key': key, **index_data['nodes'][key]}
            if not _passes_filters(node, domain, cluster, include_history, filter_expr):
                continue
            if not include_history and node.get('status', '').lower() == 'decayed' and intent != 'temporal':
                score *= 0.2
            scored.append((score, node))

    scored.sort(key=lambda item: item[0], reverse=True)

    # P2-1: Dynamic Graph Expansion via Multi-hop PPR (Personalized PageRank)
    top_keys = {node["_key"] for _, node in scored[:5]}
    if top_keys and index_data.get("weighted_edges"):
        # Build adjacency list
        adj = {}
        for edge in index_data["weighted_edges"]:
            s, t, w = edge["source"], edge["target"], edge.get("weight", 1.0)
            adj.setdefault(s, []).append((t, w))
            adj.setdefault(t, []).append((s, w))
            
        # PPR parameters.  Non-seed nodes must still receive teleportation mass,
        # otherwise the walk collapses to "adjacent to a seed" after two steps.
        seed_keys = set(top_keys)
        alpha = 0.85
        restart_mass = (1 - alpha) / len(top_keys)
        ppr_scores = {k: 1.0 / len(top_keys) for k in seed_keys}

        for _ in range(2):
            next_scores = {k: restart_mass if k in seed_keys else 0.0 for k in adj}
            for node, current_score in ppr_scores.items():
                neighbors = adj.get(node, [])
                if neighbors:
                    total_weight = sum(w for _, w in neighbors)
                    if total_weight <= 0:
                        continue
                    for neighbor, w in neighbors:
                        next_scores[neighbor] = next_scores.get(neighbor, 0.0) + alpha * current_score * (w / total_weight)
            ppr_scores = next_scores

        existing_keys = {node["_key"] for _, node in scored}
        expansion_limit = 12 if intent == "entity" else 5
        
        sorted_expansions = sorted(
            [(k, v) for k, v in ppr_scores.items() if k not in existing_keys], 
            key=lambda x: x[1], 
            reverse=True
        )
        
        for expanded_key, ppr_weight in sorted_expansions[:expansion_limit]:
            expanded_node = index_data["nodes"].get(expanded_key)
            if expanded_node is None:
                continue
            # Graph expansion is an additional candidate source, not an exemption
            # from the caller's filters.
            if not _passes_filters(expanded_node, domain, cluster, include_history, filter_expr):
                continue
            scored.append((ppr_weight * 15.0, {"_key": expanded_key, **expanded_node}))

    scored.sort(key=lambda item: item[0], reverse=True)

    # Phase 1: Expand candidate pool for reranking
    candidate_pool = []
    source_count = 0
    pool_size = max(40, top_k * 3)
    max_sources_pool = int(pool_size * 0.6)
    for score, node in scored:
        node_type = node.get("type", "").lower()
        if node_type == "source":
            if source_count < max_sources_pool:
                candidate_pool.append((score, node))
                source_count += 1
        else:
            candidate_pool.append((score, node))
        if len(candidate_pool) >= pool_size:
            break
            
    # Phase 2: Local deterministic ranking. Text-model reranking is delegated
    # to the host agent when explicitly requested, not performed by runtime code.
    reranked = _rerank_candidates_locally(query, candidate_pool)

    # Phase 3: Final top_k extraction
    final_scored = []
    source_count = 0
    max_sources_final = int(top_k * 0.6)
    for score, node in reranked:
        node_type = node.get("type", "").lower()
        if node_type == "source":
            if source_count < max_sources_final:
                final_scored.append((score, node))
                source_count += 1
        else:
            final_scored.append((score, node))
        if len(final_scored) >= top_k:
            break

    return final_scored, vector_notes, None


def search_vector_lake(query: str, top_k: int = 5, as_xml: bool = False, domain: str = None, cluster: str = None, include_history: bool = False, mode: str = "page", filter_expr: str = None):
    normalized_mode = str(mode or "page").lower()
    if normalized_mode in {"memory", "operational-memory", "operational_memory"}:
        return format_operational_memory_results(query, top_k=top_k, as_xml=as_xml, include_history=include_history)
    if normalized_mode in {"claim", "claims"}:
        return format_operational_memory_results(query, top_k=top_k, as_xml=as_xml, include_history=include_history, memory_types=["fact"])

    wiki_dir = str(get_wiki_dir())
    final_scored, vector_notes, error = _search_scored_pages(
        query,
        top_k=top_k,
        domain=domain,
        cluster=cluster,
        include_history=include_history,
        filter_expr=filter_expr,
    )
    if error:
        return error

    result = ""
    if vector_notes:
        result += "[DEGRADED] Vector retrieval did not contribute: " + "; ".join(vector_notes) + "\n\n"
    if not final_scored:
        return result + "No matching pages."
    for index, (score, node) in enumerate(final_scored):
        filepath = os.path.join(wiki_dir, f"{node['_key']}.md")
        snippet = ""
        if os.path.exists(filepath):
            with open(filepath, "r", encoding="utf-8", errors="replace") as handle:
                content = handle.read()
            snippet = re.sub(r"^---.*?---\s*", "", content, flags=re.DOTALL)[:2500]  # V11.2: Expanded chunk limit
            
        tension_edges = node.get("tension_edges", [])
        tension_info = ""
        if tension_edges:
            tension_info = "  [Tension Edges]:\n"
            for te in tension_edges:
                tension_info += f"    -> {te.get('target')} (Polarity: {te.get('polarity')}, Intensity: {te.get('intensity')}): {te.get('context')}\n"
                
        if as_xml:
            result += f"<Evidence_Node ID='Wiki_{index}' Source='{node['_key']}.md'>\n{tension_info}{snippet}\n</Evidence_Node>\n"
        else:
            result += f"- **{node.get('title', node['_key'])}** (score: {score:.3f})\n{tension_info}  {snippet}...\n\n"
    return result


def assemble_context(query: str, max_chars: int = DEFAULT_MAX_CHARS) -> dict:
    """Assemble a budget-bounded reasoning context for a query.

    Pages come from ``_search_scored_pages`` directly.  This function previously
    re-parsed the formatted search string with a regex, which produced an empty
    wiki context whenever the formatting drifted.  The returned budget is now
    enforced: ``budget_used <= budget_max`` always holds.
    """
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")

    index_budget = int(max_chars * TOKEN_BUDGET["index_summary"])

    # P2-2: Dynamic Sliding Window for Budget
    # Allow memory to burst up to 50% if there are critical alerts
    memory_packet = build_memory_packet(query, max_chars=int(max_chars * 0.50))
    actual_memory_used = len(memory_packet["packet"])

    purpose = ""
    try:
        from vector_lake.purpose_contract import render_strategy_directive

        purpose = render_strategy_directive()
    except Exception as exc:
        log.warning("Strategy directive unavailable for context assembly: %s", exc)
    purpose_budget = int(max_chars * TOKEN_BUDGET["system_prompt"])

    # Wiki dynamically eats the remaining budget.
    wiki_budget = max(0, max_chars - actual_memory_used - index_budget - purpose_budget)

    scored_pages, vector_notes, retrieval_error = _search_scored_pages(query, top_k=15)

    wiki_dir = str(get_wiki_dir())
    wiki_blocks: list[str] = []
    wiki_used = 0
    page_count = 0
    for score, node in scored_pages:
        key = node["_key"]
        filepath = os.path.join(wiki_dir, f"{key}.md")
        snippet = ""
        try:
            with open(filepath, "r", encoding="utf-8", errors="replace") as handle:
                snippet = re.sub(r"^---.*?---\s*", "", handle.read(), flags=re.DOTALL)[:2500]
        except OSError:
            snippet = ""
        block = f"- **{node.get('title', key)}** (score: {score:.3f})\n  {snippet}\n\n"
        if wiki_used + len(block) > wiki_budget:
            break
        wiki_blocks.append(block)
        wiki_used += len(block)
        page_count += 1
    wiki_context = "".join(wiki_blocks)

    index_summary = ""
    index_path = str(get_index_path())
    if os.path.exists(index_path):
        import time

        for attempt in range(3):
            try:
                with open(index_path, "r", encoding="utf-8") as handle:
                    index_data = json.load(handle)
                lines = [
                    f"[{node.get('type', '?')}] {node.get('title', key)}"
                    for key, node in list(index_data.get("nodes", {}).items())[:50]
                ]
                index_summary = "\n".join(lines)[:index_budget]
                break
            except Exception as exc:
                if attempt == 2:
                    index_summary = "[Index read failed]"
                    log.error("Index summary unavailable: %s", exc)
                time.sleep(0.2)

    # ``purpose`` is the last claimant on the budget and must not overflow it.
    remaining = max_chars - (len(memory_packet["packet"]) + len(wiki_context) + len(index_summary))
    purpose = purpose[:max(0, min(purpose_budget, remaining))]

    budget_used = len(memory_packet["packet"]) + len(wiki_context) + len(index_summary) + len(purpose)
    return {
        "memory_packet": memory_packet["packet"],
        "memory_count": memory_packet["memory_count"],
        "memory_warning_count": memory_packet["warning_count"],
        "memory_omitted_count": memory_packet["omitted_count"],
        "wiki_context": wiki_context,
        "wiki_page_count": page_count,
        "index_summary": index_summary,
        "purpose": purpose,
        "retrieval_notes": list(vector_notes or []) + ([retrieval_error] if retrieval_error else []),
        "budget_used": budget_used,
        "budget_max": max_chars,
    }
