import unittest
import json
import time
from unittest.mock import patch

import pytest
from filelock import FileLock, Timeout
from vector_lake.indexer import (
    MAX_EDGES_PER_NODE,
    _apply_graph_topology,
    _bounded_node_edge_candidates,
    _calculate_weighted_edges,
    index_projection_matches_canonical,
    _entity_to_index_node,
    _strip_legacy_embedded_payloads,
    _tokenize,
    get_pred_weight,
    is_graph_dirty,
    _write_index,
    claim_graph_projection_parity,
    refresh_claim_graph_projection,
    update_index_items,
)

class TestIndexer(unittest.TestCase):
    def test_candidate_edge_frontier_is_bounded_and_deterministic(self):
        candidates = [(1.5 + index / 1000, f"Concept_Target-{index:03d}") for index in range(200)]

        retained = _bounded_node_edge_candidates("Concept_Source", candidates, limit=60)

        self.assertEqual(len(retained), 60)
        self.assertEqual(retained[0]["target"], "Concept_Target-199")
        self.assertEqual(retained[-1]["target"], "Concept_Target-140")

    def test_large_incremental_batch_uses_one_full_rebuild(self):
        filenames = [f"Concept_Node-{index}.md" for index in range(251)]

        with patch("vector_lake.indexer.generate_index", return_value="rebuilt") as rebuild:
            result = update_index_items(filenames)

        self.assertEqual(result, "rebuilt")
        rebuild.assert_called_once_with()

    def test_index_write_collapses_undirected_duplicate_edges(self):
        from tempfile import TemporaryDirectory
        from pathlib import Path

        with TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "index.json"
            data = {
                "nodes": {"Concept_A": {}, "Concept_B": {}},
                "aliases": {},
                "weighted_edges": [
                    {"source": "Concept_A", "target": "Concept_B", "weight": 2.0},
                    {"source": "Concept_B", "target": "Concept_A", "weight": 3.0},
                    {"source": "Concept_A", "target": "Concept_B", "weight": 2.5},
                ],
            }

            _write_index(str(output), data)

            written = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(
                written["weighted_edges"],
                [{"source": "Concept_A", "target": "Concept_B", "weight": 3.0}],
            )

    def test_index_write_enforces_global_degree_cap(self):
        from tempfile import TemporaryDirectory
        from pathlib import Path

        with TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "index.json"
            targets = [f"Concept_Target-{index:03d}" for index in range(80)]
            data = {
                "nodes": {"Concept_Hub": {}, **{target: {} for target in targets}},
                "aliases": {},
                "weighted_edges": [
                    {
                        "source": "Concept_Hub",
                        "target": target,
                        "weight": float(100 - index),
                    }
                    for index, target in enumerate(targets)
                ],
            }

            _write_index(str(output), data)

            written = json.loads(output.read_text(encoding="utf-8"))
            hub_edges = [
                edge
                for edge in written["weighted_edges"]
                if "Concept_Hub" in (edge["source"], edge["target"])
            ]
            self.assertEqual(len(hub_edges), MAX_EDGES_PER_NODE)

    def test_topology_analysis_marks_graph_clean_and_surfaces_isolate(self):
        index_data = {
            "nodes": {
                "Concept_A": {"title": "A", "decay_weight": 1.0},
                "Concept_B": {"title": "B", "decay_weight": 1.0},
                "Concept_C": {"title": "C", "decay_weight": 0.8},
                "Concept_Isolated": {"title": "Isolated", "decay_weight": 1.0},
            },
            "weighted_edges": [
                {"source": "Concept_A", "target": "Concept_B", "weight": 3.0},
                {"source": "Concept_B", "target": "Concept_C", "weight": 2.0},
            ],
            "communities": {},
            "community_labels": {},
            "graph_insights": [],
            "graph_state": {"dirty": True, "reason": "test"},
        }

        _apply_graph_topology(index_data)

        self.assertFalse(index_data["graph_state"]["dirty"])
        self.assertEqual(set(index_data["communities"]), set(index_data["nodes"]))
        self.assertTrue(index_data["community_labels"])
        self.assertTrue(
            any(
                insight.get("type") == "isolated_node"
                and insight.get("node") == "Concept_Isolated"
                for insight in index_data["graph_insights"]
            )
        )
        self.assertGreater(index_data["nodes"]["Concept_B"]["centrality_score"], 0)
    def test_get_pred_weight(self):
        # Taxonomy weights
        self.assertEqual(get_pred_weight("is_a"), 3.0)
        self.assertEqual(get_pred_weight("属于"), 9.0)

        # Relation weights
        self.assertEqual(get_pred_weight("related_to"), 4.5)
        self.assertEqual(get_pred_weight("peer"), 4.5)

        # Mention weights
        self.assertAlmostEqual(get_pred_weight("mentions"), 1.2)

        # Default weight
        self.assertEqual(get_pred_weight("unknown_pred"), 3.0)

    def test_tokenize(self):
        # Test basic English and Chinese tokenization
        text = "Hello 架构 world"
        tokens = _tokenize(text)
        self.assertIn("hello", tokens)
        self.assertIn("world", tokens)
        self.assertIn("架", tokens)
        self.assertIn("构", tokens)
        self.assertIn("架构", tokens)

    def test_is_graph_dirty(self):
        self.assertTrue(is_graph_dirty(None))
        self.assertTrue(is_graph_dirty({}))
        self.assertTrue(is_graph_dirty({"graph_state": {"dirty": True}}))
        self.assertFalse(is_graph_dirty({"graph_state": {"dirty": False}}))

    def test_strip_legacy_embedded_payloads(self):
        index_data = {
            "bm25_index": {},
            "governance_queue": {},
            "valid_key": "data"
        }
        removed = _strip_legacy_embedded_payloads(index_data)
        self.assertIn("bm25_index", removed)
        self.assertIn("governance_queue", removed)
        self.assertNotIn("bm25_index", index_data)
        self.assertIn("valid_key", index_data)

    def test_entity_to_index_node_uses_page_key_and_canonical_shape(self):
        entity = {
            "entity_id": "entity_123",
            "page_key": "Vendor_Acme",
            "canonical_name": "Acme Inc",
            "type": "vendor",
            "raw_text": "Canonical body",
            "summary": "Canonical summary",
            "aliases": ["Acme"],
            "categories": ["Company"],
            "sources": ["raw/acme.pdf"],
            "links": ["Product_Widget"],
            "triples": [{"predicate": "created", "target": "Product_Widget"}],
            "updated": "2026-07-13T00:00:00+00:00",
        }

        node_key, node = _entity_to_index_node(entity, "entity_123")

        self.assertEqual(node_key, "Vendor_Acme")
        self.assertEqual(node["title"], "Acme Inc")
        self.assertEqual(node["type"], "vendor")
        self.assertEqual(node["raw_text"], "Canonical body")
        self.assertEqual(node["links"], ["Product_Widget"])

