import unittest
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

    def test_full_date_timeline_metadata_preserves_claim_text_and_identity(self):
        fm = {
            "id": "concept_timeline",
            "title": "Timeline Test",
            "type": "concept",
            "domain": "General",
            "status": "Active",
            "epistemic-status": "seed",
            "categories": ["Uncategorized"],
            "updated": "2026-09-09",
            "temporal_anchor": "2026-09",
            "sources": [],
        }
        text = "[2026-08-24] [Observation] CMS changed [scope] only."
        result = extract_page_objects(
            "Concept_Timeline.md",
            fm,
            f"## 1. 编译事实\n\nTimeline metadata fixture.\n\n"
            f"## 2. 证据时间线\n\n{text}",
        )
        claim = next(item for item in result["claims"] if item["claim_type"] == "timeline-event")

        self.assertEqual(claim["claim_text"], text)
        self.assertEqual(claim["claim_id"], _stable_id("claim", f"Concept_Timeline:{text}"))
        self.assertEqual(claim["event_date"], "2026-08-24")
        self.assertEqual(claim["temporal_anchor"], "2026-08-24")
        self.assertEqual(claim["event_tag"], "Observation")

    def test_legacy_temporal_cleaning_preserves_canonical_text_and_identity(self):
        fm = {
            "id": "concept_legacy_timeline",
            "title": "Legacy Timeline",
            "type": "concept",
            "domain": "General",
            "status": "Active",
            "epistemic-status": "seed",
            "categories": ["Uncategorized"],
            "updated": "2026-09-09",
            "sources": ["raw/research/timeline-primary.md"],
        }
        cases = [
            ("[2026] [Pivot] Legacy change.", "[Pivot] Legacy change.", "2026", "Pivot"),
            ("[2026-09] [Pivot] Legacy change.", "[Pivot] Legacy change.", "2026-09", "Pivot"),
            ("[2026-Q2] [Pivot] Legacy change.", "[Pivot] Legacy change.", "2026-Q2", "Pivot"),
            ("[2026-H1] [Pivot] Legacy change.", "[Pivot] Legacy change.", "2026-H1", "Pivot"),
            ("[2026] Legacy change.", "Legacy change.", "2026", None),
            ("[2026-09] Legacy change.", "Legacy change.", "2026-09", None),
            ("[2026-Q2] Legacy change.", "Legacy change.", "2026-Q2", None),
            ("[2026-H1] Legacy change.", "Legacy change.", "2026-H1", None),
            ("[2026-08-24] [Pivot] Legacy change.", "[2026-08-24] [Pivot] Legacy change.", "2026-08-24", "Pivot"),
            ("[2026-08-24] Legacy change.", "[2026-08-24] Legacy change.", "2026-08-24", None),
        ]
        for raw, canonical, event_date, event_tag in cases:
            with self.subTest(raw=raw):
                # Baseline source-marker removal retains its preceding space;
                # that space also participates in canonical identity.
                canonical += " "
                result = extract_page_objects(
                    "Concept_Legacy_Timeline.md",
                    fm,
                    "## 1. 编译事实\n\nLegacy timeline fixture.\n\n"
                    f"## 2. 证据时间线\n\n{raw} (Source: [[Source_Timeline_Inline]])",
                )
                claim = next(item for item in result["claims"] if item["claim_type"] == "timeline-event")
                self.assertEqual(claim["claim_text"], canonical)
                self.assertEqual(claim["claim_id"], _stable_id("claim", f"Concept_Legacy_Timeline:{canonical}"))
                self.assertEqual(claim["event_date"], event_date)
                self.assertEqual(claim["event_tag"], event_tag)
                refs = ["raw/research/timeline-primary.md", "Source_Timeline_Inline"]
                self.assertEqual(claim["inline_sources"], ["Source_Timeline_Inline"])
                self.assertEqual(claim["source_ids"], [_stable_id("source", ref) for ref in refs])
                self.assertEqual(
                    set(claim["evidence_ids"]),
                    {_stable_id("evidence", f"Concept_Legacy_Timeline:{ref}:{canonical}") for ref in refs},
                )
                self.assertEqual({source["raw_ref"] for source in result["sources"]}, set(refs))

    def test_extract_page_objects_basic(self):
        fm = {
            "title": "Test Concept",
            "type": "concept",
            "id": "concept_123",
            "domain": "General",
            "status": "Active",
            "epistemic-status": "seed",
            "categories": ["Uncategorized"],
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
        self.assertEqual(result["entities"][0]["categories"], ["Uncategorized"])
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
            "categories": ["Uncategorized"],
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

    def test_entity_only_projection_matches_full_entity_without_claim_parsing(self):
        fm = {
            "id": "concept_entity_only",
            "title": "Entity Only",
            "type": "concept",
            "domain": "General",
            "status": "Active",
            "epistemic-status": "seed",
            "categories": ["Uncategorized"],
            "created": "2026-07-21T00:00:00+00:00",
            "updated": "2026-07-21T00:00:00+00:00",
            "sources": [],
        }
        body = """## 1. 编译事实
This is a claim with [is-a:: [[Category]]].

## 2. 证据时间线
"""

        full = extract_page_objects("Concept_Entity-Only.md", fm, body)
        entity_only = extract_page_objects(
            "Concept_Entity-Only.md",
            fm,
            body,
            entity_only=True,
        )

        self.assertEqual(entity_only["entities"], full["entities"])
        self.assertEqual(
            [(edge["relation"], edge["target_id"]) for edge in entity_only["edges"]],
            [(edge["relation"], edge["target_id"]) for edge in full["edges"]],
        )
        self.assertEqual(entity_only["claims"], [])
        self.assertGreater(len(full["claims"]), 0)

    def test_source_page_is_a_canonical_entity(self):
        fm = {
            "id": "source_123",
            "title": "Primary Source",
            "type": "source",
            "domain": "General",
            "status": "Active",
            "epistemic-status": "seed",
            "categories": ["Uncategorized"],
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
            "categories": ["Uncategorized"],
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

    def test_generated_template_and_migration_artifacts_are_not_claims(self):
        fm = {
            "id": "concept_template_filter",
            "title": "Template Filter",
            "type": "concept",
            "domain": "General",
            "status": "Active",
            "epistemic-status": "seed",
            "categories": ["Uncategorized"],
            "updated": "2026-07-21T00:00:00+00:00",
            "sources": [],
        }
        body = """## 1. 编译事实
This is an auto-generated stub page to prevent broken links from [[Concept_Missing]].

[2026-07-11] [Observation] Node auto-migrated to V11 schema.

Read mode: utf-8-ignore

(Semantic Summary) (To be generated by LLM during Review/Synthesis)

This is a durable business claim.

## 2. 证据时间线
"""

        result = extract_page_objects("Concept_Template_Filter.md", fm, body)

        self.assertEqual(
            [claim["claim_text"] for claim in result["claims"]],
            ["This is a durable business claim."],
        )

if __name__ == "__main__":
    unittest.main()


class TestMaintenanceStubSuppression(unittest.TestCase):
    """Stub notices must never be extractable as claims, in any known spelling."""

    STUB_VARIANTS = {
        "generated_stub": "This is an auto-generated stub page to prevent broken "
        "links from [[Concept_X]].",
        "generated_stub_truncated": "This is an auto-generated stub page to "
        "prevent broken links.",
        "generated_broken_link_stub": "Auto-generated stub to resolve broken link "
        "from Concept_Index-Orphans.",
        "bullet_timeline": "- [2026-06-01] [Observation] This is an "
        "auto-generated stub page to prevent broken links from Product_Gemini.",
        "tagged_date": "- [2026-01-01] [Observation] [concept] 2026-06-01: This is "
        "an auto-generated stub page to prevent broken links from Concept_Agentic.",
    }

    def test_every_stub_spelling_is_classified(self):
        from vector_lake.claim_extractor import (
            classify_maintenance_only_text,
            classify_non_claim_text,
        )

        for name, text in self.STUB_VARIANTS.items():
            with self.subTest(variant=name):
                reason = classify_non_claim_text(
                    text, page_key="Concept_Test.md"
                ) or classify_maintenance_only_text(text, page_key="Concept_Test.md")
                self.assertIsNotNone(reason, f"{name} went unclassified")

    def test_stub_blocks_never_reach_extraction(self):
        from vector_lake.claim_extractor import _iter_blocks

        body = "\n".join(self.STUB_VARIANTS.values())
        body += "\n\nA real paragraph claim about hospital digital transformation.\n"
        blocks = _iter_blocks(body, page_key="Concept_Test.md")

        self.assertEqual(len(blocks), 1)
        self.assertIn("real paragraph claim", blocks[0]["text"])

    def test_genuine_timeline_claims_survive(self):
        from vector_lake.claim_extractor import _iter_blocks

        body = (
            "- [2026-06-02] [Observation] HIMSS 2026 会议确认医疗数字化正进入 "
            "Agentic AI 落地肉搏战。\n"
            "- Auto-generated stub to resolve broken link from Concept_X.\n"
        )
        blocks = _iter_blocks(body, page_key="Concept_Test.md")

        self.assertEqual(len(blocks), 1)
        self.assertIn("HIMSS 2026", blocks[0]["text"])

    def test_lint_treats_the_new_reasons_as_stub_debt(self):
        from vector_lake.tool_lint import _GENERATED_STUB_REASONS

        expected = {
            "generated_stub",
            "generated_stub_truncated",
            "generated_broken_link_stub",
            "generated_reshaped_stub",
            "generated_entity_stub",
        }
        self.assertTrue(expected.issubset(_GENERATED_STUB_REASONS))


class TestDuplicateBlockIdentity(unittest.TestCase):
    """A repeated sentence must yield one claim, not two records for one id."""

    FM = {
        "title": "Test Concept",
        "type": "concept",
        "id": "concept_dupe",
        "domain": "General",
        "status": "Active",
        "epistemic-status": "seed",
        "categories": ["Uncategorized"],
        "updated": "2026-07-13T00:00:00+00:00",
        "aliases": [],
        "sources": ["MEMORY/wiki/raw/Test.pdf"],
    }

    def test_repeated_bullet_yields_one_claim(self):
        body = """## 1. 编译事实
### 物理机制 (Mechanism)
- [[Source_Repeated-Member]]
### 演进关联 (Evolution)
- [[Source_Repeated-Member]]
## 2. 证据时间线
"""
        once = extract_page_objects("Concept_Dupe.md", dict(self.FM), body)
        claim_ids = [record["claim_id"] for record in once["claims"]]
        self.assertEqual(len(claim_ids), len(set(claim_ids)), "duplicate claim_id emitted")
        bullets = [
            record for record in once["claims"]
            if record["claim_type"] == "bullet-claim"
        ]
        self.assertEqual(len(bullets), 1)

    def test_distinct_sentences_still_both_extracted(self):
        body = """## 1. 编译事实
### 物理机制 (Mechanism)
- [[Source_First-Member]]
- [[Source_Second-Member]]
## 2. 证据时间线
"""
        result = extract_page_objects("Concept_Dupe.md", dict(self.FM), body)
        bullets = [
            record for record in result["claims"]
            if record["claim_type"] == "bullet-claim"
        ]
        self.assertEqual(len(bullets), 2)

    def test_claim_identity_is_shared_by_guard_and_builder(self):
        from vector_lake.claim_extractor import _claim_identity

        first = _claim_identity(
            page_key="P", cleaned_text="same", block_index=1, frontmatter={}
        )
        repeat = _claim_identity(
            page_key="P", cleaned_text="same", block_index=7, frontmatter={}
        )
        other = _claim_identity(
            page_key="P", cleaned_text="other", block_index=2, frontmatter={}
        )
        self.assertEqual(first, repeat)
        self.assertNotEqual(first, other)
