import logging
import os
import re
import threading
import time
from array import array
from collections import OrderedDict
from datetime import datetime, timezone

import functools
import ast
import operator

from vector_lake import governance_store, search_ledger
from vector_lake.wiki_utils import get_index_path, get_wiki_dir


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("vector-lake-tool-search")

BUDGET_SHARES = {
    # Character shares.  The constant was called ``TOKEN_BUDGET``, but nothing here counts tokens:
    # ``assemble_context`` compares these shares against ``len(str)`` and ``DEFAULT_MAX_CHARS``.  ``operational_memory`` is the nominal memory share and
    # ``memory_burst`` the ceiling the documented alert burst may raise it to; ``wiki_pages`` is
    # realised as the budget's *remainder* rather than as this share, and ``chat_history`` is not
    # used by this assembler at all.  Three of these five keys were read nowhere, while the memory
    # share was a 0.50 literal that contradicted ``operational_memory``.
    "operational_memory": 0.30,
    "memory_burst": 0.50,
    "wiki_pages": 0.45,
    "chat_history": 0.05,
    "index_summary": 0.05,
    "system_prompt": 0.15,
}
DEFAULT_MAX_CHARS = 200000

# A query embedding runs inside an MCP tool call, so it must not outlive the
# caller's patience.  A healthy provider round trip measured 9.6 s cold and ~0.5 s
# warm on this host; a quota-retry loop used to sleep a flat 60 s per attempt.
# The budget is generous against the healthy path and hard against the loop.
QUERY_EMBEDDING_BUDGET_SECONDS = 20.0

#: Minimum cosine similarity a vector hit must reach to enter the fusion.
#:
#: It is a cosine threshold and not an L2 one because of the conversion above: ``vec_embeddings``
#: stores unit vectors (``db_store.upsert_embedding`` normalises before writing, which is why a
#: sampled norm measures exactly 1.000000), so ``1 - d^2/2`` is the cosine.  If that write-time
#: normalisation were ever removed, this gate and the conversion would both go wrong silently --
#: they are one assumption, stated here rather than implied by a bare literal.
VECTOR_MIN_COSINE = 0.5

#: Reciprocal-rank-fusion constant, used when ``VECTOR_LAKE_FUSION=rrf``.  It is the only knob
#: because it is the only insensitive one: moving k shifts ranks by a few positions, whereas the
#: magnitude sum decides its winner by which source's arbitrary scale happens to be larger for
#: that query (``-bm25`` measured 5.1-47.6 across five queries against a hard vector ceiling of 15).
RRF_K = 60

FUSION_MODES = ("sum", "rrf")


def _fusion_mode() -> str:
    """``sum`` (the long-standing blend) or ``rrf`` (``VECTOR_LAKE_FUSION=rrf``)."""
    requested = os.environ.get("VECTOR_LAKE_FUSION", "sum").strip().lower()
    if requested not in FUSION_MODES:
        log.warning("Unknown VECTOR_LAKE_FUSION=%r; using 'sum'.", requested)
        return "sum"
    return requested


def _expansion_quota(pool_size: int) -> int | None:
    """Pool slots guaranteed to graph expansion, or ``None`` for the long-standing behaviour.

    Unset means expansion competes for whatever the fusion stage left over.  Both recall paths are
    asked for ``_candidate_depth()`` candidates and the pool is ``_candidate_pool()``, so the
    leftovers are often nothing: measured on the live lake, three of five sampled queries left zero
    slots, which made graph expansion a documented candidate source that could not supply a
    candidate.
    """
    raw = os.environ.get("VECTOR_LAKE_EXPANSION_QUOTA")
    if raw is None or not str(raw).strip():
        return None
    try:
        value = int(str(raw).strip())
    except ValueError:
        log.warning("Unknown VECTOR_LAKE_EXPANSION_QUOTA=%r; using the default.", raw)
        return None
    if value < 0:
        log.warning("VECTOR_LAKE_EXPANSION_QUOTA must be >= 0; using the default.")
        return None
    return min(value, pool_size)


def _env_positive_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        value = int(str(raw).strip())
    except ValueError:
        log.warning("Ignoring unparsable %s=%r", name, raw)
        return default
    if value < 1:
        log.warning("%s must be >= 1; using %d", name, default)
        return default
    return value


