import tracemalloc

import pytest

from vector_lake import tool_search


def _legacy_graph_expansion_scores(seed_keys, weighted_edges, *, hops=2, alpha=0.85):
    adjacency = {}
    for edge in weighted_edges:
        source = edge["source"]
        target = edge["target"]
        weight = edge.get("weight", 1.0)
        adjacency.setdefault(source, []).append((target, weight))
        adjacency.setdefault(target, []).append((source, weight))

    scores = {key: 1.0 for key in seed_keys}
    for _ in range(hops):
        next_scores = {
            key: (1 - alpha) if key in seed_keys else 0.0
            for key in scores
        }
        for node, current_score in scores.items():
            neighbors = adjacency.get(node, ())
            if neighbors:
                total_weight = sum(weight for _, weight in neighbors)
                for neighbor, weight in neighbors:
                    next_scores[neighbor] = (
                        next_scores.get(neighbor, 0.0)
                        + alpha * current_score * (weight / total_weight)
                    )
        scores = next_scores
    return scores


def test_graph_expansion_preserves_legacy_two_hop_scores():
    edges = [
        {"source": "Seed", "target": "A", "weight": 2.0},
        {"source": "A", "target": "B", "weight": 1.0},
        {"source": "Seed", "target": "Seed", "weight": 0.5},
        {"source": "A", "target": "B", "weight": 0.75},
        {"source": "Detached", "target": "Other", "weight": 100.0},
    ]

    from vector_lake import index_snapshot

    class FailOnIteration(list):
        def __iter__(self):
            raise AssertionError("compact graph expansion rescanned weighted_edges")

    expected = _legacy_graph_expansion_scores({"Seed"}, edges)
    actual = tool_search._graph_expansion_scores({"Seed"}, edges)
    adjacency = index_snapshot.get_compact_graph_adjacency(
        {"weighted_edges": edges}
    )
    compact = tool_search._graph_expansion_scores(
        {"Seed"},
        FailOnIteration(edges),
        adjacency=adjacency,
    )

    assert actual == pytest.approx(expected, rel=1e-12, abs=1e-12)
    assert compact == pytest.approx(expected, rel=1e-12, abs=1e-12)
    assert adjacency.edge_count == len(edges)
    assert adjacency.directed_entry_count == len(edges) * 2
    assert "Detached" not in actual
    assert "Other" not in actual


@pytest.mark.parametrize("weight", [0.0, -1.0, float("inf"), float("nan")])
def test_graph_expansion_skips_invalid_total_weight_in_both_paths(weight):
    from vector_lake import index_snapshot

    edges = [{"source": "Seed", "target": "Invalid", "weight": weight}]
    adjacency = index_snapshot.get_compact_graph_adjacency(
        {"weighted_edges": edges}
    )

    fallback = tool_search._graph_expansion_scores({"Seed"}, edges)
    compact = tool_search._graph_expansion_scores(
        {"Seed"},
        edges,
        adjacency=adjacency,
    )

    assert fallback == pytest.approx({"Seed": 0.15})
    assert compact == pytest.approx(fallback)


def test_graph_expansion_does_not_allocate_disconnected_adjacency():
    edges = [
        {
            "source": f"Detached_{index}",
            "target": f"Other_{index}",
            "weight": 1.0,
        }
        for index in range(10_000)
    ]
    edges.append({"source": "Seed", "target": "Neighbor", "weight": 1.0})

    tracemalloc.start()
    try:
        scores = tool_search._graph_expansion_scores({"Seed"}, edges)
        _, peak_bytes = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert set(scores) == {"Seed", "Neighbor"}
    assert peak_bytes < 256 * 1024


