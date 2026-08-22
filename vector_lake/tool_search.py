import logging
import json
import math
import os
import re
import threading
import time
import unicodedata
from collections import OrderedDict
from datetime import datetime, timezone
from itertools import islice

import functools
import ast
import operator
from xml.sax.saxutils import escape, quoteattr

from vector_lake import governance_store
from vector_lake.index_snapshot import (
    CompactGraphAdjacency,
    get_compact_graph_adjacency,
)
from vector_lake.indexer import read_committed_index_snapshot
from vector_lake.wiki_utils import get_index_path, get_wiki_dir


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("vector-lake-tool-search")


class SearchIndexError(RuntimeError):
    """The durable search projection exists but cannot be decoded safely."""


class SearchBackendError(RuntimeError):
    """A search backend failed; expose only the backend identity to callers."""

    def __init__(self, backend: str):
        self.backend = str(backend)
        super().__init__(f"Search backend unavailable: {self.backend}")


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


_SEARCH_QUERY_CHAR_LIMIT = 16_384
_SEARCH_TOP_K_LIMIT = 100
_QUERY_EMBEDDING_OPT_IN_ENV = "VECTOR_LAKE_QUERY_EMBEDDING"

_QUERY_EMBEDDING_STATE_LOCK = threading.Lock()
_QUERY_EMBEDDING_FAILURE_UNTIL = 0.0
_QUERY_EMBEDDING_KEY_LOCKS = tuple(threading.Lock() for _ in range(16))
_QUERY_EMBEDDING_CACHE_KEYS: OrderedDict[
    tuple[str, str, int, int], None
] = OrderedDict()
_SEARCH_PERFORMANCE_LOCK = threading.Lock()
_SEARCH_PERFORMANCE = {
    "completed_calls": 0,
    "last": {},
    "max_total_ms": 0.0,
}
_SEARCH_BACKEND_LOG_LOCK = threading.Lock()
_SEARCH_BACKEND_LOG_STATE: dict[str, dict[str, float | int]] = {}
_SEARCH_BACKEND_LOG_INTERVAL_SECONDS = 30.0
_IDENTITY_LOOKUP_LOCK = threading.Lock()
_IDENTITY_LOOKUP_CACHE: dict[str, object] = {
    "signature": "",
    "lookup": {},
}

_EXACT_IDENTITY_WEIGHTS = {
    "key": 120.0,
    "id": 118.0,
    "title": 116.0,
    "alias": 112.0,
}



def _query_embedding_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


def _search_result_char_limit() -> int:
    try:
        value = int(os.environ.get("VECTOR_LAKE_SEARCH_RESULT_MAX_CHARS", "24000"))
    except (TypeError, ValueError):
        value = 24_000
    return max(1_000, min(256_000, value))


def _search_result_byte_limit() -> int:
    try:
        value = int(os.environ.get("VECTOR_LAKE_SEARCH_RESULT_MAX_BYTES", "32768"))
    except (TypeError, ValueError):
        value = 32_768
    return max(4_096, min(1_048_576, value))


def _record_search_performance(
    timings: dict[str, float],
    *,
    result_chars: int,
    result_bytes: int,
    backend_issues: list[str],
) -> None:
    normalized = {
        key: round(max(0.0, float(value)), 3)
        for key, value in timings.items()
    }
    normalized["result_chars"] = max(0, int(result_chars))
    normalized["result_bytes"] = max(0, int(result_bytes))
    normalized["backend_issues"] = sorted(set(backend_issues))
    with _SEARCH_PERFORMANCE_LOCK:
        _SEARCH_PERFORMANCE["completed_calls"] += 1
        _SEARCH_PERFORMANCE["last"] = normalized
        _SEARCH_PERFORMANCE["max_total_ms"] = round(
            max(
                float(_SEARCH_PERFORMANCE["max_total_ms"]),
                float(normalized.get("total_ms") or 0.0),
            ),
            3,
        )


def search_performance_status() -> dict:
    """Return query-text-free process-local search timing telemetry."""
    with _SEARCH_BACKEND_LOG_LOCK:
        suppressed = {
            backend: int(state.get("suppressed", 0))
            for backend, state in _SEARCH_BACKEND_LOG_STATE.items()
            if int(state.get("suppressed", 0)) > 0
        }
    with _SEARCH_PERFORMANCE_LOCK:
        status = {
            "completed_calls": int(_SEARCH_PERFORMANCE["completed_calls"]),
            "last": dict(_SEARCH_PERFORMANCE["last"]),
            "max_total_ms": float(_SEARCH_PERFORMANCE["max_total_ms"]),
            "result_char_limit": _search_result_char_limit(),
            "result_byte_limit": _search_result_byte_limit(),
        }
    status["backend_log_suppressed"] = suppressed
    return status


