import unittest
import json
import subprocess
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory
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
    _incremental_relevance_candidate_keys,
    _strip_legacy_embedded_payloads,
    _tokenize,
    get_pred_weight,
    is_graph_dirty,
    _write_index,
    claim_graph_projection_parity,
    refresh_claim_graph_projection,
    update_index_items,
)


def _projection_v2_commit_receipt(memory_dir: Path) -> tuple[bytes, int, dict]:
    """Capture the mutable v2 commit surfaces, excluding static locators."""
    from vector_lake import db_store

    marker = memory_dir / "wiki" / "projection_pair_manifest.json"
    return (
        marker.read_bytes(),
        marker.stat().st_mtime_ns,
        db_store.get_projection_runtime_v9(),
    )


class TestIndexer(unittest.TestCase):
    def test_incremental_relevance_frontier_excludes_unrelated_nodes(self):
        node_keys = ["Concept_Touched"] + [
            f"Concept_Unrelated-{index:05d}" for index in range(10000)
        ] + [
            "Concept_Direct",
            "Concept_Inbound",
            "Concept_Shared",
            "Concept_Source-Peer",
        ]
        all_nodes = {key: {} for key in node_keys}
        node_order = {key: position for position, key in enumerate(node_keys)}

        candidates = _incremental_relevance_candidate_keys(
            "Concept_Touched",
            {"Concept_Direct", "shared-link"},
            {"source-a"},
            all_nodes,
            {
                "Concept_Touched": {"Concept_Inbound"},
                "shared-link": {"Concept_Touched", "Concept_Shared"},
            },
            {"source-a": {"Concept_Touched", "Concept_Source-Peer"}},
            node_order,
        )

        self.assertEqual(
            candidates,
            [
                "Concept_Direct",
                "Concept_Inbound",
                "Concept_Shared",
                "Concept_Source-Peer",
            ],
        )

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
        rebuild.assert_called_once_with(
            invalidate_embedding_ids={
                f"Concept_Node-{index}" for index in range(251)
            }
        )

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

    def test_topology_worker_failure_falls_back_to_connected_components(self):
        index_data = {
            "nodes": {
                key: {"title": key, "decay_weight": 1.0}
                for key in ("Concept_A", "Concept_B", "Concept_C", "Concept_D")
            },
            "weighted_edges": [
                {"source": "Concept_A", "target": "Concept_B", "weight": 2.0},
                {"source": "Concept_C", "target": "Concept_D", "weight": 2.0},
            ],
            "graph_state": {"dirty": True, "reason": "test"},
        }

        with patch(
            "vector_lake.indexer._louvain_partition_in_subprocess",
            side_effect=subprocess.TimeoutExpired("worker", 5),
        ):
            _apply_graph_topology(index_data)

        communities = index_data["communities"]
        self.assertEqual(communities["Concept_A"], communities["Concept_B"])
        self.assertEqual(communities["Concept_C"], communities["Concept_D"])
        self.assertNotEqual(communities["Concept_A"], communities["Concept_C"])

    def test_topology_refresh_does_not_import_heavy_runtime_in_parent(self):
        repository_root = Path(__file__).resolve().parents[1]
        script = "\n".join(
            (
                "import json,sys",
                f"sys.path.insert(0, {str(repository_root)!r})",
                "from vector_lake.indexer import _louvain_partition_in_subprocess",
                "partition = _louvain_partition_in_subprocess(",
                "  ['Concept_A', 'Concept_B', 'Concept_C'],",
                "  [",
                "    {'source': 'Concept_A', 'target': 'Concept_B', 'weight': 2.0},",
                "    {'source': 'Concept_B', 'target': 'Concept_C', 'weight': 1.0},",
                "  ],",
                ")",
                "print(json.dumps({",
                "  'partition_keys': sorted(partition),",
                "  'heavy': [name for name in ('numpy', 'networkx', 'community') if name in sys.modules],",
                "}))",
            )
        )

        with TemporaryDirectory() as temp_dir:
            probe = subprocess.run(
                [sys.executable, "-c", script],
                cwd=temp_dir,
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=True,
                timeout=30,
            )

        result = json.loads(probe.stdout)
        self.assertEqual(
            result["partition_keys"],
            ["Concept_A", "Concept_B", "Concept_C"],
        )
        self.assertEqual(result["heavy"], [])
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


def test_noop_full_rebuild_performs_zero_fts_dml(isolated_memory):
    from vector_lake import db_store, governance_store, indexer

    db_store.init_db()
    for suffix in ("Alpha", "Beta"):
        governance_store.upsert_entity(
            f"entity_fts_{suffix.casefold()}",
            {
                "entity_id": f"entity_fts_{suffix.casefold()}",
                "page_key": f"Concept_FTS-{suffix}",
                "canonical_name": f"FTS {suffix}",
                "type": "concept",
                "raw_text": f"Stable body {suffix}.",
            },
        )

    indexer.generate_index()
    conn = db_store.get_connection()
    before_rows = conn.execute(
        "SELECT rowid, node_key, title, summary, text "
        "FROM wiki_search_index ORDER BY node_key"
    ).fetchall()
    before_changes = conn.total_changes
    before_commit = _projection_v2_commit_receipt(isolated_memory)

    indexer.generate_index()

    after_rows = conn.execute(
        "SELECT rowid, node_key, title, summary, text "
        "FROM wiki_search_index ORDER BY node_key"
    ).fetchall()
    assert [tuple(row) for row in after_rows] == [tuple(row) for row in before_rows]
    # Projection v2 generations are content-addressed. Identical roots bound to
    # the same canonical generation are an exact byte/mtime/DB no-op.
    assert conn.total_changes == before_changes
    assert _projection_v2_commit_receipt(isolated_memory) == before_commit