#: How deep each retrieval stage looks, and how large the pool the reranker sees.
#:
#: These are properties of the *pipeline*, not of the window the caller asked for, and the values
#: are exactly what the pipeline computed at the default ``top_k=5`` before this change
#: (``top_k * 5`` candidates, ``max(40, top_k * 3)`` pool, ``int(pool * 0.6)`` source slots in the
#: pool, ``max(1, int(top_k * 0.6))`` source slots in the answer).
#:
#: They used to scale with ``top_k``, which meant ``top_k`` did not only decide how many answers
#: came back -- it decided *which* pages were eligible.  A larger window drew a deeper candidate
#: list, resized the pool, refilled the source caps and therefore changed the pool-local min-max
#: normalisation inside the reranker, so the top-5 of a top-20 request was not the top-5 of a top-5
#: request.  Measured: of 268 queries whose top-5 pool held no relevant page, 36 (13.4%) had their
#: best page inside the top five of the same ranking asked for twenty, while that page was absent
#: from the top-5 result -- the evaluation read "not found" where the pipeline had capped it out,
#: and D2 had already recorded the same heuristic's non-monotonicity at ``top_k=1``.
#:
#: With the depth fixed, the first *k* of a top-k result are the first *k* of any larger one.  The
#: two knobs exist so a deeper pipeline can be measured as its own change, one variable at a time.
CANDIDATE_DEPTH = 25
CANDIDATE_POOL = 40
#: How much a Source page's score is scaled down for the final ordering, once the pool has been
#: chosen.  A *preference*, not a filter: the ranking still contains every eligible page, and the
#: source type only decides where it sits.
#:
#: This replaces an absolute cap on how many Source pages could appear in the answer, which had two
#: costs that were measured rather than imagined.  It shortened the window: with the cap at 3, a
#: top-20 request came back with fewer than twenty results for 104 of 333 queries (31%), because a
#: source-heavy pool had nothing else to fill the remaining slots with.  And any threshold on the
#: answer's *contents* is a function of the window size, which is the one property restored by
#: keeping the pipeline independent of top_k; a multiplicative penalty is scale-free (it means the
#: same thing under sum, whose scores are 5-50, and under rrf, whose scores are ~0.016), so the
#: ordering is a function of the pool alone and top-k stays a pure window.
SOURCE_RANK_PENALTY = 0.6


def _candidate_depth() -> int:
    return _env_positive_int("VECTOR_LAKE_CANDIDATE_DEPTH", CANDIDATE_DEPTH)


def _candidate_pool() -> int:
    return _env_positive_int("VECTOR_LAKE_CANDIDATE_POOL", CANDIDATE_POOL)


def _source_rank_penalty() -> float:
    """``SOURCE_RANK_PENALTY``, or a value from ``VECTOR_LAKE_SOURCE_RANK_PENALTY``.

    ``1.0`` turns the preference off (pure score order) and ``0.0`` pushes every Source page to the
    bottom while keeping it in the ranking.  Both are useful for measuring the preference rather
    than assuming it.
    """
    raw = os.environ.get("VECTOR_LAKE_SOURCE_RANK_PENALTY")
    if raw is None or not str(raw).strip():
        return SOURCE_RANK_PENALTY
    try:
        value = float(str(raw).strip())
    except ValueError:
        log.warning("Ignoring unparsable VECTOR_LAKE_SOURCE_RANK_PENALTY=%r", raw)
        return SOURCE_RANK_PENALTY
    if not 0.0 <= value <= 1.0:
        log.warning("VECTOR_LAKE_SOURCE_RANK_PENALTY must be within 0..1; using the default.")
        return SOURCE_RANK_PENALTY
    return value

CJK_REGEX = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")

#: A four-digit year, not the substring the intent guess used to match on.
_YEAR_PATTERN = re.compile(r"\b(19|20)\d{2}\b")
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