def _log_search_backend_failure(backend: str) -> None:
    """Rate-limit repeated backend failures without hiding degradation state."""
    normalized = str(backend or "unknown").strip().lower() or "unknown"
    now = time.monotonic()
    with _SEARCH_BACKEND_LOG_LOCK:
        state = _SEARCH_BACKEND_LOG_STATE.setdefault(
            normalized,
            {"last_logged": float("-inf"), "suppressed": 0},
        )
        elapsed = now - float(state["last_logged"])
        if elapsed < _SEARCH_BACKEND_LOG_INTERVAL_SECONDS:
            state["suppressed"] = int(state["suppressed"]) + 1
            return
        suppressed = int(state["suppressed"])
        state["last_logged"] = now
        state["suppressed"] = 0
    suffix = f"; suppressed {suppressed} repeated failures" if suppressed else ""
    log.error(
        "Search backend %s failed; using bounded fallback%s",
        normalized,
        suffix,
    )


def _reset_search_backend_log_state() -> None:
    with _SEARCH_BACKEND_LOG_LOCK:
        _SEARCH_BACKEND_LOG_STATE.clear()


def _normalize_identity_label(value: object) -> str:
    """Normalize an identity label without turning fuzzy text into equality."""
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    if text.casefold().endswith(".md"):
        text = text[:-3]
    return " ".join(text.split()).casefold()


