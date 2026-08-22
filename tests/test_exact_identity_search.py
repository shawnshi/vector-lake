import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from vector_lake import tool_search


def _snapshot(*, generation: str = "generation-1") -> dict:
    return {
        "projection_manifest": {"generation": generation},
        "graph_state": {"dirty": True},
        "weighted_edges": [],
        "nodes": {
            "Source_Alpha": {
                "id": "source-alpha",
                "title": "Alpha Source",
                "aliases": ["Shared Alias", "ＡＬＰＨＡ"],
                "type": "source",
                "status": "Active",
                "summary": "Primary source.",
            },
            "Concept_Beta": {
                "id": "concept-beta",
                "title": "Beta",
                "aliases": ["Shared Alias"],
                "type": "concept",
                "status": "Active",
                "summary": "Secondary concept.",
            },
        },
    }


def test_exact_identity_lookup_preserves_ambiguous_aliases_and_unicode():
    snapshot = _snapshot()

    shared = tool_search._exact_identity_scores(snapshot, "shared alias")
    unicode_match = tool_search._exact_identity_scores(snapshot, "alpha")

    assert set(shared) == {"Source_Alpha", "Concept_Beta"}
    assert unicode_match == {"Source_Alpha": 112.0}


@pytest.mark.parametrize(
    ("query", "expected_score"),
    [
        ("Source_Alpha.md", 120.0),
        ("source-alpha", 118.0),
        ("Alpha Source", 116.0),
        ("alpha", 112.0),
    ],
)
def test_exact_identity_lookup_covers_each_supported_identity(query, expected_score):
    assert tool_search._exact_identity_scores(_snapshot(), query) == {
        "Source_Alpha": expected_score
    }


def test_exact_identity_lookup_is_generation_scoped():
    first = _snapshot(generation="first")
    second = _snapshot(generation="second")
    second["nodes"]["Concept_Beta"]["aliases"] = ["Replacement"]

    assert "Concept_Beta" in tool_search._exact_identity_scores(first, "Shared Alias")
    assert "Concept_Beta" not in tool_search._exact_identity_scores(
        second, "Shared Alias"
    )
    assert tool_search._exact_identity_scores(second, "replacement") == {
        "Concept_Beta": 112.0
    }


def test_exact_identity_cold_build_is_single_flight():
    class CountingNodes(dict):
        def __init__(self, values):
            super().__init__(values)
            self.items_calls = 0
            self.lock = threading.Lock()

        def items(self):
            with self.lock:
                self.items_calls += 1
            return super().items()

    snapshot = _snapshot(generation="concurrent-single-flight")
    nodes = CountingNodes(snapshot["nodes"])
    snapshot["nodes"] = nodes
    barrier = threading.Barrier(8)

    def resolve(index):
        barrier.wait(timeout=5.0)
        query = "Source_Alpha" if index % 2 else "Concept_Beta"
        return tool_search._exact_identity_scores(snapshot, query)

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(resolve, range(8)))

    assert all(results)
    assert nodes.items_calls == 1


def test_exact_source_identity_survives_top_one_source_budget(
    tmp_path,
    monkeypatch,
):
    snapshot = _snapshot(generation="integration")
    index_path = tmp_path / "index.json"
    index_path.write_text("{}", encoding="utf-8")
    wiki_dir = tmp_path / "wiki"
    wiki_dir.mkdir()
    (wiki_dir / "Source_Alpha.md").write_text(
        "Alpha source body.", encoding="utf-8"
    )

    monkeypatch.setattr(tool_search, "get_index_path", lambda: index_path)
    monkeypatch.setattr(tool_search, "get_wiki_dir", lambda: wiki_dir)
    monkeypatch.setattr(tool_search, "_load_search_index", lambda _path: snapshot)
    monkeypatch.setattr(tool_search, "_get_fts_search_results", lambda *_a, **_k: [])
    monkeypatch.setenv("VECTOR_LAKE_QUERY_EMBEDDING", "1")
    monkeypatch.setattr(
        tool_search,
        "_get_query_embedding",
        lambda _query: (_ for _ in ()).throw(
            AssertionError("exact identity must bypass remote embedding")
        ),
    )

    result = tool_search.search_vector_lake("ＡＬＰＨＡ", top_k=1)

    assert "Alpha Source" in result
    assert "Concept_Beta" not in result


def test_ineligible_exact_identity_does_not_suppress_embedding(
    tmp_path,
    monkeypatch,
):
    snapshot = _snapshot(generation="ineligible")
    snapshot["nodes"]["Source_Alpha"]["status"] = "Archived"
    index_path = tmp_path / "index.json"
    index_path.write_text("{}", encoding="utf-8")
    wiki_dir = tmp_path / "wiki"
    wiki_dir.mkdir()

    monkeypatch.setattr(tool_search, "get_index_path", lambda: index_path)
    monkeypatch.setattr(tool_search, "get_wiki_dir", lambda: wiki_dir)
    monkeypatch.setattr(tool_search, "_load_search_index", lambda _path: snapshot)
    monkeypatch.setattr(tool_search, "_get_fts_search_results", lambda *_a, **_k: [])
    monkeypatch.setenv("VECTOR_LAKE_QUERY_EMBEDDING", "1")
    observed = []

    def get_embedding(query):
        observed.append(query)
        return []

    monkeypatch.setattr(tool_search, "_get_query_embedding", get_embedding)

    result = tool_search.search_vector_lake("alpha", top_k=1)

    assert observed == ["alpha"]
    assert "Alpha Source" not in result
