import unittest
import pytest
from datetime import datetime, timezone
from vector_lake.claim_extractor import extract_page_objects, _clean_claim_text, _stable_id

class TestClaimExtractor(unittest.TestCase):
    def test_clean_claim_text(self):
        raw = "This is a [[Target|Alias]] and [predicate:: [[Target]]] with (Source: [[WikiPage]])."
        cleaned = _clean_claim_text(raw)
        self.assertEqual(cleaned, "This is a Alias and Target with .")

        # Test legacy links without aliases
        raw_legacy = "Check [[DirectLink]] here."
        self.assertEqual(_clean_claim_text(raw_legacy), "Check DirectLink here.")

        # Test multiple inline sources
        raw_multi_source = "Fact A (Source: [[A]]) Fact B (Sources: [[B]], [[C]])."
        self.assertEqual(_clean_claim_text(raw_multi_source), "Fact A  Fact B .")

    def test_stable_id_algorithm(self):
        # Verify that _stable_id uses blake2b and produces 24 chars (12 bytes hex)
        id_val = _stable_id("test", "hello world")
        self.assertTrue(id_val.startswith("test_"))
        self.assertEqual(len(id_val.split("_")[1]), 24)

    def test_extract_page_objects_basic(self):
        fm = {
            "title": "Test Concept",
            "type": "concept",
            "id": "concept_123",
            "domain": "General",
            "status": "Active",
            "epistemic-status": "seed",
            "categories": ["Testing"],
            "updated": "2026-07-13T00:00:00+00:00",
            "aliases": ["Test Alias"],
            "sources": ["MEMORY/wiki/raw/Test.pdf"]
        }
        body = """## 1. 编译事实
### 物理机制 (Mechanism)
This is a paragraph claim.

- This is a bullet claim.

## 2. 证据时间线
"""
        result = extract_page_objects("Concept_Test.md", fm, body)

        self.assertEqual(result["page_key"], "Concept_Test")
        self.assertEqual(result["page_type"], "concept")
        self.assertEqual(len(result["entities"]), 1)
        self.assertEqual(result["entities"][0]["canonical_name"], "Test Concept")
        self.assertEqual(result["entities"][0]["title"], "Test Concept")
        self.assertEqual(result["entities"][0]["type"], "concept")
        self.assertEqual(result["entities"][0]["categories"], ["Testing"])
        self.assertEqual(result["entities"][0]["sources"], ["wiki/raw/Test.pdf"])
        self.assertIn("This is a paragraph claim.", result["entities"][0]["raw_text"])

        claims = result["claims"]
        self.assertTrue(len(claims) >= 2)

        claim_texts = [c["claim_text"] for c in claims]
        self.assertIn("This is a paragraph claim.", claim_texts)
        self.assertIn("This is a bullet claim.", claim_texts)

        # Check that edges are extracted correctly (empty in this case)
        self.assertEqual(len(result["edges"]), 0)

    def test_extract_page_objects_edges(self):
        fm = {
            "id": "2024_0002",
            "title": "Edge Test",
            "type": "concept",
            "domain": "Medical_IT",
            "status": "Active",
            "epistemic-status": "seed",
            "categories": [],
            "updated": "2024-01-01",
            "sources": []
        }
        body = """## 1. 编译事实
### 物理机制 (Mechanism)
This page mentions OtherPage and defines [is-a:: [[Category]]].

## 2. 证据时间线
"""
        result = extract_page_objects("Concept_EdgeTest.md", fm, body)
        edges = result["edges"]

        self.assertEqual(len(edges), 1)

        is_a_edge = next((e for e in edges if e["relation"] == "is-a"), None)
        self.assertIsNotNone(is_a_edge)
        self.assertEqual(is_a_edge["target_id"], "Category")

    def test_source_page_is_a_canonical_entity(self):
        fm = {
            "id": "source_123",
            "title": "Primary Source",
            "type": "source",
            "domain": "General",
            "status": "Active",
            "epistemic-status": "seed",
            "categories": ["Source"],
            "updated": "2026-07-13T00:00:00+00:00",
            "sources": ["raw/primary.pdf"],
        }
        result = extract_page_objects("Source_Primary.md", fm, "Primary source content.")
        self.assertEqual(len(result["entities"]), 1)
        self.assertEqual(result["entities"][0]["page_key"], "Source_Primary")
        self.assertEqual(result["entities"][0]["type"], "source")

    def test_system_directives_and_footnotes_are_not_claims(self):
        fm = {
            "id": "concept_filter",
            "title": "Claim Filter",
            "type": "concept",
            "domain": "General",
            "status": "Active",
            "epistemic-status": "seed",
            "categories": [],
            "updated": "2026-07-18T00:00:00+00:00",
            "sources": [],
        }
        body = """## 1. 编译事实
[System Directive: This section represents the latest consensus.]

This is a real claim.[^1]

[^1]: Source_Primary, supporting citation.

## 2. 证据时间线
"""

        result = extract_page_objects("Concept_Filter.md", fm, body)
        claim_texts = [claim["claim_text"] for claim in result["claims"]]

        self.assertEqual(claim_texts, ["This is a real claim.[^1]"])

if __name__ == "__main__":
    unittest.main()
