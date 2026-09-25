"""Context assembly must consume structured search results, not re-parse text.

The previous implementation scanned the human-readable search string with the
regex ``\\*\\*(.+?)\\*\\*.*?\\n\\s+(.*?)\\.\\.\\.\\n``.  Any title containing the
delimiter, or any change to the formatter, silently produced an empty wiki
context while still reporting success.
"""
import pytest

from vector_lake import db_store, tool_search


def _page(name: str, title: str, summary: str) -> str:
    return f"""---
id: {name.lower()}
title: {title}
type: concept
domain: General
status: Active
epistemic-status: seed
categories: [System_Architecture]
strategic_scope: core
evidence_tier: primary
topic_cluster: Test
updated: 2026-01-01
sources: [raw/original.md]
---
## 1. 编译事实 (Compiled Truth - READ MODEL)
### 物理机制 (Mechanism)
{summary}

## 2. 证据时间线 (Evidence Timeline - WRITE MODEL)
- [2026-01-01] [Observation] observed. (Source: [[Source_Original]])
"""


@pytest.fixture
def scored(monkeypatch):
    """Stub the ranked pages so the assembly logic is tested in isolation."""
    pages = [
        (0.9, {"_key": "Concept_Alpha", "title": "Alpha 集成平台", "type": "concept"}),
        (0.5, {"_key": "Concept_Beta", "title": "Beta 电子病历", "type": "concept"}),
    ]
    monkeypatch.setattr(
        tool_search,
        "_search_scored_pages",
        lambda query, top_k=15, **kwargs: (pages[:top_k], [], None),
    )
    return pages


def test_wiki_context_is_built_from_structured_pages(isolated_memory, scored):
    (isolated_memory / "wiki" / "Concept_Alpha.md").write_text(
        _page("Concept_Alpha", "Alpha 集成平台", "集成平台负责数据交换。"), encoding="utf-8"
    )
    (isolated_memory / "wiki" / "Concept_Beta.md").write_text(
        _page("Concept_Beta", "Beta 电子病历", "电子病历六级评级。"), encoding="utf-8"
    )

    context = tool_search.assemble_context("集成平台", max_chars=50000)

    assert context["wiki_page_count"] == 2
    assert "Alpha 集成平台" in context["wiki_context"]
    assert "Beta 电子病历" in context["wiki_context"]
    assert "集成平台负责数据交换" in context["wiki_context"]


def test_titles_containing_markdown_delimiters_survive(isolated_memory, monkeypatch):
    """A title containing `**` confuses any formatter-based parse."""
    tricky = [(0.9, {"_key": "Concept_Tricky", "title": "A **bold** title", "type": "concept"})]
    monkeypatch.setattr(
        tool_search,
        "_search_scored_pages",
        lambda query, top_k=15, **kwargs: (tricky, [], None),
    )
    (isolated_memory / "wiki" / "Concept_Tricky.md").write_text(
        _page("Concept_Tricky", "A **bold** title", "body text"), encoding="utf-8"
    )

    context = tool_search.assemble_context("bold", max_chars=50000)

    assert context["wiki_page_count"] == 1
    assert "A **bold** title" in context["wiki_context"]


def test_assembly_is_independent_of_the_formatter(isolated_memory, scored, monkeypatch):
    """Context assembly must not depend on how search results are rendered."""
    (isolated_memory / "wiki" / "Concept_Alpha.md").write_text(
        _page("Concept_Alpha", "Alpha 集成平台", "snippet body"), encoding="utf-8"
    )

    def unparseable_formatter(*args, **kwargs):
        return "NOT A PARSEABLE SEARCH DOCUMENT"

    monkeypatch.setattr(tool_search, "search_vector_lake", unparseable_formatter)

    context = tool_search.assemble_context("集成平台", max_chars=50000)

    assert context["wiki_page_count"] == 2
    assert "Alpha 集成平台" in context["wiki_context"]


def test_budget_invariant_holds_for_tiny_and_large_budgets(isolated_memory, scored):
    (isolated_memory / "wiki" / "Concept_Alpha.md").write_text(
        _page("Concept_Alpha", "Alpha 集成平台", "x" * 5000), encoding="utf-8"
    )

    for max_chars in (150, 400, 2000, 50000):
        context = tool_search.assemble_context("集成平台", max_chars=max_chars)
        assert context["budget_used"] <= context["budget_max"], (max_chars, context["budget_used"])


def test_retrieval_notes_are_surfaced(isolated_memory, monkeypatch):
    monkeypatch.setattr(
        tool_search,
        "_search_scored_pages",
        lambda query, top_k=15, **kwargs: ([], ["no stored vectors matched"], None),
    )

    context = tool_search.assemble_context("anything", max_chars=10000)

    assert context["wiki_context"] == ""
    assert any("no stored vectors matched" in note for note in context["retrieval_notes"])


def test_missing_index_reports_a_retrieval_note(isolated_memory):
    """No index on disk is reported, not silently treated as an empty answer."""
    context = tool_search.assemble_context("anything", max_chars=10000)

    assert context["wiki_context"] == ""
    assert any("drying" in note.lower() for note in context["retrieval_notes"])


def test_zero_budget_is_rejected(isolated_memory):
    with pytest.raises(ValueError):
        tool_search.assemble_context("anything", max_chars=0)


def test_search_vector_lake_still_reports_a_missing_index(isolated_memory):
    db_store.init_db()
    assert "drying" in tool_search.search_vector_lake("anything").lower()


def test_the_memory_packet_is_built_once_when_it_has_alerts(monkeypatch, isolated_memory):
    """The order of the burst/nominal steps is a measured decision, not a preference.

    ``assemble_context`` built the packet 1.76 times per query before 2026-09-25, i.e. alerts are the
    common case and the discarded build was the nominal one (~0.8 s of retrieval per query).  Building
    at the burst ceiling first produces the identical packet and pays the second retrieval only when
    there is nothing to warn about -- this pins both branches and the budget each one uses.
    """
    from vector_lake import author_facet, page_index_projection, purpose_contract

    db_store.init_db()
    calls: list[int] = []
    state = {"warnings": 1}

    def fake_build(query, max_chars=0):
        calls.append(max_chars)
        return {
            "packet": "packet",
            "memory_count": 0,
            "warning_count": state["warnings"],
            "omitted_count": 0,
        }

    monkeypatch.setattr(tool_search, "build_memory_packet", fake_build)
    monkeypatch.setattr(page_index_projection, "read_catalog", lambda: (None, None))
    monkeypatch.setattr(purpose_contract, "render_strategy_directive", lambda: "")
    monkeypatch.setattr(tool_search, "_search_scored_pages", lambda *a, **k: ([], [], None))
    monkeypatch.setattr(author_facet, "author_page_keys", lambda **k: frozenset())

    budget = 100_000
    tool_search.assemble_context("q", max_chars=budget)
    assert len(calls) == 1, f"alerts are the common case: {calls}"
    assert calls[0] == int(budget * tool_search.BUDGET_SHARES["memory_burst"])

    calls.clear()
    state["warnings"] = 0
    tool_search.assemble_context("q", max_chars=budget)
    assert len(calls) == 2, f"a quiet packet is re-cut to the nominal share: {calls}"
    assert calls[0] == int(budget * tool_search.BUDGET_SHARES["memory_burst"])
    assert calls[1] == int(budget * tool_search.BUDGET_SHARES["operational_memory"])