def _identity_lookup(index_data: dict) -> dict[str, tuple[tuple[str, float], ...]]:
    """Build one generation-scoped exact key/title/alias lookup.

    The existing projection-level alias map stores one target per spelling and
    can therefore hide an ambiguous alias.  This cache is rebuilt from nodes so
    every exact claimant remains visible and the O(N) scan is paid once per
    committed projection generation rather than once per query.
    """
    signature = json.dumps(
        index_data.get("projection_manifest") or {},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    with _IDENTITY_LOOKUP_LOCK:
        if _IDENTITY_LOOKUP_CACHE["signature"] == signature:
            return _IDENTITY_LOOKUP_CACHE["lookup"]  # type: ignore[return-value]
        # Build under the same lock as the generation check.  The work is O(N)
        # and a concurrent cold start previously let every request repeat the
        # full allocation before the first result reached the cache.
        mutable: dict[str, dict[str, float]] = {}
        for raw_key, raw_node in (index_data.get("nodes") or {}).items():
            key = str(raw_key)
            node = raw_node if isinstance(raw_node, dict) else {}
            aliases = node.get("aliases") or []
            if isinstance(aliases, str):
                aliases = [aliases]
            labels = [
                (key, "key"),
                (node.get("id"), "id"),
                (node.get("title"), "title"),
                *((alias, "alias") for alias in aliases),
            ]
            for label, kind in labels:
                normalized = _normalize_identity_label(label)
                if not normalized:
                    continue
                matches = mutable.setdefault(normalized, {})
                matches[key] = max(
                    matches.get(key, 0.0),
                    _EXACT_IDENTITY_WEIGHTS[kind],
                )

        lookup = {
            label: tuple(
                sorted(matches.items(), key=lambda item: (-item[1], item[0]))
            )
            for label, matches in mutable.items()
        }
        _IDENTITY_LOOKUP_CACHE["signature"] = signature
        _IDENTITY_LOOKUP_CACHE["lookup"] = lookup
        return lookup


def _exact_identity_scores(index_data: dict, query: str) -> dict[str, float]:
    normalized = _normalize_identity_label(query)
    if not normalized:
        return {}
    return dict(_identity_lookup(index_data).get(normalized, ()))


def _query_embedding_enabled() -> bool:
    """Return true only for the trusted host's explicit provider opt-in."""
    return os.environ.get(_QUERY_EMBEDDING_OPT_IN_ENV) == "1"


def _should_query_embedding(*, fts_result_count: int, top_k: int) -> bool:
    if not _query_embedding_enabled():
        return False
    if os.environ.get("VECTOR_LAKE_QUERY_EMBEDDING_ALWAYS") == "1":
        return True
    minimum = _query_embedding_int(
        "VECTOR_LAKE_QUERY_EMBEDDING_FTS_BYPASS_MIN_RESULTS",
        5,
    )
    minimum = min(max(1, int(top_k)), minimum)
    return max(0, int(fts_result_count)) < minimum


@functools.lru_cache(maxsize=64)
def _cached_query_embedding(
    query: str,
    model: str,
    timeout_ms: int,
    max_wait_ms: int,
) -> tuple[float, ...]:
    del model  # The model remains part of the cache key.
    from vector_lake.embedding_scheduler import embed_texts

    embeddings = embed_texts(
        [query],
        max_retries=0,
        timeout_ms=timeout_ms,
        max_wait_seconds=max_wait_ms / 1000.0,
        initialize_schema=False,
    )
    return tuple(embeddings[0]) if embeddings else ()


def _reset_query_embedding_state_for_tests() -> None:
    global _QUERY_EMBEDDING_FAILURE_UNTIL
    _cached_query_embedding.cache_clear()
    with _QUERY_EMBEDDING_STATE_LOCK:
        _QUERY_EMBEDDING_CACHE_KEYS.clear()
        _QUERY_EMBEDDING_FAILURE_UNTIL = 0.0


def _get_query_embedding(query: str) -> list[float]:
    global _QUERY_EMBEDDING_FAILURE_UNTIL
    normalized_query = str(query or "").strip()
    if len(normalized_query) > _SEARCH_QUERY_CHAR_LIMIT:
        raise ValueError(
            f"search query exceeds {_SEARCH_QUERY_CHAR_LIMIT} characters"
        )
    if (
        not _query_embedding_enabled()
        or not os.environ.get("GEMINI_API_KEY")
        or not normalized_query
    ):
        return []

    timeout_ms = _query_embedding_int(
        "VECTOR_LAKE_QUERY_EMBEDDING_TIMEOUT_MS",
        2_000,
    )
    max_wait_ms = _query_embedding_int(
        "VECTOR_LAKE_QUERY_EMBEDDING_MAX_WAIT_MS",
        250,
    )
    model = os.environ.get(
        "VECTOR_LAKE_EMBEDDING_MODEL",
        "gemini-embedding-2",
    )
    cache_key = (normalized_query, model, timeout_ms, max_wait_ms)
    key_lock = _QUERY_EMBEDDING_KEY_LOCKS[
        hash(cache_key) % len(_QUERY_EMBEDDING_KEY_LOCKS)
    ]

    with key_lock:
        now = time.monotonic()
        with _QUERY_EMBEDDING_STATE_LOCK:
            cache_known = cache_key in _QUERY_EMBEDDING_CACHE_KEYS
            if not cache_known and now < _QUERY_EMBEDDING_FAILURE_UNTIL:
                return []

        try:
            vector = list(
                _cached_query_embedding(
                    normalized_query,
                    model,
                    timeout_ms,
                    max_wait_ms,
                )
            )
            with _QUERY_EMBEDDING_STATE_LOCK:
                if not cache_known:
                    _QUERY_EMBEDDING_FAILURE_UNTIL = 0.0
                _QUERY_EMBEDDING_CACHE_KEYS[cache_key] = None
                _QUERY_EMBEDDING_CACHE_KEYS.move_to_end(cache_key)
                while len(_QUERY_EMBEDDING_CACHE_KEYS) > 64:
                    _QUERY_EMBEDDING_CACHE_KEYS.popitem(last=False)
            return vector
        except Exception as exc:
            cooldown = _query_embedding_int(
                "VECTOR_LAKE_QUERY_EMBEDDING_FAILURE_COOLDOWN_SECONDS",
                30,
            )
            with _QUERY_EMBEDDING_STATE_LOCK:
                _QUERY_EMBEDDING_FAILURE_UNTIL = time.monotonic() + cooldown
            log.warning(f"Failed to get query embedding: {exc}")
            return []

_VECTOR_CACHE = {
    "mtime": 0.0,
    "keys": [],
    "matrix": None
}

def _get_vector_search_results(query_vector: list[float], limit: int = 50) -> dict[str, float]:
    try:
        from vector_lake.db_store import (
            get_vector_connection,
            serialize_float32_vector,
        )
        conn = get_vector_connection()
        query_blob = serialize_float32_vector(query_vector)
        # Using match because it's fast. It returns L2 distance.
        # Cosine similarity for normalized vectors: 1 - L2^2 / 2
        # But sqlite-vec also has vec_distance_cosine which returns cosine distance.
        # We can just use match and sort by distance.
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
        return results
    except Exception as exc:
        log.warning("Failed to query vec_embeddings: %s", exc)
        raise SearchBackendError("vector") from exc

def _get_fts_search_results(query: str, limit: int = 50) -> list[dict]:
    try:
        from vector_lake.tokenizer_runtime import tokenize_for_fts
        query_tok = tokenize_for_fts(query)
    except ImportError:
        query_tok = query if query else ""
        
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
    except Exception as exc:
        log.warning("Failed to query fts5: %s", exc)
        raise SearchBackendError("fts5") from exc

def _classify_intent(query: str) -> str:
    temporal_keywords = {"上周", "去年", "昨天", "最近", "历史", "last week", "yesterday", "202"}
    entity_keywords = {"是谁", "哪里", "谁在", "who is", "where is", "公司", "人员", "关联", "图谱", "网络"}
    for kw in temporal_keywords:
        if kw in query.lower():
            return "temporal"
    for kw in entity_keywords:
        if kw in query.lower():
            return "entity"
    return "general"


@functools.lru_cache(maxsize=128)
def _expand_query_locally(query: str) -> list[str]:
    expanded_terms = set([query])
    for key, expansions in QUERY_EXPANSION_DICT.items():
        if key in query:
            expanded_terms.update(expansions)

    tokens = set()
    try:
        from vector_lake.tokenizer_runtime import segment_text
    except ImportError:
        segment_text = None

    for term in expanded_terms:
        if segment_text and CJK_REGEX.search(term):
            tokens.add(term.lower())
            for word in segment_text(term):
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
            f"ID={quoteattr(f'Memory_{index}')} Type={quoteattr(str(memory_type))} "
            f"State={quoteattr(str(state))} Score={quoteattr(str(score))} "
            f"Source={quoteattr(str(source))}"
        )
        return f"<Memory_Item {attrs}>{escape(text)}</Memory_Item>\n"
    return (
        f"- **{memory_type}:{memory.get('memory_key', memory.get('memory_id'))}** "
        f"(score: {score:.2f}, state: {state})\n"
        f"  {text}\n"
        f"  Source: {source}\n\n"
    )


