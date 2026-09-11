import copy
import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vector_lake.claim_extractor import extract_page_objects


class SourceIdentityPathTests(unittest.TestCase):
    def _fm(self, sources=None, **extra):
        value = {
            "id": "concept_paths", "title": "Paths", "type": "concept",
            "domain": "General", "status": "Active", "epistemic-status": "seed",
            "categories": ["Uncategorized"], "updated": "2026-09-11",
            "sources": sources or [],
        }
        value.update(extra)
        return value

    def _extract(self, fm, body="A claim."):
        page = f"## 1. 编译事实\n\n{body}\n\n## 2. 证据时间线\n"
        return extract_page_objects("Concept_Paths.md", fm, page)

    def test_inline_only_md_keeps_legacy_ids_but_reads_original_path(self):
        with tempfile.TemporaryDirectory() as root:
            target = Path(root, "raw/news/briefing.md")
            target.parent.mkdir(parents=True)
            target.write_bytes(b"verified briefing")
            fm = self._fm(source_locators={"raw/news/briefing.md": {"page": 7}})
            before = copy.deepcopy(fm)
            with patch.dict(os.environ, {"VECTOR_LAKE_MEMORY_DIR": root}):
                result = self._extract(fm, "A claim. (Source: [[raw/news/briefing.md]])")

        claim = result["claims"][0]
        source = result["sources"][0]
        artifact = result["source_artifacts"][0]
        evidence = result["evidence"][0]
        self.assertEqual(claim["inline_sources"], ["raw/news/briefing"])
        self.assertEqual(claim["source_ids"], ["source_debc1dd759b6366c6dc4179d"])
        self.assertEqual(evidence["evidence_id"], "evidence_288454aea6f77431744bfaa1")
        self.assertEqual(claim["claim_id"], "claim_df24488233371778ca5641bc")
        self.assertEqual(source["raw_ref"], "raw/news/briefing.md")
        self.assertEqual(artifact["raw_ref"], "raw/news/briefing.md")
        self.assertEqual(artifact["sha256"], hashlib.sha256(b"verified briefing").hexdigest())
        self.assertEqual(artifact["integrity_status"], "verified")
        self.assertEqual(evidence["source_locator"], {"page": 7})
        self.assertEqual(fm, before)

    def test_frontmatter_md_and_inline_md_keep_distinct_historical_identities(self):
        with tempfile.TemporaryDirectory() as root:
            target = Path(root, "raw/a.md")
            target.parent.mkdir(parents=True)
            target.write_bytes(b"same verified bytes")
            fm = self._fm(["raw/a.md"])
            with patch.dict(os.environ, {"VECTOR_LAKE_MEMORY_DIR": root}):
                result = self._extract(fm, "A claim. (Source: [[raw/a.md]])")

        source_ids = {record["source_id"] for record in result["sources"]}
        self.assertEqual(len(source_ids), 2)
        self.assertEqual(set(result["claims"][0]["source_ids"]), source_ids)
        self.assertEqual(
            {record["artifact_id"] for record in result["source_artifacts"]},
            {result["source_artifacts"][0]["artifact_id"]},
        )
        self.assertEqual(
            {record["raw_ref"] for record in result["sources"]}, {"raw/a.md"}
        )

    def test_frontmatter_and_inline_alias_deduplicate_same_physical_ref(self):
        fm = self._fm()
        result = self._extract(
            fm, "A claim. (Source: [[raw/item.md]], [[raw/item.md]])"
        )
        self.assertEqual(len(result["sources"]), 1)
        self.assertEqual(result["claims"][0]["source_ids"], [result["sources"][0]["source_id"]])
        self.assertEqual(result["sources"][0]["raw_ref"], "raw/item.md")

    def test_lossy_middle_and_repeated_md_identity_is_preserved(self):
        result = self._extract(self._fm(), "A claim. (Source: [[raw/a.md/b.md.md]])")
        self.assertEqual(result["claims"][0]["inline_sources"], ["raw/a/b"])
        self.assertEqual(result["sources"][0]["raw_ref"], "raw/a.md/b.md.md")
        self.assertEqual(result["sources"][0]["integrity_status"], "unverified")

    def test_same_identity_different_physical_refs_fail_in_both_orders(self):
        refs = ["raw/a.md/b.md", "raw/a/b.md"]
        for ordered in (refs, list(reversed(refs))):
            body = "A claim. " + " ".join(f"(Source: [[{ref}]])" for ref in ordered)
            with self.subTest(ordered=ordered):
                with self.assertRaisesRegex(ValueError, "^conflicting source identity bindings$"):
                    self._extract(self._fm(), body)

    def test_physical_and_legacy_configuration_resolution(self):
        physical, legacy = "raw/news/item.md", "raw/news/item"
        for key in (physical, legacy):
            fm = self._fm(source_artifacts={key: {"classification": "public"}},
                          source_locators={key: {"line": 3}})
            result = self._extract(fm, f"A claim. (Source: [[{physical}]])")
            self.assertEqual(result["sources"][0]["classification"], "public")
            self.assertEqual(result["evidence"][0]["source_locator"], {"line": 3})
        same = {physical: {"classification": "public"}, legacy: {"classification": "public"}}
        self.assertEqual(self._extract(self._fm(source_artifacts=same),
                         f"A claim. (Source: [[{physical}]])")["sources"][0]["classification"], "public")
        different = {physical: {"classification": "public"}, legacy: {"classification": "secret"}}
        with self.assertRaisesRegex(ValueError, "^conflicting source configuration bindings$"):
            self._extract(self._fm(source_artifacts=different), f"A claim. (Source: [[{physical}]])")

    def test_later_artifact_configuration_conflict_precedes_all_resolution(self):
        refs = ["raw/first.md", "raw/later.md"]
        config = {
            "raw/later.md": {"classification": "public"},
            "raw/later": {"classification": "secret"},
        }
        with patch("vector_lake.claim_extractor.resolve_source_artifact") as resolver:
            with self.assertRaisesRegex(
                ValueError, "^conflicting source configuration bindings$"
            ):
                self._extract(self._fm(source_artifacts=config),
                              "A claim. " + " ".join(f"(Source: [[{ref}]])" for ref in refs))
        resolver.assert_not_called()

    def test_later_locator_dual_key_conflict_precedes_all_resolution(self):
        refs = ["raw/first.md", "raw/later.md"]
        locators = {"raw/later.md": {"page": 1}, "raw/later": {"page": 2}}
        with patch("vector_lake.claim_extractor.resolve_source_artifact") as resolver:
            with self.assertRaisesRegex(
                ValueError, "^conflicting source configuration bindings$"
            ):
                self._extract(self._fm(source_locators=locators),
                              "A claim. " + " ".join(f"(Source: [[{ref}]])" for ref in refs))
        resolver.assert_not_called()

    def test_non_paths_wiki_owner_and_operational_markers_regressions(self):
        cases = ["https://example.test/a.md", "Source_Primary", "decision_log"]
        for ref in cases:
            with self.subTest(ref=ref):
                result = self._extract(self._fm(), f"A claim. (Source: [[{ref}]])")
                self.assertEqual(result["sources"][0]["integrity_status"], "unverified")
        wiki = self._extract(self._fm(), "A claim. (Source: [[Source_Primary]])")
        self.assertEqual(wiki["sources"][0]["canonical_source_page"], "Source_Primary.md")
        owner = extract_page_objects(
            "Source_Owner.md", self._fm(["raw/file.pdf"], type="source"),
            "## 1. 编译事实\n\nA claim.\n\n## 2. 证据时间线\n",
        )
        self.assertEqual(owner["sources"][0]["canonical_source_page"], "Source_Owner.md")
        marker = self._extract(self._fm(), "A claim. (Source: [[operational_memory]])")
        self.assertTrue(marker["claims"][0]["operational_memory_provenance"])


if __name__ == "__main__":
    unittest.main()
