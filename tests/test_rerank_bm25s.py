"""Tests for the bm25s-powered Phase-2 candidate reranker.

Contract that matters:

* reranking must never change candidate *membership* (recall is decided upstream
  by FTS5 + graph expansion), only the order within the pool,
* ``VECTOR_LAKE_RERANK_WEIGHT=0`` must reproduce the previous ordering exactly,
* a missing or failing bm25s must fail open, not drop results.
"""
import sys
import types

import pytest

from vector_lake import tool_search


def _node(key, title, summary=""):
    return {"_key": key, "title": title, "summary": summary, "aliases": []}


def _candidates():
    return [
        (10.0, _node("Concept_Weather", "天气预报方法")),
        (8.0, _node("Concept_Emr", "电子病历系统", "电子病历与集成平台的解耦设计")),
        (6.0, _node("Concept_Platform", "医疗数据平台", "集成平台对接核心业务系统")),
    ]


@pytest.fixture(autouse=True)
def _reset_env(monkeypatch):
    monkeypatch.delenv("VECTOR_LAKE_RERANK_WEIGHT", raising=False)
    yield


def test_reranking_preserves_candidate_membership():
    before = _candidates()

    after = tool_search._rerank_candidates_locally("电子病历集成平台", before)

    assert {node["_key"] for _, node in after} == {node["_key"] for _, node in before}
    assert len(after) == len(before)


def test_lexically_relevant_candidate_rises_to_the_top():
    before = _candidates()

    after = tool_search._rerank_candidates_locally("电子病历集成平台", before)

    assert after[0][1]["_key"] == "Concept_Emr", after
    # The lexically unrelated candidate must no longer lead the pool.
    assert after[0][1]["_key"] != "Concept_Weather", after


def test_higher_upstream_score_still_breaks_ties_in_favour_of_expansion(monkeypatch):
    """A graph-expanded candidate has no lexical overlap by construction.

    The blend deliberately keeps upstream weight so expansion keeps its value:
    a heavily-weighted, lexically-unmatched candidate must still be able to
    outrank a weak lexical match.
    """
    before = [
        (50.0, _node("Concept_Expanded", "邻近节点", "图扩展带来的候选")),
        (1.0, _node("Concept_Weak", "弱相关", "平台")),
    ]

    after = tool_search._rerank_candidates_locally("电子病历集成平台", before)

    assert after[0][1]["_key"] == "Concept_Expanded", after


def test_weight_zero_reproduces_the_previous_order(monkeypatch):
    before = _candidates()
    monkeypatch.setenv("VECTOR_LAKE_RERANK_WEIGHT", "0")

    assert tool_search._rerank_candidates_locally("电子病历集成平台", before) == before


def test_invalid_weight_falls_back_to_the_default(monkeypatch):
    before = _candidates()
    monkeypatch.setenv("VECTOR_LAKE_RERANK_WEIGHT", "not-a-number")

    after = tool_search._rerank_candidates_locally("电子病历", before)

    assert {node["_key"] for _, node in after} == {node["_key"] for _, node in before}


def test_missing_bm25s_fails_open(monkeypatch):
    before = _candidates()
    monkeypatch.setitem(sys.modules, "bm25s", None)

    assert tool_search._rerank_candidates_locally("电子病历", before) == before


def test_bm25s_failure_fails_open(monkeypatch):
    class Exploding(types.ModuleType):
        class BM25:
            def index(self, *args, **kwargs):
                raise RuntimeError("index exploded")

        @staticmethod
        def tokenize(*args, **kwargs):
            return []

    monkeypatch.setitem(sys.modules, "bm25s", Exploding("bm25s"))
    before = _candidates()

    assert tool_search._rerank_candidates_locally("电子病历", before) == before


def test_runs_are_deterministic():
    before = _candidates()

    assert (
        tool_search._rerank_candidates_locally("电子病历集成平台", before)
        == tool_search._rerank_candidates_locally("电子病历集成平台", before)
    )


def test_scores_are_pool_normalised_to_unit_interval():
    before = _candidates()

    after = tool_search._rerank_candidates_locally("电子病历集成平台", before)

    assert all(0.0 <= score <= 1.0 for score, _ in after), after
    # The pool maximum is a blend of two differently-ranked signals, so no single
    # candidate is guaranteed to reach 1.0 - only the interval bound is.
    assert max(score for score, _ in after) <= 1.0
    assert min(score for score, _ in after) >= 0.0


def test_ordering_follows_the_pool_normalised_score():
    after = tool_search._rerank_candidates_locally("电子病历集成平台", _candidates())

    scores = [score for score, _ in after]
    assert scores == sorted(scores, reverse=True), after


def test_single_candidate_is_returned_untouched():
    before = _candidates()[:1]

    assert tool_search._rerank_candidates_locally("电子病历", before) == before


def test_blank_query_keeps_upstream_order():
    before = _candidates()

    assert tool_search._rerank_candidates_locally("", before) == before


def test_search_output_reports_the_pool_normalised_score(isolated_memory, monkeypatch):
    """The rendered result must expose the same score the reranker produced."""
    import json

    from vector_lake import db_store, wiki_utils

    db_store.init_db()
    db_store.upsert_search_index("Concept_Emr", "电子病历", "系统", "电子 病历 系统 集成 平台")
    wiki = wiki_utils.get_wiki_dir()
    (wiki / "Concept_Emr.md").write_text("---\n---\n电子病历系统\n", encoding="utf-8")
    (wiki / "index.json").write_text(json.dumps({
        "nodes": {"Concept_Emr": {
            "id": "concept_emr", "title": "电子病历", "summary": "系统", "type": "concept",
            "domain": "Healthcare_IT", "topic_cluster": "EMR", "status": "Active",
            "aliases": [], "links": [], "sources": [], "triples": [], "raw_text": "",
        }},
        "weighted_edges": [], "aliases": {}, "categories": [], "error_log": [],
        "graph_state": {"dirty": False},
    }, ensure_ascii=False), encoding="utf-8")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    out = tool_search.search_vector_lake("电子病历集成平台", top_k=3)

    assert "score: " in out
    score_text = out.split("score: ", 1)[1].split(")", 1)[0]
    assert 0.0 <= float(score_text) <= 1.0, out