def test_assemble_context_reads_at_most_fifty_summary_nodes(
    isolated_memory,
    monkeypatch,
):
    class GuardedNodes:
        def __init__(self):
            self.yielded = 0

        def items(self):
            for index in range(50):
                self.yielded += 1
                yield (
                    f"Concept_{index}",
                    {"type": "concept", "title": f"Concept {index}"},
                )
            raise AssertionError("index summary consumed more than 50 nodes")

    index_path = isolated_memory / "wiki" / "index.json"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text("{}", encoding="utf-8")
    nodes = GuardedNodes()
    monkeypatch.setattr(tool_search, "get_index_path", lambda: index_path)
    monkeypatch.setattr(
        tool_search,
        "_load_search_index",
        lambda _path: {"nodes": nodes},
    )
    monkeypatch.setattr(tool_search, "search_vector_lake", lambda *_args, **_kwargs: "")
    monkeypatch.setattr(
        tool_search,
        "build_memory_packet",
        lambda *_args, **_kwargs: {
            "packet": "",
            "memory_count": 0,
            "warning_count": 0,
            "omitted_count": 0,
        },
    )

    context = tool_search.assemble_context("seed", max_chars=100_000)

    assert nodes.yielded == 50
    assert context["index_summary"].count("\n") == 49
    assert "[concept] Concept 49" in context["index_summary"]


@pytest.mark.parametrize(
    "damage",
    (
        "missing_sidecar",
        "tampered_digest",
        "projection_generation",
        "blocking_generation",
    ),
)
def test_assemble_context_fails_closed_when_projection_changes_after_search(
    isolated_memory,
    monkeypatch,
    damage,
):
    import json

    from vector_lake import db_store, governance_store, indexer
    from vector_lake.wiki_utils import (
        get_index_path,
        get_projection_manifest_path,
    )

    db_store.init_db()
    governance_store.upsert_entity(
        "entity_context_seed",
        {
            "entity_id": "entity_context_seed",
            "canonical_name": "Context Seed",
            "entity_type": "concept",
            "page_key": "Concept_Context-Seed",
        },
    )
    indexer.generate_index()
    monkeypatch.setattr(
        tool_search,
        "build_memory_packet",
        lambda *_args, **_kwargs: {
            "packet": "",
            "memory_count": 0,
            "warning_count": 0,
            "omitted_count": 0,
        },
    )

    def damage_after_search(*_args, **_kwargs):
        if damage == "missing_sidecar":
            get_projection_manifest_path().unlink()
        elif damage == "tampered_digest":
            index_path = get_index_path()
            payload = json.loads(index_path.read_text(encoding="utf-8"))
            payload["schema_version"] = "tampered"
            index_path.write_text(json.dumps(payload), encoding="utf-8")
        elif damage == "projection_generation":
            governance_store.upsert_entity(
                "entity_context_projection_generation",
                {
                    "entity_id": "entity_context_projection_generation",
                    "canonical_name": "Context Projection Generation",
                    "entity_type": "concept",
                    "page_key": "Concept_Context-Projection-Generation",
                },
            )
            indexer.generate_index()
        else:
            governance_store.upsert_entity(
                "entity_context_generation_drift",
                {
                    "entity_id": "entity_context_generation_drift",
                    "canonical_name": "Context Generation Drift",
                    "entity_type": "concept",
                    "page_key": "Concept_Context-Generation-Drift",
                },
            )
        return ""

    monkeypatch.setattr(
        tool_search,
        "search_vector_lake",
        damage_after_search,
    )

    with pytest.raises(tool_search.SearchIndexError, match="changed during context"):
        tool_search.assemble_context("seed", max_chars=12_000)