def format_operational_memory_results(query: str, top_k: int = 8, as_xml: bool = False, include_history: bool = False, memory_types: list[str] | None = None) -> str:
    try:
        memories = governance_store.search_operational_memory(
            query,
            top_k=top_k,
            include_history=include_history,
            memory_types=memory_types,
        )
    except governance_store.OperationalMemoryNotReady as exc:
        if as_xml:
            return f"<MemoryResults State='unavailable' Reason={quoteattr(exc.reason)}/>"
        return (
            f"Operational memory unavailable ({exc.reason}); run projection maintenance."
        )
    if not memories:
        return "<MemoryResults />" if as_xml else "No operational memory matched the query."
    formatted = "".join(
        _format_memory_result(memory, as_xml=as_xml, index=index)
        for index, memory in enumerate(memories)
    )
    return f"<MemoryResults>\n{formatted}</MemoryResults>" if as_xml else formatted


def build_memory_packet(query: str, max_chars: int = 60000) -> dict:
    try:
        memories, historical = governance_store.search_operational_memory_views(
            query,
            current_top_k=24,
            history_top_k=12,
        )
    except governance_store.OperationalMemoryNotReady as exc:
        packet = (
            f"<MEMORY_PACKET status='unavailable' reason={quoteattr(exc.reason)}>\n"
            "Operational-memory projection requires explicit maintenance.\n"
            "</MEMORY_PACKET>"
        )
        return {
            "packet": packet,
            "memory_count": 0,
            "warning_count": 1,
            "omitted_count": 0,
        }
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
    """Apply a deterministic semantic-text boost without an external model call."""
    query_text = str(query or "").strip().casefold()
    terms = {
        str(term).strip().casefold()
        for term in _expand_query_locally(query)
        if str(term).strip()
    }
    reranked: list[tuple[float, int, dict]] = []
    for position, (base_score, node) in enumerate(candidates):
        key = str(node.get("_key") or "").casefold()
        title = str(node.get("title") or "").casefold()
        summary = str(node.get("summary") or "").casefold()
        aliases = " ".join(map(str, node.get("aliases") or ())).casefold()
        bonus = 0.0
        if query_text and query_text in {key, title}:
            bonus += 5.0
        for term in terms:
            if term in title or term in key:
                bonus += 2.0
            if term in aliases:
                bonus += 1.0
            if term in summary:
                bonus += 0.5
        reranked.append((float(base_score) + bonus, position, node))
    reranked.sort(key=lambda item: (-item[0], item[1]))
    return [(score, node) for score, _position, node in reranked]


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

def _load_search_index(index_path: str | os.PathLike) -> dict:
    """Load a current committed snapshot without waiting on the publisher lock.

    The projection sidecar is published last. ``read_committed_index_snapshot``
    verifies the sidecar, both artifact digests and their identities before and
    after decoding, so a concurrent atomic publisher is detected without making
    every search queue behind a potentially long rebuild lock.
    """
    last_error = None
    for attempt in range(3):
        try:
            return read_committed_index_snapshot(index_path, _acquire_lock=False)
        except Exception as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(0.05)
    assert last_error is not None
    raise last_error


def resolve_exact_entities(
    query: str,
    *,
    limit: int = 10,
    domain: str | None = None,
    cluster: str | None = None,
    include_history: bool = False,
) -> list[dict]:
    """Resolve exact page keys, ids, titles, and aliases from one committed index."""
    query = str(query or "").strip()
    if not query:
        return []
    if len(query) > _SEARCH_QUERY_CHAR_LIMIT:
        raise ValueError(
            f"identity query exceeds {_SEARCH_QUERY_CHAR_LIMIT} characters"
        )
    try:
        limit = max(1, min(_SEARCH_TOP_K_LIMIT, int(limit)))
    except (TypeError, ValueError) as exc:
        raise ValueError("limit must be an integer") from exc
    index_path = str(get_index_path())
    if not os.path.exists(index_path):
        return []
    index_data = _load_search_index(index_path)
    resolved = []
    for key, score in sorted(
        _exact_identity_scores(index_data, query).items(),
        key=lambda item: (-item[1], item[0]),
    ):
        raw_node = (index_data.get("nodes") or {}).get(key)
        if not isinstance(raw_node, dict):
            continue
        node = {"_key": key, **raw_node}
        if not _search_node_is_eligible(
            node,
            domain=domain,
            cluster=cluster,
            include_history=include_history,
            filter_expr=None,
        ):
            continue
        aliases = node.get("aliases") or []
        if isinstance(aliases, str):
            aliases = [aliases]
        resolved.append(
            {
                "key": key,
                "score": score,
                "id": node.get("id"),
                "title": node.get("title") or key,
                "aliases": list(aliases),
                "type": node.get("type"),
                "status": node.get("status"),
                "summary": node.get("summary") or "",
                "updated_at": node.get("updated_at") or node.get("updated") or "",
            }
        )
        if len(resolved) >= limit:
            break
    return resolved


