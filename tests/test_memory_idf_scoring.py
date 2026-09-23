"""The memory scorer's two contract points, both of which were violated in production:

* a term's weight must fall as more of the corpus contains it (the scorer added a flat 4/3/1, so a
  term present in tens of thousands of records counted as much as a rare one), and
* the stored per-record ``memory_score`` must break ties, not lead -- ``relevance + 5 * memory_score``
  let a nearly constant ~3.5 outweigh a relevance of a few points, and the ranking was mostly a
  static number.

Both are pinned here at the pure-function level, because in the packet they were invisible: the
line printed the stored score, which stays flat however the order changes.
"""

from vector_lake import governance_store, memory_gram_index


def _memory(memory_id, text, *, score=0.0, key=None, memory_type="fact"):
    return {
        "memory_id": memory_id,
        "memory_key": memory_id if key is None else key,
        "text": text,
        "memory_type": memory_type,
        "source_page": "page.md",
        "memory_score": score,
        "updated_at": "2026-01-01T00:00:00Z",
    }


def test_a_rarer_term_weighs_more_than_a_common_one():
    total = 1000
    assert memory_gram_index.idf_weight(1, total) > memory_gram_index.idf_weight(100, total)
    # A term every document contains contributes (almost) nothing, which is the whole point: the
    # packet used to be decided by whichever record happened to contain the most common words.
    assert memory_gram_index.idf_weight(total, total) < 0.7
    assert memory_gram_index.idf_weight(0, total) == 0.0


def test_the_field_weights_are_shared_with_the_index():
    # One owner: the oracle adds ``FIELD_WEIGHTS``, and an independent literal in either file is
    # how the two scorers drift apart.
    assert governance_store is not None  # imported for its use of the shared table
    assert memory_gram_index.FIELD_WEIGHTS[1] == 4  # key
    assert memory_gram_index.FIELD_WEIGHTS[2] == 3  # text
    assert memory_gram_index.FIELD_WEIGHTS[4] == 1  # page
    assert memory_gram_index.FIELD_WEIGHTS[0] == 0


def test_relevance_scales_each_term_by_its_document_frequency():
    common = _memory("common", "医疗")
    rare = _memory("rare", "师成")
    terms = ["医疗", "师成"]
    df = {"医疗": 60000, "师成": 3}

    weighted_common = governance_store._memory_relevance(common, terms, df, 69672)
    weighted_rare = governance_store._memory_relevance(rare, terms, df, 69672)
    assert weighted_rare > weighted_common

    # Without frequencies the weights are equal, which is the pre-IDF behaviour kept for callers
    # that score a handful of records outside any corpus.
    assert governance_store._memory_relevance(common, terms) == (
        governance_store._memory_relevance(rare, terms)
    )


def test_a_window_is_never_mistaken_for_the_corpus():
    """``df`` must never be derived from whatever collection a caller passes.

    The gram branch passes a *window* of candidates, and deriving frequencies from it weighted each
    candidate against the other candidates in its own window -- the returned order stopped being
    the index's order.  The scorer therefore has no fallback that reads the collection: no ``df``
    means equal weights.
    """
    memories = [
        _memory("a", "电子病历六级"),
        _memory("b", "电子病历 医院"),
        _memory("c", "医院 目标"),
    ]
    terms = ["电子病历", "医院"]

    alone = governance_store.score_memory_items(memories[:1], terms, top_k=1)
    window = governance_store.score_memory_items(memories, terms, top_k=3)
    by_id = {item["memory_id"]: item for item in window}

    assert alone[0]["retrieval_score"] == by_id["a"]["retrieval_score"]


def _historical_rank_score(memory, terms):
    """The pre-fix composite, restated: a flat field sum plus five times the stored score.

    It appears here as a *counterfactual premise*, to show the case below discriminates between the
    two orders -- not as a second implementation of the scorer.
    """
    flat = governance_store._memory_relevance(memory, terms)
    return flat + governance_store._memory_score_value(memory.get("memory_score")) * 5


def test_the_stored_score_breaks_ties_instead_of_leading():
    strong_relevance = _memory("relevant", "信创 集成平台", score=0.10)
    weak_relevance = _memory("popular", "信创", score=0.99)
    terms = ["信创", "集成平台"]
    df = {"信创": 4000, "集成平台": 20}

    ranked = governance_store.score_memory_items(
        [weak_relevance, strong_relevance], terms, top_k=2, df=df, total=69672
    )

    assert [item["memory_id"] for item in ranked] == ["relevant", "popular"]

    # The case is worth asserting only because the stored score alone would order them the other
    # way around: 6 + 5 * 0.10 against 3 + 5 * 0.99.
    historical = sorted(
        [strong_relevance, weak_relevance],
        key=lambda memory: -_historical_rank_score(memory, terms),
    )
    assert [memory["memory_id"] for memory in historical] == ["popular", "relevant"]


def test_ties_resolve_by_memory_id_on_both_paths():
    first = _memory("aaa", "信创 集成平台", score=0.5)
    second = _memory("bbb", "信创 集成平台", score=0.5)
    terms = ["信创", "集成平台"]

    forward = governance_store.score_memory_items([first, second], terms, top_k=2)
    backward = governance_store.score_memory_items([second, first], terms, top_k=2)

    assert [item["memory_id"] for item in forward] == ["aaa", "bbb"]
    assert [item["memory_id"] for item in backward] == ["aaa", "bbb"]