def test_graph_expansion_applies_same_eligibility_gate(
    isolated_memory,
    monkeypatch,
):
    import json

    def node(title, **overrides):
        return {
            "title": title,
            "type": "concept",
            "status": "Active",
            "domain": "clinical",
            "topic_cluster": "policy",
            "approved": True,
            **overrides,
        }

    nodes = {
        "Seed": node("Seed"),
        "WrongDomain": node("Wrong Domain", domain="finance"),
        "WrongCluster": node("Wrong Cluster", topic_cluster="other"),
        "Archived": node("Archived", status="archived"),
        "Deprecated": node("Deprecated", status="deprecated"),
        "Filtered": node("Filtered", approved=False),
        "Allowed": node("Allowed"),
    }
    expansion_keys = [
        "WrongDomain",
        "WrongCluster",
        "Archived",
        "Deprecated",
        "Filtered",
        "Allowed",
    ]
    index_data = {
        "nodes": nodes,
        "weighted_edges": [
            {
                "source": "Seed",
                "target": key,
                "weight": float(len(expansion_keys) - index),
            }
            for index, key in enumerate(expansion_keys)
        ],
        "graph_state": {"dirty": False},
    }
    index_path = isolated_memory / "wiki" / "index.json"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(json.dumps(index_data), encoding="utf-8")
    monkeypatch.setattr(tool_search, "_load_search_index", lambda _path: index_data)
    monkeypatch.setattr(
        tool_search,
        "_get_fts_search_results",
        lambda *_args, **_kwargs: [{"node_key": "Seed", "rank": -1.0}],
    )
    monkeypatch.setattr(tool_search, "_get_query_embedding", lambda *_args: [])
    monkeypatch.setattr(tool_search, "_expand_query_locally", lambda *_args: ["seed"])

    result = tool_search.search_vector_lake(
        "seed",
        top_k=10,
        domain="clinical",
        cluster="policy",
        filter_expr="get('approved') == True",
    )

    assert "**Seed**" in result
    assert "**Allowed**" in result
    for blocked_title in (
        "Wrong Domain",
        "Wrong Cluster",
        "Archived",
        "Deprecated",
        "Filtered",
    ):
        assert f"**{blocked_title}**" not in result


def test_loaded_index_snapshot_is_deeply_read_only_and_json_compatible(
    isolated_memory,
):
    import json

    from vector_lake import index_snapshot

    index_path = isolated_memory / "wiki" / "index.json"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(
        json.dumps(
            {
                "nodes": {
                    "Seed": {
                        "title": "Seed",
                        "aliases": ["Alias"],
                    }
                },
                "weighted_edges": [
                    {"source": "Seed", "target": "Other", "weight": 1.0}
                ],
            }
        ),
        encoding="utf-8",
    )
    index_snapshot.clear_index_snapshot_cache_for_tests()

    snapshot = index_snapshot.load_legacy_index_snapshot_for_migration(index_path)

    assert isinstance(snapshot, dict)
    assert isinstance(snapshot["weighted_edges"], list)
    assert json.loads(json.dumps(snapshot))["nodes"]["Seed"]["title"] == "Seed"
    with pytest.raises(TypeError, match="read-only"):
        snapshot["nodes"] = {}
    with pytest.raises(TypeError, match="read-only"):
        snapshot["nodes"]["Seed"]["title"] = "Changed"
    with pytest.raises(TypeError, match="read-only"):
        snapshot["nodes"]["Seed"]["aliases"].append("Changed")
    with pytest.raises(TypeError, match="read-only"):
        snapshot["weighted_edges"].append({})
    with pytest.raises(TypeError, match="read-only"):
        snapshot["weighted_edges"][0]["weight"] = 2.0


def test_index_snapshot_uses_streaming_decoder_by_default(
    isolated_memory,
    monkeypatch,
):
    import json

    from vector_lake import index_snapshot

    class FailIfCalled:
        @staticmethod
        def loads(_payload):
            raise AssertionError("orjson requires an explicit memory tradeoff")

    def fail_full_load(_handle):
        raise AssertionError("full-document json.load requires explicit opt-in")

    index_path = isolated_memory / "wiki" / "index.json"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(json.dumps({"nodes": {}}), encoding="utf-8")
    monkeypatch.setattr(index_snapshot, "_orjson", FailIfCalled())
    monkeypatch.setattr(index_snapshot.json, "load", fail_full_load)
    monkeypatch.delenv("VECTOR_LAKE_INDEX_USE_ORJSON", raising=False)
    monkeypatch.delenv("VECTOR_LAKE_INDEX_FULL_LOAD", raising=False)
    index_snapshot.clear_index_snapshot_cache_for_tests()

    snapshot = index_snapshot.load_legacy_index_snapshot_for_migration(index_path)

    assert snapshot["nodes"] == {}


def test_index_snapshot_full_load_decoder_requires_explicit_opt_in(
    isolated_memory,
    monkeypatch,
):
    from vector_lake import index_snapshot

    calls = []

    def recording_load(handle):
        calls.append(handle.read())
        return {"nodes": {}}

    index_path = isolated_memory / "wiki" / "index.json"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text('{"nodes": {}}', encoding="utf-8")
    monkeypatch.setattr(index_snapshot.json, "load", recording_load)
    monkeypatch.delenv("VECTOR_LAKE_INDEX_USE_ORJSON", raising=False)
    monkeypatch.setenv("VECTOR_LAKE_INDEX_FULL_LOAD", "1")
    index_snapshot.clear_index_snapshot_cache_for_tests()

    snapshot = index_snapshot.load_legacy_index_snapshot_for_migration(index_path)

    assert snapshot["nodes"] == {}
    assert calls == ['{"nodes": {}}']


