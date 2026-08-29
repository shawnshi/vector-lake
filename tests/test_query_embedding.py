import threading
import time

import pytest

from vector_lake import embedding_scheduler, tool_search


def _reset_query_embedding_state():
    tool_search._reset_query_embedding_state_for_tests()


def test_query_embedding_is_cached_and_bounded(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("VECTOR_LAKE_QUERY_EMBEDDING", "1")
    monkeypatch.setenv("VECTOR_LAKE_QUERY_EMBEDDING_TIMEOUT_MS", "1500")
    monkeypatch.setenv("VECTOR_LAKE_QUERY_EMBEDDING_MAX_WAIT_MS", "200")
    calls = []

    def fake_embed(texts, **kwargs):
        calls.append((texts, kwargs))
        return [[0.25, 0.5]]

    monkeypatch.setattr(embedding_scheduler, "embed_texts", fake_embed)
    _reset_query_embedding_state()

    first = tool_search._get_query_embedding("same query")
    second = tool_search._get_query_embedding("same query")

    assert first == [0.25, 0.5]
    assert second == first
    assert len(calls) == 1
    assert calls[0][1] == {
        "max_retries": 0,
        "timeout_ms": 1500,
        "max_wait_seconds": 0.2,
        "initialize_schema": False,
    }
    _reset_query_embedding_state()


def test_query_embedding_failure_opens_short_circuit(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("VECTOR_LAKE_QUERY_EMBEDDING", "1")
    monkeypatch.setenv(
        "VECTOR_LAKE_QUERY_EMBEDDING_FAILURE_COOLDOWN_SECONDS",
        "30",
    )
    calls = []

    def failing_embed(texts, **kwargs):
        calls.append((texts, kwargs))
        raise TimeoutError("provider unavailable")

    monkeypatch.setattr(embedding_scheduler, "embed_texts", failing_embed)
    _reset_query_embedding_state()

    assert tool_search._get_query_embedding("first query") == []
    assert tool_search._get_query_embedding("second query") == []
    assert len(calls) == 1
    assert tool_search._QUERY_EMBEDDING_FAILURE_UNTIL > 0
    _reset_query_embedding_state()


def test_query_embedding_same_key_is_single_flight(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("VECTOR_LAKE_QUERY_EMBEDDING", "1")
    calls = []
    calls_lock = threading.Lock()
    barrier = threading.Barrier(3)
    results = []

    def fake_embed(texts, **kwargs):
        with calls_lock:
            calls.append((texts, kwargs))
        time.sleep(0.05)
        return [[0.25, 0.5]]

    def worker():
        barrier.wait()
        results.append(tool_search._get_query_embedding("concurrent query"))

    monkeypatch.setattr(embedding_scheduler, "embed_texts", fake_embed)
    _reset_query_embedding_state()
    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=2)

    assert results == [[0.25, 0.5], [0.25, 0.5]]
    assert len(calls) == 1
    _reset_query_embedding_state()


def test_cached_query_embedding_bypasses_open_circuit(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("VECTOR_LAKE_QUERY_EMBEDDING", "1")
    calls = []

    def fake_embed(texts, **kwargs):
        calls.append((texts, kwargs))
        return [[0.25, 0.5]]

    monkeypatch.setattr(embedding_scheduler, "embed_texts", fake_embed)
    _reset_query_embedding_state()
    assert tool_search._get_query_embedding("cached query") == [0.25, 0.5]
    tool_search._QUERY_EMBEDDING_FAILURE_UNTIL = time.monotonic() + 30

    assert tool_search._get_query_embedding("cached query") == [0.25, 0.5]
    assert tool_search._get_query_embedding("uncached query") == []
    assert len(calls) == 1
    _reset_query_embedding_state()


def test_search_rejects_oversized_query_before_caches_or_embedding(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("VECTOR_LAKE_QUERY_EMBEDDING", "1")
    _reset_query_embedding_state()
    tool_search._expand_query_locally.cache_clear()
    expansion_before = tool_search._expand_query_locally.cache_info()
    embedding_before = tool_search._cached_query_embedding.cache_info()
    calls = []
    monkeypatch.setattr(
        embedding_scheduler,
        "embed_texts",
        lambda *_args, **_kwargs: calls.append(1) or [[0.1]],
    )

    with pytest.raises(ValueError, match="search query exceeds"):
        tool_search.search_vector_lake(
            "x" * (tool_search._SEARCH_QUERY_CHAR_LIMIT + 1),
            mode="page",
        )

    assert calls == []
    assert tool_search._expand_query_locally.cache_info() == expansion_before
    assert tool_search._cached_query_embedding.cache_info() == embedding_before
    assert tool_search._QUERY_EMBEDDING_CACHE_KEYS == {}


def test_search_clamps_top_k_before_mode_dispatch(monkeypatch):
    captured = []
    monkeypatch.setattr(
        tool_search,
        "format_operational_memory_results",
        lambda _query, **kwargs: captured.append(kwargs["top_k"]) or "ok",
    )

    result = tool_search.search_vector_lake("query", mode="memory", top_k=10_000)
    assert result.startswith("<SemanticReadinessEnvelope>\n")
    assert result.endswith("</SemanticReadinessEnvelope>\nok")
    assert captured == [tool_search._SEARCH_TOP_K_LIMIT]


def test_direct_query_embedding_rejects_oversized_text_before_api(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("VECTOR_LAKE_QUERY_EMBEDDING", "1")
    calls = []
    monkeypatch.setattr(
        embedding_scheduler,
        "embed_texts",
        lambda *_args, **_kwargs: calls.append(1) or [[0.1]],
    )

    with pytest.raises(ValueError, match="search query exceeds"):
        tool_search._get_query_embedding(
            "x" * (tool_search._SEARCH_QUERY_CHAR_LIMIT + 1)
        )

    assert calls == []


def test_query_embedding_requires_explicit_opt_in_even_with_api_key(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.delenv("VECTOR_LAKE_QUERY_EMBEDDING", raising=False)
    calls = []
    monkeypatch.setattr(
        embedding_scheduler,
        "embed_texts",
        lambda *_args, **_kwargs: calls.append(1) or [[0.1]],
    )
    _reset_query_embedding_state()

    assert tool_search._get_query_embedding("default local-only query") == []
    assert calls == []
    assert tool_search._cached_query_embedding.cache_info().misses == 0
