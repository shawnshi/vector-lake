"""Query embedding is enabled by default, with a fail-closed opt-out.

The default flipped from opt-in to opt-out.  Everything else about the cost
profile is unchanged: ``GEMINI_API_KEY`` is still a separate runtime capability
check, and ``_should_query_embedding`` still only reaches the provider when
lexical retrieval is weak, so a search that already has enough FTS candidates
never pays the measured ~4 s remote call.
"""

from __future__ import annotations

import pytest

from vector_lake import tool_search


@pytest.fixture(autouse=True)
def _clear_flag(monkeypatch):
    monkeypatch.delenv("VECTOR_LAKE_QUERY_EMBEDDING", raising=False)
    monkeypatch.delenv("VECTOR_LAKE_QUERY_EMBEDDING_ALWAYS", raising=False)
    monkeypatch.delenv("VECTOR_LAKE_QUERY_EMBEDDING_FTS_BYPASS_MIN_RESULTS", raising=False)


def test_default_is_enabled():
    assert tool_search._query_embedding_enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "False", "no", "off", "disabled", "", "  "])
def test_explicit_non_one_values_still_disable(monkeypatch, value):
    """Any value other than ``1`` keeps the provider off, so a host can opt out."""
    monkeypatch.setenv("VECTOR_LAKE_QUERY_EMBEDDING", value)
    assert tool_search._query_embedding_enabled() is False


def test_explicit_one_enables(monkeypatch):
    monkeypatch.setenv("VECTOR_LAKE_QUERY_EMBEDDING", "1")
    assert tool_search._query_embedding_enabled() is True


def test_opt_out_suppresses_the_provider_even_when_always_is_set(monkeypatch):
    monkeypatch.setenv("VECTOR_LAKE_QUERY_EMBEDDING", "0")
    monkeypatch.setenv("VECTOR_LAKE_QUERY_EMBEDDING_ALWAYS", "1")
    assert tool_search._should_query_embedding(fts_result_count=0, top_k=5) is False


def test_strong_fts_result_does_not_reach_the_provider(monkeypatch):
    """The latency guard: enough lexical candidates means no remote call."""
    monkeypatch.setenv("VECTOR_LAKE_QUERY_EMBEDDING_ALWAYS", "0")
    assert tool_search._should_query_embedding(fts_result_count=5, top_k=5) is False
    assert tool_search._should_query_embedding(fts_result_count=0, top_k=5) is True


def test_always_flag_restores_blended_retrieval(monkeypatch):
    monkeypatch.setenv("VECTOR_LAKE_QUERY_EMBEDDING_ALWAYS", "1")
    assert tool_search._should_query_embedding(fts_result_count=50, top_k=5) is True