def _graph_expansion_scores(
    seed_keys: set[str],
    weighted_edges: list[dict],
    *,
    hops: int = 2,
    alpha: float = 0.85,
    adjacency: CompactGraphAdjacency | None = None,
) -> dict[str, float]:
    """Run the bounded-hop walk without materializing whole-graph adjacency."""
    ppr_scores = {key: 1.0 for key in seed_keys}
    for _ in range(hops):
        active_adjacency: dict[str, list[tuple[str, float]]] | None = None
        if adjacency is None:
            active_keys = set(ppr_scores)
            active_adjacency = {}
            for edge in weighted_edges:
                source = edge["source"]
                target = edge["target"]
                weight = edge.get("weight", 1.0)
                if source in active_keys:
                    active_adjacency.setdefault(source, []).append((target, weight))
                if target in active_keys:
                    active_adjacency.setdefault(target, []).append((source, weight))

        next_scores = {
            key: (1 - alpha) if key in seed_keys else 0.0
            for key in ppr_scores
        }
        for node, current_score in ppr_scores.items():
            if adjacency is not None:
                total_weight = adjacency.total_weight(node)
                if not math.isfinite(total_weight) or total_weight <= 0:
                    continue
                for neighbor, weight in adjacency.iter_weighted_neighbors(node):
                    next_scores[neighbor] = (
                        next_scores.get(neighbor, 0.0)
                        + alpha * current_score * (weight / total_weight)
                    )
            else:
                assert active_adjacency is not None
                neighbors = active_adjacency.get(node, ())
                if not neighbors:
                    continue
                total_weight = sum(weight for _, weight in neighbors)
                if not math.isfinite(total_weight) or total_weight <= 0:
                    continue
                for neighbor, weight in neighbors:
                    next_scores[neighbor] = (
                        next_scores.get(neighbor, 0.0)
                        + alpha * current_score * (weight / total_weight)
                    )
        ppr_scores = next_scores
    return ppr_scores


def _search_node_is_eligible(
    node: dict,
    *,
    domain: str | None,
    cluster: str | None,
    include_history: bool,
    filter_expr: str | None,
) -> bool:
    """Apply the same metadata eligibility gate to every search candidate."""
    if domain and str(node.get("domain") or "").casefold() != str(domain).casefold():
        return False
    if (
        cluster
        and str(node.get("topic_cluster") or "").casefold()
        != str(cluster).casefold()
    ):
        return False
    if (
        not include_history
        and str(node.get("status") or "").casefold()
        in {"deprecated", "archived"}
    ):
        return False
    if filter_expr:
        try:
            if not _safe_eval(filter_expr, node):
                return False
        except Exception as exc:
            log.warning(
                "Filter expr evaluation failed for node %s: %s",
                node.get("_key", "<unknown>"),
                exc,
            )
            return False
    return True


def _eligible_exact_identity_scores(
    index_data: dict,
    query: str,
    *,
    domain: str | None,
    cluster: str | None,
    include_history: bool,
    filter_expr: str | None,
) -> dict[str, float]:
    eligible = {}
    nodes = index_data.get("nodes") or {}
    for key, score in _exact_identity_scores(index_data, query).items():
        raw_node = nodes.get(key)
        if not isinstance(raw_node, dict):
            continue
        node = {"_key": key, **raw_node}
        if _search_node_is_eligible(
            node,
            domain=domain,
            cluster=cluster,
            include_history=include_history,
            filter_expr=filter_expr,
        ):
            eligible[key] = score
    return eligible

def _lexical_fallback_scores(
    index_data: dict,
    terms,
    *,
    limit: int,
) -> dict[str, float]:
    """Provide a deterministic index-only fallback when FTS is unavailable."""
    normalized_terms = {
        str(term).strip().casefold()
        for term in terms
        if str(term).strip()
    }
    if not normalized_terms:
        return {}
    scored: list[tuple[float, str]] = []
    for key, node in index_data.get("nodes", {}).items():
        fields = (
            (str(key).casefold(), 4.0),
            (str(node.get("title") or "").casefold(), 4.0),
            (str(node.get("summary") or "").casefold(), 2.0),
            (str(node.get("raw_text") or "").casefold(), 1.0),
            (" ".join(map(str, node.get("aliases") or ())).casefold(), 1.0),
        )
        score = sum(
            weight
            for term in normalized_terms
            for value, weight in fields
            if term in value
        )
        if score > 0:
            scored.append((score, str(key)))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return {key: score for score, key in scored[: max(1, int(limit))]}


def _read_search_snippet(
    path: str | os.PathLike,
    *,
    max_chars: int = 2_500,
    max_frontmatter_chars: int = 65_536,
) -> str:
    """Read a bounded body snippet without materializing the whole Markdown file."""
    if max_chars <= 0:
        return ""
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        first_line = handle.readline(8_193)
        if not first_line.lstrip("\ufeff").startswith("---"):
            return (first_line + handle.read(max_chars))[:max_chars]
        if len(first_line) > 8_192 and not first_line.endswith(("\n", "\r")):
            raise SearchIndexError("Wiki frontmatter contains an oversized line.")
        consumed = len(first_line)
        while consumed <= max_frontmatter_chars:
            line = handle.readline(8_193)
            if not line:
                return ""
            if len(line) > 8_192 and not line.endswith(("\n", "\r")):
                raise SearchIndexError("Wiki frontmatter contains an oversized line.")
            consumed += len(line)
            if line.strip() == "---":
                return handle.read(max_chars)
        raise SearchIndexError("Wiki frontmatter exceeds the bounded search limit.")


