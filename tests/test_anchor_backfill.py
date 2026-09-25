"""The anchor drafter and applier: propose only with discriminating evidence, write without churn.

Two contracts are pinned here.  A proposal needs a source the block's discriminating terms actually
cover; everything else abstains with a reason, because a topic-similarity match would fabricate
provenance.  And the write must leave the *claim* alone -- ``claim_id`` is minted from the cleaned
block text, so the anchor is appended flush (no space) and skipped entirely when its path carries
parentheses, both of which were measured as id churn on the live corpus.
"""

import json

import pytest

from vector_lake import db_store, governance_store
from vector_lake.anchor_backfill import (
    _attribute,
    _is_scaffolding,
    _locate_line,
    _paren_safe,
    backfill_anchors,
    draft_anchors,
    drafts_path,
    parse_only,
)
from vector_lake.claim_extractor import _clean_claim_text, _is_page_boilerplate
from vector_lake.wiki_utils import get_wiki_dir

from tests.test_mutation_coordinator import _write_purpose_contract

_BODY = (
    "## 1. 编译事实 (Compiled Truth - READ MODEL)\n\n"
    "### 物理机制 (Mechanism)\n"
    "- 医疗语义层与智能体调度引擎共同构成悬挂与刹车机制。\n\n"
    "## 2. 证据时间线 (Evidence Timeline - WRITE MODEL)\n\n"
    "- [2026-01-01] [Observation] 悬挂与刹车机制来自医疗语义层与智能体调度引擎。\n"
)
_SOURCE_A = "raw/a/语义层与调度引擎.md"
_SOURCE_B = "raw/b/无关的周报.md"


def _page(name: str = "Concept_Anchor-Probe", sources: list[str] | None = None, body: str = _BODY) -> None:
    get_wiki_dir().mkdir(parents=True, exist_ok=True)
    (get_wiki_dir() / f"{name}.md").write_text(
        "---\n"
        "id: concept_anchor_probe\n"
        f"title: {name}\n"
        "type: concept\n"
        "domain: General\n"
        "status: Active\n"
        "epistemic-status: seed\n"
        "categories: [System_Architecture]\n"
        "strategic_scope: core\n"
        "evidence_tier: primary\n"
        "topic_cluster: Test\n"
        "updated: 2026-01-01\n"
        f"sources: {json.dumps(sources or [_SOURCE_A, _SOURCE_B], ensure_ascii=False)}\n"
        "---\n" + body,
        encoding="utf-8",
    )


def _raw(relative: str, text: str) -> None:
    path = get_wiki_dir().parent / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def prepared(isolated_memory, monkeypatch):
    monkeypatch.setenv("VECTOR_LAKE_DISABLE_WRITE_HEALTH_GATE", "1")
    _write_purpose_contract(isolated_memory)
    db_store.init_db()
    return isolated_memory


def _materialize(name: str = "Concept_Anchor-Probe") -> None:
    governance_store.create_change_set(
        [str(get_wiki_dir() / f"{name}.md")], origin="probe", auto_approve=True
    )


def _claims(name: str = "Concept_Anchor-Probe") -> dict[str, dict]:
    rows = db_store.get_connection().execute(
        "SELECT claim_id, data_json FROM claims WHERE json_extract(data_json,'$.locator.page_key')=?",
        (name,),
    ).fetchall()
    return {row["claim_id"]: json.loads(row["data_json"]) for row in rows}


def test_the_flush_anchor_leaves_the_claim_text_untouched():
    """This is why the applier writes no space: a trailing space is a different claim id."""
    original = "医疗语义层与智能体调度引擎共同构成悬挂与刹车机制。"
    flush = _clean_claim_text(original + "(Source: [[raw/a.md]])")
    spaced = _clean_claim_text(original + " (Source: [[raw/a.md]])")
    assert flush == _clean_claim_text(original)
    assert spaced != flush  # the spaced form churns the id; measured on the live corpus


def test_a_path_with_parentheses_cannot_be_anchored():
    assert _paren_safe("raw/a/plain.md")
    assert not _paren_safe("raw/a/report (2026)_final.md")


def test_a_migration_marker_is_not_a_claim_even_when_it_carries_a_date():
    """The date prefix is kept in a claim's text on purpose, so the guard has to strip it."""
    assert _is_page_boilerplate("[2026-07-11] [Observation] Node auto-migrated to V11 schema.")
    assert _is_page_boilerplate("[System Directive: This section represents the LATEST consensus.]")
    assert not _is_page_boilerplate("[2026-07-11] [Observation] 真实的观察记录。")


