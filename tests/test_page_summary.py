"""The page ``summary`` claim must carry the page's words, not its scaffolding.

It used to be the first 320 characters of the raw body, which on every page starts with the
template's headings and -- on 3 214 live pages -- the system directive.  The row that resulted said
nothing, its ``page-summary`` evidence recorded the directive as ``evidence_text``, and because the
summary is re-minted from the body on every extraction, re-extracting those pages could not remove
it.  These tests pin the fixed rule.
"""

from vector_lake.claim_extractor import _body_summary, extract_page_objects
from vector_lake.wiki_utils import get_wiki_dir

_DIRECTIVE = (
    "*[System Directive: This section represents the LATEST consensus. NO historical narrative "
    "here. NO marketing.]*"
)
_FRONTMATTER = {
    "id": "concept_summary_probe",
    "title": "Concept_Summary-Probe",
    "type": "concept",
    "domain": "General",
    "status": "Active",
    "epistemic-status": "seed",
    "categories": ["System_Architecture"],
    "strategic_scope": "core",
    "evidence_tier": "primary",
    "topic_cluster": "Test",
    "updated": "2026-01-01",
    "sources": ["raw/a.md"],
}


def test_the_summary_skips_the_directive_and_the_headings():
    body = (
        "\n# Summary Probe\n\n"
        "## 1. 编译事实 (Compiled Truth - READ MODEL)\n"
        f"{_DIRECTIVE}\n\n"
        "真实的第一个事实陈述，来自页面自己的内容。\n\n"
        "## 2. 证据时间线 (Evidence Timeline - WRITE MODEL)\n\n"
        "- [2026-01-01] [Observation] 一条时间线记录。\n"
    )

    summary = _body_summary(body)

    assert summary.startswith("真实的第一个事实陈述")
    assert "System Directive" not in summary
    assert "编译事实" not in summary


def test_the_summary_strips_inline_anchors():
    """It becomes both a claim text and the page-summary evidence text."""
    body = (
        "## 1. 编译事实 (Compiled Truth - READ MODEL)\n\n"
        "一个事实。(Source: [[raw/a.md]])\n"
    )

    summary = _body_summary(body)

    assert summary == "一个事实。", summary
    assert "Source" not in summary


def test_a_page_with_nothing_but_scaffolding_still_yields_a_summary():
    """The fallback is the raw head, which the callers rely on for a stub page."""
    body = "## 1. 编译事实 (Compiled Truth - READ MODEL)\n\n" + _DIRECTIVE + "\n"

    summary = _body_summary(body)

    assert summary  # never empty
    assert "System Directive" in summary  # the fallback, explicitly, not a silent nothing


def test_the_summary_claim_does_not_carry_the_directive(isolated_memory):
    get_wiki_dir().mkdir(parents=True, exist_ok=True)
    body = (
        "\n# Summary Probe\n\n"
        "## 1. 编译事实 (Compiled Truth - READ MODEL)\n"
        f"{_DIRECTIVE}\n\n"
        "真实的第一句。\n\n"
        "## 2. 证据时间线 (Evidence Timeline - WRITE MODEL)\n\n"
        "- [2026-01-01] [Observation] 一条时间线记录。\n"
    )

    extracted = extract_page_objects("Concept_Summary-Probe.md", _FRONTMATTER, body)

    summaries = [claim for claim in extracted["claims"] if claim["claim_type"] == "summary"]
    assert len(summaries) == 1
    assert summaries[0]["claim_text"].startswith("真实的第一句")
    assert not any("System Directive" in claim["claim_text"] for claim in extracted["claims"])