# A query embedding costs one provider round trip and the embedding is a pure
# function of the query text, so repeats (the common case for an agent that
# re-asks with a stable phrasing) can be served locally.  Bounded and LRU so a
# long-lived server cannot grow without limit; vectors are stored as float32
# because that is what the vector projection consumes anyway.
QUERY_EMBEDDING_CACHE_SIZE = 256
_QUERY_EMBEDDING_CACHE: "OrderedDict[str, array]" = OrderedDict()
_QUERY_EMBEDDING_CACHE_LOCK = threading.Lock()


def _cached_query_embedding(query: str):
    with _QUERY_EMBEDDING_CACHE_LOCK:
        cached = _QUERY_EMBEDDING_CACHE.get(query)
        if cached is not None:
            _QUERY_EMBEDDING_CACHE.move_to_end(query)
        return cached


def _store_query_embedding(query: str, values) -> None:
    with _QUERY_EMBEDDING_CACHE_LOCK:
        _QUERY_EMBEDDING_CACHE[query] = array("f", values)
        _QUERY_EMBEDDING_CACHE.move_to_end(query)
        while len(_QUERY_EMBEDDING_CACHE) > QUERY_EMBEDDING_CACHE_SIZE:
            _QUERY_EMBEDDING_CACHE.popitem(last=False)


def _get_query_embedding(query: str) -> tuple[list[float], str | None]:
    """Query vector plus an explicit degradation reason when it is unavailable."""
    if not os.environ.get("GEMINI_API_KEY"):
        return [], "GEMINI_API_KEY is not set"
    cached = _cached_query_embedding(query)
    if cached is not None:
        return list(cached), None
    try:
        from vector_lake.embedding_scheduler import embed_texts

        embeddings = embed_texts(
            [query],
            budget_seconds=QUERY_EMBEDDING_BUDGET_SECONDS,
            durable_reservation=False,
        )
        if not embeddings:
            return [], "embedding provider returned no vector"
        _store_query_embedding(query, embeddings[0])
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
            # ``vec_embeddings.entity_id`` holds a **page key** (the writer passes ``node_key``, and
            # ``db_store`` deletes by the same name), not ``entities.entity_id``.  The column name is
            # a trap: a join to ``entities.entity_id`` returns nothing, silently.  The FTS path keys
            # by ``node_key`` too, so the two remain in one namespace and the fusion cannot mix them.
            page_key = row["entity_id"]
            # distance is L2. convert to approx sim: 1 - (dist^2)/2
            dist = row["distance"]
            sim = 1.0 - (dist * dist) / 2.0
            if sim > VECTOR_MIN_COSINE:
                results[page_key] = sim
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

def _keyword_intent(query: str) -> str:
    """A keyword guess at the query's shape -- not an intent classifier, and named accordingly.

    Two consumers only: it exempts a decayed page from the relevance penalty when the query looks
    temporal, and it chooses between 12 and 5 graph-expansion candidates.  The year used to be the
    substring ``"202"``, which matched any query containing those digits (an identifier, a version,
    a price) and called it temporal; it is a number pattern now.  ``"公司"``/``"关联"`` stay
    deliberately broad: they are hints, and a wrong ``entity`` guess costs five more expansion
    candidates, not a wrong answer.
    """
    temporal_keywords = {"上周", "去年", "昨天", "最近", "历史", "last week", "yesterday"}
    entity_keywords = {"是谁", "哪里", "谁在", "who is", "where is", "公司", "人员", "关联", "图谱", "网络"}
    lowered = query.lower()
    if _YEAR_PATTERN.search(lowered) or any(kw in lowered for kw in temporal_keywords):
        return "temporal"
    for kw in entity_keywords:
        if kw in lowered:
            return "entity"
    return "general"


_QUERY_TERMS_REGISTERED = False


def _ensure_query_terms() -> None:
    """Register ``QUERY_EXPANSION_DICT`` terms once, *outside* any cached function.

    This used to run inside ``_expand_query_locally``, which is ``lru_cache``d: the side effect
    happened on a miss and not on a hit, and the cache key covered the backend identity but not the
    dictionary state.  No backend in this tree exposes ``add_word`` -- ``tokenizer.add_word`` returns
    False and warns once -- so nothing depends on it today.  It is still the wrong place for a
    global mutation: the next backend that does support it would make indexing and querying disagree
    about where words end, which is exactly what the search index's content digest exists to prevent.
    """
    global _QUERY_TERMS_REGISTERED
    if _QUERY_TERMS_REGISTERED:
        return
    from vector_lake import tokenizer as _tokenizer

    if _tokenizer.backend_name() != "unavailable":
        for term in QUERY_EXPANSION_DICT:
            _tokenizer.add_word(term)
        for expansions in QUERY_EXPANSION_DICT.values():
            for expansion in expansions:
                _tokenizer.add_word(expansion)
    _QUERY_TERMS_REGISTERED = True


