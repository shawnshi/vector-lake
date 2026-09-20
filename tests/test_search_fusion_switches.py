"""The two retrieval switches, and the default they must not disturb.

``VECTOR_LAKE_FUSION`` and ``VECTOR_LAKE_EXPANSION_QUOTA`` exist so a ranking change can be
measured before it is adopted.  Three properties matter:

* the default is the long-standing behaviour, and an unreadable value falls back to it rather than
  to something new;
* ``rrf`` really fuses by rank (a rank-based score cannot be dominated by one source's magnitudes);
* a quota makes graph expansion a candidate source on a query whose recall paths already fill the
  pool -- which is exactly where it could not supply one before.

Measured on the live lake with the 40-query set in ``benchmarks/search_eval_queries.jsonl``:
expansion supplied 2 of 200 returned pages unset, 0 of 200 on half the queries, and ``rrf`` as it
stands is a *regression* (recall@5 0.75 -> 0.33) because the expansion stage's score is written in
the old fusion's units.  That is why ``rrf`` is not the default and why the README says not to
enable it alone.
"""

from __future__ import annotations

import json

import pytest

from vector_lake import db_store, page_index_projection, tool_search
from vector_lake.wiki_utils import get_index_path


def test_the_default_is_the_long_standing_blend(isolated_memory, monkeypatch):
    monkeypatch.delenv("VECTOR_LAKE_FUSION", raising=False)
    assert tool_search._fusion_mode() == "sum"


def test_an_unreadable_fusion_value_falls_back_to_the_default(isolated_memory, monkeypatch):
    """A typo must not silently select a different ranking."""
    monkeypatch.setenv("VECTOR_LAKE_FUSION", "RRF-ish")
    assert tool_search._fusion_mode() == "sum"


def test_rrf_is_selectable_and_case_insensitive(isolated_memory, monkeypatch):
    monkeypatch.setenv("VECTOR_LAKE_FUSION", "RRF")
    assert tool_search._fusion_mode() == "rrf"


@pytest.mark.parametrize(
    "raw,expected",
    [(None, None), ("", None), ("6", 6), ("0", 0), ("-1", None), ("many", None)],
)
def test_the_quota_parses_conservatively(isolated_memory, monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("VECTOR_LAKE_EXPANSION_QUOTA", raising=False)
    else:
        monkeypatch.setenv("VECTOR_LAKE_EXPANSION_QUOTA", raw)

    assert tool_search._expansion_quota(40) == expected


def test_the_quota_cannot_exceed_the_pool(isolated_memory, monkeypatch):
    monkeypatch.setenv("VECTOR_LAKE_EXPANSION_QUOTA", "999")
    assert tool_search._expansion_quota(40) == 40


def _crowded_lake(memory_dir):
    """A pool the two recall paths fill on their own, plus a frontier only expansion can reach.

    Both paths are asked for ``top_k * 5`` candidates and the pool is ``max(40, top_k * 3)``, so the
    pool is full whenever the two sets are disjoint and large enough -- which is the condition under
    which expansion used to receive zero slots.  The fixture builds exactly that: 45 FTS-matched
    pages, 25 vector-only pages, and 5 pages reachable only by walking an edge.
    """
    db_store.init_db()
    nodes = {
        f"Concept_Fill_{index:02d}": {
            "title": f"Fill {index}",
            "type": "concept",
            "status": "Active",
            "domain": "AI_Engineering",
            "summary": "共享 关键词 填充",
        }
        for index in range(45)
    }
    for index in range(25):
        nodes[f"Concept_Vec_{index:02d}"] = {
            "title": f"Vec {index}",
            "type": "concept",
            "status": "Active",
            "domain": "AI_Engineering",
            "summary": "向量专属",
        }
    for index in range(5):
        nodes[f"Concept_Reach_{index}"] = {
            "title": f"Reach {index}",
            "type": "concept",
            "status": "Active",
            "domain": "AI_Engineering",
            "summary": "远处 无关",
        }
    edges = [
        {"source": f"Concept_Fill_{index:02d}", "target": f"Concept_Reach_{index % 5}", "weight": 3.0}
        for index in range(45)
    ]
    get_index_path().write_text(
        json.dumps({"nodes": nodes, "weighted_edges": edges}, ensure_ascii=False), encoding="utf-8"
    )
    conn = db_store.get_connection()
    with db_store.transaction():
        for key in [k for k in nodes if k.startswith("Concept_Fill_")]:
            row = nodes[key]
            conn.execute(
                "INSERT INTO wiki_search_index (node_key, title, summary, text) VALUES (?, ?, ?, ?)",
                (key, row["title"], row["summary"], row["summary"]),
            )
    page_index_projection.reset_catalog_cache()
    page_index_projection.refresh_page_index_projection(
        json.loads(get_index_path().read_text(encoding="utf-8"))
    )
    assert page_index_projection.projection_is_current() is True
    return memory_dir


#: The vector path's contribution: distinct keys, high similarity, so the fusion set is large and
#: strictly stronger than anything graph expansion can produce on this fixture.
_VECTOR_HITS = {f"Concept_Vec_{index:02d}": 0.99 - index * 0.001 for index in range(25)}


def _pool_origins(monkeypatch, quota):
    """The candidate pool's origin tally for one query, with ``quota`` in force."""
    if quota is None:
        monkeypatch.delenv("VECTOR_LAKE_EXPANSION_QUOTA", raising=False)
    else:
        monkeypatch.setenv("VECTOR_LAKE_EXPANSION_QUOTA", str(quota))
    monkeypatch.setattr(tool_search, "_get_query_embedding", lambda query: ([0.5] * 3072, None))
    monkeypatch.setattr(
        tool_search, "_get_vector_search_results", lambda vector, limit=50: (dict(_VECTOR_HITS), None)
    )

    pools: list[list] = []
    real = tool_search._rerank_candidates_locally

    def _spy(query, pool):
        pools.append(pool)
        return real(query, pool)

    monkeypatch.setattr(tool_search, "_rerank_candidates_locally", _spy)
    tool_search._search_scored_pages("共享 关键词", top_k=5)

    assert pools, "the reranker was never reached"
    return {
        origin: sum(1 for _, node in pools[0] if node.get("_origin") == origin)
        for origin in ("fts", "vec", "both", "ppr")
    }


def test_without_a_quota_expansion_gets_no_slot_when_the_pool_is_full(isolated_memory, monkeypatch):
    """The measured defect: the stage exists, the pool has no room for it."""
    _crowded_lake(isolated_memory)

    origins = _pool_origins(monkeypatch, None)

    # The precondition the assertion depends on, stated so fixture drift fails loudly instead of
    # turning this into a test that passes for the wrong reason.
    assert origins["fts"] + origins["vec"] + origins["both"] >= 40, origins
    assert origins["ppr"] == 0


def test_a_quota_admits_expansion_into_a_full_pool(isolated_memory, monkeypatch):
    """Discriminates the fix: with a quota the same query yields expansion candidates."""
    _crowded_lake(isolated_memory)

    without = _pool_origins(monkeypatch, None)
    with_quota = _pool_origins(monkeypatch, 5)

    assert without["ppr"] == 0
    assert with_quota["ppr"] > 0
    assert with_quota["ppr"] <= 5