def test_a_template_footer_does_not_turn_a_real_claim_into_scaffolding():
    """``Last Reshaped`` sits at the end of substantive claims; matching it hid 107 of them."""
    assert _is_scaffolding("ACE引擎（多智能体协同引擎）是卫宁健康用于门诊智能分流的核心软件系统。 (Last Reshaped: 2026-06-28)") is None
    assert _is_scaffolding("1.58-Bit Quantization 是一种极低位宽的大模型压缩架构。") is None
    assert _is_scaffolding("本页只记录证据中明确出现的定义、机制、适用范围或限制。 未使用冻结证据范围之外的材料。") == "page_scaffolding"
    assert _is_scaffolding("[System Directive: This section represents the LATEST consensus.]") == "not_a_claim"
    assert _is_scaffolding("[2026-07-11] [Observation] Node auto-migrated to V11 schema.") == "not_a_claim"


def test_only_ranges_are_read_from_the_confirmation_string():
    proposals = [{"n": n} for n in range(1, 15)]
    assert parse_only(None, proposals) == proposals
    assert [item["n"] for item in parse_only("1,2,5,9-12", proposals)] == [1, 2, 5, 9, 10, 11, 12]


def test_the_line_locator_ignores_frontmatter_and_headings_and_grows_until_unique():
    lines = [
        "---",
        "title: Concept_Anchor-Probe",
        "sources: []",
        "---",
        "### 物理机制 (Mechanism)",
        "- [2026-06-01] [Observation] first claim about the same day.",
        "- [2026-06-01] [Observation] second claim about the same day.",
    ]
    # A frontmatter line would corrupt the YAML if anchored, a heading is refused by the schema.
    assert _locate_line(lines, "sources: []") is None
    assert _locate_line(lines, "### 物理机制 (Mechanism)") is None
    # Same-day timeline entries share their first 24 characters, so the needle has to grow.
    assert _locate_line(lines, "[2026-06-01] [Observation] second claim about the same day.") == 6


def test_an_undominated_block_abstains():
    sources = {
        "raw/a.md": {"lines": ["alpha beta"], "compact": "共同词汇共同词汇"},
        "raw/b.md": {"lines": ["alpha beta"], "compact": "共同词汇共同词汇"},
    }
    verdict = _attribute("共同词汇共同词汇", sources)
    assert verdict["reason"] == "no_discriminating_terms"
    assert "verdict" not in verdict


def test_a_dominated_block_is_proposed_with_its_citation():
    sources = {
        "raw/a.md": {"lines": ["", "医疗语义层与智能体调度引擎共同构成悬挂与刹车"], "compact": "医疗语义层与智能体调度引擎共同构成悬挂与刹车"},
        "raw/b.md": {"lines": ["无关的周报内容"], "compact": "无关的周报内容"},
    }
    verdict = _attribute("医疗语义层与智能体调度引擎共同构成悬挂与刹车机制。", sources)
    assert verdict["verdict"] == "proposed"
    assert verdict["chosen"] == "raw/a.md"
    assert verdict["citation"]["line"] == 2


def test_drafting_then_applying_clears_the_gap_and_keeps_the_claim(prepared):
    _page()
    _raw(_SOURCE_A, "医疗语义层与智能体调度引擎共同构成悬挂与刹车机制，并决定系统的主权归属。\n")
    _raw(_SOURCE_B, "无关的周报内容，谈的是资本与市场规模。\n")
    _materialize()
    before = _claims()
    gapped = {cid: claim for cid, claim in before.items() if claim["evidence_gap"] == "ambiguous_source"}
    assert gapped, before  # the two content blocks: the mechanism bullet and the timeline entry
    assert all(claim["evidence_ids"] == [] for claim in gapped.values())

    report = draft_anchors(write=True)
    assert "proposed for review" in report
    drafts = [json.loads(line) for line in drafts_path().read_text(encoding="utf-8").splitlines()]
    proposed = [item for item in drafts if item["verdict"] == "proposed"]
    assert proposed and all(item["chosen"] == _SOURCE_A for item in proposed)
    assert {item["claim_id"] for item in proposed} == set(gapped), proposed

    applied = backfill_anchors(apply=True, batch=5)

    assert "wrote" in applied
    text = (get_wiki_dir() / "Concept_Anchor-Probe.md").read_text(encoding="utf-8")
    assert f"[[{_SOURCE_A}]]" in text
    assert " (Source:" not in text  # flush, not spaced
    after = _claims()
    # Every claim the rule wrote for keeps its id and gains evidence -- that is the contract the
    # flush anchor exists for.
    assert set(gapped) <= set(after), "the anchor must not re-mint the claims it anchors"
    assert all(after[cid]["evidence_ids"] for cid in gapped)
    assert all(claim["evidence_gap"] == "" for claim in after.values())
    # The whole-body scope block is built from the raw body rather than from ``_clean_claim_text``,
    # so *any* body edit re-mints it.  That is a property of that block, not of the anchor form, and
    # it is the only id allowed to move (the live run of 240 anchors moved none).
    moved = set(before) - set(after)
    assert all(before[cid]["claim_text"].startswith("## 1. 编译事实") for cid in moved), moved
