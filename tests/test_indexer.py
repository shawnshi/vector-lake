import unittest
from vector_lake.indexer import (
    _entity_to_index_node,
    _strip_legacy_embedded_payloads,
    _tokenize,
    get_pred_weight,
    is_graph_dirty,
)

class TestIndexer(unittest.TestCase):
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
