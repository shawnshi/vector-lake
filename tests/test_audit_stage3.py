"""Regression coverage for bounded, truthful retrieval and audit previews."""
import json
import re
import xml.etree.ElementTree as ET

import pytest

from vector_lake import db_store, governance_store, memory_protocol, runtime_health, tool_graph, tool_query, tool_search


@pytest.mark.parametrize("length", [419, 420, 421])
def test_memory_excerpt_discloses_only_real_truncation(length):
    memory = {"memory_id": "m", "text": ("<&" * 211)[:length], "memory_score": 0.8}
    result = ET.fromstring(tool_search._format_memory_result(memory, as_xml=True))
    assert result.attrib["Truncated"] == str(length > 420).lower()
    assert len(result.text) <= 420
    plain = tool_search._format_memory_result(memory)
    assert ("[truncated]" in plain) is (length > 420)


def _packet_records():
    return [
        {"memory_id": f"memory_{i}", "memory_type": "fact", "memory_score": 0.8,
         "validity_state": "active", "text": f"item{i} " + "x" * 500 + " except when revoked.",
         "source_page": f"Concept_{i}.md"}
        for i in range(6)
    ]


@pytest.mark.parametrize("budget", [0, 50, 400, 1200, 10000])
def test_packet_counts_only_complete_included_items(budget, monkeypatch):
    calls = []

    def retrieve(*_args, **_kwargs):
        calls.append(1)
        return _packet_records(), []

    monkeypatch.setattr(governance_store, "search_operational_memory_views", retrieve)
    result = tool_search.build_memory_packet("test", max_chars=budget)
    assert len(result["packet"]) <= budget
    if result["packet"]:
        root = ET.fromstring(result["packet"])
        actual = sum(line.startswith("- [0.80/active] item") for line in (root.text or "").splitlines())
    else:
        actual = 0
    assert result["memory_count"] == actual
    if budget:
        assert calls == [1]
        assert result["omitted_count"] == 6 - actual
    else:
        assert calls == []
        assert result["omitted_count"] is None
    assert result["text_truncated_count"] == actual
    if actual < 6:
        assert result["budget_truncated"] is True


def test_packet_escapes_untrusted_xml_and_never_cuts_an_entity(monkeypatch):
    records = _packet_records()
    records[0]["text"] = "<qualifier> " + "&" * 500
    monkeypatch.setattr(governance_store, "search_operational_memory_views", lambda *_args, **_kwargs: (records, []))
    for budget in (400, 800, 1600, 10000):
        result = tool_search.build_memory_packet("<query> & data", max_chars=budget)
        assert len(result["packet"]) <= budget
        if result["packet"]:
            ET.fromstring(result["packet"])


def _empty_packet(*_args, **_kwargs):
    return {"packet": "", "memory_count": 0, "warning_count": 0, "omitted_count": 0,
            "budget_truncated": False, "text_truncated_count": 0}


def _sqlite_context_fixture(monkeypatch):
    db_store.init_db()
    monkeypatch.setattr(tool_search, "_context_purpose", lambda _budget: "purpose")
    monkeypatch.setattr(tool_search, "build_memory_packet", _empty_packet)
    monkeypatch.setattr(tool_search, "_search_projection_generation_issue", lambda _conn: None)
    monkeypatch.setattr(db_store, "verify_search_projection_integrity", lambda _conn: {"status": "ready", "signature": ("fixture",)})
    monkeypatch.setattr(db_store, "search_wiki", lambda *_args, **_kwargs: [
        {"node_key": "A", "title": "First", "summary": "a" * 1200},
        {"node_key": "B", "title": "Second", "summary": "b" * 1200 + " except revoked"},
        {"node_key": "", "title": "Missing identity", "summary": "must not count"},
    ])


@pytest.mark.parametrize("budget, included, truncated", [(1400, 1, 0), (5000, 2, 1)])
def test_sqlite_context_discloses_omissions_and_exact_boundaries(budget, included, truncated, isolated_memory, monkeypatch):
    _sqlite_context_fixture(monkeypatch)
    result = tool_search.assemble_context("test words", max_chars=budget, lightweight=True)
    assert result["wiki_page_count"] == included
    assert result["wiki_omitted_count"] == 2 - included
    assert result["wiki_text_truncated_count"] == truncated
    assert result["_retrieved_page_keys"] == ["A", "B"][:included]
    assert result["budget_used"] <= budget
    assert ("[truncated]" in result["wiki_context"]) is bool(truncated)


def test_query_and_context_pack_preserve_omission_metadata(monkeypatch):
    context = {"memory_omitted_count": 4, "memory_packet_truncated": True,
               "memory_text_truncated_count": 2, "wiki_omitted_count": 3,
               "wiki_text_truncated_count": 1, "index_summary_truncated": False}
    monkeypatch.setattr(runtime_health, "get_semantic_readiness_envelope", lambda **_kwargs: {"status": "unknown", "results_are_not_accepted_facts": True})
    envelope = tool_query._context_envelope("test", context)
    for key, value in context.items():
        assert envelope["retrieval"][key] == value
    monkeypatch.setattr(memory_protocol, "assemble_context", lambda *_args, **_kwargs: context)
    monkeypatch.setattr(memory_protocol, "get_semantic_readiness_envelope", lambda **_kwargs: {"status": "unknown"})
    packet = memory_protocol.context_pack("test")
    assert packet["context"] == context