def test_index_snapshot_orjson_decoder_requires_explicit_opt_in(
    isolated_memory,
    monkeypatch,
):
    from vector_lake import index_snapshot

    calls = []

    class RecordingDecoder:
        @staticmethod
        def loads(payload):
            calls.append(payload)
            return {"nodes": {}}

    index_path = isolated_memory / "wiki" / "index.json"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text('{"nodes": {}}', encoding="utf-8")
    monkeypatch.setattr(index_snapshot, "_orjson", RecordingDecoder())
    monkeypatch.setenv("VECTOR_LAKE_INDEX_USE_ORJSON", "1")
    index_snapshot.clear_index_snapshot_cache_for_tests()

    snapshot = index_snapshot.load_legacy_index_snapshot_for_migration(index_path)

    assert snapshot["nodes"] == {}
    assert calls == [b'{"nodes": {}}']


def test_compact_graph_cache_reuses_snapshot_without_mutating_it_and_invalidates(
    isolated_memory,
):
    import json

    from vector_lake import index_snapshot

    index_path = isolated_memory / "wiki" / "index.json"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    first_payload = {
        "nodes": {
            "Seed": {"title": "Seed"},
            "A": {"title": "A"},
        },
        "weighted_edges": [
            {"source": "Seed", "target": "A", "weight": 1.0}
        ],
    }
    index_path.write_text(json.dumps(first_payload), encoding="utf-8")
    index_snapshot.clear_index_snapshot_cache_for_tests()

    first_snapshot = index_snapshot.load_legacy_index_snapshot_for_migration(
        index_path
    )
    before = json.dumps(first_snapshot, sort_keys=True)
    first_adjacency = index_snapshot.get_compact_graph_adjacency(first_snapshot)
    reused_adjacency = index_snapshot.get_compact_graph_adjacency(first_snapshot)

    assert reused_adjacency is first_adjacency
    assert json.dumps(first_snapshot, sort_keys=True) == before

    second_payload = {
        "nodes": {
            **first_payload["nodes"],
            "B": {"title": "B"},
        },
        "weighted_edges": [
            *first_payload["weighted_edges"],
            {"source": "A", "target": "B", "weight": 2.0},
        ],
    }
    index_path.write_text(json.dumps(second_payload), encoding="utf-8")
    second_snapshot = index_snapshot.load_legacy_index_snapshot_for_migration(
        index_path
    )
    second_adjacency = index_snapshot.get_compact_graph_adjacency(second_snapshot)

    assert second_snapshot is not first_snapshot
    assert second_adjacency is not first_adjacency
    assert second_adjacency.edge_count == 2

    rebuilt_first_adjacency = index_snapshot.get_compact_graph_adjacency(
        first_snapshot
    )
    assert rebuilt_first_adjacency is not first_adjacency


def test_compact_graph_cache_capacity_falls_back_to_scan(
    monkeypatch,
):
    from vector_lake import index_snapshot

    edges = [
        {"source": "Seed", "target": "A", "weight": 2.0},
        {"source": "A", "target": "B", "weight": 1.0},
    ]
    snapshot = {"weighted_edges": edges}
    monkeypatch.setattr(index_snapshot, "_graph_cache_budget_bytes", lambda: 1)
    index_snapshot.clear_index_snapshot_cache_for_tests()

    adjacency = index_snapshot.get_compact_graph_adjacency(snapshot)
    scores = tool_search._graph_expansion_scores(
        {"Seed"},
        edges,
        adjacency=adjacency,
    )

    assert adjacency is None
    assert scores == pytest.approx(
        _legacy_graph_expansion_scores({"Seed"}, edges),
        rel=1e-12,
        abs=1e-12,
    )


