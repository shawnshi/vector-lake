import json
import os
import shutil
import threading
import unittest
import uuid
from pathlib import Path
from unittest import mock

import ingest
import indexer
import review
import governance_store
import claim_extractor
import tool_debt
import tool_delete
import tool_graph
import tool_merge
import tool_migrate
import tool_publish
import tool_query
import tool_trace
import wiki_utils
from wiki_utils import write_markdown_file


def _make_page(path: Path, title: str, page_type: str, sources=None, body: str = ""):
    write_markdown_file(
        path,
        {
            "title": title,
            "type": page_type,
            "domain": "General",
            "topic_cluster": "General",
            "status": "Active",
            "epistemic-status": "seed",
            "categories": ["Uncategorized"],
            "sources": sources or [],
        },
        body or f"# {title}\n",
    )


class VectorLakeTestCase(unittest.TestCase):
    def setUp(self):
        self.workspace_root = Path(__file__).resolve().parents[1] / "tests_tmp" / uuid.uuid4().hex
        self.memory_dir = self.workspace_root / "MEMORY"
        self.wiki_dir = self.memory_dir / "wiki"
        self.raw_dir = self.memory_dir / "raw"
        self.wiki_dir.mkdir(parents=True)
        self.raw_dir.mkdir(parents=True)
        self.previous_memory_dir = os.environ.get("VECTOR_LAKE_MEMORY_DIR")
        os.environ["VECTOR_LAKE_MEMORY_DIR"] = str(self.memory_dir)

    def tearDown(self):
        if self.previous_memory_dir is None:
            os.environ.pop("VECTOR_LAKE_MEMORY_DIR", None)
        else:
            os.environ["VECTOR_LAKE_MEMORY_DIR"] = self.previous_memory_dir
        shutil.rmtree(self.workspace_root, ignore_errors=True)


class QueryDryRunTests(VectorLakeTestCase):
    def test_query_dry_run_does_not_persist_files(self):
        expected = "---\ntitle: Preview\n---\nBody"
        with mock.patch("tool_query.assemble_context", return_value={"wiki_context": "", "wiki_page_count": 0, "purpose": "", "budget_used": 0, "budget_max": 0}):
            with mock.patch("tool_query._run_gemini", return_value=mock.Mock(returncode=0, stdout=expected, stderr="")) as run_mock:
                result = tool_query.query_logic_lake("test", dry_run=True)

        self.assertIn(expected, result)
        self.assertIn("No provenance trace found.", result)
        self.assertEqual(run_mock.call_count, 1)
        self.assertEqual(list(self.wiki_dir.glob("*.md")), [])


class DeleteSourceTests(VectorLakeTestCase):
    def test_delete_source_respects_dry_run_and_deletes_raw_on_execute(self):
        raw_path = self.raw_dir / "paper.md"
        raw_path.write_text("source", encoding="utf-8")
        _make_page(self.wiki_dir / "Source_paper.md", "Source paper", "source", ["raw/paper.md"])
        _make_page(self.wiki_dir / "Concept_related.md", "Related", "concept", ["raw/paper.md", "raw/other.md"])

        with mock.patch("tool_delete.indexer.generate_index"):
            preview = tool_delete.delete_source(str(raw_path), dry_run=True)
            self.assertIn("[DELETE_RAW]", preview)
            self.assertTrue(raw_path.exists())

            result = tool_delete.delete_source(str(raw_path), dry_run=False)

        self.assertIn("raw_deleted=True", result)
        self.assertFalse(raw_path.exists())
        self.assertFalse((self.wiki_dir / "Source_paper.md").exists())
        remaining_frontmatter, _, _ = wiki_utils.read_markdown_file(self.wiki_dir / "Concept_related.md")
        self.assertEqual(remaining_frontmatter["sources"], ["raw/other.md"])

    def test_delete_source_preserves_raw_when_wiki_cleanup_fails(self):
        raw_path = self.raw_dir / "paper.md"
        raw_path.write_text("source", encoding="utf-8")
        _make_page(self.wiki_dir / "Source_paper.md", "Source paper", "source", ["raw/paper.md"])
        _make_page(self.wiki_dir / "Concept_related.md", "Related", "concept", ["raw/paper.md", "raw/other.md"])

        with mock.patch("tool_delete.write_markdown_file", side_effect=OSError("write failed")):
            with mock.patch("tool_delete.indexer.generate_index"):
                result = tool_delete.delete_source(str(raw_path), dry_run=False)

        self.assertIn("Raw source was preserved", result)
        self.assertTrue(raw_path.exists())