if __name__ == "__main__":
    unittest.main()


def test_full_index_rebuild_does_not_load_duplicate_canonical_snapshots(isolated_memory, monkeypatch):
    from vector_lake import db_store, governance_store, indexer

    db_store.init_db()
    governance_store.upsert_entity(
        "entity_streaming_index",
        {
            "entity_id": "entity_streaming_index",
            "page_key": "Concept_Streaming-Index",
            "canonical_name": "Streaming Index",
            "type": "concept",
            "raw_text": "Compact canonical body.",
            "updated": "2026-07-19T00:00:00+00:00",
        },
    )
    monkeypatch.setattr(
        governance_store,
        "load_entities",
        lambda: (_ for _ in ()).throw(AssertionError("duplicate entity snapshot")),
    )
    monkeypatch.setattr(
        governance_store,
        "load_claims",
        lambda: (_ for _ in ()).throw(AssertionError("duplicate claim snapshot")),
    )

    output = indexer.generate_index()

    assert output.endswith("index.json")
    assert index_projection_matches_canonical(["Concept_Streaming-Index.md"]) is True

    row = db_store.get_connection().execute(
        "SELECT entity_id, data_json FROM entities WHERE entity_id = 'entity_streaming_index'"
    ).fetchone()
    changed = json.loads(row["data_json"])
    changed["raw_text"] = "Changed after index generation."
    governance_store.upsert_entity(row["entity_id"], changed)

    assert index_projection_matches_canonical(["Concept_Streaming-Index.md"]) is False