def test_full_context_reports_dropped_snippets(isolated_memory, monkeypatch):
    monkeypatch.setattr(tool_search, "_context_purpose", lambda _budget: "purpose")
    monkeypatch.setattr(tool_search, "build_memory_packet", _empty_packet)
    monkeypatch.setattr(tool_search, "get_index_path", lambda: isolated_memory / "absent-index.json")
    monkeypatch.setattr(tool_search, "search_vector_lake", lambda *_args, **_kwargs:
                        "- **A** (score: 1)\n  " + "a" * 100 + "...\n" +
                        "- **B** (score: 1)\n  " + "b" * 100 + "...\n")
    result = tool_search.assemble_context("test", max_chars=200)
    assert result["wiki_page_count"] == 1
    assert result["wiki_omitted_count"] == 1
    assert result["budget_used"] <= 200


def _graph_fixture(isolated_memory, monkeypatch):
    governance_store.initialize_meta_store()
    index = isolated_memory / "test-index.json"
    index.write_text("{}", encoding="utf-8")
    data = {"graph_insights": [
        {"type": "test_gap", "node": "Concept_One", "description": "First candidate"},
        {"type": "test_gap", "node": "Concept_Two", "description": "Second candidate"},
    ]}
    monkeypatch.setattr(tool_graph, "get_index_path", lambda: index)
    monkeypatch.setattr(tool_graph, "read_committed_index_snapshot", lambda *_args, **_kwargs: data)
    monkeypatch.setattr(runtime_health, "enforce_runtime_write_health", lambda **_kwargs: None)
    return data


def test_topology_preview_is_reviewable_and_read_only(isolated_memory, monkeypatch):
    _graph_fixture(isolated_memory, monkeypatch)
    preview = tool_graph.audit_graph()
    assert "Concept_One" in preview and "Concept_Two" in preview
    assert "First candidate" in preview and "gov_topology_" in preview
    assert db_store.get_connection().execute("SELECT COUNT(*) FROM governance_queue").fetchone()[0] == 0


def test_topology_deduplicates_by_identity_not_shared_title(isolated_memory, monkeypatch):
    data = _graph_fixture(isolated_memory, monkeypatch)
    preview = tool_graph.audit_graph()
    fingerprint = re.search(r"confirmation=([0-9a-f]{64})", preview).group(1)
    tool_graph.audit_graph(dry_run=False, confirmation=fingerprint)
    conn = db_store.get_connection()
    assert conn.execute("SELECT COUNT(*) FROM governance_queue").fetchone()[0] == 2
    tool_graph.audit_graph(dry_run=False, confirmation=fingerprint)
    assert conn.execute("SELECT COUNT(*) FROM governance_queue").fetchone()[0] == 2
    data["graph_insights"][0]["description"] = "Changed candidate"
    with pytest.raises(ValueError, match="exact confirmation"):
        tool_graph.audit_graph(dry_run=False, confirmation=fingerprint)
    assert conn.execute("SELECT COUNT(*) FROM governance_queue").fetchone()[0] == 2


@pytest.mark.parametrize("oversized", ["count", "text"])
def test_topology_rejects_unreviewable_oversized_plan(oversized, isolated_memory, monkeypatch):
    data = _graph_fixture(isolated_memory, monkeypatch)
    if oversized == "count":
        data["graph_insights"] = data["graph_insights"] * 26
    else:
        data["graph_insights"][0]["description"] = "x" * 50000
    with pytest.raises(ValueError, match="preview limit"):
        tool_graph.audit_graph()
    with pytest.raises(ValueError, match="preview limit"):
        tool_graph.audit_graph(dry_run=False, confirmation="old-unreviewable-plan")
    assert db_store.get_connection().execute("SELECT COUNT(*) FROM governance_queue").fetchone()[0] == 0


def test_normal_twenty_four_item_plan_is_fully_reviewable(isolated_memory, monkeypatch):
    data = _graph_fixture(isolated_memory, monkeypatch)
    data["graph_insights"] = [{"type": "test_gap", "node": f"Concept_{i}", "description": f"Candidate {i}"} for i in range(24)]
    preview = tool_graph.audit_graph()
    items = json.loads(preview.split("\n", 1)[1].rsplit("\nconfirmation=", 1)[0])
    assert len(items) == 24
    assert len({item["item_id"] for item in items}) == 24
    assert len(preview) <= 40000
    assert items[-1]["affected_pages"] == ["wiki/Concept_23.md"]