def test_full_rebuild_repairs_only_changed_stale_and_duplicate_fts_keys(
    isolated_memory,
):
    from vector_lake import db_store, governance_store, indexer

    db_store.init_db()
    entities = {
        "alpha": {
            "entity_id": "entity_delta_alpha",
            "page_key": "Concept_Delta-Alpha",
            "canonical_name": "Delta Alpha",
            "type": "concept",
            "raw_text": "Alpha body.",
        },
        "beta": {
            "entity_id": "entity_delta_beta",
            "page_key": "Concept_Delta-Beta",
            "canonical_name": "Delta Beta",
            "type": "concept",
            "raw_text": "Beta body.",
        },
    }
    for entity in entities.values():
        governance_store.upsert_entity(entity["entity_id"], entity)
    indexer.generate_index()
    conn = db_store.get_connection()
    alpha_rowid = conn.execute(
        "SELECT rowid FROM wiki_search_index WHERE node_key = ?",
        (entities["alpha"]["page_key"],),
    ).fetchone()[0]
    beta_rowid = conn.execute(
        "SELECT rowid FROM wiki_search_index WHERE node_key = ?",
        (entities["beta"]["page_key"],),
    ).fetchone()[0]
    with db_store.transaction():
        conn.execute(
            "INSERT INTO wiki_search_index (node_key, title, summary, text) "
            "VALUES (?, ?, ?, ?)",
            (entities["beta"]["page_key"], "duplicate", "", "duplicate"),
        )
        conn.execute(
            "INSERT INTO wiki_search_index (node_key, title, summary, text) "
            "VALUES ('Concept_Stale', 'stale', '', 'stale')"
        )

    changed = dict(entities["alpha"])
    changed["raw_text"] = "Alpha body changed."
    governance_store.upsert_entity(changed["entity_id"], changed)
    indexer.generate_index()

    assert conn.execute(
        "SELECT COUNT(*) FROM wiki_search_index WHERE node_key = 'Concept_Stale'"
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM wiki_search_index WHERE node_key = ?",
        (entities["beta"]["page_key"],),
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT rowid FROM wiki_search_index WHERE node_key = ?",
        (entities["alpha"]["page_key"],),
    ).fetchone()[0] != alpha_rowid
    assert conn.execute(
        "SELECT rowid FROM wiki_search_index WHERE node_key = ?",
        (entities["beta"]["page_key"],),
    ).fetchone()[0] != beta_rowid


def test_search_projection_replacement_rejects_duplicate_desired_key_before_dml(
    isolated_memory,
):
    from vector_lake import db_store

    db_store.init_db()
    conn = db_store.get_connection()
    with db_store.transaction():
        db_store.apply_search_projection_mutations(
            conn,
            upserts=[("Concept_Stable", "Stable", "", "stable")],
        )
    before = [
        tuple(row)
        for row in conn.execute(
            "SELECT rowid, node_key, title, summary, text FROM wiki_search_index"
        ).fetchall()
    ]

    with pytest.raises(ValueError, match="duplicate search projection key"):
        with db_store.transaction():
            db_store.apply_search_projection_mutations(
                conn,
                upserts=[
                    ("Concept_Duplicate", "One", "", "one"),
                    ("Concept_Duplicate", "Two", "", "two"),
                ],
                reset_search=True,
            )

    after = [
        tuple(row)
        for row in conn.execute(
            "SELECT rowid, node_key, title, summary, text FROM wiki_search_index"
        ).fetchall()
    ]
    assert after == before


def test_index_projection_allows_only_explicit_merge_alias_redirect(
    isolated_memory,
    monkeypatch,
):
    from vector_lake import db_store, indexer

    db_store.init_db()
    indexer.generate_index()
    snapshot = indexer.read_committed_index_snapshot(_mutable=True)
    snapshot["aliases"]["Source_Alpha-ab12cd34"] = "Source_Alpha"
    monkeypatch.setattr(
        indexer,
        "read_committed_index_snapshot",
        lambda *_args, **_kwargs: snapshot,
    )
    assert index_projection_matches_canonical(["Source_Alpha-ab12cd34.md"]) is False
    assert index_projection_matches_canonical(
        ["Source_Alpha-ab12cd34.md"],
        allowed_alias_redirects={"Source_Alpha-ab12cd34": "Source_Alpha"},
    ) is True


def test_index_projection_proves_system_pages_are_intentionally_absent(
    isolated_memory,
    monkeypatch,
):
    from vector_lake import db_store, indexer
    from vector_lake.wiki_utils import get_index_path

    db_store.init_db()
    indexer.generate_index()
    snapshot = indexer.read_committed_index_snapshot(_mutable=True)
    monkeypatch.setattr(
        indexer,
        "read_committed_index_snapshot",
        lambda *_args, **_kwargs: snapshot,
    )

    selected = ["System_Target.md", "System_Source.md"]
    assert index_projection_matches_canonical(
        selected,
        allowed_alias_redirects={"System_Source": "System_Target"},
    ) is True
    assert index_projection_matches_canonical(["system_source.MD"]) is True

    snapshot["nodes"]["Ｓｙｓｔｅｍ＿Ｓｏｕｒｃｅ"] = {}
    assert index_projection_matches_canonical(selected) is False
    snapshot["nodes"].clear()
    snapshot["nodes"]["System_Source"] = {}
    assert index_projection_matches_canonical(
        ["Ｓｙｓｔｅｍ＿Ｓｏｕｒｃｅ.md"]
    ) is False
    snapshot["nodes"].clear()
    snapshot["aliases"]["System_Source"] = "Concept_Other"
    assert index_projection_matches_canonical(selected) is False
    snapshot["aliases"].clear()
    snapshot["aliases"]["Concept_Other"] = "System_Source"
    assert index_projection_matches_canonical(selected) is False
    snapshot["aliases"].clear()
    snapshot["weighted_edges"].append(
        {"source": "System_Source", "target": "Concept_Other"}
    )
    assert index_projection_matches_canonical(selected) is False
    snapshot["weighted_edges"] = [
        {"source": "Concept_Other", "target": "System_Source"}
    ]
    assert index_projection_matches_canonical(selected) is False
    snapshot["weighted_edges"].clear()

    db_store.upsert_search_index("System_Source", "system", "system", "system")
    assert index_projection_matches_canonical(selected) is False
    with db_store.transaction() as connection:
        connection.execute(
            "DELETE FROM wiki_search_index WHERE node_key = 'System_Source'"
        )
    db_store.upsert_embedding("System_Source", [1.0] * 3072)
    assert index_projection_matches_canonical(selected) is False
    db_store.delete_embedding("System_Source")
    assert index_projection_matches_canonical(selected) is True

    get_index_path().unlink()
    assert index_projection_matches_canonical(selected) is False