def test_index_projection_allows_only_explicit_merge_alias_redirect(
    isolated_memory,
):
    from vector_lake import db_store
    from vector_lake.wiki_utils import get_index_path

    db_store.init_db()
    get_index_path().write_text(
        json.dumps(
            {
                "nodes": {},
                "aliases": {"Source_Alpha-ab12cd34": "Source_Alpha"},
                "weighted_edges": [],
            }
        ),
        encoding="utf-8",
    )

    assert index_projection_matches_canonical(["Source_Alpha-ab12cd34.md"]) is False
    assert index_projection_matches_canonical(
        ["Source_Alpha-ab12cd34.md"],
        allowed_alias_redirects={"Source_Alpha-ab12cd34": "Source_Alpha"},
    ) is True


def test_generic_source_does_not_create_similarity_clique(isolated_memory):
    from vector_lake import db_store

    db_store.init_db()
    index_data = {
        "nodes": {
            f"Concept_Node-{index:03d}": {
                "type": "concept",
                "sources": ["[[Source_Auto_Fixed]]"],
                "links": [],
                "triples": [],
                "decay_weight": 1.0,
                "alignment_score": 100.0,
            }
            for index in range(40)
        }
    }

    assert _calculate_weighted_edges(index_data) == []


def test_full_index_rebuild_uses_shared_projection_publish_lock(
    isolated_memory, monkeypatch
):
    from vector_lake import indexer

    seen = []

    class RecordingLock:
        def __init__(self, path, timeout):
            seen.append((path, timeout))

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(indexer, "FileLock", RecordingLock)
    monkeypatch.setattr(
        indexer,
        "_generate_index_unlocked",
        lambda skip_embeddings=True: "rebuilt",
    )

    assert indexer.generate_index() == "rebuilt"
    assert seen == [(str(isolated_memory / "wiki" / "index.json") + ".lock", 15)]


def test_claim_graph_projection_has_independent_parity_and_refresh(isolated_memory):
    from vector_lake import db_store, governance_store

    db_store.init_db()
    claim = {
        "claim_id": "claim_graph_refresh",
        "claim_text": "Original graph claim",
        "status": "Active",
        "source_ids": [],
        "evidence_ids": [],
        "subject_entity_ids": [],
        "updated_at": "2026-07-19T00:00:00+00:00",
        "locator": {"page_key": "Concept_Graph-Refresh"},
    }

    def publish_claim():
        governance_store.apply_change_set(
            {
                "affected_pages": ["Concept_Graph-Refresh.md"],
                "proposed_entities": [],
                "proposed_claims": [claim],
                "proposed_evidence": [],
                "proposed_source_updates": [],
                "proposed_edges": [],
            }
        )

    publish_claim()

    refresh_claim_graph_projection()
    assert claim_graph_projection_parity()["missing_nodes"] == 0
    assert claim_graph_projection_parity()["extra_nodes"] == 0

    claim_graph_path = isolated_memory / "wiki" / "claim_graph.json"
    stored = json.loads(claim_graph_path.read_text(encoding="utf-8"))
    stored.setdefault("edges", []).append(
        {"source": "claim_graph_refresh", "target": "fake", "relation": "fake", "weight": 1.0}
    )
    claim_graph_path.write_text(json.dumps(stored), encoding="utf-8")
    assert claim_graph_projection_parity()["extra_edges"] == 1
    refresh_claim_graph_projection()
    assert claim_graph_projection_parity()["extra_edges"] == 0

    claim["claim_text"] = "Changed graph claim"
    claim["updated_at"] = "2026-07-19T01:00:00+00:00"
    publish_claim()
    drift = claim_graph_projection_parity()
    assert drift["missing_nodes"] == 1
    assert drift["extra_nodes"] == 1

    refresh_claim_graph_projection()
    assert claim_graph_projection_parity()["missing_nodes"] == 0
    assert claim_graph_projection_parity()["extra_nodes"] == 0


