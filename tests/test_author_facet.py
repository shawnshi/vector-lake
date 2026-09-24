"""Authorship is a facet the corpus has to supply, because the text does not.

Measured on the live corpus: 149 pages come from ``raw/article/`` and 131 of them never name the
author, so a query for his view reached his own pages only at ranks 12, 13, 14, 17 and 18 -- behind a
page *about* him.  The mapping is read from the ingest ledger's own ``filepath``/``canonical_name``
pair; nothing here infers authorship from a title.
"""

from __future__ import annotations

import json

import pytest

from vector_lake import author_facet, db_store, page_index_projection, tool_search
from vector_lake.tool_search import _search_scored_pages
from vector_lake.wiki_utils import get_index_path, get_wiki_dir

QUERY = "人工智能 观点"


@pytest.fixture
def lake_with_author_page(isolated_memory):
    db_store.init_db()
    nodes = {
        "Source_团队内参": {
            "title": "团队内参",
            "type": "source",
            "status": "Active",
            "domain": "AI_Engineering",
            "summary": "人工智能 观点 内部",
        },
        "Concept_第三方综述": {
            "title": "第三方综述",
            "type": "concept",
            "status": "Active",
            "domain": "AI_Engineering",
            "summary": "人工智能 观点 综述",
        },
        "Source_别人的文章": {
            "title": "别人的文章",
            "type": "source",
            "status": "Active",
            "domain": "AI_Engineering",
            "summary": "人工智能 观点 转载",
        },
    }
    get_index_path().write_text(
        json.dumps({"nodes": nodes, "weighted_edges": []}, ensure_ascii=False), encoding="utf-8"
    )
    for key, node in nodes.items():
        (get_wiki_dir() / f"{key}.md").write_text(
            f"# {node['title']}\n\n{node['summary']}\n", encoding="utf-8"
        )
    conn = db_store.get_connection()
    with db_store.transaction():
        for key, node in nodes.items():
            conn.execute(
                "INSERT INTO wiki_search_index (node_key, title, summary, text) VALUES (?, ?, ?, ?)",
                (key, node["title"], node["summary"], node["summary"]),
            )
        # The ledger: the author wrote 团队内参 from raw/article, and 别人的文章 came from raw/news.
        for index, (job, status, filepath, canonical) in enumerate(
            [
                ("job-author", "finalized", "C:/MEMORY/raw/article/团队内参.md", "Source_团队内参.md"),
                ("job-other", "finalized", "C:/MEMORY/raw/news/别人的文章.md", "Source_别人的文章.md"),
            ]
        ):
            conn.execute(
                "INSERT INTO jobs (job_id, task_type, payload, status, created_at, updated_at) "
                "VALUES (?, 'ingest', ?, ?, '2026-01-01', '2026-01-01')",
                (job, json.dumps({"filepath": filepath, "canonical_name": canonical}), status),
            )
        db_store.get_connection().execute("SELECT 1")
    page_index_projection.reset_catalog_cache()
    page_index_projection.refresh_page_index_projection(
        json.loads(get_index_path().read_text(encoding="utf-8"))
    )
    author_facet.author_page_keys(refresh=True)
    return isolated_memory


def _two_candidates(monkeypatch):
    """Both pages as candidates, near-equal rank so the facet is what decides the order."""
    monkeypatch.setattr(
        tool_search,
        "_get_fts_search_results",
        lambda query, limit=50: [
            {"node_key": "Concept_第三方综述", "title": "第三方综述", "summary": "人工智能 观点 综述", "rank": -20.0},
            {"node_key": "Source_团队内参", "title": "团队内参", "summary": "人工智能 观点 内部", "rank": -19.9},
        ],
    )
    monkeypatch.setattr(tool_search, "_get_query_embedding", lambda q: ([], "no embedding provider"))
    monkeypatch.setattr(tool_search, "_get_vector_search_results", lambda v, limit=50: ({}, None))


def _order(monkeypatch, **env):
    for name in ("VECTOR_LAKE_AUTHOR_FACET", "VECTOR_LAKE_AUTHOR_BOOST", "VECTOR_LAKE_AUTHOR_SOURCES"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    _two_candidates(monkeypatch)
    scored, _notes, _filtered = _search_scored_pages(QUERY, 5)
    return [node["_key"] for _score, node in scored]


def _scores(monkeypatch, **env):
    for name in ("VECTOR_LAKE_AUTHOR_FACET", "VECTOR_LAKE_AUTHOR_BOOST", "VECTOR_LAKE_AUTHOR_SOURCES"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    _two_candidates(monkeypatch)
    scored, _notes, _filtered = _search_scored_pages(QUERY, 5)
    return {node["_key"]: score for score, node in scored}


def _order(monkeypatch, **env):
    return list(_scores(monkeypatch, **env))


# --- the ledger is the mapping, and only the declared prefixes count ---------------------------


def test_the_ledger_supplies_the_author_pages(lake_with_author_page):
    keys = author_facet.author_page_keys(refresh=True)
    assert "Source_团队内参" in keys
    assert "Source_别人的文章" not in keys


def test_the_declared_prefixes_are_configurable(lake_with_author_page, monkeypatch):
    monkeypatch.setenv("VECTOR_LAKE_AUTHOR_SOURCES", "raw/news")
    keys = author_facet.author_page_keys(refresh=True)
    assert "Source_别人的文章" in keys
    assert "Source_团队内参" not in keys


def test_a_new_ingest_lands_without_a_restart(lake_with_author_page):
    conn = db_store.get_connection()
    with db_store.transaction():
        conn.execute(
            "INSERT INTO jobs (job_id, task_type, payload, status, created_at, updated_at) "
            "VALUES ('job-new', 'ingest', ?, 'completed', '2026-02-01', '2026-02-01')",
            (json.dumps({"filepath": "C:/MEMORY/raw/article/新文章.md", "canonical_name": "Source_新文章.md"}),),
        )
    assert "Source_新文章" in author_facet.author_page_keys()


def test_an_absent_ledger_reports_nothing(isolated_memory):
    db_store.init_db()
    assert author_facet.author_page_keys(refresh=True) == frozenset()


# --- the knobs ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [("raw/article", ("raw/article/",)), ("", ("raw/article/",)), ("raw/article,raw/personal-insights",
     ("raw/article/", "raw/personal-insights/"))],
)
def test_prefix_parsing(raw, expected, monkeypatch):
    monkeypatch.setenv("VECTOR_LAKE_AUTHOR_SOURCES", raw)
    assert author_facet.author_source_prefixes() == expected