def test_index_projection_requires_both_mixed_partitions_to_match(
    isolated_memory,
    monkeypatch,
):
    from vector_lake import db_store, governance_store, indexer
    db_store.init_db()
    entity = {
        "entity_id": "entity_mixed_projection",
        "page_key": "Concept_Mixed-Projection",
        "canonical_name": "Mixed Projection",
        "type": "concept",
        "raw_text": "Current canonical body.",
        "updated": "2026-08-09T00:00:00+00:00",
    }
    governance_store.upsert_entity(entity["entity_id"], entity)
    indexer.generate_index()
    selected = ["Concept_Mixed-Projection.md", "System_Source.md"]

    assert index_projection_matches_canonical(selected) is True
    snapshot = indexer.read_committed_index_snapshot(_mutable=True)
    monkeypatch.setattr(
        indexer,
        "read_committed_index_snapshot",
        lambda *_args, **_kwargs: snapshot,
    )
    snapshot["nodes"]["System_Source"] = {}
    assert index_projection_matches_canonical(
        selected,
        allowed_alias_redirects={"System_Source": "Concept_Mixed-Projection"},
    ) is False
    snapshot["nodes"].pop("System_Source")
    assert index_projection_matches_canonical(selected) is True

    changed = dict(entity)
    changed["raw_text"] = "Canonical drift after projection."
    governance_store.upsert_entity(entity["entity_id"], changed)
    assert index_projection_matches_canonical(selected) is False


def test_automatic_merge_discovery_keeps_system_entities_excluded():
    from vector_lake.merge_analysis import analyze_entities

    entities = [
        {
            "entity_id": "system_candidate_a",
            "page_key": "System_Time-Series-Foundation-Models",
            "canonical_name": "Time Series Foundation Models",
            "type": "system",
            "status": "Active",
        },
        {
            "entity_id": "system_candidate_b",
            "page_key": "System_Time_Series_Foundation_Models",
            "canonical_name": "Time-Series Foundation Models",
            "type": "system",
            "status": "Active",
        },
    ]

    assert analyze_entities(entities, limit=None, versions={}) == []


def test_projection_pair_generation_ignores_soft_drift_and_blocks_entity_drift(
    isolated_memory,
):
    from vector_lake import db_store, governance_store, indexer

    db_store.init_db()
    indexer.generate_index()
    assert indexer.projection_pair_matches_current_generation() is True

    governance_store.upsert_governance_item(
        {
            "item_id": "gov_generation_drift",
            "type": "gap",
            "title": "Generation drift",
            "status": "pending",
        }
    )

    assert indexer.projection_pair_matches_current_generation() is True

    governance_store.upsert_entity(
        "entity_generation_drift",
        {
            "entity_id": "entity_generation_drift",
            "canonical_name": "Generation Drift",
            "entity_type": "concept",
            "page_key": "Concept_Generation-Drift",
        },
    )
    assert indexer.projection_pair_matches_current_generation() is False


def test_projection_sidecar_is_last_commit_marker_and_fast_matcher_skips_payloads(
    isolated_memory,
    monkeypatch,
):
    from vector_lake import db_store, indexer, projection_format_v2
    from vector_lake.projection_format_v2 import read_committed_sidecar
    from vector_lake.wiki_utils import get_projection_manifest_path

    db_store.init_db()
    published = []
    real_replace = projection_format_v2._write_durable_replace

    def record_replace(output_path, payload):
        published.append(Path(output_path).name)
        return real_replace(output_path, payload)

    monkeypatch.setattr(
        projection_format_v2,
        "_write_durable_replace",
        record_replace,
    )
    indexer.generate_index()

    assert published[-3:] == [
        "index.json",
        "claim_graph.json",
        "projection_pair_manifest.json",
    ]
    sidecar, _identity, runtime = read_committed_sidecar(
        isolated_memory / "wiki"
    )
    assert sidecar["contract"] == projection_format_v2.SIDECAR_CONTRACT
    assert sidecar["projection_generation"] == runtime["projection_generation"]
    assert sidecar["index_root_sha256"]
    assert sidecar["claim_graph_root_sha256"]
    assert get_projection_manifest_path().read_bytes() == (
        projection_format_v2.canonical_json_bytes(sidecar)
    )
    assert indexer.projection_pair_matches_current_generation() is True

    monkeypatch.setattr(
        projection_format_v2,
        "materialize_index",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("fast matcher must not materialize projection roots")
        ),
    )
    assert indexer.projection_pair_matches_current_generation() is True


def test_projection_sidecar_fails_closed_for_missing_corrupt_or_tampered_state(
    isolated_memory,
):
    from vector_lake import db_store, indexer
    from vector_lake.wiki_utils import (
        get_index_path,
        get_projection_manifest_path,
    )

    db_store.init_db()
    indexer.generate_index()
    sidecar_path = get_projection_manifest_path()
    sidecar_bytes = sidecar_path.read_bytes()

    sidecar_path.unlink()
    assert indexer.projection_pair_matches_current_generation() is False
    sidecar_path.write_bytes(b"{")
    assert indexer.projection_pair_matches_current_generation() is False
    sidecar_path.write_bytes(sidecar_bytes)
    assert indexer.projection_pair_matches_current_generation() is True

    index_path = get_index_path()
    payload = bytearray(index_path.read_bytes())
    payload[-1] = ord(" ") if payload[-1] != ord(" ") else ord("}")
    index_path.write_bytes(payload)
    assert indexer.projection_pair_matches_current_generation() is False