@functools.lru_cache(maxsize=128)
def _local_expansions(query: str) -> tuple[str, ...]:
    """The token set for a query.  Pure: same input, same output, no global side effects."""
    expanded_terms = set([query])
    for key, expansions in QUERY_EXPANSION_DICT.items():
        if key in query:
            expanded_terms.update(expansions)

    tokens = set()
    from vector_lake import tokenizer as _tokenizer

    backend_ready = _tokenizer.backend_name() != "unavailable"

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
    # A tuple, not a list: this value is the cache entry, and a caller that mutated what it got
    # back would corrupt the cache for every later query.
    return tuple(tokens)


def _expand_query_locally(query: str) -> list[str]:
    """The expansion tokens for a query.  Registers the dictionary terms first, once."""
    _ensure_query_terms()
    return list(_local_expansions(query))


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
    memories, historical = governance_store.search_memory_packet_views(query)
    stale_or_conflicted = [
        item for item in historical
        if str(item.get("validity_state", "")).lower() in {"conflicted", "review-due", "needs-review", "superseded", "expired"}
    ]

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

    # The packet is built to fit rather than assembled and then cut.  Cutting at ``max_chars``
    # severed lines and could leave the closing tag detached, and it made ``omitted_count``
    # uncomputable: the field reported ``len(memories) - 12``, a number that matched neither the
    # memories dropped nor the 12-pointer display cap.  It is now exactly the number of memory
    # lines the budget could not carry, and the packet stays well-formed.
    #
    # ``warning_count`` is the true count while only six warnings are shown, because the field is
    # read as ``memory_warning_count``, printed to the caller as "warnings", and used as the
    # trigger for the burst re-render -- a capped counter made all three report "6" for any larger
    # set.
    section_order = ("Current Preferences", "Open Decisions", "Task State", "Relevant Facts")
    truncated_marker = "...[memory packet truncated]"

    warnings_block = [
        f"- [{memory.get('validity_state')}] {memory.get('memory_type')}:{memory.get('memory_key')} "
        f"-> {str(memory.get('text', ''))[:260]}"
        for memory in stale_or_conflicted[:6]
    ] or ["- None matched."]

    head = [
        "<MEMORY_PACKET>",
        f"Generated: {datetime.now(timezone.utc).isoformat()}",
        f"Query: {query}",
        "Policy: Use this packet as the machine-facing runtime memory. If it conflicts with wiki prose, prefer active non-conflicted memory items and surface the conflict.",
        "",
    ]
    tail = [
        "",
        "## Conflicts / Stale Warnings",
        *warnings_block,
        "",
        "## Evidence Pointers",
        *(evidence_pointers[:12] or ["- None matched."]),
        "</MEMORY_PACKET>",
    ]

    # The marker is reserved whether or not it is needed, so appending it cannot push the packet
    # past the budget it was fitted to.
    overhead = sum(len(line) + 1 for line in head + tail) + len(truncated_marker) + 1
    body_budget = max(0, max_chars - overhead)

    body: list[str] = []
    used = 0
    truncated = False
    for title in section_order:
        if truncated:
            break
        block = [f"## {title}", *(sections[title] or ["- None matched."]), ""]
        for line in block:
            if used + len(line) + 1 > body_budget:
                truncated = True
                break
            body.append(line)
            used += len(line) + 1
    if truncated:
        body.append(truncated_marker)

    emitted_memories = sum(1 for line in body if line.startswith("- ["))
    omitted = max(0, len(memories) - emitted_memories)

    packet = "\n".join(head + body + tail)
    if len(packet) > max_chars:
        # ``head`` plus ``tail`` alone overran the budget, so there was nothing to fit: no memory
        # line was emitted above, and the packet is cut to keep the ``budget_used <= budget_max``
        # invariant ``assemble_context`` depends on.
        packet = packet[: max(0, max_chars - 80)].rstrip() + f"\n{truncated_marker}\n</MEMORY_PACKET>"
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


