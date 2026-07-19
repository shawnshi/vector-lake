import unittest
import json
from unittest.mock import patch
from vector_lake.indexer import (
    _bounded_node_edge_candidates,
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
    from vector_lake import db_store

    db_store.init_db()
    claim = {
        "claim_id": "claim_graph_refresh",
        "claim_text": "Original graph claim",
        "status": "Active",
        "source_ids": [],
        "evidence_ids": [],
        "subject_entity_ids": [],
        "updated_at": "2026-07-19T00:00:00+00:00",
    }
    conn = db_store.get_connection()
    with db_store.transaction():
        conn.execute(
            "INSERT INTO claims (claim_id, claim_text, status, data_json, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (claim["claim_id"], claim["claim_text"], claim["status"], json.dumps(claim), claim["updated_at"]),
        )

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
    with db_store.transaction():
        conn.execute(
            "UPDATE claims SET claim_text = ?, data_json = ?, updated_at = ? WHERE claim_id = ?",
            (claim["claim_text"], json.dumps(claim), "2026-07-19T01:00:00+00:00", claim["claim_id"]),
        )
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
    conn = db_store.get_connection()
    with db_store.transaction():
        for claim, storage_time in zip(
            claims,
            ("2000-01-01T00:00:00+00:00", "2099-01-01T00:00:00+00:00"),
        ):
            conn.execute(
                "INSERT INTO claims (claim_id, claim_text, status, data_json, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (claim["claim_id"], claim["claim_text"], claim["status"], json.dumps(claim), storage_time),
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