def test_committed_index_reader_rejects_sidecar_and_blocking_canonical_drift(
    isolated_memory,
):
    from vector_lake import db_store, governance_store, indexer
    from vector_lake.wiki_utils import get_projection_manifest_path

    db_store.init_db()
    indexer.generate_index()
    first = indexer.read_committed_index_snapshot()
    second = indexer.read_committed_index_snapshot()
    assert second is first

    sidecar_path = get_projection_manifest_path()
    sidecar_bytes = sidecar_path.read_bytes()
    sidecar_path.unlink()
    with pytest.raises(
        indexer.ProjectionPairContractError,
        match="sidecar_unreadable",
    ):
        indexer.read_committed_index_snapshot()
    sidecar_path.write_bytes(sidecar_bytes)

    governance_store.upsert_governance_item(
        {
            "item_id": "gov_committed_reader_drift",
            "type": "gap",
            "title": "Committed reader drift",
            "status": "pending",
        }
    )
    assert indexer.read_committed_index_snapshot() == first

    governance_store.upsert_entity(
        "entity_committed_reader_drift",
        {
            "entity_id": "entity_committed_reader_drift",
            "canonical_name": "Committed Reader Drift",
            "entity_type": "concept",
            "page_key": "Concept_Committed-Reader-Drift",
        },
    )
    with pytest.raises(
        indexer.ProjectionPairContractError,
        match="canonical_generation_stale",
    ):
        indexer.read_committed_index_snapshot()


@pytest.mark.parametrize(
    ("damage", "reason"),
    [("missing", "sidecar_unreadable"), ("corrupt", "sidecar_json_invalid")],
)
def test_incremental_writer_fails_closed_for_damaged_projection_sidecar(
    isolated_memory,
    monkeypatch,
    damage,
    reason,
):
    from vector_lake import db_store, governance_store, indexer
    from vector_lake.wiki_utils import get_projection_manifest_path

    db_store.init_db()
    governance_store.upsert_entity(
        "entity_damaged_incremental_pair",
        {
            "entity_id": "entity_damaged_incremental_pair",
            "canonical_name": "Damaged Incremental Pair",
            "entity_type": "concept",
            "page_key": "Concept_Damaged-Incremental-Pair",
        },
    )
    indexer.generate_index()
    sidecar_path = get_projection_manifest_path()
    sidecar_bytes = sidecar_path.read_bytes()
    if damage == "missing":
        sidecar_path.unlink()
    else:
        sidecar_path.write_bytes(b"{")

    rebuilds = []
    real_rebuild = indexer._generate_index_unlocked

    def track_rebuild(skip_embeddings=True, *, invalidate_embedding_ids=()):
        rebuilds.append(skip_embeddings)
        return real_rebuild(
            skip_embeddings=skip_embeddings,
            invalidate_embedding_ids=invalidate_embedding_ids,
        )

    monkeypatch.setattr(indexer, "_generate_index_unlocked", track_rebuild)
    with pytest.raises(indexer.ProjectionV2ContractError, match=reason):
        indexer.update_index_items(["Concept_Damaged-Incremental-Pair.md"])

    # A ready commit with a missing/corrupt marker is not reconstructed from
    # mutable canonical state by an incremental writer. It remains fail-closed.
    assert rebuilds == []
    assert indexer.projection_pair_matches_current_generation() is False
    sidecar_path.write_bytes(sidecar_bytes)
    committed = indexer.read_committed_index_snapshot()
    assert "Concept_Damaged-Incremental-Pair" in committed["nodes"]


def test_topology_writer_rebuilds_damaged_projection_sidecar(
    isolated_memory,
    monkeypatch,
):
    from vector_lake import db_store, indexer
    from vector_lake.wiki_utils import get_projection_manifest_path

    db_store.init_db()
    indexer.generate_index()
    get_projection_manifest_path().write_bytes(b"{")
    rebuilds = []
    real_rebuild = indexer.generate_index

    def track_rebuild(skip_embeddings=True):
        rebuilds.append(skip_embeddings)
        return real_rebuild(skip_embeddings=skip_embeddings)

    monkeypatch.setattr(indexer, "generate_index", track_rebuild)

    assert indexer.refresh_graph_topology_if_dirty() is True
    assert rebuilds == [True]
    indexer.read_committed_index_snapshot()
    assert indexer.projection_pair_matches_current_generation() is True


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
        lambda skip_embeddings=True, *, invalidate_embedding_ids=(): "rebuilt",
    )

    assert indexer.generate_index() == "rebuilt"
    assert seen == [(str(isolated_memory / "wiki" / "index.json") + ".lock", 15)]


def test_claim_graph_projection_has_independent_parity_and_refresh(
    isolated_memory,
    monkeypatch,
):
    from vector_lake import db_store, governance_store, indexer
    from vector_lake.projection_format_v2 import load_committed_claim_graph

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

    stored = load_committed_claim_graph(isolated_memory / "wiki")
    stored.setdefault("edges", []).append(
        {"source": "claim_graph_refresh", "target": "fake", "relation": "fake", "weight": 1.0}
    )
    with monkeypatch.context() as patcher:
        patcher.setattr(
            indexer,
            "_read_claim_graph_snapshot",
            lambda *_args, **_kwargs: stored,
        )
        assert claim_graph_projection_parity()["extra_edges"] == 1
    refresh_claim_graph_projection()
    assert claim_graph_projection_parity()["extra_edges"] == 0

    claim["claim_text"] = "Changed graph claim"
    claim["updated_at"] = "2026-07-19T01:00:00+00:00"
    publish_claim()
    with pytest.raises(
        indexer.ProjectionSnapshotChanged,
        match="canonical_generation_stale",
    ):
        claim_graph_projection_parity()

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
):
    from vector_lake import db_store, governance_store, indexer
    from vector_lake.wiki_utils import get_index_path

    db_store.init_db()
    governance_store.apply_change_set(
        {
            "affected_pages": ["Concept_Lock-Free-Parity.md"],
            "proposed_entities": [],
            "proposed_claims": [
                {
                    "claim_id": "claim_lock_free_parity",
                    "claim_text": "Parity reads do not take the publish lock.",
                    "status": "Active",
                    "source_ids": [],
                    "evidence_ids": [],
                    "subject_entity_ids": [],
                    "updated_at": "2026-08-28T00:00:00+00:00",
                    "locator": {"page_key": "Concept_Lock-Free-Parity"},
                }
            ],
            "proposed_evidence": [],
            "proposed_source_updates": [],
            "proposed_edges": [],
        }
    )
    indexer.generate_index()

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