class ReviewQueueTests(VectorLakeTestCase):
    def test_review_queue_add_items_is_safe_under_concurrent_writes(self):
        review.WIKI_DIR = self.wiki_dir
        review.REVIEW_FILE = self.wiki_dir / ".meta" / "review_queue.json"

        def add_item(index: int):
            review.add_items([{
                "type": "suggestion",
                "title": f"item-{index}",
                "description": "desc",
                "source": "test",
                "search_queries": [],
                "affected_pages": [],
                "created": f"t-{index}",
                "resolved": False,
                "resolution": None,
            }])

        threads = [threading.Thread(target=add_item, args=(index,)) for index in range(10)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        pending = review.get_pending()
        self.assertEqual(len(pending), 10)


class DedupParsingTests(VectorLakeTestCase):
    def test_scan_existing_sources_handles_multiline_yaml_lists(self):
        source_file = self.wiki_dir / "Source_multi.md"
        source_file.write_text(
            "---\n"
            "title: Multi\n"
            "type: source\n"
            "domain: General\n"
            "topic_cluster: General\n"
            "status: Active\n"
            "epistemic-status: seed\n"
            "categories:\n"
            "  - Uncategorized\n"
            "sources:\n"
            "  - raw/a.md\n"
            "  - raw/b.md\n"
            "---\n\n"
            "# body\n",
            encoding="utf-8",
        )

        mapping = ingest.scan_existing_sources(self.wiki_dir)
        self.assertEqual(mapping["raw/a.md"], "Source_multi.md")
        self.assertEqual(mapping["raw/b.md"], "Source_multi.md")


class IndexRefreshTests(VectorLakeTestCase):
    def test_partial_updates_mark_graph_dirty_and_refresh_cleans_it(self):
        indexer.WIKI_DIR = str(self.wiki_dir)
        _make_page(self.wiki_dir / "Concept_alpha.md", "Alpha", "concept", ["raw/a.md"], "[支持:: [[Concept_beta]]]\n")
        _make_page(self.wiki_dir / "Concept_beta.md", "Beta", "concept", ["raw/b.md"], "[支持:: [[Concept_alpha]]]\n")

        indexer.generate_index()
        index_path = wiki_utils.get_index_path()
        with open(index_path, "r", encoding="utf-8") as handle:
            generated = json.load(handle)
        self.assertFalse(generated["graph_state"]["dirty"])

        _make_page(self.wiki_dir / "Concept_gamma.md", "Gamma", "concept", ["raw/c.md"], "[支持:: [[Concept_alpha]]]\n")
        indexer.update_index_item("Concept_gamma.md")
        with open(index_path, "r", encoding="utf-8") as handle:
            partial = json.load(handle)
        self.assertTrue(partial["graph_state"]["dirty"])

        refreshed = indexer.refresh_graph_topology_if_dirty()
        self.assertTrue(refreshed)
        with open(index_path, "r", encoding="utf-8") as handle:
            clean = json.load(handle)
        self.assertFalse(clean["graph_state"]["dirty"])

    def test_update_index_item_rebuilds_from_corrupt_index(self):
        indexer.WIKI_DIR = str(self.wiki_dir)
        _make_page(self.wiki_dir / "Concept_alpha.md", "Alpha", "concept", ["raw/a.md"])
        index_path = wiki_utils.get_index_path()
        index_path.parent.mkdir(parents=True, exist_ok=True)
        index_path.write_text("{broken", encoding="utf-8")

        indexer.update_index_item("Concept_alpha.md")

        with open(index_path, "r", encoding="utf-8") as handle:
            rebuilt = json.load(handle)
        self.assertIn("Concept_alpha", rebuilt["nodes"])


class AuditGraphTests(VectorLakeTestCase):
    def test_audit_graph_emits_review_fields_expected_by_queue(self):
        index_path = wiki_utils.get_index_path()
        index_path.parent.mkdir(parents=True, exist_ok=True)
        index_path.write_text(json.dumps({
            "graph_insights": [
                {"type": "isolated_node", "node": "Concept_alpha", "description": "isolated"}
            ]
        }), encoding="utf-8")

        captured = []
        with mock.patch("tool_graph.indexer.refresh_graph_topology_if_dirty", return_value=False):
            with mock.patch("tool_graph.review.add_items", side_effect=lambda items: captured.extend(items)):
                result = tool_graph.audit_graph()

        self.assertIn("Pushed 1", result)
        self.assertEqual(captured[0]["search_queries"], ["Concept_alpha"])
        self.assertEqual(captured[0]["affected_pages"], ["wiki/Concept_alpha.md"])


class QueryUpdateTests(VectorLakeTestCase):
    def test_query_reindexes_when_existing_node_is_updated(self):
        target = self.wiki_dir / "Synthesis_existing.md"
        _make_page(target, "Existing", "synthesis", ["raw/context.md"])

        def fake_run(_prompt: str):
            current_frontmatter, _, _ = wiki_utils.read_markdown_file(target)
            write_markdown_file(target, current_frontmatter, "# Existing\n\nUpdated body\n")
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch("tool_query.assemble_context", return_value={"wiki_context": "", "wiki_page_count": 0, "purpose": "", "budget_used": 0, "budget_max": 0}):
            with mock.patch("tool_query._run_gemini", side_effect=fake_run):
                with mock.patch("tool_query.indexer.generate_index") as generate_index_mock:
                    result = tool_query.query_logic_lake("refresh existing synthesis", dry_run=False)

        self.assertIn("1 existing page(s) updated", result)
        self.assertGreaterEqual(generate_index_mock.call_count, 1)


class GovernanceV8Tests(VectorLakeTestCase):
    def test_migrate_v8_populates_canonical_store(self):
        _make_page(self.wiki_dir / "Concept_alpha.md", "Alpha", "concept", ["raw/a.md"], "Alpha body")
        _make_page(self.wiki_dir / "Source_a.md", "Source A", "source", ["raw/a.md"], "Source body")

        result = tool_migrate.migrate_v8(dry_run=False)

        self.assertIn("completed", result)
        entities = governance_store.load_entities()["items"]
        claims = governance_store.load_claims()["items"]
        sources = governance_store.load_sources()["items"]
        self.assertGreaterEqual(len(entities), 1)
        self.assertGreaterEqual(len(claims), 2)
        self.assertGreaterEqual(len(sources), 1)

    def test_publish_vector_lake_publishes_pending_change_set(self):
        _make_page(self.wiki_dir / "Concept_alpha.md", "Alpha", "concept", ["raw/a.md"], "Alpha body")
        change_set = governance_store.sync_pages_to_canonical(
            [str(self.wiki_dir / "Concept_alpha.md")],
            origin="test",
            auto_approve=False,
            summary="pending alpha",
        )

        result = tool_publish.publish_vector_lake(limit=1)

        self.assertIn("Published 1", result)
        updated_change_sets = governance_store.load_change_sets()["items"]
        published = next(item for item in updated_change_sets if item["change_set_id"] == change_set["change_set_id"])
        self.assertEqual(published["status"], "published")

    def test_debt_and_trace_use_canonical_projection(self):
        _make_page(self.wiki_dir / "Concept_alpha.md", "Alpha", "concept", ["raw/a.md"], "Alpha body")
        tool_migrate.migrate_v8(dry_run=False)

        debt = tool_debt.debt_vector_lake()
        trace = tool_trace.trace_vector_lake("Alpha")

        self.assertIn("Vector Lake Debt Dashboard", debt)
        self.assertIn("pending_change_set_count", debt)
        self.assertIn("Provenance Trace", trace)

    def test_claim_extractor_emits_block_level_claims_and_evidence(self):
        body = "# Alpha\n\nFirst assertion paragraph.\n\n- Bullet evidence\n\nSecond assertion."
        extracted = claim_extractor.extract_page_objects(
            str(self.wiki_dir / "Concept_alpha.md"),
            {
                "title": "Alpha",
                "type": "concept",
                "sources": ["raw/a.md"],
                "domain": "General",
                "topic_cluster": "General",
                "status": "Active",
            },
            body,
        )

        block_claims = [claim for claim in extracted["claims"] if claim.get("claim_scope") == "block"]
        self.assertGreaterEqual(len(block_claims), 3)
        self.assertTrue(all(claim.get("locator", {}).get("block_index") for claim in block_claims))
        self.assertTrue(any(evidence.get("evidence_type", "").startswith("block-") for evidence in extracted["evidence"]))

    def test_publish_rebuilds_canonical_views(self):
        page = self.wiki_dir / "Concept_alpha.md"
        _make_page(page, "Alpha", "concept", ["raw/a.md"], "Alpha body")
        governance_store.sync_pages_to_canonical([str(page)], origin="test", auto_approve=False, summary="pending alpha")

        result = tool_publish.publish_vector_lake(limit=1)

        self.assertIn("Rebuilt", result)
        self.assertTrue((self.wiki_dir / "views" / "Concept_alpha.md").exists())
        self.assertTrue((self.wiki_dir / "views" / "open_questions.md").exists())

    def test_merge_suggestions_detect_alias_collisions(self):
        _make_page(self.wiki_dir / "Concept_alpha.md", "Alpha", "concept", ["raw/a.md"], "Alpha body")
        _make_page(self.wiki_dir / "Concept_alpha_alt.md", "Alpha", "concept", ["raw/b.md"], "Alt body")
        tool_migrate.migrate_v8(dry_run=False)

        result = tool_merge.merge_suggestions_vector_lake(limit=10, enqueue=True)
        queue = governance_store.load_governance_queue()["items"]

        self.assertIn("Merge Suggestions", result)
        self.assertTrue(any(item.get("type") == "merge" for item in queue))

    def test_validity_state_inference_surfaces_review_due_claims(self):
        write_markdown_file(
            self.wiki_dir / "Concept_alpha.md",
            {
                "title": "Alpha",
                "type": "concept",
                "domain": "General",
                "topic_cluster": "General",
                "status": "Active",
                "epistemic-status": "seed",
                "categories": ["Uncategorized"],
                "sources": ["raw/a.md"],
                "review_after": "2020-01-01T00:00:00+00:00",
            },
            "Alpha body",
        )
        tool_migrate.migrate_v8(dry_run=False)

        metrics = governance_store.governance_projection()
        claims = list(metrics["claim_index"].values())

        self.assertTrue(any(claim.get("validity_state") == "review-due" for claim in claims))
        self.assertIn("review-due", tool_trace.trace_vector_lake("Alpha"))

    def test_graph_payload_contains_claim_view_projection(self):
        _make_page(self.wiki_dir / "Concept_alpha.md", "Alpha", "concept", ["raw/a.md"], "Alpha body")
        _make_page(self.wiki_dir / "Concept_beta.md", "Beta", "concept", ["raw/a.md"], "Beta body")
        tool_migrate.migrate_v8(dry_run=False)
        indexer.generate_index()

        with open(wiki_utils.get_index_path(), "r", encoding="utf-8") as handle:
            data = json.load(handle)
        payload = tool_graph._build_graph_payload(data)

        self.assertIn("claimGraph", payload)
        self.assertGreaterEqual(len(payload["claimGraph"]["nodes"]), 2)
        self.assertTrue(all(node.get("node_kind") == "claim" for node in payload["claimGraph"]["nodes"]))


if __name__ == "__main__":
    unittest.main()
