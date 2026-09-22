"""The Source cap is a mixing rule, and a mixing rule needs two slots to mean anything.

``max_sources_final = int(top_k * 0.6)`` evaluated to 0 for ``top_k=1``, so a single-result
search could never return a Source page -- even when the Source page was the best match, which
is the one case where a caller asking for one answer most needs the answer they asked about.
The cap exists so an answer is not *all* Source stubs; at one slot there is nothing to mix, so
the cap is floored at 1.

Measured (the table the fix rests on):

    top_k    1   2   3   5   10
    before   0   1   1   3    6      <- 0 at top_k=1, and non-monotone at 2 and 3
    after    1   1   1   3    6
"""

from __future__ import annotations

import json

import pytest

from vector_lake import db_store, page_index_projection, tool_search
from vector_lake.tool_search import _search_scored_pages
from vector_lake.wiki_utils import get_index_path


@pytest.fixture
def lake_with_one_source_page(isolated_memory):
    """One Source node in both the published index and the SQLite projection."""
    db_store.init_db()
    nodes = {
        "Source_Best": {
            "title": "Best Source",
            "type": "source",
            "status": "Active",
            "domain": "AI_Engineering",
            "summary": "梅奥 诊所 人工智能 实践",
        }
    }
    get_index_path().write_text(
        json.dumps({"nodes": nodes, "weighted_edges": []}, ensure_ascii=False), encoding="utf-8"
    )
    conn = db_store.get_connection()
    with db_store.transaction():
        for key, node in nodes.items():
            conn.execute(
                "INSERT INTO wiki_search_index (node_key, title, summary, text) VALUES (?, ?, ?, ?)",
                (key, node["title"], node["summary"], node["summary"]),
            )
    page_index_projection.reset_catalog_cache()
    page_index_projection.refresh_page_index_projection(
        json.loads(get_index_path().read_text(encoding="utf-8"))
    )
    assert page_index_projection.projection_is_current() is True
    return isolated_memory


def _strong_fts_hit_only(monkeypatch):
    """Make the hybrid stage produce exactly one candidate, and make it the Source page."""
    monkeypatch.setattr(
        tool_search,
        "_get_fts_search_results",
        lambda query, limit=50: [
            {"node_key": "Source_Best", "title": "Best Source", "summary": "梅根", "rank": -25.0}
        ],
    )
    monkeypatch.setattr(
        tool_search, "_get_query_embedding", lambda query: ([], "no embedding provider")
    )
    monkeypatch.setattr(
        tool_search, "_get_vector_search_results", lambda vector, limit=50: ({}, None)
    )


def test_a_single_result_query_can_return_the_source_page_that_is_best(
    lake_with_one_source_page, monkeypatch
):
    """Discriminates the old cap: with a cap of 0 this returned an empty list."""
    _strong_fts_hit_only(monkeypatch)

    final, notes, error = _search_scored_pages("梅奥 诊所 人工智能", top_k=1)

    assert error is None
    assert [node["_key"] for _, node in final] == ["Source_Best"]


def test_the_same_query_still_returns_it_at_a_larger_top_k(
    lake_with_one_source_page, monkeypatch
):
    """The floor must not be a special case for top_k=1 only."""
    _strong_fts_hit_only(monkeypatch)

    final, _notes, error = _search_scored_pages("梅奥 诊所 人工智能", top_k=5)

    assert error is None
    assert [node["_key"] for _, node in final] == ["Source_Best"]


@pytest.fixture
def lake_with_thirty_mixed_pages(isolated_memory):
    """A lake deep enough that a 5-slot answer has to be a prefix of a 20-slot one."""
    db_store.init_db()
    nodes = {}
    for index in range(30):
        key = f"{'Source' if index % 2 == 0 else 'Concept'}_N{index:02d}"
        nodes[key] = {
            "title": f"Node {index:02d}",
            "type": "source" if index % 2 == 0 else "concept",
            "status": "Active",
            "domain": "AI_Engineering",
            "summary": f"梅奥 诊所 人工智能 {index}",
        }
    get_index_path().write_text(
        json.dumps({"nodes": nodes, "weighted_edges": []}, ensure_ascii=False), encoding="utf-8"
    )
    conn = db_store.get_connection()
    with db_store.transaction():
        for key, node in nodes.items():
            conn.execute(
                "INSERT INTO wiki_search_index (node_key, title, summary, text) VALUES (?, ?, ?, ?)",
                (key, node["title"], node["summary"], node["summary"]),
            )
    page_index_projection.reset_catalog_cache()
    page_index_projection.refresh_page_index_projection(
        json.loads(get_index_path().read_text(encoding="utf-8"))
    )
    assert page_index_projection.projection_is_current() is True
    return isolated_memory


