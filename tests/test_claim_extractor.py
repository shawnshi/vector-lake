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

    def test_extract_page_objects_basic(self):
        fm = {
            "title": "Test Concept",
            "type": "concept",
            "id": "concept_123",
            "domain": "General",
            "status": "Active",
            "epistemic-status": "seed",
            "categories": ["System_Architecture"],
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
        self.assertEqual(result["entities"][0]["categories"], ["System_Architecture"])
        self.assertEqual(result["entities"][0]["sources"], ["wiki/raw/Test.pdf"])
        self.assertIn("This is a paragraph claim.", result["entities"][0]["raw_text"])

        claims = result["claims"]
        self.assertTrue(len(claims) >= 2)

        claim_texts = [c["claim_text"] for c in claims]
        self.assertIn("This is a paragraph claim.", claim_texts)
        self.assertIn("This is a bullet claim.", claim_texts)

        # Check that edges are extracted correctly (empty in this case)
        self.assertEqual(len(result["edges"]), 0)

    def test_system_directive_blocks_are_not_claims(self):
        """A reader instruction is not a claim, even under a claim-heavy heading.

        ``tool_query`` and the lint stub builder write ``[System Directive: ...]``
        inside the compiled-truth and timeline sections.  The heading rule turned
        them into claims, and because the timeline projection id is
        content-addressed and the directive is rewritten on every reshape, each
        reshape orphaned the previous ``timeline_events`` row.
        """
        fm = {
            "title": "Test Directive",
            "type": "concept",
            "id": "concept_directive",
            "domain": "General",
            "status": "Active",
            "epistemic-status": "seed",
            "categories": ["System_Architecture"],
            "updated": "2026-09-17T00:00:00+00:00",
            "sources": [],
        }
        body = """## 1. 编译事实
*[System Directive: This section represents the LATEST consensus.]*

A real compiled-truth sentence.

## 2. 证据时间线
*[System Directive: This is the immutable event ledger.]*

- [2026-09-17] [Observation] A real event.
"""
        result = extract_page_objects("Concept_Directive.md", fm, body)

        texts = [c["claim_text"] for c in result["claims"]]
        self.assertFalse([t for t in texts if "System Directive" in t], texts)
        self.assertIn("A real compiled-truth sentence.", texts)
        self.assertIn("[2026-09-17] [Observation] A real event.", texts)

        timeline = [c for c in result["claims"] if c.get("claim_type") == "timeline-event"]
        self.assertEqual(len(timeline), 1)
        self.assertIn("A real event.", timeline[0]["claim_text"])

    def test_extract_page_objects_edges(self):
        fm = {
            "id": "2024_0002",
            "title": "Edge Test",
            "type": "concept",
            "domain": "Medical_IT",
            "status": "Active",
            "epistemic-status": "seed",
            "categories": ["System_Architecture"],
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
            "categories": ["System_Architecture"],
            "updated": "2026-07-13T00:00:00+00:00",
            "sources": ["raw/primary.pdf"],
        }
        result = extract_page_objects("Source_Primary.md", fm, "Primary source content.")
        self.assertEqual(len(result["entities"]), 1)
        self.assertEqual(result["entities"][0]["page_key"], "Source_Primary")
        self.assertEqual(result["entities"][0]["type"], "source")

    def test_the_ledger_date_prefix_becomes_a_temporal_anchor(self):
        """The ``[YYYY-MM-DD]`` form the format mandates used to parse to nothing.

        ``_parse_temporal``'s pattern accepted only ``[2026-07]``/``[2026-Q1]`` (its
        ``[H|Q]`` is a character class, not an alternation), so 7246 of 9974 live timeline
        claims carried no anchor and every reader had to re-parse the date out of free
        text.  The prefix must still stay *in* the text: it is an input to ``claim_id`` and
        ``evidence_id``, so stripping it would re-mint the identity of every dated entry.
        """
        fm = _timeline_frontmatter("concept_anchor")
        body = """## 1. 编译事实

A compiled sentence.

## 2. 证据时间线

- [2026-09-17] [Observation] A dated event.
"""
        result = extract_page_objects("Concept_Anchor.md", fm, body)

        timeline = [c for c in result["claims"] if c.get("claim_type") == "timeline-event"]
        self.assertEqual(len(timeline), 1)
        self.assertEqual(timeline[0]["temporal_anchor"], "2026-09-17")
        self.assertEqual(timeline[0]["claim_text"], "[2026-09-17] [Observation] A dated event.")

    def test_an_evidence_boundary_section_is_not_an_event_ledger(self):
        """A bare ``证据`` used to qualify a block as a ledger entry.

        On the live corpus that typed 1595 claims from headings like ``证据边界`` as events;
        99% of them carry no date, so the projection then dated them by ingestion time.
        """
        fm = _timeline_frontmatter("concept_boundary")
        body = """## 1. 编译事实

A compiled sentence.

## 2. 证据时间线

- [2026-09-17] [Observation] A real event.

## 3. 证据边界

本页未使用上述映射范围之外的材料。
"""
        result = extract_page_objects("Concept_Boundary.md", fm, body)

        timeline = [c for c in result["claims"] if c.get("claim_type") == "timeline-event"]
        self.assertEqual([c["claim_text"] for c in timeline], ["[2026-09-17] [Observation] A real event."])
        # The boundary sentence is still a claim, it is just not an event.
        self.assertIn("本页未使用上述映射范围之外的材料。", [c["claim_text"] for c in result["claims"]])

    def test_the_ledger_caption_is_markup_and_the_scope_sentence_is_not_an_event(self):
        """The template's own text reaches the ledger as an undated "event" otherwise.

        Live corpus: 199 claims whose whole text is ``(Timeline - EVENT STORE)`` -- markup,
        dropped -- and 345 carrying the ingest template's scope sentence, which stays a claim
        because it says how the page was built, but is not an event.
        """
        fm = _timeline_frontmatter("concept_caption")
        body = """## 1. 编译事实

A compiled sentence.

## 2. 证据时间线

(Timeline - EVENT STORE)

本页只记录证据中明确出现的定义、机制、适用范围或限制。

- [2026-09-17] [Observation] A real event.
"""
        result = extract_page_objects("Concept_Caption.md", fm, body)

        texts = [c["claim_text"] for c in result["claims"]]
        events = [c["claim_text"] for c in result["claims"] if c.get("claim_type") == "timeline-event"]
        self.assertEqual(events, ["[2026-09-17] [Observation] A real event."])
        self.assertFalse([t for t in texts if "EVENT STORE" in t], texts)
        self.assertIn("本页只记录证据中明确出现的定义、机制、适用范围或限制。", texts)


def _timeline_frontmatter(page_id: str) -> dict:
    return {
        "title": "Anchor Test",
        "type": "concept",
        "id": page_id,
        "domain": "General",
        "status": "Active",
        "epistemic-status": "seed",
        "categories": ["System_Architecture"],
        "updated": "2026-09-17T00:00:00+00:00",
        "sources": [],
    }


if __name__ == "__main__":
    unittest.main()