def test_compact_graph_cache_builds_once_for_concurrent_readers(
    monkeypatch,
):
    import time
    from concurrent.futures import ThreadPoolExecutor

    from vector_lake import index_snapshot

    snapshot = {
        "weighted_edges": [
            {"source": "Seed", "target": "A", "weight": 1.0},
            {"source": "A", "target": "B", "weight": 2.0},
        ]
    }
    real_build = index_snapshot._build_compact_graph_adjacency
    build_calls = []

    def tracked_build(weighted_edges):
        build_calls.append(1)
        time.sleep(0.02)
        return real_build(weighted_edges)

    monkeypatch.setattr(
        index_snapshot,
        "_build_compact_graph_adjacency",
        tracked_build,
    )
    index_snapshot.clear_index_snapshot_cache_for_tests()

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(
            executor.map(
                index_snapshot.get_compact_graph_adjacency,
                [snapshot] * 8,
            )
        )

    assert len(build_calls) == 1
    assert all(result is results[0] for result in results)



def test_streaming_index_decoder_matches_stdlib_for_nested_json(
    isolated_memory,
    monkeypatch,
):
    import json

    from vector_lake import index_snapshot

    payload = {
        "nodes": {
            "多语言\\\"键": {
                "title": "换行\\n制表\\t反斜杠\\\\引号\\\"",
                "values": [None, True, False, -7, 1.25, 6.02e23],
                "nested": {"empty_object": {}, "empty_list": []},
            }
        },
        "weighted_edges": [
            {"source": "多语言\\\"键", "target": "Other", "weight": 0.75}
        ],
    }
    index_path = isolated_memory / "wiki" / "index.json"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    monkeypatch.delenv("VECTOR_LAKE_INDEX_USE_ORJSON", raising=False)
    monkeypatch.delenv("VECTOR_LAKE_INDEX_FULL_LOAD", raising=False)

    snapshot = index_snapshot._decode_index_snapshot(index_path)

    assert snapshot == payload
    assert isinstance(snapshot, index_snapshot.FrozenDict)
    assert isinstance(
        snapshot["nodes"]["多语言\\\"键"]["values"],
        index_snapshot.FrozenList,
    )


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"[]",
        b'{"x": [1,]}',
        b'{"x": tru}',
        b'{"x": "unterminated}',
        b'{"x": 1} trailing',
        b'{"x": "control\\x01"}',
        b'{"x": 2\x00}',
        b'{"x": \x002}',
    ],
)
def test_streaming_index_decoder_rejects_invalid_or_non_object_json(
    isolated_memory,
    payload,
):
    from vector_lake import index_snapshot

    index_path = isolated_memory / "wiki" / "index.json"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_bytes(payload)

    with pytest.raises(ValueError):
        index_snapshot._decode_index_snapshot_streaming(index_path)


def test_graph_cache_capacity_uses_memory_budget_instead_of_fixed_counts(
    monkeypatch,
):
    from vector_lake import index_snapshot

    edges = [
        {"source": "Seed", "target": "A", "weight": 2.0},
        {"source": "A", "target": "B", "weight": 1.0},
    ]
    exact_budget = index_snapshot._estimated_graph_cache_bytes(2, 3)
    monkeypatch.setattr(
        index_snapshot,
        "_graph_cache_budget_bytes",
        lambda: exact_budget,
    )

    adjacency = index_snapshot._build_compact_graph_adjacency(edges)

    assert adjacency.edge_count == 2
    assert adjacency.node_count == 3

    monkeypatch.setattr(
        index_snapshot,
        "_graph_cache_budget_bytes",
        lambda: exact_budget - 1,
    )
    with pytest.raises(index_snapshot._GraphCacheCapacityExceeded):
        index_snapshot._build_compact_graph_adjacency(edges)


def test_graph_cache_budget_environment_is_bounded(monkeypatch):
    from vector_lake import index_snapshot

    monkeypatch.setenv("VECTOR_LAKE_GRAPH_CACHE_MAX_MIB", "invalid")
    assert index_snapshot._graph_cache_budget_bytes() == 64 * 1024 * 1024

    monkeypatch.setenv("VECTOR_LAKE_GRAPH_CACHE_MAX_MIB", "1")
    assert index_snapshot._graph_cache_budget_bytes() == 8 * 1024 * 1024

    monkeypatch.setenv("VECTOR_LAKE_GRAPH_CACHE_MAX_MIB", "9999")
    assert index_snapshot._graph_cache_budget_bytes() == 512 * 1024 * 1024