def search_vector_lake(query: str, top_k: int = 5, as_xml: bool = False, domain: str = None, cluster: str = None, include_history: bool = False, mode: str = "page", filter_expr: str = None):
    query = str(query or "").strip()
    if len(query) > _SEARCH_QUERY_CHAR_LIMIT:
        raise ValueError(
            f"search query exceeds {_SEARCH_QUERY_CHAR_LIMIT} characters"
        )
    try:
        top_k = int(top_k)
    except (TypeError, ValueError) as exc:
        raise ValueError("top_k must be an integer") from exc
    top_k = max(1, min(_SEARCH_TOP_K_LIMIT, top_k))
    normalized_mode = str(mode or "page").lower()
    if normalized_mode in {"memory", "operational-memory", "operational_memory"}:
        return format_operational_memory_results(query, top_k=top_k, as_xml=as_xml, include_history=include_history)
    if normalized_mode in {"claim", "claims"}:
        return format_operational_memory_results(query, top_k=top_k, as_xml=as_xml, include_history=include_history, memory_types=["fact"])

    search_started = time.perf_counter()
    timings: dict[str, float] = {}
    backend_issues: list[str] = []

    def finish(value: str) -> str:
        timings["total_ms"] = (time.perf_counter() - search_started) * 1000.0
        encoded_size = len(value.encode("utf-8"))
        _record_search_performance(
            timings,
            result_chars=len(value),
            result_bytes=encoded_size,
            backend_issues=backend_issues,
        )
        return value

    def fail(issue: str) -> None:
        backend_issues.append(issue)
        timings["total_ms"] = (time.perf_counter() - search_started) * 1000.0
        _record_search_performance(
            timings,
            result_chars=0,
            result_bytes=0,
            backend_issues=backend_issues,
        )

    wiki_dir = str(get_wiki_dir())
    index_path = str(get_index_path())
    if not os.path.exists(index_path):
        return finish("Lake is drying. No index.json found, please ingest sources first.")
    phase_started = time.perf_counter()
    try:
        index_data = _load_search_index(index_path)
    except Exception as exc:
        log.error(f"Failed to read index.json: {exc}")
        timings["index_load_ms"] = (time.perf_counter() - phase_started) * 1000.0
        fail("projection_snapshot")
        raise SearchIndexError("The knowledge base index could not be read safely.") from exc
    timings["index_load_ms"] = (time.perf_counter() - phase_started) * 1000.0

    # ⚡ Bolt: Removed unused O(N) list comprehension of all index nodes to eliminate unnecessary memory allocation and CPU overhead in the search hot path.
    intent = _classify_intent(query)
    phase_started = time.perf_counter()
    exact_identity_scores = _eligible_exact_identity_scores(
        index_data,
        query,
        domain=domain,
        cluster=cluster,
        include_history=include_history,
        filter_expr=filter_expr,
    )
    exact_identity_keys = set(exact_identity_scores)
    timings["exact_identity_ms"] = (
        time.perf_counter() - phase_started
    ) * 1000.0
    timings["exact_identity_hits"] = float(len(exact_identity_scores))
    phase_started = time.perf_counter()
    tokens = _expand_query_locally(query)
    timings["tokenize_ms"] = (time.perf_counter() - phase_started) * 1000.0
    if not tokens and not exact_identity_scores:
        return finish("No valid search tokens.")

    scored = []
    
    # PHASE 2 FTS5 + VECTOR HYBRID QUERY
    hybrid_scores = dict(exact_identity_scores)
    fts_result_count = 0

    # 1. FTS5 Search
    phase_started = time.perf_counter()
    try:
        # Use expanded tokens as the query basis to preserve LLM synonym expansions
        expanded_query = query + " " + " ".join(tokens)
        fts_results = _get_fts_search_results(expanded_query, limit=top_k * 5)
        fts_result_count = len(fts_results)
        for row in fts_results:
            key = row['node_key']
            raw_score = row.get('rank')
            if raw_score is None:
                raw_score = row.get('score', 0)
            fts_score = raw_score * -1.0  # SQLite BM25 is negative
            hybrid_scores[key] = hybrid_scores.get(key, 0.0) + fts_score
    except Exception as exc:
        backend = exc.backend if isinstance(exc, SearchBackendError) else "fts5"
        backend_issues.append(backend)
        _log_search_backend_failure(backend)
        hybrid_scores.update(
            _lexical_fallback_scores(
                index_data, [query, *tokens], limit=top_k * 5
            )
        )
    timings["fts_ms"] = (time.perf_counter() - phase_started) * 1000.0

    # 2. Vector Search (Hybrid blending)
    phase_started = time.perf_counter()
    embedding_requested = not exact_identity_scores and _should_query_embedding(
        fts_result_count=fts_result_count,
        top_k=top_k,
    )
    query_vector = _get_query_embedding(query) if embedding_requested else []
    timings["embedding_ms"] = (time.perf_counter() - phase_started) * 1000.0
    timings["embedding_bypassed_by_fts"] = 0.0 if embedding_requested else 1.0
    if (
        not query_vector
        and embedding_requested
        and _query_embedding_enabled()
        and os.environ.get("GEMINI_API_KEY")
    ):
        backend_issues.append("query_embedding")
    if query_vector:
        phase_started = time.perf_counter()
        try:
            vector_results = _get_vector_search_results(query_vector, limit=top_k * 5)
            for key, sim in vector_results.items():
                vec_score = (sim ** 2) * 15.0
                hybrid_scores[key] = hybrid_scores.get(key, 0.0) + vec_score
        except SearchBackendError as exc:
            backend_issues.append(exc.backend)
        timings["vector_ms"] = (time.perf_counter() - phase_started) * 1000.0
    else:
        timings["vector_ms"] = 0.0

    # FTS/vec rows are committed before their matching projection files are
    # published.  Revalidate after those SQLite reads so results from a newer
    # database generation are never blended with the older index snapshot.
    phase_started = time.perf_counter()
    try:
        current_index_data = _load_search_index(index_path)
    except Exception as exc:
        timings["generation_check_ms"] = (
            time.perf_counter() - phase_started
        ) * 1000.0
        fail("projection_generation_check")
        raise SearchIndexError(
            "The knowledge base projection changed during search; retry after sync."
        ) from exc
    if current_index_data.get("projection_manifest") != index_data.get(
        "projection_manifest"
    ):
        index_data = current_index_data
        hybrid_scores = _lexical_fallback_scores(
            index_data,
            [query, *tokens],
            limit=top_k * 5,
        )
        exact_identity_scores = _eligible_exact_identity_scores(
            index_data,
            query,
            domain=domain,
            cluster=cluster,
            include_history=include_history,
            filter_expr=filter_expr,
        )
        exact_identity_keys = set(exact_identity_scores)
        for key, score in exact_identity_scores.items():
            hybrid_scores[key] = max(hybrid_scores.get(key, 0.0), score)
        backend_issues.append("projection_generation_changed")
    timings["generation_check_ms"] = (
        time.perf_counter() - phase_started
    ) * 1000.0

    for key, score in hybrid_scores.items():
        if key in index_data.get('nodes', {}):
            node = {'_key': key, **index_data['nodes'][key]}
            if not _search_node_is_eligible(
                node,
                domain=domain,
                cluster=cluster,
                include_history=include_history,
                filter_expr=filter_expr,
            ):
                continue

            if not include_history and node.get('status', '').lower() == 'decayed' and intent != 'temporal':
                score *= 0.2
            scored.append((score, node))

    scored.sort(key=lambda item: item[0], reverse=True)

    # P2-1: Dynamic Graph Expansion via Multi-hop PPR (Personalized PageRank)
    phase_started = time.perf_counter()
    top_keys = {node["_key"] for _, node in scored[:5]}
    graph_ready = (index_data.get("graph_state") or {}).get("dirty") is False
    if top_keys and graph_ready and index_data.get("weighted_edges"):
        adjacency = get_compact_graph_adjacency(index_data)
        ppr_scores = _graph_expansion_scores(
            top_keys,
            index_data["weighted_edges"],
            adjacency=adjacency,
        )

        existing_keys = {node["_key"] for _, node in scored}
        expansion_limit = 12 if intent == "entity" else 5
        
        sorted_expansions = sorted(
            [(k, v) for k, v in ppr_scores.items() if k not in existing_keys], 
            key=lambda x: x[1], 
            reverse=True
        )
        
        expansion_count = 0
        for expanded_key, ppr_weight in sorted_expansions:
            if expansion_count >= expansion_limit:
                break
            expanded_node = index_data["nodes"].get(expanded_key)
            if expanded_node:
                candidate = {"_key": expanded_key, **expanded_node}
                if not _search_node_is_eligible(
                    candidate,
                    domain=domain,
                    cluster=cluster,
                    include_history=include_history,
                    filter_expr=filter_expr,
                ):
                    continue
                # Scale PPR weight to match BM25 range approximately
                scored.append((ppr_weight * 15.0, candidate))
                expansion_count += 1
    timings["graph_ms"] = (time.perf_counter() - phase_started) * 1000.0

    scored.sort(key=lambda item: item[0], reverse=True)

    # Phase 1: Expand candidate pool for reranking
    candidate_pool = []
    source_count = 0
    pool_size = max(40, top_k * 3)
    max_sources_pool = int(pool_size * 0.6)
    for score, node in scored:
        node_type = node.get("type", "").lower()
        if node_type == "source":
            if source_count < max_sources_pool or node.get("_key") in exact_identity_keys:
                candidate_pool.append((score, node))
                source_count += 1
        else:
            candidate_pool.append((score, node))
        if len(candidate_pool) >= pool_size:
            break
            
    # Phase 2: Local deterministic ranking. Text-model reranking is delegated
    # to the host agent when explicitly requested, not performed by runtime code.
    phase_started = time.perf_counter()
    reranked = _rerank_candidates_locally(query, candidate_pool)
    timings["rerank_ms"] = (time.perf_counter() - phase_started) * 1000.0

    # Phase 3: Final top_k extraction
    final_scored = []
    source_count = 0
    max_sources_final = int(top_k * 0.6)
    for score, node in reranked:
        node_type = node.get("type", "").lower()
        if node_type == "source":
            if source_count < max_sources_final or node.get("_key") in exact_identity_keys:
                final_scored.append((score, node))
                source_count += 1
        else:
            final_scored.append((score, node))
        if len(final_scored) >= top_k:
            break

    result = ""
    result_char_limit = _search_result_char_limit()
    result_byte_limit = _search_result_byte_limit()
    result_bytes = 0
    phase_started = time.perf_counter()
    for index, (score, node) in enumerate(final_scored):
        filepath = os.path.join(wiki_dir, f"{node['_key']}.md")
        snippet = ""
        if os.path.exists(filepath):
            try:
                snippet = _read_search_snippet(filepath)
            except SearchIndexError:
                backend_issues.append("wiki_snippet")
                snippet = "[Snippet unavailable]"
            
        tension_edges = node.get("tension_edges", [])
        tension_info = ""
        if tension_edges:
            tension_info = "  [Tension Edges]:\n"
            for te in tension_edges:
                tension_info += f"    -> {te.get('target')} (Polarity: {te.get('polarity')}, Intensity: {te.get('intensity')}): {te.get('context')}\n"
                
        if as_xml:
            source_name = f"{node['_key']}.md"
            block = (
                f"<Evidence_Node ID={quoteattr(f'Wiki_{index}')} "
                f"Source={quoteattr(source_name)}>\n"
                f"{escape(tension_info + snippet)}\n</Evidence_Node>\n"
            )
        else:
            block = f"- **{node.get('title', node['_key'])}** (score: {score:.1f})\n{tension_info}  {snippet}...\n\n"
        block_bytes = len(block.encode("utf-8"))
        if (
            len(result) + len(block) > result_char_limit
            or result_bytes + block_bytes > result_byte_limit
        ):
            backend_issues.append("result_budget")
            break
        result += block
        result_bytes += block_bytes
    timings["materialize_ms"] = (time.perf_counter() - phase_started) * 1000.0
    issues = sorted(set(backend_issues))
    if as_xml:
        state = "degraded" if issues else "ok"
        status = (
            f"<SearchStatus State={quoteattr(state)} "
            f"Backends={quoteattr(','.join(issues))}/>\n"
        )
        body = result or "<NoEvidence/>\n"
        return finish(f"<EvidenceResults>\n{status}{body}</EvidenceResults>")
    if not result:
        result = "No matching evidence found.\n"
    if issues:
        return finish(f"[Search degraded: {', '.join(issues)}]\n{result}")
    return finish(result)


