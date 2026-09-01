import io
import json

from vector_lake import tool_search


def _base_search_result(payload: str) -> str:
    prefix = "<SemanticReadinessEnvelope>\n"
    suffix = "\n</SemanticReadinessEnvelope>\n"
    assert payload.startswith(prefix)
    envelope_text, result = payload[len(prefix) :].split(suffix, 1)
    envelope = json.loads(envelope_text)
    assert envelope["results_are_not_accepted_facts"] is True
    return result


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

    base_result = _base_search_result(result)
    assert base_result.startswith("[Search degraded: fts5]")
    assert "**Alpha Policy**" in base_result


def test_repeated_fts_failures_are_log_rate_limited(
    isolated_memory,
    monkeypatch,
    caplog,
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
    monkeypatch.setattr(
        tool_search,
        "_get_fts_search_results",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            tool_search.SearchBackendError("fts5")
        ),
    )
    tool_search._reset_search_backend_log_state()

    for _ in range(20):
        assert "**Alpha Policy**" in tool_search.search_vector_lake("alpha")

    records = [
        record
        for record in caplog.records
        if "Search backend fts5 failed" in record.getMessage()
    ]
    status = tool_search.search_performance_status()

    assert len(records) == 1
    assert status["backend_log_suppressed"] == {"fts5": 19}


def test_healthy_fts_does_not_scan_index_fallback(
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
        "_fts_projection_probe",
        lambda _snapshot: (("ready",), None),
    )
    monkeypatch.setattr(
        tool_search,
        "_get_fts_search_results",
        lambda *_args, **_kwargs: [{"node_key": "Concept_Alpha", "rank": -1.0}],
    )
    monkeypatch.setattr(
        tool_search,
        "_lexical_fallback_scores",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("healthy FTS must not trigger corpus fallback")
        ),
    )

    assert "**Alpha Policy**" in tool_search.search_vector_lake("alpha", top_k=5)


def test_degraded_lexical_fallback_has_hard_node_budget(monkeypatch):
    observed = []

    class CountingNodes:
        def __init__(self, values):
            self.values = values

        def items(self):
            for item in self.values.items():
                observed.append(item[0])
                yield item

    nodes = CountingNodes(
        {
            f"Concept_{index}": {"raw_text": "needle"}
            for index in range(10)
        }
    )
    monkeypatch.setenv("VECTOR_LAKE_LEXICAL_FALLBACK_MAX_NODES", "3")

    scores = tool_search._lexical_fallback_scores(
        {"nodes": nodes},
        ["needle"],
        limit=2,
    )

    assert observed == ["Concept_0", "Concept_1", "Concept_2"]
    assert list(scores) == ["Concept_0", "Concept_1"]


def test_degraded_fallback_cannot_reduce_exact_identity_score():
    scores = {"Concept_Alpha": 120.0}

    tool_search._merge_fallback_scores(scores, {"Concept_Alpha": 1.0})

    assert scores == {"Concept_Alpha": 120.0}


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
    monkeypatch.setenv("VECTOR_LAKE_QUERY_EMBEDDING", "1")
    monkeypatch.setattr(tool_search, "_get_query_embedding", lambda _query: [0.1])

    def unavailable(*_args, **_kwargs):
        raise tool_search.SearchBackendError("vector")

    monkeypatch.setattr(tool_search, "_get_vector_search_results", unavailable)

    result = tool_search.search_vector_lake("alpha", top_k=3)

    base_result = _base_search_result(result)
    assert base_result.startswith("[Search degraded: vector]")
    assert "**Alpha Policy**" in base_result


def test_healthy_empty_search_is_explicit(isolated_memory, monkeypatch):
    _install_index(isolated_memory, monkeypatch, {})
    monkeypatch.setattr(
        tool_search,
        "_get_fts_search_results",
        lambda *_args, **_kwargs: [],
    )

    assert _base_search_result(
        tool_search.search_vector_lake("absent")
    ) == "No matching evidence found.\n"


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

    assert _base_search_result(result).startswith(
        "[Search degraded: result_budget]"
    )
    assert len(result) < 1_200
    assert status["last"]["result_chars"] == len(result)
    assert status["last"]["result_bytes"] == len(result.encode("utf-8"))
    assert status["last"]["total_ms"] >= 0
    assert status["last"]["fts_ms"] >= 0


