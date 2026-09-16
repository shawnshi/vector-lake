"""Regression tests for the hand-edited Markdown projection path (P0-3)."""
import json

from vector_lake import db_store, indexer, wiki_utils
from vector_lake.mutation_coordinator import execute_mutation_plan
from vector_lake.watchdog_app import canonicalise_manual_edits


PURPOSE = """---
purpose_version: "12.0"
intent_keywords: [test]
intent_weight_boost: 0.1
scope:
  core: [test]
  edge: [edge]
  excluded: [excluded]
  marketing_noise: [noise]
evidence_tiers:
  primary: Primary evidence
sir_registry:
  - id: SIR_TEST
    status: active
    review_after: 2099-01-01
    signal_keywords: [test]
synthesis_policy:
  min_distinct_sources: 2
  min_tension_intensity: 0.5
---
Test purpose.
"""


def _page(key: str, marker: str = "初始") -> str:
    return "\n".join([
        "---", f"id: {key}", f"title: {key}", "type: concept", "domain: Healthcare_IT",
        "status: Active", "epistemic-status: evergreen", "categories: [Entities_and_Actors]",
        "strategic_scope: core", "evidence_tier: primary", "tags: [t1]",
        "updated: 2026-01-01T00:00:00+00:00", "links: []", "sources: []", "---", "",
        f"# {key}", "",
        "## 1. 编译事实 (Compiled Truth)", "",
        "### 物理机制 (Mechanism)", "", f"{key} {marker}机制描述。", "",
        "### 适用与失效边界 (Boundaries)", "", f"{key} {marker}边界描述。", "",
        "## 2. 证据时间线 (Evidence Timeline)", "",
        f"- [2026-01-01] [Observation] {key} {marker}观察记录。", "",
    ])


def _prepare(memory_dir):
    (memory_dir / "purpose.md").write_text(PURPOSE, encoding="utf-8")
    db_store.init_db()


def _canonical_row(page_key: str):
    conn = db_store.get_connection()
    return conn.execute(
        "SELECT data_json FROM entities WHERE json_extract(data_json, '$.page_key') = ?",
        (page_key,),
    ).fetchone()


def test_manual_edit_is_canonicalised(isolated_memory):
    _prepare(isolated_memory)
    execute_mutation_plan("Concept_Alpha.md", _page("Concept_Alpha"))
    indexer.generate_index()

    # A human edits the Markdown directly, bypassing the coordinator entirely.
    edited = _page("Concept_Alpha", "人工修改后的")
    (wiki_utils.get_wiki_dir() / "Concept_Alpha.md").write_text(edited, encoding="utf-8")

    handled, failures = canonicalise_manual_edits(["Concept_Alpha.md"])

    assert (handled, failures) == (1, [])
    row = _canonical_row("Concept_Alpha")
    assert row is not None
    assert "人工修改后的机制描述" in json.loads(row["data_json"])["raw_text"]


def test_manually_created_page_enters_canonical(isolated_memory):
    _prepare(isolated_memory)
    execute_mutation_plan("Concept_Alpha.md", _page("Concept_Alpha"))
    (wiki_utils.get_wiki_dir() / "Concept_Beta.md").write_text(_page("Concept_Beta"), encoding="utf-8")

    handled, failures = canonicalise_manual_edits(["Concept_Beta.md"])

    assert (handled, failures) == (1, [])
    assert _canonical_row("Concept_Beta") is not None


def test_manual_deletion_removes_canonical_row(isolated_memory):
    _prepare(isolated_memory)
    execute_mutation_plan("Concept_Alpha.md", _page("Concept_Alpha"))
    (wiki_utils.get_wiki_dir() / "Concept_Alpha.md").unlink()

    handled, failures = canonicalise_manual_edits(["Concept_Alpha.md"])

    assert (handled, failures) == (1, [])
    assert _canonical_row("Concept_Alpha") is None


def test_invalid_manual_edit_is_preserved_and_reported(isolated_memory):
    _prepare(isolated_memory)
    execute_mutation_plan("Concept_Alpha.md", _page("Concept_Alpha"))
    broken = _page("Concept_Alpha").replace("status: Active", "status: Bogus")
    target = wiki_utils.get_wiki_dir() / "Concept_Alpha.md"
    target.write_text(broken, encoding="utf-8")

    handled, failures = canonicalise_manual_edits(["Concept_Alpha.md"])

    assert handled == 0
    assert len(failures) == 1 and failures[0][0] == "Concept_Alpha.md"
    # The human's edit must never be destroyed by a failed canonicalisation.
    assert target.read_text(encoding="utf-8") == broken
    row = _canonical_row("Concept_Alpha")
    assert "初始机制描述" in json.loads(row["data_json"])["raw_text"]


def test_already_projected_content_is_skipped(isolated_memory):
    _prepare(isolated_memory)
    execute_mutation_plan("Concept_Alpha.md", _page("Concept_Alpha"))

    # The coordinator's own projection write must not be re-processed.
    handled, failures = canonicalise_manual_edits(["Concept_Alpha.md"])

    assert (handled, failures) == (0, [])