@pytest.mark.parametrize("raw,expected", [("boost", "boost"), ("FILTER", "filter"), ("nonsense", "off")])
def test_mode_parsing(raw, expected, monkeypatch):
    monkeypatch.setenv("VECTOR_LAKE_AUTHOR_FACET", raw)
    assert author_facet.author_facet_mode() == expected


def test_the_boost_is_a_relative_lift_and_is_clamped(monkeypatch):
    monkeypatch.setenv("VECTOR_LAKE_AUTHOR_BOOST", "1.5")
    assert author_facet.author_boost() == 1.0  # level with the pool top, not past it
    monkeypatch.setenv("VECTOR_LAKE_AUTHOR_BOOST", "0.2")
    assert author_facet.author_boost() == 0.2
    monkeypatch.setenv("VECTOR_LAKE_AUTHOR_BOOST", "-3")
    assert author_facet.author_boost() == 0.0
    monkeypatch.setenv("VECTOR_LAKE_AUTHOR_BOOST", "later")
    assert author_facet.author_boost() == author_facet.AUTHOR_BOOST_DEFAULT


# --- ranking, with the default pinned -----------------------------------------------------------


def test_the_default_ranking_is_untouched(lake_with_author_page, monkeypatch):
    # The FTS rank puts the third-party page first; with the facet off nothing may change that.
    assert _order(monkeypatch) == ["Concept_第三方综述", "Source_团队内参"]


def test_boost_lifts_the_author_page_by_the_relative_amount(lake_with_author_page, monkeypatch):
    # Asserted on the score, not on the order: a two-candidate pool normalises to its extremes, so
    # the bottom candidate sits at exactly 0.0 and the ordering claim belongs to the live corpus
    # (where the default lift moved four author pages into the top ten).
    plain = _scores(monkeypatch)
    boosted = _scores(monkeypatch, VECTOR_LAKE_AUTHOR_FACET="boost", VECTOR_LAKE_AUTHOR_BOOST="0.5")
    assert boosted["Source_团队内参"] == pytest.approx(
        plain["Source_团队内参"] + 0.5 * max(plain.values()), rel=1e-6
    )


def test_boost_does_not_touch_a_page_the_author_did_not_write(lake_with_author_page, monkeypatch):
    plain = _scores(monkeypatch)
    boosted = _scores(monkeypatch, VECTOR_LAKE_AUTHOR_FACET="boost", VECTOR_LAKE_AUTHOR_BOOST="0.5")
    assert plain["Concept_第三方综述"] > 0  # non-vacuous: the untouched score is not zero
    assert boosted["Concept_第三方综述"] == pytest.approx(plain["Concept_第三方综述"], rel=1e-9)


def test_the_maximum_lift_reaches_the_pool_top(lake_with_author_page, monkeypatch):
    scores = _scores(monkeypatch, VECTOR_LAKE_AUTHOR_FACET="boost", VECTOR_LAKE_AUTHOR_BOOST="1")
    assert scores["Source_团队内参"] == pytest.approx(max(scores.values()), rel=1e-9)
    assert list(scores)[0] in {"Source_团队内参", "Concept_第三方综述"}


def test_filter_narrows_to_the_author_pages(lake_with_author_page, monkeypatch):
    assert _order(monkeypatch, VECTOR_LAKE_AUTHOR_FACET="filter") == ["Source_团队内参"]


def test_the_envelope_marks_authorship_without_reordering(lake_with_author_page, monkeypatch):
    monkeypatch.delenv("VECTOR_LAKE_AUTHOR_FACET", raising=False)
    _two_candidates(monkeypatch)
    context = tool_search.assemble_context(QUERY)
    assert "**团队内参** `[author]`" in context["wiki_context"]
    assert "**第三方综述** `[author]`" not in context["wiki_context"]


def test_the_marker_can_be_turned_off(lake_with_author_page, monkeypatch):
    monkeypatch.setenv("VECTOR_LAKE_AUTHOR_ANNOTATE", "0")
    _two_candidates(monkeypatch)
    context = tool_search.assemble_context(QUERY)
    assert "[author]" not in context["wiki_context"]