def test_claim_graph_window_ignores_storage_timestamp_churn(isolated_memory):
    from vector_lake import db_store, governance_store

    db_store.init_db()
    claims = [
        {
            "claim_id": "claim_business_newer",
            "claim_text": "Newer business claim",
            "status": "Active",
            "updated_at": "2026-07-19T00:00:00+00:00",
            "source_ids": [],
            "evidence_ids": [],
            "subject_entity_ids": [],
        },
        {
            "claim_id": "claim_business_older",
            "claim_text": "Older business claim",
            "status": "Active",
            "updated_at": "2026-07-18T00:00:00+00:00",
            "source_ids": [],
            "evidence_ids": [],
            "subject_entity_ids": [],
        },
    ]
    for claim, page_key in zip(
        claims,
        ("Concept_Business-Newer", "Concept_Business-Older"),
    ):
        claim["locator"] = {"page_key": page_key}
    governance_store.apply_change_set(
        {
            "affected_pages": [
                "Concept_Business-Newer.md",
                "Concept_Business-Older.md",
            ],
            "proposed_entities": [],
            "proposed_claims": claims,
            "proposed_evidence": [],
            "proposed_source_updates": [],
            "proposed_edges": [],
        }
    )
    conn = db_store.get_connection()
    with db_store.transaction():
        conn.execute(
            "UPDATE claims SET updated_at = CASE claim_id "
            "WHEN 'claim_business_newer' THEN '2000-01-01T00:00:00+00:00' "
            "ELSE '2099-01-01T00:00:00+00:00' END"
        )

    before = governance_store.build_claim_graph_projection(limit_nodes=1)
    with db_store.transaction():
        conn.execute(
            "UPDATE claims SET updated_at = CASE claim_id "
            "WHEN 'claim_business_newer' THEN '2099-01-01T00:00:00+00:00' "
            "ELSE '2000-01-01T00:00:00+00:00' END"
        )
    after = governance_store.build_claim_graph_projection(limit_nodes=1)

    assert [node["id"] for node in before["nodes"]] == ["claim_business_newer"]
    assert [node["id"] for node in after["nodes"]] == ["claim_business_newer"]


def test_claim_graph_parity_does_not_wait_for_publish_lock(
    isolated_memory,
    monkeypatch,
):
    from vector_lake import governance_store
    from vector_lake.wiki_utils import get_claim_graph_path, get_index_path

    expected = {"nodes": [{"id": "claim"}], "edges": []}
    monkeypatch.setattr(
        governance_store,
        "build_claim_graph_projection",
        lambda: expected,
    )
    claim_graph_path = get_claim_graph_path()
    claim_graph_path.parent.mkdir(parents=True, exist_ok=True)
    claim_graph_path.write_text(json.dumps(expected), encoding="utf-8")

    publish_lock = FileLock(str(get_index_path()) + ".lock")
    publish_lock.acquire(timeout=0)
    started = time.monotonic()
    try:
        parity = claim_graph_projection_parity()
    finally:
        elapsed = time.monotonic() - started
        publish_lock.release()

    assert elapsed < 0.25
    assert parity["missing_nodes"] == 0
    assert parity["extra_nodes"] == 0