def _search_scored_pages(
    query: str,
    top_k: int,
    domain: str = None,
    cluster: str = None,
    include_history: bool = False,
    filter_expr: str = None,
    catalog=None,
    projection_note: str | None = None,
):
    """Ranked pages plus retrieval notes.

    Split out of ``search_vector_lake`` so callers that need the pages can use
    them directly.  ``assemble_context`` used to re-parse the human-readable
    string with a regex, which silently produced an empty wiki context whenever
    the formatting changed or a title contained the delimiter.

    Nodes and the personalised-PageRank adjacency come from the SQLite projection
    (``page_index_projection``) instead of a full ``index.json`` parse per process
    and a full 30 140-edge dict rebuild per query.
    """
    from vector_lake import page_index_projection

    started = time.perf_counter()

    def _record(returned_rows, notes, error=None):
        search_ledger.record(
            query,
            mode="page",
            top_k=top_k,
            returned=returned_rows,
            notes=notes,
            elapsed_ms=(time.perf_counter() - started) * 1000.0,
            error=error,
        )

    # Read-only source selection.  A reader never repairs the projection: the
    # rebuild needs the write lock, and a reader that lost that race used to give
    # up entirely (a 60 s MCP timeout with no answer).  ``index.json`` is the
    # sovereign artifact, so when the projection is behind the file answers
    # instead -- the note records that the answer did not come from the
    # projection, and the outbox consumer rebuilds it out of band.
    #
    # ``catalog`` is injectable so a caller that already resolved it for this
    # request (``assemble_context``) does not parse the file a second time.
    if catalog is None:
        catalog, projection_note = page_index_projection.read_catalog()
    if catalog is None:
        if page_index_projection.index_file_stamp() is None:
            answer = "Lake is drying. No index.json found, please ingest sources first."
            _record([], [], error=answer)
            return [], [], answer
        answer = "Error reading the knowledge base index. Please ensure the index exists and is not corrupted."
        _record([], [], error=answer)
        return [], [], answer

    intent = _keyword_intent(query)
    tokens = _expand_query_locally(query)
    if not tokens:
        _record([], [], error="No valid search tokens.")
        return [], [], "No valid search tokens."

    fusion_mode = _fusion_mode()
    scored = []
    
    # PHASE 2 FTS5 + VECTOR HYBRID QUERY
    hybrid_scores = {}
    
    # 1. FTS5 Search
    fts_ranked: list[str] = []
    try:
        # Use expanded tokens as the query basis to preserve LLM synonym expansions
        expanded_query = query + " " + " ".join(tokens)
        fts_results = _get_fts_search_results(expanded_query, limit=_candidate_depth())
        for position, row in enumerate(fts_results):
            key = row['node_key']
            raw_score = row.get('rank')
            if raw_score is None:
                raw_score = row.get('score', 0)
            if fusion_mode == "rrf":
                # Reciprocal rank: the source's position, not its arbitrary magnitude.
                contribution = 1.0 / (RRF_K + position + 1)
            else:
                contribution = raw_score * -1.0  # SQLite BM25 is negative
            hybrid_scores[key] = hybrid_scores.get(key, 0.0) + contribution
            fts_ranked.append(key)
    except Exception as e:
        log.error(f"FTS5 Search failed: {e}")

    # 2. Vector Search (Hybrid blending)
    vector_notes = []
    vector_ranked: list[str] = []
    query_vector, embedding_error = _get_query_embedding(query)
    if query_vector:
        vector_results, vector_error = _get_vector_search_results(query_vector, limit=_candidate_depth())
        if vector_error:
            vector_notes.append(vector_error)
        elif not vector_results:
            vector_notes.append(
                "no stored vectors matched; run `embedding-backfill --apply` to (re)build the vector projection"
            )
        # ``_get_vector_search_results`` inserts in ascending distance, so enumeration order is
        # the vector ranking that RRF needs.
        for position, (key, sim) in enumerate(vector_results.items()):
            if fusion_mode == "rrf":
                contribution = 1.0 / (RRF_K + position + 1)
            else:
                # scale similarity so it competes/blends with BM25.
                contribution = (sim ** 2) * 15.0
            hybrid_scores[key] = hybrid_scores.get(key, 0.0) + contribution
            vector_ranked.append(key)
    else:
        vector_notes.append(embedding_error or "query embedding unavailable")

    fts_hits = set(fts_ranked)
    vector_hits = set(vector_ranked)

    # Only the keys the hybrid stage actually produced are materialised, so the
    # 7 178-node dict never has to exist in memory.
    nodes = catalog.nodes_by_key(list(hybrid_scores))

    for key, score in hybrid_scores.items():
        node = nodes.get(key)
        if node is None:
            continue
        if key in fts_hits and key in vector_hits:
            origin = "both"
        elif key in fts_hits:
            origin = "fts"
        else:
            origin = "vec"
        node = {"_key": key, **node, "_origin": origin}
        if not _passes_filters(node, domain, cluster, include_history, filter_expr):
            continue
        if not include_history and node.get('status', '').lower() == 'decayed' and intent != 'temporal':
            score *= 0.2
        scored.append((score, node))

    scored.sort(key=lambda item: item[0], reverse=True)

    # P2-1: Dynamic Graph Expansion via Multi-hop PPR (Personalized PageRank)
    top_keys = {node["_key"] for _, node in scored[:5]}
    adj = catalog.adjacency() if top_keys else None
    if top_keys and adj:
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
        expansion_keys = [key for key, _ in sorted_expansions[:expansion_limit]]
        expanded_nodes = catalog.nodes_by_key(expansion_keys)
        
        for expansion_rank, (expanded_key, ppr_weight) in enumerate(sorted_expansions[:expansion_limit]):
            expanded_node = expanded_nodes.get(expanded_key)
            if expanded_node is None:
                continue
            # Graph expansion is an additional candidate source, not an exemption
            # from the caller's filters.
            if not _passes_filters(expanded_node, domain, cluster, include_history, filter_expr):
                continue
            if fusion_mode == "rrf":
                # The expansion ordering is a third *ranked list*, expressed in the same units as
                # the fused ones.  Scaling it by 15 instead put it in the space the other two no
                # longer use after RRF: at sum-fusion ``ppr_weight * 15`` is about 0.45 against
                # BM25 magnitudes, but against RRF's ~0.015 it dominated -- measured 40 queries with
                # real embeddings, expansion's share of returned pages went 2/200 to 156/200 and
                # recall@5 fell 0.75 to 0.33.  One list, one vote, or the stages are not comparable.
                expansion_score = 1.0 / (RRF_K + expansion_rank + 1)
            else:
                expansion_score = ppr_weight * 15.0
            scored.append((expansion_score, {"_key": expanded_key, **expanded_node, "_origin": "ppr"}))

    scored.sort(key=lambda item: item[0], reverse=True)

    # Phase 1: Expand candidate pool for reranking.  Pool size is fixed, so the reranker's
    # pool-local min-max normalisation no longer depends on how many answers the caller asked for.
    pool_size = _candidate_pool()
    max_sources_pool = int(pool_size * 0.6)
    expansion_quota = _expansion_quota(pool_size)
    source_budget = [0]

    def _fill(entries, capacity):
        """Take up to ``capacity`` entries in score order, with the source-type cap applied.

        ``source_budget`` is shared across calls so the cap stays pool-wide, exactly as the single
        loop it replaces applied it.
        """
        taken = []
        for score, node in entries:
            if node.get("type", "").lower() == "source":
                if source_budget[0] >= max_sources_pool:
                    continue
                source_budget[0] += 1
            taken.append((score, node))
            if len(taken) >= capacity:
                break
        return taken

    if expansion_quota:
        primary = [(s, n) for s, n in scored if n.get("_origin") != "ppr"]
        expanded = [(s, n) for s, n in scored if n.get("_origin") == "ppr"]
        candidate_pool = _fill(primary, pool_size - expansion_quota)
        candidate_pool += _fill(expanded, expansion_quota)
        if len(candidate_pool) < pool_size:
            # The reservation is a floor for expansion, not a ceiling for the rest: what expansion
            # did not use goes back to the fusion candidates, still in score order.
            already = {node["_key"] for _, node in candidate_pool}
            leftover = [(s, n) for s, n in scored if n["_key"] not in already]
            candidate_pool += _fill(leftover, pool_size - len(candidate_pool))
        candidate_pool.sort(key=lambda item: item[0], reverse=True)
    else:
        candidate_pool = _fill(scored, pool_size)
    # Phase 2: Local deterministic ranking. Text-model reranking is delegated
    # to the host agent when explicitly requested, not performed by runtime code.
    reranked = _rerank_candidates_locally(query, candidate_pool)

    # Phase 3: Final top_k extraction.  Sources are demoted, not counted: a fixed multiplicative
    # penalty keeps the ordering a function of the pool (so top-k remains a pure window) and keeps
    # every eligible page in the ranking, while still making an all-Source answer hard to produce.
    # The absolute cap this replaces shortened the window for 104 of 333 queries at top_k=20.
    penalty = _source_rank_penalty()
    ordered = sorted(
        (
            (score * penalty if node.get("type", "").lower() == "source" else score, node)
            for score, node in reranked
        ),
        key=lambda item: item[0],
        reverse=True,
    )
    final_scored = ordered[:top_k]

    if projection_note:
        vector_notes.append(projection_note)
    _record(
        [
            {"key": node["_key"], "origin": node.get("_origin", "?"), "score": round(score, 6)}
            for score, node in final_scored
        ],
        vector_notes,
    )
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
        result += "[DEGRADED] Retrieval notes: " + "; ".join(vector_notes) + "\n\n"
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

    index_budget = int(max_chars * BUDGET_SHARES["index_summary"])

    # The nominal share, with the burst the comment below documents: memory used to take a
    # hardcoded 0.50 of *every* budget, so the declared ``operational_memory`` share was
    # decorative and the alert condition was never tested.  The packet is rebuilt at the burst
    # ceiling only when it actually reports alerts, which is a rare second call.
    memory_packet = build_memory_packet(
        query, max_chars=int(max_chars * BUDGET_SHARES["operational_memory"])
    )
    if memory_packet["warning_count"]:
        memory_packet = build_memory_packet(
            query, max_chars=int(max_chars * BUDGET_SHARES["memory_burst"])
        )
    actual_memory_used = len(memory_packet["packet"])

    purpose = ""
    try:
        from vector_lake.purpose_contract import render_strategy_directive

        purpose = render_strategy_directive()
    except Exception as exc:
        log.warning("Strategy directive unavailable for context assembly: %s", exc)
    purpose_budget = int(max_chars * BUDGET_SHARES["system_prompt"])
    purpose = purpose[:purpose_budget]

    from vector_lake import page_index_projection

    # Resolved once and handed to the scoring pass, so one request parses
    # index.json at most once even when the projection is behind.
    catalog, projection_note = page_index_projection.read_catalog()
    scored_pages, vector_notes, retrieval_error = _search_scored_pages(
        query, top_k=15, catalog=catalog, projection_note=projection_note
    )

    # The index summary is materialised *before* the wiki budget so the wiki gets whatever the
    # summary did not use.  Charging the wiki budget for the index's *allocation* rather than its
    # actual size lost the difference on every request whose catalog was small.
    index_summary = ""
    if os.path.exists(str(get_index_path())):
        try:
            if catalog is None:
                index_summary = "[Index read failed]"
            else:
                index_summary = "\n".join(catalog.node_summary_lines(50))[:index_budget]
                # The projection note is not appended here: ``_search_scored_pages`` already
                # returned it in ``vector_notes`` from the same ``projection_note`` this function
                # handed it, and appending it a second time made every degraded answer report the
                # same fallback twice.
        except Exception as exc:
            index_summary = "[Index read failed]"
            log.error("Index summary unavailable: %s", exc)

    # Wiki dynamically eats what memory, the index summary and the purpose left.
    wiki_budget = max(0, max_chars - actual_memory_used - len(index_summary) - len(purpose))

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

    # ``purpose`` is the last claimant on the budget and must not overflow it.  It was already
    # capped at its own share above; this is the invariant's backstop.
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