def test_empty_packet_exact_budget_does_not_claim_truncation(monkeypatch):
    from datetime import datetime, timezone
    from types import SimpleNamespace
    monkeypatch.setattr(tool_search, "datetime", SimpleNamespace(now=lambda _zone: datetime(2026, 9, 7, tzinfo=timezone.utc)))
    monkeypatch.setattr(governance_store, "search_operational_memory_views", lambda *_args, **_kwargs: ([], []))
    first = tool_search.build_memory_packet("test")
    exact = tool_search.build_memory_packet("test", max_chars=len(first["packet"]))
    assert exact["packet"] == first["packet"]
    assert exact["budget_truncated"] is False
    assert exact["memory_count"] == exact["omitted_count"] == exact["text_truncated_count"] == 0


def test_unavailable_packet_remains_a_failure_not_no_data(monkeypatch):
    def unavailable(*_args, **_kwargs):
        raise governance_store.OperationalMemoryNotReady("fixture_not_ready")
    monkeypatch.setattr(governance_store, "search_operational_memory_views", unavailable)
    for budget in (30, 200, 1000):
        result = tool_search.build_memory_packet("test", max_chars=budget)
        assert result["warning_count"] == 1 and result["omitted_count"] is None
        assert len(result["packet"]) <= budget
        if result["packet"]:
            assert ET.fromstring(result["packet"]).attrib["status"] == "unavailable"


@pytest.mark.parametrize("state", ["missing", "empty", "unreadable"])
def test_full_context_does_not_count_unavailable_wiki_content(state, isolated_memory, monkeypatch):
    from vector_lake import index_snapshot
    wiki = isolated_memory / "wiki"
    wiki.mkdir(parents=True, exist_ok=True)
    (wiki / "index.json").write_text(json.dumps({
        "nodes": {"Concept_Seed": {"title": "Seed", "type": "concept", "status": "Active"}},
        "weighted_edges": [], "graph_state": {"dirty": True},
    }), encoding="utf-8")
    if state == "empty":
        (wiki / "Concept_Seed.md").write_text("", encoding="utf-8")
    if state == "unreadable":
        (wiki / "Concept_Seed.md").write_text("# Seed", encoding="utf-8")
        def unavailable(_path):
            raise tool_search.SearchIndexError("fixture read denied")
        monkeypatch.setattr(tool_search, "_read_search_snippet", unavailable)
    monkeypatch.setattr(tool_search, "_get_fts_search_results", lambda *_args, **_kwargs: [{"node_key": "Concept_Seed", "rank": -1.0}])
    monkeypatch.setattr(tool_search, "_get_query_embedding", lambda *_args: None)
    monkeypatch.setattr(tool_search, "_expand_query_locally", lambda *_args: ["seed"])
    monkeypatch.setattr(tool_search, "read_committed_index_snapshot", lambda path, **_kwargs: index_snapshot.load_legacy_index_snapshot_for_migration(path))
    monkeypatch.setattr(tool_search, "build_memory_packet", _empty_packet)
    monkeypatch.setattr(tool_search, "_context_purpose", lambda _budget: "purpose")
    index_snapshot.clear_index_snapshot_cache_for_tests()
    discovery = tool_search.search_vector_lake("seed")
    assert "**Seed**" in discovery
    if state != "unreadable":
        assert "[Search degraded:" not in discovery
    result = tool_search.assemble_context("seed", max_chars=10000)
    assert result["wiki_page_count"] == 0
    assert result["wiki_omitted_count"] == 1
    assert result["wiki_retrieval_degraded"] is True
    assert "wiki_snippet" in result["wiki_context"]


@pytest.mark.parametrize("summary", [None, "", " \t\n "])
def test_empty_sqlite_summary_is_discovery_not_included_content(summary, isolated_memory, monkeypatch):
    _sqlite_context_fixture(monkeypatch)
    monkeypatch.setattr(db_store, "search_wiki", lambda *_args, **_kwargs: [
        {"node_key": "Empty", "title": "Title match", "summary": summary},
        {"node_key": "Usable", "title": "Usable match", "summary": "Actual summary"},
    ])
    result = tool_search.assemble_context("test words", max_chars=5000, lightweight=True)
    assert result["wiki_page_count"] == 1
    assert result["wiki_omitted_count"] == 1
    assert result["_retrieved_page_keys"] == ["Usable"]
    assert result["wiki_retrieval_degraded"] is True
    assert "Actual summary" in result["wiki_context"]
    assert result["budget_used"] <= 5000


def test_missing_header_cannot_absorb_next_valid_snippet(isolated_memory, monkeypatch):
    monkeypatch.setattr(tool_search, "build_memory_packet", _empty_packet)
    monkeypatch.setattr(tool_search, "_context_purpose", lambda _budget: "purpose")
    monkeypatch.setattr(tool_search, "search_vector_lake", lambda *_args, **_kwargs:
        "- **Missing** (score: 2.0; snippet: missing)\n\n"
        "- **Present** (score: 1.0)\n  Real content...\n\n")
    result = tool_search.assemble_context("test", max_chars=5000)
    assert result["wiki_page_count"] == 1
    assert result["wiki_omitted_count"] == 1
    assert result["wiki_retrieval_degraded"] is True
    assert "Missing" not in result["wiki_context"]
    assert "Real content" in result["wiki_context"]