def test_projection_pair_publishes_shared_manifest_and_rotates_generation(
    isolated_memory,
):
    from vector_lake import db_store, indexer
    from vector_lake.wiki_utils import get_claim_graph_path, get_index_path

    db_store.init_db()
    indexer.generate_index()

    first_index = json.loads(get_index_path().read_text(encoding="utf-8"))
    first_graph = json.loads(get_claim_graph_path().read_text(encoding="utf-8"))
    first_generation = indexer.validate_projection_pair(first_index, first_graph)
    first_binding = indexer.projection_canonical_generation(
        first_index,
        first_graph,
    )
    assert first_index[indexer.PROJECTION_MANIFEST_KEY] == first_graph[
        indexer.PROJECTION_MANIFEST_KEY
    ]
    assert first_binding["status"] == "verified"
    assert first_binding["runtime_generations"] == (
        indexer.canonical_runtime_generation_snapshot()
    )

    indexer.refresh_claim_graph_projection()

    refreshed_index = json.loads(get_index_path().read_text(encoding="utf-8"))
    refreshed_graph = json.loads(
        get_claim_graph_path().read_text(encoding="utf-8")
    )
    refreshed_generation = indexer.validate_projection_pair(
        refreshed_index,
        refreshed_graph,
    )
    refreshed_binding = indexer.projection_canonical_generation(
        refreshed_index,
        refreshed_graph,
    )
    assert refreshed_generation != first_generation
    assert refreshed_binding == first_binding


def test_projection_pair_rejects_tampered_canonical_generation_token(
    isolated_memory,
):
    from vector_lake import db_store, indexer
    from vector_lake.wiki_utils import get_claim_graph_path, get_index_path

    db_store.init_db()
    indexer.generate_index()
    index_data = json.loads(get_index_path().read_text(encoding="utf-8"))
    claim_graph = json.loads(get_claim_graph_path().read_text(encoding="utf-8"))
    index_data[indexer.PROJECTION_MANIFEST_KEY]["canonical_generation"][
        "token"
    ] = "tampered"
    claim_graph[indexer.PROJECTION_MANIFEST_KEY]["canonical_generation"][
        "token"
    ] = "tampered"

    with pytest.raises(
        indexer.ProjectionPairContractError,
        match="token does not match",
    ):
        indexer.validate_projection_pair(index_data, claim_graph)


def test_full_projection_rebuild_rejects_moving_canonical_generation(
    isolated_memory,
    monkeypatch,
):
    from vector_lake import db_store, governance_store, indexer
    from vector_lake.wiki_utils import get_claim_graph_path, get_index_path

    db_store.init_db()
    real_builder = governance_store.build_claim_graph_projection

    def mutate_during_build():
        governance_store.upsert_entity(
            "entity_racing_projection",
            {
                "entity_id": "entity_racing_projection",
                "canonical_name": "Racing Projection",
                "page_key": "Concept_Racing-Projection",
            },
        )
        return real_builder()

    monkeypatch.setattr(
        governance_store,
        "build_claim_graph_projection",
        mutate_during_build,
    )

    with pytest.raises(
        indexer.ProjectionCanonicalGenerationChanged,
        match="generation changed",
    ):
        indexer.generate_index()

    assert not get_index_path().exists()
    assert not get_claim_graph_path().exists()


def test_incremental_projection_marks_unproven_generation_unverifiable(
    isolated_memory,
):
    from vector_lake import db_store, governance_store, indexer
    from vector_lake.wiki_utils import get_claim_graph_path, get_index_path

    db_store.init_db()
    indexer.generate_index()
    governance_store.upsert_entity(
        "entity_incremental_generation",
        {
            "entity_id": "entity_incremental_generation",
            "canonical_name": "Incremental Generation",
            "entity_type": "concept",
            "page_key": "Concept_Incremental-Generation",
        },
    )

    indexer.update_index_items(["Concept_Incremental-Generation.md"])

    index_data = json.loads(get_index_path().read_text(encoding="utf-8"))
    claim_graph = json.loads(get_claim_graph_path().read_text(encoding="utf-8"))
    binding = indexer.projection_canonical_generation(index_data, claim_graph)
    assert binding["status"] == "unverifiable"
    assert binding["reason"] == (
        "existing-index-generation-does-not-match-current-canonical-generation"
    )