def test_projection_pair_uses_shared_sidecar_and_rotates_for_changed_roots(
    isolated_memory,
):
    from vector_lake import db_store, governance_store, indexer
    from vector_lake.projection_format_v2 import (
        load_committed_claim_graph,
        read_committed_sidecar,
    )

    db_store.init_db()
    indexer.generate_index()

    first_index = indexer.read_committed_index_snapshot()
    first_graph = load_committed_claim_graph(isolated_memory / "wiki")
    first_sidecar, _identity, first_runtime = read_committed_sidecar(
        isolated_memory / "wiki"
    )
    first_commit = _projection_v2_commit_receipt(isolated_memory)
    assert first_sidecar["canonical_generation"] == (
        indexer.canonical_runtime_generation_snapshot()
    )
    assert first_sidecar["projection_generation"] == first_runtime[
        "projection_generation"
    ]

    governance_store.apply_change_set(
        {
            "affected_pages": ["Concept_Shared-Sidecar.md"],
            "proposed_entities": [],
            "proposed_claims": [
                {
                    "claim_id": "claim_shared_sidecar",
                    "claim_text": "Changed roots rotate the v2 generation.",
                    "status": "Active",
                    "source_ids": [],
                    "evidence_ids": [],
                    "subject_entity_ids": [],
                    "updated_at": "2026-08-28T00:00:00+00:00",
                    "locator": {"page_key": "Concept_Shared-Sidecar"},
                }
            ],
            "proposed_evidence": [],
            "proposed_source_updates": [],
            "proposed_edges": [],
        }
    )
    indexer.refresh_claim_graph_projection()

    refreshed_index = indexer.read_committed_index_snapshot()
    refreshed_graph = load_committed_claim_graph(isolated_memory / "wiki")
    refreshed_sidecar, _identity, refreshed_runtime = read_committed_sidecar(
        isolated_memory / "wiki"
    )
    assert set(refreshed_index) == set(first_index)
    assert refreshed_graph != first_graph
    assert any(
        node.get("id") == "claim_shared_sidecar"
        for node in refreshed_graph["nodes"]
    )
    assert refreshed_sidecar["projection_generation"] != first_sidecar[
        "projection_generation"
    ]
    assert refreshed_sidecar["canonical_generation"] == (
        indexer.canonical_runtime_generation_snapshot()
    )
    assert refreshed_runtime["projection_generation"] == refreshed_sidecar[
        "projection_generation"
    ]
    assert _projection_v2_commit_receipt(isolated_memory) != first_commit