def test_streaming_decoder_preserves_stdlib_supported_nesting_depth(
    isolated_memory,
):
    import json

    from vector_lake import index_snapshot

    depth = 2_500
    payload = b'{"x":' + b"[" * depth + b"0" + b"]" * depth + b"}"
    assert json.loads(payload)
    index_path = isolated_memory / "wiki" / "index.json"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_bytes(payload)

    snapshot = index_snapshot._decode_index_snapshot_streaming(index_path)

    value = snapshot["x"]
    for _ in range(depth):
        value = value[0]
    assert value == 0


def test_stale_graph_build_does_not_retain_previous_snapshot(monkeypatch):
    from vector_lake import index_snapshot

    old_snapshot = {
        "weighted_edges": [
            {"source": "Old", "target": "A", "weight": 1.0}
        ]
    }
    current_snapshot = {
        "weighted_edges": [
            {"source": "Current", "target": "B", "weight": 1.0}
        ]
    }
    real_build = index_snapshot._build_compact_graph_adjacency

    def replace_snapshot_during_build(weighted_edges):
        adjacency = real_build(weighted_edges)
        with index_snapshot._CACHE_LOCK:
            index_snapshot._CACHE.update(
                {"key": ("current", 1, 1, 1), "value": current_snapshot}
            )
            index_snapshot._reset_graph_cache_locked()
        return adjacency

    index_snapshot.clear_index_snapshot_cache_for_tests()
    with index_snapshot._CACHE_LOCK:
        index_snapshot._CACHE.update(
            {"key": ("old", 1, 1, 1), "value": old_snapshot}
        )
    monkeypatch.setattr(
        index_snapshot,
        "_build_compact_graph_adjacency",
        replace_snapshot_during_build,
    )

    adjacency = index_snapshot.get_compact_graph_adjacency(old_snapshot)

    assert adjacency is not None
    with index_snapshot._CACHE_LOCK:
        assert index_snapshot._CACHE["value"] is current_snapshot
        assert index_snapshot._GRAPH_CACHE["snapshot"] is not old_snapshot
        assert index_snapshot._GRAPH_CACHE["edges"] is not old_snapshot["weighted_edges"]


def test_streaming_decoder_has_stable_nesting_limit(isolated_memory):
    from vector_lake import index_snapshot

    depth = index_snapshot._MAX_JSON_NESTING_DEPTH
    payload = b'{"x":' + b"[" * depth + b"0" + b"]" * depth + b"}"
    index_path = isolated_memory / "wiki" / "index.json"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_bytes(payload)

    with pytest.raises(ValueError, match="JSON nesting exceeds 4096"):
        index_snapshot._decode_index_snapshot_streaming(index_path)


def test_streaming_decode_releases_source_before_parsing(
    isolated_memory,
    monkeypatch,
):
    import os
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from vector_lake import index_snapshot

    index_path = isolated_memory / "wiki" / "index.json"
    replacement_path = isolated_memory / "wiki" / "replacement.json"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text('{"version": 1}', encoding="utf-8")
    replacement_path.write_text('{"version": 2}', encoding="utf-8")
    decode_started = threading.Event()
    allow_decode = threading.Event()
    real_decoder = index_snapshot._IncrementalJsonDecoder

    class BlockingDecoder:
        def __init__(self, payload):
            self._payload = payload

        def decode(self):
            decode_started.set()
            assert allow_decode.wait(timeout=5)
            return real_decoder(self._payload).decode()

    monkeypatch.setattr(
        index_snapshot,
        "_IncrementalJsonDecoder",
        BlockingDecoder,
    )
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            index_snapshot._decode_index_snapshot_streaming,
            index_path,
        )
        assert decode_started.wait(timeout=5)
        os.replace(replacement_path, index_path)
        allow_decode.set()
        decoded = future.result(timeout=5)

    assert decoded["version"] == 1
    assert index_path.read_text(encoding="utf-8") == '{"version": 2}'