def test_search_result_budget_is_enforced_in_utf8_bytes(
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
        "中" * 2_000,
        encoding="utf-8",
    )
    monkeypatch.setenv("VECTOR_LAKE_SEARCH_RESULT_MAX_CHARS", "10000")
    monkeypatch.setenv("VECTOR_LAKE_SEARCH_RESULT_MAX_BYTES", "4096")
    monkeypatch.setattr(
        tool_search,
        "_get_fts_search_results",
        lambda *_args, **_kwargs: [
            {"node_key": "Concept_Alpha", "rank": -1.0}
        ],
    )

    result = tool_search.search_vector_lake("alpha", top_k=5)
    status = tool_search.search_performance_status()

    assert _base_search_result(result).startswith(
        "[Search degraded: result_budget]"
    )
    assert status["result_byte_limit"] == 4096
    assert status["last"]["result_bytes"] == len(result.encode("utf-8"))


def test_search_snapshot_reads_do_not_wait_for_publisher_lock(monkeypatch):
    calls = []
    expected = {"nodes": {}}

    def read_snapshot(path, **kwargs):
        calls.append((path, kwargs))
        return expected

    monkeypatch.setattr(tool_search, "read_committed_index_snapshot", read_snapshot)

    assert tool_search._load_search_index("index.json") is expected
    assert calls == [("index.json", {"_acquire_lock": False})]


def test_failed_search_records_projection_timing(isolated_memory, monkeypatch):
    index_path = isolated_memory / "wiki" / "index.json"
    index_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        tool_search,
        "_load_search_index",
        lambda _path: (_ for _ in ()).throw(RuntimeError("publisher changed")),
    )

    try:
        tool_search.search_vector_lake("alpha")
    except tool_search.SearchIndexError:
        pass
    else:
        raise AssertionError("search must fail closed on an unstable projection")

    status = tool_search.search_performance_status()
    assert "projection_snapshot" in status["last"]["backend_issues"]
    assert status["last"]["result_bytes"] == 0


def test_search_bypasses_remote_embedding_when_fts_has_enough_candidates(
    isolated_memory,
    monkeypatch,
):
    nodes = {
        f"Concept_{index}": {
            "title": f"Alpha {index}",
            "summary": "alpha",
            "type": "concept",
            "status": "active",
        }
        for index in range(5)
    }
    _install_index(isolated_memory, monkeypatch, nodes)
    monkeypatch.setenv("VECTOR_LAKE_QUERY_EMBEDDING", "1")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(
        tool_search,
        "_get_fts_search_results",
        lambda *_args, **_kwargs: [
            {"node_key": key, "rank": -1.0} for key in nodes
        ],
    )
    monkeypatch.setattr(
        tool_search,
        "_get_query_embedding",
        lambda _query: (_ for _ in ()).throw(
            AssertionError("remote embedding must be bypassed")
        ),
    )

    result = tool_search.search_vector_lake("alpha", top_k=5)

    assert "Alpha" in result
    assert (
        tool_search.search_performance_status()["last"][
            "embedding_bypassed_by_fts"
        ]
        == 1.0
    )


def test_search_keeps_embedding_for_sparse_fts_or_explicit_always(
    monkeypatch,
):
    monkeypatch.setenv("VECTOR_LAKE_QUERY_EMBEDDING", "1")

    assert tool_search._should_query_embedding(fts_result_count=1, top_k=5) is True
    assert tool_search._should_query_embedding(fts_result_count=5, top_k=5) is False

    monkeypatch.setenv("VECTOR_LAKE_QUERY_EMBEDDING_ALWAYS", "1")
    assert tool_search._should_query_embedding(fts_result_count=5, top_k=5) is True
