"""The synthesis skeleton rule: what the gate enforces and what it only reports.

``schema.md`` used to say a ``Synthesis_`` page "MUST begin with" its two required sections, while
``validate_schema`` only checked that they were *present*: two live pages carried the skeleton at
the end of the body and passed.  The gate stays at presence (enforcing order would reject the
legacy pages at once); the position is reported by lint.  These tests pin both halves so the
documented rule and the enforced rule cannot diverge again.
"""
import pathlib

import pytest

from vector_lake import tool_lint
from vector_lake.schema_validator import (
    SYNTHESIS_SKELETON_HEADINGS,
    SchemaViolationException,
    synthesis_skeleton_order_report,
    validate_schema,
)
from vector_lake.wiki_utils import get_wiki_dir

FRONTMATTER = {
    "id": "synth_contract_test",
    "title": "Synthesis Contract Test",
    "type": "synthesis",
    "domain": "General",
    "status": "Active",
    "epistemic-status": "seed",
    "categories": ["Uncategorized"],
    "updated": "2026-09-25T00:00:00+00:00",
    "sources": [],
    "strategic_scope": "core",
}

SECTION_A, SECTION_B = SYNTHESIS_SKELETON_HEADINGS
CANONICAL_BODY = f"{SECTION_A}\n\n- 论点一。\n\n{SECTION_B}\n\n- [part-of:: [[Product_X]]]\n"
BURIED_BODY = (
    "# Synthesis: 测试\n\n"
    "## 1. 核心结论 (Core Conclusion)\n\n"
    "现有证据不足。\n\n"
    f"{SECTION_A}\n\n- 论点一。\n\n"
    f"{SECTION_B}\n\n- [part-of:: [[Product_X]]]\n"
)


def test_presence_is_enforced():
    validate_schema(dict(FRONTMATTER), CANONICAL_BODY, "Synthesis_Contract-Test.md", None, False)


def test_a_buried_skeleton_still_passes_the_gate():
    # Deliberate: the gate is presence-only, and this is the shape the live pages had.
    validate_schema(dict(FRONTMATTER), BURIED_BODY, "Synthesis_Contract-Test.md", None, False)


def test_a_missing_section_is_rejected():
    body = CANONICAL_BODY.replace(SECTION_B, "## 支撑拓扑缺失\n")
    with pytest.raises(SchemaViolationException, match="支撑拓扑"):
        validate_schema(dict(FRONTMATTER), body, "Synthesis_Contract-Test.md", None, False)


def test_order_report_is_silent_for_the_recommended_shape():
    assert synthesis_skeleton_order_report(CANONICAL_BODY) is None


def test_order_report_names_a_buried_skeleton():
    report = synthesis_skeleton_order_report(BURIED_BODY)
    assert report and "not the opening sections" in report


def test_order_report_handles_a_page_with_too_few_sections():
    assert synthesis_skeleton_order_report("## 核心合成论点 (Core Synthesized Claims)\n") == (
        "synthesis page has fewer than two H2 sections"
    )


def test_lint_reports_the_order_without_failing(isolated_memory):
    wiki = get_wiki_dir()
    (wiki / "Synthesis_Contract-Test.md").write_text(
        "---\n"
        "id: synth_contract_test\n"
        "title: Synthesis Contract Test\n"
        "type: synthesis\n"
        "domain: General\n"
        "status: Active\n"
        "epistemic-status: seed\n"
        "categories:\n- Uncategorized\n"
        "updated: '2026-09-25T00:00:00+00:00'\n"
        "sources: []\n"
        "strategic_scope: core\n"
        "---\n" + BURIED_BODY,
        encoding="utf-8",
    )

    report = tool_lint.lint_vector_lake()

    assert "synthesis skeleton is present but not the opening sections" in report
    assert pathlib.Path(wiki / "Synthesis_Contract-Test.md").exists()