def test_graph_reader_fails_closed_for_legacy_and_mismatched_projection_pairs(
    isolated_memory,
):
    from vector_lake import indexer
    from vector_lake.tool_graph import _read_projection_pair
    from vector_lake.wiki_utils import get_claim_graph_path, get_index_path

    index_path = get_index_path()
    claim_graph_path = get_claim_graph_path()
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(
        json.dumps({"nodes": {}, "weighted_edges": []}),
        encoding="utf-8",
    )
    claim_graph_path.write_text(
        json.dumps({"nodes": [], "edges": []}),
        encoding="utf-8",
    )

    with pytest.raises(
        indexer.ProjectionPairContractError,
        match="Legacy index/claim-graph projections",
    ):
        _read_projection_pair(str(index_path), str(claim_graph_path))

    index_manifest = {
        "contract": indexer.PROJECTION_CONTRACT,
        "version": indexer.PROJECTION_CONTRACT_VERSION,
        "generation": "generation-index",
        "published_at": "2026-07-27T00:00:00+00:00",
    }
    graph_manifest = {
        **index_manifest,
        "generation": "generation-graph",
    }
    index_path.write_text(
        json.dumps({"nodes": {}, indexer.PROJECTION_MANIFEST_KEY: index_manifest}),
        encoding="utf-8",
    )
    claim_graph_path.write_text(
        json.dumps(
            {"nodes": [], "edges": [], indexer.PROJECTION_MANIFEST_KEY: graph_manifest}
        ),
        encoding="utf-8",
    )

    with pytest.raises(
        indexer.ProjectionPairContractError,
        match="generations do not match",
    ):
        _read_projection_pair(str(index_path), str(claim_graph_path))

    claim_graph_path.write_text(
        json.dumps(
            {"nodes": [], "edges": [], indexer.PROJECTION_MANIFEST_KEY: index_manifest}
        ),
        encoding="utf-8",
    )
    with pytest.raises(
        indexer.ProjectionPairContractError,
        match="no canonical_generation",
    ):
        _read_projection_pair(
            str(index_path),
            str(claim_graph_path),
        )


def test_graph_reader_observes_shared_publish_lock(isolated_memory):
    from vector_lake import indexer
    from vector_lake.tool_graph import _read_projection_pair
    from vector_lake.wiki_utils import get_claim_graph_path, get_index_path

    index_path = get_index_path()
    claim_graph_path = get_claim_graph_path()
    index_path.parent.mkdir(parents=True, exist_ok=True)
    manifest = {
        "contract": indexer.PROJECTION_CONTRACT,
        "version": indexer.PROJECTION_CONTRACT_VERSION,
        "generation": "locked-generation",
        "published_at": "2026-07-27T00:00:00+00:00",
    }
    index_path.write_text(
        json.dumps({"nodes": {}, indexer.PROJECTION_MANIFEST_KEY: manifest}),
        encoding="utf-8",
    )
    claim_graph_path.write_text(
        json.dumps(
            {"nodes": [], "edges": [], indexer.PROJECTION_MANIFEST_KEY: manifest}
        ),
        encoding="utf-8",
    )

    publish_lock = FileLock(str(index_path) + ".lock")
    publish_lock.acquire(timeout=0)
    try:
        with pytest.raises(Timeout):
            _read_projection_pair(
                str(index_path),
                str(claim_graph_path),
                lock_timeout=0,
            )
    finally:
        publish_lock.release()