def test_projection_pair_rejects_tampered_canonical_generation_sidecar(
    isolated_memory,
):
    from vector_lake import db_store, indexer
    from vector_lake.projection_format_v2 import (
        ProjectionV2ContractError,
        read_committed_sidecar,
    )
    from vector_lake.wiki_utils import get_projection_manifest_path

    db_store.init_db()
    indexer.generate_index()
    sidecar_path = get_projection_manifest_path()
    original = sidecar_path.read_bytes()
    sidecar, _identity, _runtime = read_committed_sidecar(
        isolated_memory / "wiki"
    )
    tampered = dict(sidecar)
    tampered["canonical_generation"] = dict(sidecar["canonical_generation"])
    tampered["canonical_generation"]["entities"] += 1
    sidecar_path.write_text(
        json.dumps(tampered, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    try:
        with pytest.raises(
            ProjectionV2ContractError,
            match="projection_generation_mismatch",
        ):
            read_committed_sidecar(isolated_memory / "wiki")
    finally:
        sidecar_path.write_bytes(original)


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


def test_full_projection_rebuild_rechecks_generation_after_write_lock(
    isolated_memory,
    monkeypatch,
):
    from vector_lake import db_store, indexer
    from vector_lake.wiki_utils import get_claim_graph_path, get_index_path

    db_store.init_db()
    stable = indexer.canonical_runtime_generation_snapshot()
    drifted = dict(stable)
    drifted["entities"] += 1
    snapshots = iter((stable, stable, drifted))
    monkeypatch.setattr(
        indexer,
        "canonical_runtime_generation_snapshot",
        lambda: next(snapshots),
    )

    with pytest.raises(
        indexer.ProjectionCanonicalGenerationChanged,
        match="while publishing projection v2",
    ):
        indexer.generate_index()

    assert not get_index_path().exists()
    assert not get_claim_graph_path().exists()


def test_full_projection_rebuild_rechecks_generation_after_projection_commit(
    isolated_memory,
    monkeypatch,
):
    from vector_lake import db_store, indexer
    from vector_lake.wiki_utils import get_claim_graph_path, get_index_path

    db_store.init_db()
    stable = indexer.canonical_runtime_generation_snapshot()
    drifted = dict(stable)
    drifted["entities"] += 1
    snapshots = iter((stable, stable, stable, drifted))
    monkeypatch.setattr(
        indexer,
        "canonical_runtime_generation_snapshot",
        lambda *_args, **_kwargs: next(snapshots),
    )

    with pytest.raises(
        indexer.ProjectionCanonicalGenerationChanged,
        match="while publishing projection v2",
    ):
        indexer.generate_index()

    assert not get_index_path().exists()
    assert not get_claim_graph_path().exists()


def test_claim_graph_refresh_rechecks_generation_before_publish(
    isolated_memory,
    monkeypatch,
):
    from vector_lake import db_store, indexer
    from vector_lake.wiki_utils import (
        get_claim_graph_path,
        get_index_path,
        get_projection_manifest_path,
    )

    db_store.init_db()
    indexer.generate_index()
    paths = (get_index_path(), get_claim_graph_path(), get_projection_manifest_path())
    before = {path: path.read_bytes() for path in paths}
    before_commit = _projection_v2_commit_receipt(isolated_memory)
    stable = indexer.canonical_runtime_generation_snapshot()
    drifted = dict(stable)
    drifted["claims"] += 1
    snapshots = iter((stable, stable, drifted))
    monkeypatch.setattr(
        indexer,
        "canonical_runtime_generation_snapshot",
        lambda *_args, **_kwargs: next(snapshots),
    )

    with pytest.raises(
        indexer.ProjectionCanonicalGenerationChanged,
        match="while publishing projection v2",
    ):
        indexer.refresh_claim_graph_projection()

    assert {path: path.read_bytes() for path in paths} == before
    assert _projection_v2_commit_receipt(isolated_memory) == before_commit


def test_claim_graph_refresh_rejects_generation_drift_during_computation(
    isolated_memory,
    monkeypatch,
):
    from vector_lake import db_store, indexer
    db_store.init_db()
    indexer.generate_index()
    before_commit = _projection_v2_commit_receipt(isolated_memory)
    stable = indexer.canonical_runtime_generation_snapshot()
    drifted = dict(stable)
    drifted["claims"] += 1
    snapshots = iter((stable, drifted))
    monkeypatch.setattr(
        indexer,
        "canonical_runtime_generation_snapshot",
        lambda *_args, **_kwargs: next(snapshots),
    )

    with pytest.raises(
        indexer.ProjectionCanonicalGenerationChanged,
        match="during claim-graph projection refresh",
    ):
        indexer.refresh_claim_graph_projection()

    assert _projection_v2_commit_receipt(isolated_memory) == before_commit


def test_incremental_projection_heavy_computation_runs_outside_write_transaction(
    isolated_memory,
    monkeypatch,
):
    from vector_lake import db_store, governance_store, indexer

    db_store.init_db()
    governance_store.upsert_entity(
        "entity_incremental_outside_transaction",
        {
            "entity_id": "entity_incremental_outside_transaction",
            "canonical_name": "Incremental Outside Transaction",
            "entity_type": "concept",
            "page_key": "Concept_Incremental-Outside-Transaction",
        },
    )
    indexer.generate_index()

    real_claim_builder = governance_store.build_claim_graph_projection

    def assert_outside_write_transaction():
        assert db_store.get_connection().in_transaction is False
        return real_claim_builder()

    monkeypatch.setattr(
        governance_store,
        "build_claim_graph_projection",
        assert_outside_write_transaction,
    )

    indexer.update_index_items(
        ["Concept_Incremental-Outside-Transaction.md"]
    )


def test_incremental_projection_does_not_materialize_full_search_rows(
    isolated_memory,
    monkeypatch,
):
    from vector_lake import db_store, governance_store, indexer

    db_store.init_db()
    governance_store.upsert_entity(
        "entity_incremental_streamed_digest",
        {
            "entity_id": "entity_incremental_streamed_digest",
            "canonical_name": "Incremental Streamed Digest",
            "entity_type": "concept",
            "page_key": "Concept_Incremental-Streamed-Digest",
        },
    )
    indexer.generate_index()
    monkeypatch.setattr(
        indexer,
        "_search_projection_upserts",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("incremental update must not materialize every FTS row")
        ),
    )

    indexer.update_index_items(["Concept_Incremental-Streamed-Digest.md"])

    assert db_store.get_search_projection_state()["status"] == "ready"


def test_incremental_projection_rechecks_generation_after_write_lock(
    isolated_memory,
    monkeypatch,
):
    from vector_lake import db_store, governance_store, indexer
    db_store.init_db()
    entity = {
        "entity_id": "entity_incremental_cas",
        "canonical_name": "Incremental CAS Before",
        "entity_type": "concept",
        "page_key": "Concept_Incremental-CAS",
    }
    governance_store.upsert_entity(entity["entity_id"], entity)
    indexer.generate_index()
    before_commit = _projection_v2_commit_receipt(isolated_memory)

    stable = indexer.canonical_runtime_generation_snapshot()
    drifted = dict(stable)
    drifted["entities"] += 1
    snapshots = iter((stable, stable, drifted))
    monkeypatch.setattr(
        indexer,
        "canonical_runtime_generation_snapshot",
        lambda *_args, **_kwargs: next(snapshots),
    )

    with pytest.raises(
        indexer.ProjectionCanonicalGenerationChanged,
        match="while publishing incremental projection v2",
    ):
        indexer.update_index_items(["Concept_Incremental-CAS.md"])

    assert _projection_v2_commit_receipt(isolated_memory) == before_commit


def test_incremental_projection_rechecks_generation_after_projection_commit(
    isolated_memory,
    monkeypatch,
):
    from vector_lake import db_store, governance_store, indexer
    db_store.init_db()
    entity = {
        "entity_id": "entity_incremental_publish_cas",
        "canonical_name": "Incremental Publish CAS Before",
        "entity_type": "concept",
        "page_key": "Concept_Incremental-Publish-CAS",
    }
    governance_store.upsert_entity(entity["entity_id"], entity)
    indexer.generate_index()
    before_commit = _projection_v2_commit_receipt(isolated_memory)
    stable = indexer.canonical_runtime_generation_snapshot()
    drifted = dict(stable)
    drifted["entities"] += 1
    snapshots = iter((stable, stable, stable, drifted))
    monkeypatch.setattr(
        indexer,
        "canonical_runtime_generation_snapshot",
        lambda *_args, **_kwargs: next(snapshots),
    )

    with pytest.raises(
        indexer.ProjectionCanonicalGenerationChanged,
        match="while publishing incremental projection v2",
    ):
        indexer.update_index_items(["Concept_Incremental-Publish-CAS.md"])

    assert _projection_v2_commit_receipt(isolated_memory) == before_commit


def test_topology_refresh_heavy_computation_runs_outside_write_transaction(
    isolated_memory,
    monkeypatch,
):
    from vector_lake import db_store, governance_store, indexer

    db_store.init_db()
    governance_store.upsert_entity(
        "entity_topology_outside_transaction",
        {
            "entity_id": "entity_topology_outside_transaction",
            "canonical_name": "Topology Outside Transaction",
            "entity_type": "concept",
            "page_key": "Concept_Topology-Outside-Transaction",
        },
    )
    indexer.generate_index()
    real_calculator = indexer._calculate_weighted_edges

    def assert_outside_write_transaction(index_data):
        assert db_store.get_connection().in_transaction is False
        return real_calculator(index_data)

    monkeypatch.setattr(
        indexer,
        "_calculate_weighted_edges",
        assert_outside_write_transaction,
    )

    assert indexer.refresh_graph_topology_if_dirty() is True


def test_topology_refresh_rechecks_generation_after_projection_commit(
    isolated_memory,
    monkeypatch,
):
    from vector_lake import db_store, indexer
    db_store.init_db()
    indexer.generate_index()
    before_commit = _projection_v2_commit_receipt(isolated_memory)
    stable = indexer.canonical_runtime_generation_snapshot()
    drifted = dict(stable)
    drifted["page_graph_edges"] += 1
    snapshots = iter((stable, stable, stable, drifted))
    monkeypatch.setattr(
        indexer,
        "canonical_runtime_generation_snapshot",
        lambda *_args, **_kwargs: next(snapshots),
    )

    with pytest.raises(
        indexer.ProjectionCanonicalGenerationChanged,
        match="while publishing topology projection v2",
    ):
        indexer.refresh_graph_topology_if_dirty()

    assert _projection_v2_commit_receipt(isolated_memory) == before_commit


def test_incremental_projection_rebuilds_stale_generation_as_verified(
    isolated_memory,
):
    from vector_lake import db_store, governance_store, indexer
    from vector_lake.projection_format_v2 import (
        load_committed_claim_graph,
        read_committed_sidecar,
    )

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

    index_data = indexer.read_committed_index_snapshot()
    claim_graph = load_committed_claim_graph(isolated_memory / "wiki")
    sidecar, _identity, runtime = read_committed_sidecar(
        isolated_memory / "wiki"
    )
    assert "Concept_Incremental-Generation" in index_data["nodes"]
    assert isinstance(claim_graph["nodes"], list)
    assert sidecar["canonical_generation"] == (
        indexer.canonical_runtime_generation_snapshot()
    )
    assert runtime["status"] == "ready"
    assert runtime["projection_generation"] == sidecar["projection_generation"]
    assert indexer.projection_pair_matches_current_generation() is True


def test_full_rebuild_aborts_when_one_canonical_entity_cannot_project(
    isolated_memory,
    monkeypatch,
):
    from vector_lake import db_store, governance_store, indexer
    from vector_lake.wiki_utils import (
        get_claim_graph_path,
        get_index_path,
        get_projection_manifest_path,
    )

    db_store.init_db()
    governance_store.upsert_entity(
        "entity_projection_failure",
        {
            "entity_id": "entity_projection_failure",
            "canonical_name": "Projection Failure",
            "entity_type": "concept",
            "page_key": "Concept_Projection-Failure",
        },
    )
    indexer.generate_index()
    paths = (
        get_index_path(),
        get_claim_graph_path(),
        get_projection_manifest_path(),
    )
    before = {path: path.read_bytes() for path in paths}
    real_project = indexer._entity_to_index_node

    def fail_one_entity(entity_data, entity_id=""):
        if entity_id == "entity_projection_failure":
            raise ValueError("injected entity projection failure")
        return real_project(entity_data, entity_id)

    monkeypatch.setattr(indexer, "_entity_to_index_node", fail_one_entity)

    with pytest.raises(RuntimeError, match="Canonical entity projection failed"):
        indexer.generate_index(
            invalidate_embedding_ids={"entity_projection_failure"}
        )

    assert {path: path.read_bytes() for path in paths} == before
    assert indexer.projection_pair_matches_current_generation() is True


def test_full_rebuild_aborts_when_canonical_graph_edge_query_fails(
    isolated_memory,
    monkeypatch,
):
    from vector_lake import db_store, indexer
    from vector_lake.wiki_utils import (
        get_claim_graph_path,
        get_index_path,
        get_projection_manifest_path,
    )

    db_store.init_db()
    indexer.generate_index()
    paths = (
        get_index_path(),
        get_claim_graph_path(),
        get_projection_manifest_path(),
    )
    before = {path: path.read_bytes() for path in paths}
    real_connection = db_store.get_connection()

    class FailingCursor:
        def __init__(self, cursor):
            self._cursor = cursor

        def execute(self, sql, *args, **kwargs):
            if "FROM claim_graph_edges" in sql:
                raise RuntimeError("injected graph-edge query failure")
            return self._cursor.execute(sql, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._cursor, name)

    class ConnectionProxy:
        def cursor(self):
            return FailingCursor(real_connection.cursor())

        def __getattr__(self, name):
            return getattr(real_connection, name)

    monkeypatch.setattr(db_store, "get_connection", lambda: ConnectionProxy())

    with pytest.raises(RuntimeError, match="Canonical graph-edge query failed"):
        indexer.generate_index(invalidate_embedding_ids={"force-rebuild"})

    assert {path: path.read_bytes() for path in paths} == before
    assert indexer.projection_pair_matches_current_generation() is True


def test_incremental_writer_aborts_when_canonical_graph_edge_query_fails(
    isolated_memory,
    monkeypatch,
):
    from vector_lake import db_store, governance_store, indexer
    from vector_lake.wiki_utils import (
        get_claim_graph_path,
        get_index_path,
        get_projection_manifest_path,
    )

    db_store.init_db()
    governance_store.upsert_entity(
        "entity_incremental_edge_failure",
        {
            "entity_id": "entity_incremental_edge_failure",
            "canonical_name": "Incremental Edge Failure",
            "entity_type": "concept",
            "page_key": "Concept_Incremental-Edge-Failure",
        },
    )
    indexer.generate_index()
    paths = (
        get_index_path(),
        get_claim_graph_path(),
        get_projection_manifest_path(),
    )
    before = {path: path.read_bytes() for path in paths}
    real_connection = db_store.get_connection()

    class ConnectionProxy:
        def execute(self, sql, *args, **kwargs):
            if "FROM claim_graph_edges" in sql:
                raise RuntimeError("injected incremental edge-query failure")
            return real_connection.execute(sql, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(real_connection, name)

    proxy = ConnectionProxy()
    monkeypatch.setattr(db_store, "get_connection", lambda: proxy)

    with pytest.raises(RuntimeError, match="Canonical graph-edge query failed"):
        indexer.update_index_items(["Concept_Incremental-Edge-Failure.md"])

    assert {path: path.read_bytes() for path in paths} == before
    assert indexer.projection_pair_matches_current_generation() is True


def test_graph_reader_fails_closed_for_all_legacy_projection_pairs(
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
        match="Projection v2 static locators are required",
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
        match="Projection v2 static locators are required",
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
        match="Projection v2 static locators are required",
    ):
        _read_projection_pair(
            str(index_path),
            str(claim_graph_path),
        )


def test_graph_reader_observes_shared_publish_lock(isolated_memory):
    from vector_lake import db_store, indexer
    from vector_lake.tool_graph import _read_projection_pair
    from vector_lake.wiki_utils import get_claim_graph_path, get_index_path

    db_store.init_db()
    indexer.generate_index()
    index_path = get_index_path()
    claim_graph_path = get_claim_graph_path()

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


def test_graph_reader_requires_sidecar_commit_marker_and_artifact_digest(
    isolated_memory,
):
    from vector_lake import db_store, indexer
    from vector_lake.projection_format_v2 import (
        load_committed_claim_graph,
        read_committed_sidecar,
    )
    from vector_lake.projection_store_v2 import (
        ProjectionStoreError,
        ProjectionStoreV2,
    )
    from vector_lake.tool_graph import _read_projection_pair
    from vector_lake.wiki_utils import (
        get_claim_graph_path,
        get_index_path,
        get_projection_manifest_path,
    )

    db_store.init_db()
    indexer.generate_index()
    index_path = get_index_path()
    claim_graph_path = get_claim_graph_path()
    sidecar_path = get_projection_manifest_path()

    sidecar_content = sidecar_path.read_text(encoding="utf-8")
    sidecar_path.unlink()
    with pytest.raises(
        indexer.ProjectionPairContractError,
        match="sidecar is missing or unreadable",
    ):
        _read_projection_pair(str(index_path), str(claim_graph_path))

    sidecar_path.write_text(sidecar_content, encoding="utf-8")
    sidecar, _identity, _runtime = read_committed_sidecar(
        isolated_memory / "wiki"
    )
    store = ProjectionStoreV2(isolated_memory / "wiki")
    claim_root = store.object_path(sidecar["claim_graph_root_sha256"])
    claim_root.write_bytes(claim_root.read_bytes() + b" ")
    with pytest.raises(ProjectionStoreError, match="hash_mismatch"):
        load_committed_claim_graph(isolated_memory / "wiki")


def test_graph_reader_ignores_soft_drift_and_rebuilds_blocking_drift(
    isolated_memory,
):
    from vector_lake import db_store, governance_store, indexer
    from vector_lake.tool_graph import _read_projection_pair
    from vector_lake.wiki_utils import get_claim_graph_path, get_index_path

    db_store.init_db()
    indexer.generate_index()
    index_path = get_index_path()
    claim_graph_path = get_claim_graph_path()
    _read_projection_pair(str(index_path), str(claim_graph_path))

    governance_store.upsert_governance_item(
        {
            "item_id": "gov_graph_reader_stale_generation",
            "type": "gap",
            "title": "Graph reader stale generation",
            "status": "pending",
        }
    )
    _read_projection_pair(str(index_path), str(claim_graph_path))

    governance_store.upsert_entity(
        "entity_graph_reader_stale_generation",
        {
            "entity_id": "entity_graph_reader_stale_generation",
            "canonical_name": "Graph Reader Stale Generation",
            "entity_type": "concept",
            "page_key": "Concept_Graph-Reader-Stale-Generation",
        },
    )
    with pytest.raises(
        indexer.ProjectionPairContractError,
        match="binding is stale",
    ):
        _read_projection_pair(str(index_path), str(claim_graph_path))

    indexer.refresh_claim_graph_projection()
    _read_projection_pair(str(index_path), str(claim_graph_path))


def test_projection_v2_sidecar_publish_failure_is_fail_closed_and_recoverable(
    isolated_memory,
    monkeypatch,
):
    from vector_lake import db_store, governance_store, indexer, projection_format_v2
    from vector_lake.projection_format_v2 import recover_pending_publish
    from vector_lake.tool_graph import _read_projection_pair
    from vector_lake.wiki_utils import (
        get_claim_graph_path,
        get_index_path,
        get_projection_manifest_path,
    )

    db_store.init_db()
    indexer.generate_index()
    index_path = get_index_path()
    claim_graph_path = get_claim_graph_path()
    before_locator_bytes = {
        index_path: index_path.read_bytes(),
        claim_graph_path: claim_graph_path.read_bytes(),
    }
    governance_store.upsert_entity(
        "entity_publish_failure",
        {
            "entity_id": "entity_publish_failure",
            "canonical_name": "Publish Failure",
            "entity_type": "concept",
            "page_key": "Concept_Publish-Failure",
        },
    )
    real_replace = projection_format_v2._write_durable_replace

    def fail_sidecar_replace(output_path, payload):
        if Path(output_path).name == "projection_pair_manifest.json":
            raise OSError("injected projection sidecar publish failure")
        return real_replace(output_path, payload)

    monkeypatch.setattr(
        projection_format_v2,
        "_write_durable_replace",
        fail_sidecar_replace,
    )
    with pytest.raises(OSError, match="injected projection sidecar publish failure"):
        indexer.generate_index()

    assert {
        index_path: index_path.read_bytes(),
        claim_graph_path: claim_graph_path.read_bytes(),
    } == before_locator_bytes
    assert db_store.get_projection_runtime_v9()["status"] == "publish_pending"
    with pytest.raises(indexer.ProjectionPairContractError, match="not_ready"):
        _read_projection_pair(str(index_path), str(claim_graph_path))

    monkeypatch.setattr(
        projection_format_v2,
        "_write_durable_replace",
        real_replace,
    )
    assert recover_pending_publish(isolated_memory / "wiki") is True
    _read_projection_pair(str(index_path), str(claim_graph_path))
    committed = indexer.read_committed_index_snapshot()
    assert "Concept_Publish-Failure" in committed["nodes"]
    assert not list(index_path.parent.glob("*.tmp*"))
    assert get_projection_manifest_path().is_file()