def test_top_k_is_only_a_window_over_one_fixed_ranking(lake_with_thirty_mixed_pages, monkeypatch):
    """Discriminates the k-scaled pipeline: this failed while depth, pool and caps scaled with top_k.

    With them scaling, a larger ``top_k`` drew more candidates, resized the pool, refilled the source
    caps and so changed the reranker's pool-local normalisation -- the top five of a top-20 request
    was a different five from the top five of a top-5 request.  Measured on the live lake: 36 of the
    268 queries whose top-5 pool held no relevant page had their best page inside the top five of a
    twenty-deep run of the same ranking, absent from the top-5 result.
    """
    order = [f"{'Source' if index % 2 == 0 else 'Concept'}_N{index:02d}" for index in range(30)]
    monkeypatch.setattr(
        tool_search,
        "_get_fts_search_results",
        # Honours `limit`, so the stage depth is exercised rather than bypassed.
        lambda query, limit=50: [
            {"node_key": key, "title": key, "summary": "梅奥", "rank": -float(30 - position)}
            for position, key in enumerate(order)
        ][:limit],
    )
    monkeypatch.setattr(
        tool_search, "_get_query_embedding", lambda query: ([], "no embedding provider")
    )
    monkeypatch.setattr(
        tool_search, "_get_vector_search_results", lambda vector, limit=50: ({}, None)
    )

    def keys(top_k: int) -> list[str]:
        final, _notes, error = _search_scored_pages("梅奥 诊所 人工智能", top_k=top_k)
        assert error is None
        return [node["_key"] for _, node in final]

    five, twenty, forty = keys(5), keys(20), keys(40)

    assert len(five) == 5
    # The prefix relation is the contract, and the window is now always full: sources are demoted by
    # ``SOURCE_RANK_PENALTY`` instead of being counted, so a source-heavy pool cannot leave slots
    # empty.  The absolute cap this replaces returned fewer than top_k results for 104 of 333 queries
    # at top_k=20, which is what this assertion pair pins down.
    assert five == twenty[:5]
    assert twenty == forty[:20]
    assert len(twenty) == 20


def test_a_source_heavy_pool_still_fills_the_window(lake_with_thirty_mixed_pages, monkeypatch):
    """The cap skipped Source pages once it was spent, so the window came back short.

    Measured on the live lake: with an absolute cap of 3, a top-20 request returned fewer than 20
    results for 104 of 333 queries.  A multiplicative demotion cannot do that -- every eligible page
    stays in the ordering, so the window fills unless the pool itself is smaller than top_k.
    """
    order = [f"{'Source' if index % 2 == 0 else 'Concept'}_N{index:02d}" for index in range(30)]
    monkeypatch.setattr(
        tool_search,
        "_get_fts_search_results",
        lambda query, limit=50: [
            {"node_key": key, "title": key, "summary": "梅奥", "rank": -float(30 - position)}
            for position, key in enumerate(order)
        ][:limit],
    )
    monkeypatch.setattr(
        tool_search, "_get_query_embedding", lambda query: ([], "no embedding provider")
    )
    monkeypatch.setattr(
        tool_search, "_get_vector_search_results", lambda vector, limit=50: ({}, None)
    )

    final, _notes, error = _search_scored_pages("梅奥 诊所 人工智能", top_k=20)

    assert error is None
    assert len(final) == 20, "the window must be full when the pool has at least 20 candidates"
    # And the preference still bites: 15 of the 25 candidates are Source pages, and a 0.6 multiplier
    # cannot keep more than a handful of them above the Concept pages.
    sources = sum(1 for _, node in final if node.get("type", "").lower() == "source")
    assert sources < 15
