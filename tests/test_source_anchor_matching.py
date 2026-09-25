"""A block is anchored to a source by comparing the same spelling on both sides.

``sources`` keeps the ``.md`` extension (that is what the canonical store records) while
``_parse_inline_sources`` strips it, and the multi-source gate compared the two raw.  The result
was unsatisfiable: on a page declaring more than one source **no** block could attach evidence,
however it was anchored -- 926 live claims sit behind that, and one page out of 400 with several
sources had any evidence at all (via an anchor to a source *page*, which the append branch mints).
These tests drive the extractor on that exact shape.
"""

from vector_lake.claim_extractor import _stable_id, extract_page_objects
from vector_lake.wiki_utils import get_wiki_dir

_BODY = (
    "## 1. 编译事实 (Compiled Truth - READ MODEL)\n\n"
    "### 物理机制 (Mechanism)\n"
    "- Anchored fact one. (Source: [[raw/a.md]])\n"
    "- Unanchored fact two.\n\n"
    "## 2. 证据时间线 (Evidence Timeline - WRITE MODEL)\n\n"
    "- [2026-01-01] [Observation] anchored entry. (Source: [[raw/b.md]])\n"
)


def _frontmatter(sources: list[str]) -> dict:
    return {
        "id": "concept_anchor_probe",
        "title": "Concept_Anchor-Probe",
        "type": "concept",
        "domain": "General",
        "status": "Active",
        "epistemic-status": "seed",
        "categories": ["System_Architecture"],
        "strategic_scope": "core",
        "evidence_tier": "primary",
        "topic_cluster": "Test",
        "updated": "2026-01-01",
        "sources": sources,
    }


def _extract(sources: list[str], body: str = _BODY) -> tuple[dict, dict[str, dict]]:
    get_wiki_dir().mkdir(parents=True, exist_ok=True)
    extracted = extract_page_objects("Concept_Anchor-Probe.md", _frontmatter(sources), body)
    evidence = {item["evidence_id"]: item for item in extracted["evidence"]}
    # Block-scope claims only: the page-level summary carries one evidence entry per declared source
    # by design, which is a different claim from the block these tests are about.
    claims = {
        item["claim_text"].strip(): item
        for item in extracted["claims"]
        if item.get("claim_scope") == "block"
    }
    return claims, evidence


def test_a_block_anchored_to_a_declared_source_gets_that_source(isolated_memory):
    claims, evidence = _extract(["raw/a.md", "raw/b.md"])

    anchored = next(claim for text, claim in claims.items() if text.startswith("Anchored fact one"))
    assert len(anchored["evidence_ids"]) == 1
    assert evidence[anchored["evidence_ids"][0]]["source_id"] == _stable_id("source", "raw/a.md")
    assert anchored["evidence_gap"] == "" or anchored.get("evidence_gap") in ("", None)


def test_a_block_that_names_no_source_is_still_left_alone(isolated_memory):
    """The gate's purpose is unchanged: several declared sources must not all attach."""
    claims, _evidence = _extract(["raw/a.md", "raw/b.md"])

    unanchored = next(claim for text, claim in claims.items() if text.startswith("Unanchored fact two"))
    assert unanchored["evidence_ids"] == []


def test_each_block_is_anchored_to_the_source_it_names_not_to_all_of_them(isolated_memory):
    claims, evidence = _extract(["raw/a.md", "raw/b.md"])

    timeline = next(claim for text, claim in claims.items() if "anchored entry" in text)
    assert evidence[timeline["evidence_ids"][0]]["source_id"] == _stable_id("source", "raw/b.md")


def test_an_anchor_without_the_extension_is_the_same_source(isolated_memory):
    """Spelling one file two ways must not mint a second source identity for it."""
    body = _BODY.replace("(Source: [[raw/a.md]])", "(Source: [[raw/a]])")

    claims, evidence = _extract(["raw/a.md", "raw/b.md"], body)

    anchored = next(claim for text, claim in claims.items() if text.startswith("Anchored fact one"))
    assert len(anchored["evidence_ids"]) == 1
    assert evidence[anchored["evidence_ids"][0]]["source_id"] == _stable_id("source", "raw/a.md")
    ids = {item["evidence_id"] for item in evidence.values()}
    assert _stable_id("source", "raw/a") not in {item["source_id"] for item in evidence.values()}
    assert ids  # the extractor still produced evidence at all


def test_one_declared_source_still_anchors_every_block(isolated_memory):
    """The single-source path never consulted the gate, and must keep working."""
    body = (
        "## 1. 编译事实 (Compiled Truth - READ MODEL)\n\n"
        "### 物理机制 (Mechanism)\n"
        "- A fact.\n\n"
        "## 2. 证据时间线 (Evidence Timeline - WRITE MODEL)\n\n"
        "- [2026-01-01] [Observation] an entry.\n"
    )

    claims, evidence = _extract(["raw/only.md"], body)

    assert all(claim["evidence_ids"] for claim in claims.values())
    assert {item["source_id"] for item in evidence.values()} == {_stable_id("source", "raw/only.md")}