def assemble_context(query: str, max_chars: int = DEFAULT_MAX_CHARS) -> dict:
    index_budget = int(max_chars * TOKEN_BUDGET["index_summary"])
    
    # P2-2: Dynamic Sliding Window for Budget
    # Allow memory to burst up to 50% if there are critical alerts
    memory_packet = build_memory_packet(query, max_chars=int(max_chars * 0.50))
    actual_memory_used = len(memory_packet["packet"])
    
    # Wiki dynamically eats the remaining budget
    wiki_budget = max_chars - actual_memory_used - index_budget

    index_path = str(get_index_path())
    index_existed_before_search = os.path.exists(index_path)
    search_index_data = None
    if index_existed_before_search:
        try:
            search_index_data = _load_search_index(index_path)
        except Exception as exc:
            raise SearchIndexError(
                "The knowledge base projection could not be verified before "
                "context assembly."
            ) from exc

    search_results = search_vector_lake(query, top_k=15, as_xml=False)
    wiki_context = ""
    page_count = 0
    for match in re.finditer(r"\*\*(.+?)\*\*.*?\n\s+(.*?)\.\.\.\n", search_results, re.DOTALL):
        page_content = match.group(0)
        if len(wiki_context) + len(page_content) > wiki_budget:
            break
        wiki_context += page_content
        page_count += 1

    index_summary = ""
    if index_existed_before_search:
        try:
            index_data = _load_search_index(index_path)
        except Exception as exc:
            raise SearchIndexError(
                "The knowledge base projection changed during context assembly; "
                "retry after sync."
            ) from exc
        if index_data.get("projection_manifest") != search_index_data.get(
            "projection_manifest"
        ):
            raise SearchIndexError(
                "The knowledge base projection generation changed during context "
                "assembly; retry."
            )
        lines = []
        for key, node in islice(index_data.get("nodes", {}).items(), 50):
            lines.append(f"[{node.get('type', '?')}] {node.get('title', key)}")
        index_summary = "\n".join(lines)[:index_budget]
    elif os.path.exists(index_path):
        raise SearchIndexError(
            "The knowledge base projection appeared during context assembly; retry."
        )

    purpose = ""
    try:
        from vector_lake.purpose_contract import render_strategy_directive
        purpose = render_strategy_directive()
    except Exception:
        pass

    return {
        "memory_packet": memory_packet["packet"],
        "memory_count": memory_packet["memory_count"],
        "memory_warning_count": memory_packet["warning_count"],
        "memory_omitted_count": memory_packet["omitted_count"],
        "wiki_context": wiki_context,
        "wiki_page_count": page_count,
        "index_summary": index_summary,
        "purpose": purpose,
        "budget_used": len(memory_packet["packet"]) + len(wiki_context) + len(index_summary) + len(purpose),
        "budget_max": max_chars,
    }
