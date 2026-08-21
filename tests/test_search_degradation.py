import io
import json

from vector_lake import tool_search


def _install_index(isolated_memory, monkeypatch, nodes):
    index_data = {
        "nodes": nodes,
        "weighted_edges": [],
        "graph_state": {"dirty": False},
    }
    index_path = isolated_memory / "wiki" / "index.json"
    index_path.write_text(json.dumps(index_data), encoding="utf-8")
    monkeypatch.setattr(tool_search, "_load_search_index", lambda _path: index_data)
    monkeypatch.setattr(tool_search, "_get_query_embedding", lambda _query: [])
    return index_data


def test_fts_failure_uses_index_fallback_and_marks_degraded(
    isolated_memory,
    monkeypatch,
):
    _install_index(
        isolated_memory,
        monkeypatch,
        {
            "Concept_Alpha": {
                "title": "Alpha Policy",
                "summary": "alpha clinical policy",
                "type": "concept",
                "status": "active",
            }
        },
    )

    def unavailable(*_args, **_kwargs):
        raise tool_search.SearchBackendError("fts5")

    monkeypatch.setattr(tool_search, "_get_fts_search_results", unavailable)

    result = tool_search.search_vector_lake("alpha", top_k=3)

    assert result.startswith("[Search degraded: fts5]")
    assert "**Alpha Policy**" in result


def test_vector_failure_keeps_fts_results_and_marks_degraded(
    isolated_memory,
    monkeypatch,
):
    _install_index(
        isolated_memory,
        monkeypatch,
        {
            "Concept_Alpha": {
                "title": "Alpha Policy",
                "type": "concept",
                "status": "active",
            }
        },
    )
    monkeypatch.setattr(
        tool_search,
        "_get_fts_search_results",
        lambda *_args, **_kwargs: [{"node_key": "Concept_Alpha", "rank": -1.0}],
    )
    monkeypatch.setattr(tool_search, "_get_query_embedding", lambda _query: [0.1])

    def unavailable(*_args, **_kwargs):
        raise tool_search.SearchBackendError("vector")

    monkeypatch.setattr(tool_search, "_get_vector_search_results", unavailable)

    result = tool_search.search_vector_lake("alpha", top_k=3)

    assert result.startswith("[Search degraded: vector]")
    assert "**Alpha Policy**" in result


def test_healthy_empty_search_is_explicit(isolated_memory, monkeypatch):
    _install_index(isolated_memory, monkeypatch, {})
    monkeypatch.setattr(
        tool_search,
        "_get_fts_search_results",
        lambda *_args, **_kwargs: [],
    )

    assert tool_search.search_vector_lake("absent") == "No matching evidence found.\n"


def test_search_snippet_never_uses_unbounded_read(monkeypatch):
    class GuardedText(io.StringIO):
        def read(self, size=-1):
            assert 0 <= size <= 2_500
            return super().read(size)

        def readline(self, size=-1):
            assert 0 <= size <= 8_193
            return super().readline(size)

    content = "---\ntitle: Bounded\n---\n" + ("body " * 10_000)
    monkeypatch.setattr(
        tool_search,
        "open",
        lambda *_args, **_kwargs: GuardedText(content),
        raising=False,
    )

    snippet = tool_search._read_search_snippet("ignored.md")

    assert len(snippet) == 2_500
    assert snippet.startswith("body ")


def test_local_reranker_promotes_direct_title_match(monkeypatch):
    monkeypatch.setattr(
        tool_search,
        "_expand_query_locally",
        lambda _query: ["alpha"],
    )
    candidates = [
        (2.0, {"_key": "Concept_Other", "title": "Other"}),
        (1.0, {"_key": "Concept_Alpha", "title": "Alpha"}),
    ]

    reranked = tool_search._rerank_candidates_locally("alpha", candidates)

    assert reranked[0][1]["_key"] == "Concept_Alpha"
    assert reranked[0][0] > reranked[1][0]


def test_search_result_budget_and_phase_telemetry_are_enforced(
    isolated_memory,
    monkeypatch,
):
    _install_index(
        isolated_memory,
        monkeypatch,
        {
            "Concept_Alpha": {
                "title": "Alpha",
                "summary": "alpha",
                "type": "concept",
                "status": "active",
            }
        },
    )
    (isolated_memory / "wiki" / "Concept_Alpha.md").write_text(
        "alpha " * 1_000,
        encoding="utf-8",
    )
    monkeypatch.setenv("VECTOR_LAKE_SEARCH_RESULT_MAX_CHARS", "1000")
    monkeypatch.setattr(
        tool_search,
        "_get_fts_search_results",
        lambda *_args, **_kwargs: [
            {"node_key": "Concept_Alpha", "rank": -1.0}
        ],
    )

    result = tool_search.search_vector_lake("alpha", top_k=100)
    status = tool_search.search_performance_status()

    assert result.startswith("[Search degraded: result_budget]")
    assert len(result) < 1_200
    assert status["last"]["result_chars"] == len(result)
    assert status["last"]["total_ms"] >= 0
    assert status["last"]["fts_ms"] >= 0
